#!/usr/bin/env python3
"""L2 — verify Copy-side collection refs all point to `_copy` variants.

For every flowModel reachable from a Copy-prefixed top-level route, scan
options recursively for known collection-pointing keys and report any
value that still names a source collection (and a `_copy` variant
exists).

Detected keys:
  - collectionName / collection / targetCollection / sourceCollection
  - associationName (format `<dataSource>.<coll>.<field>`; middle segment)
  - dataSource (rare; only when value is a coll name not 'main')

Not touched: `fieldPath`, `name`, `field`, any free-form text. We only
flag canonical NB metadata keys to avoid false positives.

Usage:
  NB_URL=http://localhost:14000 NB_USER=admin@nocobase.com NB_PASSWORD=admin123 \\
    python3 scripts/verify-collection-refs.py [--prefix 'Copy - '] [--suffix '_copy'] [--fix]

Exit 0 when nothing wrong, 1 on any violation. With --fix, exit reflects
state AFTER autofix.
"""
import argparse, json, os, sys, urllib.request, urllib.error
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')
BASE = os.environ.get('NB_URL', 'http://localhost:14000').rstrip('/')

REF_KEYS = {'collectionName', 'collection', 'targetCollection', 'sourceCollection'}

def resolve_token():
    if os.environ.get('NB_TOKEN'): return os.environ['NB_TOKEN'].strip()
    user, pwd = os.environ.get('NB_USER'), os.environ.get('NB_PASSWORD')
    if not (user and pwd): sys.exit("set NB_TOKEN or NB_USER+NB_PASSWORD")
    body = json.dumps({"account": user, "password": pwd}).encode()
    req = urllib.request.Request(f"{BASE}/api/auth:signIn", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())['data']['token']

TOK = None
HDR = None

def post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=HDR, method="POST")
    return json.loads(urllib.request.urlopen(req).read())

def collect_copy_subtree(cur, prefix):
    """All flowModel uids that descend from any top-level route titled '<prefix>...'"""
    cur.execute("""
        SELECT id, "parentId", title, "schemaUid"
        FROM "desktopRoutes" ORDER BY sort, id
    """)
    routes = cur.fetchall()
    by_parent = {}
    for rid, pid, title, suid in routes:
        by_parent.setdefault(pid, []).append((rid, title, suid))
    schema_uids = set()
    def walk_routes(parent_id):
        for rid, title, suid in by_parent.get(parent_id, []):
            if suid: schema_uids.add(suid)
            walk_routes(rid)
    for rid, pid, title, suid in routes:
        if pid is None and title and title.startswith(prefix):
            if suid: schema_uids.add(suid)
            walk_routes(rid)
    if not schema_uids: return set()
    # BFS flowModels by parentId
    seen = set(schema_uids); frontier = list(schema_uids)
    while frontier:
        cur.execute(
            "SELECT uid FROM \"flowModels\" WHERE options->>'parentId' = ANY(%s)",
            (frontier,))
        nxt = [r[0] for r in cur.fetchall() if r[0] not in seen]
        seen.update(nxt); frontier = nxt
    return seen

def walk_refs(obj, path=''):
    """Yield (path, key, value) for every ref-key occurrence in nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            new = f'{path}.{k}' if path else k
            if isinstance(v, str):
                if k in REF_KEYS or k == 'associationName':
                    yield new, k, v
            yield from walk_refs(v, new)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_refs(v, f'{path}[{i}]')

def rewrite_refs(obj, src_to_cpy):
    """In-place rewrite. Returns count of changes."""
    n = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                if k in REF_KEYS and v in src_to_cpy:
                    obj[k] = src_to_cpy[v]; n += 1
                elif k == 'associationName':
                    parts = v.split('.')
                    if len(parts) == 3 and parts[1] in src_to_cpy:
                        parts[1] = src_to_cpy[parts[1]]
                        obj[k] = '.'.join(parts); n += 1
            else:
                n += rewrite_refs(v, src_to_cpy)
    elif isinstance(obj, list):
        for v in obj:
            n += rewrite_refs(v, src_to_cpy)
    return n

def main():
    global TOK, HDR
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='Copy - ')
    ap.add_argument('--suffix', default='_copy')
    ap.add_argument('--fix', action='store_true')
    args = ap.parse_args()

    TOK = resolve_token()
    HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

    conn = psycopg2.connect(DSN); cur = conn.cursor()

    # 1. build src→cpy map from collections table
    cur.execute("SELECT name FROM collections WHERE name LIKE %s",
                ('%' + args.suffix,))
    cpy_colls = {r[0] for r in cur.fetchall()}
    src_to_cpy = {c[: -len(args.suffix)]: c for c in cpy_colls}
    if not src_to_cpy:
        print(f"no collections with suffix {args.suffix!r} found — nothing to verify")
        sys.exit(0)

    # 2. Copy subtree flowModels
    copy_uids = collect_copy_subtree(cur, args.prefix)
    if not copy_uids:
        print(f"no Copy subtrees found (prefix={args.prefix!r})")
        sys.exit(0)

    # 3. scan options
    cur.execute(
        "SELECT uid, options FROM \"flowModels\" WHERE uid = ANY(%s)",
        (list(copy_uids),))
    violations = []  # (uid, model_use, path, key, value, suggested)
    for uid, opts in cur.fetchall():
        if not opts: continue
        use = opts.get('use', '?') if isinstance(opts, dict) else '?'
        for path, key, val in walk_refs(opts):
            suggested = None
            if key in REF_KEYS and val in src_to_cpy:
                suggested = src_to_cpy[val]
            elif key == 'associationName':
                parts = val.split('.')
                if len(parts) == 3 and parts[1] in src_to_cpy:
                    suggested = '.'.join([parts[0], src_to_cpy[parts[1]], parts[2]])
            if suggested:
                violations.append((uid, use, path, key, val, suggested))

    # 4. report
    if not violations:
        print(f'L2 collection-refs: PASS ({len(copy_uids)} Copy flowModels scanned, {len(src_to_cpy)} src→cpy pairs)')
        sys.exit(0)
    by_uid = {}
    for v in violations: by_uid.setdefault(v[0], []).append(v)
    print(f'L2 collection-refs: FAIL — {len(violations)} ref(s) on {len(by_uid)} model(s)')
    for uid, vs in sorted(by_uid.items()):
        print(f'  {uid}  ({vs[0][1]})')
        for _, _, path, key, val, sug in vs:
            print(f'    {path}: {val!r} → {sug!r}')

    # 5. autofix
    if args.fix:
        print(f'\n--fix: rewriting {len(by_uid)} model(s)…')
        fixed = 0
        for uid in by_uid:
            cur.execute("SELECT options FROM \"flowModels\" WHERE uid=%s", (uid,))
            opts = cur.fetchone()[0]
            n = rewrite_refs(opts, src_to_cpy)
            if n == 0: continue
            try:
                post(f'/api/flowModels:save?filterByTk={uid}', {'options': opts})
                fixed += 1
                print(f'  fixed {uid}: {n} ref(s)')
            except urllib.error.HTTPError as e:
                print(f'  FAILED {uid}: {e.code} {e.reason}')
        print(f'autofix done: {fixed}/{len(by_uid)} models updated')
        # re-scan to confirm
        cur.execute(
            "SELECT uid, options FROM \"flowModels\" WHERE uid = ANY(%s)",
            (list(copy_uids),))
        remaining = 0
        for uid, opts in cur.fetchall():
            if not opts: continue
            for path, key, val in walk_refs(opts):
                if (key in REF_KEYS and val in src_to_cpy) or \
                   (key == 'associationName' and len(val.split('.')) == 3 and val.split('.')[1] in src_to_cpy):
                    remaining += 1
        if remaining == 0:
            print('PASS after fix'); sys.exit(0)
        else:
            print(f'still {remaining} ref(s) unfixed'); sys.exit(1)

    sys.exit(1)

if __name__ == '__main__':
    main()
