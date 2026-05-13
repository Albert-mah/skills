#!/usr/bin/env python3
"""Side-by-side diff of source vs Copy structure on a live NocoBase instance.

Walks both route trees (e.g. "Main" vs "Copy - Main"), compares per-level:
  - direct children (titles)
  - per-page blockchild count + use-class
Shows what Copy is missing relative to source.

Usage:
  NB_URL=http://localhost:14000 NB_USER=admin@nocobase.com NB_PASSWORD=admin123 \\
    python3 scripts/copy-diff.py [--prefix "Copy - "]

Output reads top-to-bottom. ✗ marks something missing on Copy, ! marks
quantity mismatch.
"""
import os, sys, json, urllib.request, urllib.error
import argparse

BASE = os.environ.get('NB_URL', 'http://localhost:14000').rstrip('/')

def resolve_token():
    t = os.environ.get('NB_TOKEN')
    if t: return t.strip()
    user, pwd = os.environ.get('NB_USER'), os.environ.get('NB_PASSWORD')
    if not (user and pwd): sys.exit("set NB_TOKEN or NB_USER+NB_PASSWORD")
    body = json.dumps({"account": user, "password": pwd}).encode()
    req = urllib.request.Request(f"{BASE}/api/auth:signIn", data=body, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())['data']['token']

TOK = resolve_token()
HDR = {"Authorization": f"Bearer {TOK}"}

def get_json(path):
    req = urllib.request.Request(f"{BASE}{path}", headers=HDR)
    return json.loads(urllib.request.urlopen(req).read())

# ── 1. routes tree ──
routes = get_json("/api/desktopRoutes:list?paginate=false&fields=id,title,type,parentId,sort,schemaUid")['data']
by_parent = {}
for r in routes:
    by_parent.setdefault(r.get("parentId"), []).append(r)
for kids in by_parent.values():
    kids.sort(key=lambda r: (r.get("sort") or 0, r.get("id")))

by_id = {r["id"]: r for r in routes}

# Find source vs copy top entries by title (Copy has --title-prefix)
def find_top(title):
    for r in routes:
        if not r.get("parentId") and r.get("title") == title: return r
    return None

def walk_titles(rid):
    """Return ordered list of (title, type) for direct flowPage/group children, skipping tabs."""
    return [(c["title"], c.get("type")) for c in by_parent.get(rid, []) if c.get("type") in ("group", "flowPage")]

# ── 2. block count via flow_models.parentId = pageSchemaUid → tab → grid ──
import psycopg2
DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')
conn = psycopg2.connect(DSN); cur = conn.cursor()

def descendants_count(uid):
    """Total descendants under a node (recursive)."""
    cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId'=%s", (uid,))
    kids = [r[0] for r in cur.fetchall()]
    total = len(kids)
    for k in kids: total += descendants_count(k)
    return total

def page_block_signature(schema_uid):
    """For a page schemaUid, walk → (tabs|RootPageTabModel) → grid → items.
    Each top-level item is described by (use, descendant_count) so popup
    chains (PopupActionModel + ChildPageModel + grid + charts) get folded
    into a depth signature — Copy popups missing inner content show up.
    Also walks 'subKey IS NULL' for tab nodes since NB's flowSurfaces:get
    may not set subKey on RootPageTabModel.
    """
    if not schema_uid: return []
    cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId'=%s", (schema_uid,))
    page_nodes = [r[0] for r in cur.fetchall()]
    sigs = []
    for pn in page_nodes:
        cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId'=%s", (pn,))
        tabs = [r[0] for r in cur.fetchall()]
        for tab in tabs:
            cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId'=%s AND options->>'subKey'='grid' LIMIT 1", (tab,))
            r = cur.fetchone()
            if not r: continue
            grid = r[0]
            cur.execute("SELECT uid, options->>'use', (options->>'sortIndex')::float FROM \"flowModels\" WHERE options->>'parentId'=%s AND options->>'subKey'='items' ORDER BY 3 NULLS LAST", (grid,))
            for child_uid, use, _ in cur.fetchall():
                sigs.append((use, descendants_count(child_uid)))
    return sigs

def page_schema_uid(rid):
    return by_id[rid].get("schemaUid")

# ── 3. recursive compare ──
ap = argparse.ArgumentParser()
ap.add_argument("--prefix", default="Copy - ", help="title prefix used at top level (default 'Copy - ')")
args = ap.parse_args()
PFX = args.prefix

# Source candidates = top-level routes WITHOUT prefix
src_tops = [r for r in routes if not r.get("parentId") and not (r.get("title") or "").startswith(PFX.strip().rstrip("-").strip())]
src_tops = [r for r in src_tops if not (r.get("title") or "").startswith(PFX)]

missing = mismatch = ok = 0

def cmp_pair(src_r, cpy_r, depth=0):
    global missing, mismatch, ok
    indent = "  " * depth
    src_kids = walk_titles(src_r["id"])
    cpy_kids = walk_titles(cpy_r["id"]) if cpy_r else []
    cpy_kids_set = {(t, ty) for t, ty in cpy_kids}
    for t, ty in src_kids:
        if (t, ty) not in cpy_kids_set:
            print(f"{indent}✗ MISSING in Copy: {t!r} ({ty}) — source path {src_r['title']!r} > ...")
            missing += 1
            continue
        # Recursive descent
        src_child = next(c for c in by_parent.get(src_r["id"], []) if c.get("title") == t and c.get("type") == ty)
        cpy_child = next(c for c in by_parent.get(cpy_r["id"], []) if c.get("title") == t and c.get("type") == ty)
        if ty == "flowPage":
            src_sig = page_block_signature(src_child.get("schemaUid"))
            cpy_sig = page_block_signature(cpy_child.get("schemaUid"))
            src_uses = sorted([u for u, _ in src_sig])
            cpy_uses = sorted([u for u, _ in cpy_sig])
            # Per-use-class total descendant counts (catches popup-content drift
            # under same-shape top-level blocks).
            def by_use_count(sig):
                tot = {}
                for u, c in sig: tot[u] = tot.get(u, 0) + c
                return tot
            src_dc, cpy_dc = by_use_count(src_sig), by_use_count(cpy_sig)
            if src_uses != cpy_uses:
                lacks = sorted(set(src_uses) - set(cpy_uses))
                extra = sorted(set(cpy_uses) - set(src_uses))
                bits = []
                if lacks: bits.append(f"lacks {','.join(lacks)}")
                if extra: bits.append(f"extra {','.join(extra)}")
                print(f"{indent}! {t}: page blocks {len(src_uses)} vs {len(cpy_uses)} ({'; '.join(bits)})")
                mismatch += 1
            elif src_dc != cpy_dc:
                # Same top-level block use counts, but inside differs (popup
                # chains, columns, etc.). Show per-use deltas.
                deltas = []
                for u in sorted(set(src_dc) | set(cpy_dc)):
                    if src_dc.get(u, 0) != cpy_dc.get(u, 0):
                        deltas.append(f"{u}: src={src_dc.get(u,0)} cpy={cpy_dc.get(u,0)}")
                print(f"{indent}! {t}: block-tree depth differs ({'; '.join(deltas)})")
                mismatch += 1
            else:
                ok += 1
        else:  # group → recurse
            cmp_pair(src_child, cpy_child, depth+1)

for src in src_tops:
    cpy_title = f"{PFX}{src['title']}"
    cpy = find_top(cpy_title)
    if not cpy:
        print(f"✗ Copy top group missing: {cpy_title!r}")
        missing += 1
        continue
    print(f"\n=== {src['title']!r} ↔ {cpy_title!r} ===")
    cmp_pair(src, cpy)

print(f"\n--- summary ---")
print(f"  missing: {missing}")
print(f"  mismatch: {mismatch}")
print(f"  ok: {ok}")
sys.exit(1 if (missing or mismatch) else 0)
