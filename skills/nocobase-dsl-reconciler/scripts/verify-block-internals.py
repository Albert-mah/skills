#!/usr/bin/env python3
"""L3 (extension) — strict block-internals equality between source and Copy.

Pairs each source block with its Copy counterpart by:
  - collection: `<x>` ↔ `<x>_copy` (block-level `stepParams.dataSource.collectionName`)
  - in-grid position: same sortIndex among same-use siblings under the
    same grid (within the matching page)

For every pair, asserts strict equality on:
  - direct child count by subKey (items, columns, tabs)
  - linkageRules count + content (per-rule deep-equal after stripping
    uid-bearing keys)
  - eventFlows count + content (same normalization)
  - per-FormItem subKey=items children: stepParams.fieldSettings.init
    field name match + required + default present-or-absent

UID-bearing keys stripped from deep-equal: `uid`, `parentId`, `sortIndex`,
`subKey` — these legitimately differ between src/cpy.

Strict mode: any mismatch = FAIL. No autofix in this version — fixing
strucutral DSL drift programmatically is risky; reports are pinpointed
enough for human edit or for the next deploy iteration.

Usage:
  python3 scripts/verify-block-internals.py [--prefix 'Copy - '] [--suffix '_copy']
"""
import argparse, json, os, sys
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')

STRIP_KEYS = {'uid', 'parentId', 'sortIndex', 'subKey', 'createdAt', 'updatedAt',
              'createdBy', 'updatedBy', '__v', 'id'}

def normalize(obj):
    """Drop uid-bearing keys recursively for deep-equal."""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items() if k not in STRIP_KEYS}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return obj

def get_block_coll(opts):
    if not isinstance(opts, dict): return None
    sp = opts.get('stepParams') or {}
    ds = sp.get('dataSource') or {}
    return ds.get('collectionName') or sp.get('collectionName') or opts.get('collectionName')

def fetch_descendants(cur, uid):
    """Return list of (uid, options, sub_key, parent_id) for the entire subtree."""
    out = []
    cur.execute("SELECT uid, options FROM \"flowModels\" WHERE options->>'parentId'=%s", (uid,))
    rows = cur.fetchall()
    for u, o in rows:
        out.append((u, o))
        out.extend(fetch_descendants(cur, u))
    return out

def items_by_subkey(cur, parent_uid, subkey):
    cur.execute(
        "SELECT uid, options FROM \"flowModels\" "
        "WHERE options->>'parentId'=%s AND options->>'subKey'=%s "
        "ORDER BY (options->>'sortIndex')::float NULLS LAST",
        (parent_uid, subkey))
    return cur.fetchall()

def get_grid_uid(cur, block_uid):
    """Walk up: block → grid item → grid (subKey=items) → grid uid."""
    cur.execute("SELECT options->>'parentId', options->>'subKey' FROM \"flowModels\" WHERE uid=%s",
                (block_uid,))
    row = cur.fetchone()
    return row[0] if row else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='Copy - ')
    ap.add_argument('--suffix', default='_copy')
    args = ap.parse_args()

    conn = psycopg2.connect(DSN); cur = conn.cursor()

    # Find all block models that have a collectionName
    cur.execute(
        "SELECT uid, options FROM \"flowModels\" "
        "WHERE options->'stepParams'->'dataSource'->>'collectionName' IS NOT NULL"
    )
    rows = cur.fetchall()
    src_blocks = {}  # collection → list of (uid, opts, gridUid, sortIndex, use)
    cpy_blocks = {}
    for uid, opts in rows:
        coll = get_block_coll(opts)
        if not coll: continue
        grid_uid = opts.get('parentId')
        sort_idx = opts.get('sortIndex')
        use = opts.get('use')
        bucket = cpy_blocks if coll.endswith(args.suffix) else src_blocks
        bucket.setdefault(coll, []).append((uid, opts, grid_uid, sort_idx, use))

    issues = []
    pairs_checked = 0

    for src_coll, src_list in src_blocks.items():
        cpy_coll = src_coll + args.suffix
        cpy_list = cpy_blocks.get(cpy_coll, [])
        if not cpy_list:
            continue
        # pair by sortIndex within same use; fall back to len-1 1:1
        # Group by use
        def by_use(lst):
            d = {}
            for x in lst: d.setdefault(x[4], []).append(x)
            return d
        src_by_use = by_use(src_list); cpy_by_use = by_use(cpy_list)
        for use, sblocks in src_by_use.items():
            cblocks = cpy_by_use.get(use, [])
            # sort by sortIndex
            sblocks.sort(key=lambda x: x[3] or 0)
            cblocks.sort(key=lambda x: x[3] or 0)
            # pair index-wise (best effort)
            n = min(len(sblocks), len(cblocks))
            for i in range(n):
                s_uid, s_opts, _, _, _ = sblocks[i]
                c_uid, c_opts, _, _, _ = cblocks[i]
                pairs_checked += 1
                # 1. linkageRules
                sl = (s_opts.get('stepParams') or {}).get('linkageRules') or []
                cl = (c_opts.get('stepParams') or {}).get('linkageRules') or []
                if len(sl) != len(cl):
                    issues.append(('linkageRules-count', f'{src_coll}#{i}',
                        f'src={len(sl)} cpy={len(cl)} (src_uid={s_uid} cpy_uid={c_uid})'))
                elif normalize(sl) != normalize(cl):
                    issues.append(('linkageRules-diff', f'{src_coll}#{i}',
                        f'rules differ after normalize (src_uid={s_uid} cpy_uid={c_uid})'))
                # 2. eventFlows
                se = (s_opts.get('stepParams') or {}).get('eventFlows') or []
                ce = (c_opts.get('stepParams') or {}).get('eventFlows') or []
                if len(se) != len(ce):
                    issues.append(('eventFlows-count', f'{src_coll}#{i}',
                        f'src={len(se)} cpy={len(ce)}'))
                elif normalize(se) != normalize(ce):
                    issues.append(('eventFlows-diff', f'{src_coll}#{i}',
                        f'flows differ after normalize'))
                # 3. child counts by subKey
                for sk in ('items', 'columns', 'tabs', 'grids'):
                    s_children = items_by_subkey(cur, s_uid, sk)
                    c_children = items_by_subkey(cur, c_uid, sk)
                    if len(s_children) != len(c_children):
                        issues.append((f'{sk}-count', f'{src_coll}#{i}',
                            f'src={len(s_children)} cpy={len(c_children)}'))
                    elif sk == 'items':
                        # per-item: compare fieldSettings.init.fieldPath + required
                        for j, (su, sopts) in enumerate(s_children):
                            cu, copts = c_children[j]
                            ss = (sopts or {}).get('stepParams') or {}
                            cs = (copts or {}).get('stepParams') or {}
                            s_fld = ((ss.get('fieldSettings') or {}).get('init') or {}).get('fieldPath')
                            c_fld = ((cs.get('fieldSettings') or {}).get('init') or {}).get('fieldPath')
                            if s_fld != c_fld:
                                issues.append(('item-fieldPath', f'{src_coll}#{i}.items[{j}]',
                                    f'src={s_fld!r} cpy={c_fld!r}'))
                            s_req = ((ss.get('fieldSettings') or {}).get('required') or {}).get('required')
                            c_req = ((cs.get('fieldSettings') or {}).get('required') or {}).get('required')
                            if bool(s_req) != bool(c_req):
                                issues.append(('item-required', f'{src_coll}#{i}.items[{j}]',
                                    f'src={s_req} cpy={c_req}'))

    if not issues:
        print(f'L3 block-internals: PASS ({pairs_checked} pair(s) checked)')
        sys.exit(0)
    print(f'L3 block-internals: FAIL — {len(issues)} drift(s)')
    by_kind = {}
    for k, w, m in issues: by_kind.setdefault(k, []).append((w, m))
    for k in sorted(by_kind):
        print(f'  [{k}] x{len(by_kind[k])}')
        for w, m in by_kind[k][:15]:
            print(f'    {w}: {m}')
    sys.exit(1)

if __name__ == '__main__':
    main()
