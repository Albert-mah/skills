#!/usr/bin/env python3
"""L7 — verify flowModelTemplates Copy targets are non-empty.

flowModelTemplates is a metadata table; each row has a targetUid pointing
at a flowModel tree. duplicate-project may copy the template row but
forget to clone the targetUid tree, leaving Copy templates as empty
shells (1 child or 0 children).

Templates are NOT title-prefixed by duplicate-project; instead pairing is
by `(name, collectionName)` — source has collection `<x>`, Copy has
`<x>_copy`. Same name, different collection.

Strategy:
  1. List all templates. Pair each Copy template (collectionName ends
     with `_copy`) to a source template with same name + the unsuffixed
     collectionName.
  2. Count descendants under each targetUid.
  3. If Copy descendants < source descendants, that's a drift.

Report-only — auto-cloning empty templates is risky (we tried, reverted).

Usage:
  python3 scripts/verify-templates.py [--suffix '_copy'] [--prefix 'Copy - ']
  (--prefix accepted for dispatcher compatibility; not used here)
"""
import argparse, os, sys
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')

def descendants(cur, uid):
    cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId'=%s", (uid,))
    kids = [r[0] for r in cur.fetchall()]
    n = len(kids)
    for k in kids: n += descendants(cur, k)
    return n

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='Copy - ')  # accepted for dispatcher compat, unused
    ap.add_argument('--suffix', default='_copy')
    args = ap.parse_args()

    conn = psycopg2.connect(DSN); cur = conn.cursor()
    cur.execute('SELECT uid, name, "collectionName", "targetUid" FROM "flowModelTemplates"')
    rows = cur.fetchall()
    # index by (name, collectionName)
    by_key = {}
    for uid, name, coll, tuid in rows:
        by_key.setdefault((name, coll), []).append((uid, tuid))

    issues = []
    pairs_checked = 0
    for (name, coll), vs in by_key.items():
        if not coll or not coll.endswith(args.suffix): continue
        src_coll = coll[: -len(args.suffix)]
        src_rows = by_key.get((name, src_coll))
        if not src_rows:
            issues.append(('no-source', f'{name!r}@{coll}',
                f'no source template (name, {src_coll})'))
            continue
        _, src_tuid = src_rows[0]
        for cuid, ctuid in vs:
            if not ctuid:
                issues.append(('empty-target', f'{name!r}@{coll}', f'targetUid empty (uid={cuid})'))
                continue
            pairs_checked += 1
            src_n = descendants(cur, src_tuid) if src_tuid else 0
            cpy_n = descendants(cur, ctuid)
            if cpy_n < src_n:
                issues.append(('shorter', f'{name!r}@{coll}',
                    f'src descendants={src_n} cpy={cpy_n} (uid={cuid}, target={ctuid})'))

    if not issues:
        print(f'L7 templates: PASS ({pairs_checked} pair(s) checked)')
        sys.exit(0)
    print(f'L7 templates: FAIL — {len(issues)} issue(s)')
    by_kind = {}
    for k, *rest in issues: by_kind.setdefault(k, []).append(rest)
    for k in sorted(by_kind):
        print(f'  [{k}] x{len(by_kind[k])}')
        for title, msg in by_kind[k][:20]:
            print(f'    {title}: {msg}')
    sys.exit(1)

if __name__ == '__main__':
    main()
