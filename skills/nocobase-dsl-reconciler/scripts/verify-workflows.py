#!/usr/bin/env python3
"""L5 — verify workflows: every source workflow has a Copy clone and the
Copy's trigger/nodes refer to `_copy` collections.

Pairing strategy:
  Copy workflows are identified by `key LIKE '%_copy'` OR `title LIKE
  'Copy - %'`. For each Copy, derive expected source = key without
  `_copy` suffix, or title without `Copy - ` prefix.

Checks:
  - every source workflow has a Copy (by either pairing)
  - workflow.config.collection == `<src>_copy`
  - every node.config.collection / sourceCollection / targetCollection
    referring to a known source coll is flagged
  - sql-node script scanned for source table FROM/JOIN refs

Reports only — no autofix in this version (changing live workflow config
is high-risk; user wanted manual review).

Usage:
  python3 scripts/verify-workflows.py [--prefix 'Copy - '] [--suffix '_copy']
"""
import argparse, json, os, re, sys, urllib.request, urllib.error
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')
BASE = os.environ.get('NB_URL', 'http://localhost:14000').rstrip('/')

SQL_TABLE_RE = re.compile(r'\b(FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+["\']?([a-zA-Z_][\w$]*)["\']?', re.IGNORECASE)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='Copy - ')
    ap.add_argument('--suffix', default='_copy')
    args = ap.parse_args()

    conn = psycopg2.connect(DSN); cur = conn.cursor()
    cur.execute("""
        SELECT id, key, title, type, "current", enabled, config
        FROM workflows
    """)
    workflows = cur.fetchall()
    # group by key
    src = {}  # key (no _copy) → list of rows
    cpy = {}  # key (no _copy) → list of rows
    for row in workflows:
        wid, key, title, wtype, current, enabled, config = row
        if not key: continue
        if key.endswith(args.suffix):
            cpy.setdefault(key[: -len(args.suffix)], []).append(row)
        elif title and title.startswith(args.prefix):
            base = title[len(args.prefix):]
            cpy.setdefault(base, []).append(row)
        else:
            src.setdefault(key, []).append(row)

    # all tables for SQL scan
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
    all_tables = {r[0] for r in cur.fetchall()}
    src_to_cpy_tables = {t[: -len(args.suffix)]: t for t in all_tables if t.endswith(args.suffix)}
    cur.execute("SELECT name FROM collections WHERE name LIKE %s", ('%' + args.suffix,))
    cpy_colls = {r[0] for r in cur.fetchall()}
    src_to_cpy_colls = {c[: -len(args.suffix)]: c for c in cpy_colls}

    problems = []

    # 1. missing Copy
    missing_cpy = sorted(set(src) - set(cpy))
    for k in missing_cpy:
        problems.append(('missing-copy', k, f'source workflow {k!r} has no Copy clone'))

    # 2. per Copy workflow: scan config + nodes
    for base, rows in cpy.items():
        for row in rows:
            wid, key, title, wtype, current, enabled, config = row
            cfg = config or {}
            wcoll = cfg.get('collection')
            if isinstance(wcoll, str) and wcoll in src_to_cpy_colls:
                problems.append(('config-collection', key or title or str(wid),
                    f'workflow.config.collection={wcoll!r} should be {src_to_cpy_colls[wcoll]!r}'))
            # nodes
            cur.execute(
                "SELECT id, type, key, config FROM flow_nodes WHERE \"workflowId\"=%s",
                (wid,))
            for nid, ntype, nkey, ncfg in cur.fetchall():
                if not ncfg: continue
                # collection ref keys
                for refkey in ('collection', 'sourceCollection', 'targetCollection', 'collectionName'):
                    v = ncfg.get(refkey)
                    if isinstance(v, str) and v in src_to_cpy_colls:
                        problems.append(('node-coll', f'{key}/{nkey}',
                            f'node.{refkey}={v!r} should be {src_to_cpy_colls[v]!r}'))
                # sql node script
                script = ncfg.get('sql') or ncfg.get('script')
                if isinstance(script, str):
                    for m in SQL_TABLE_RE.finditer(script):
                        tbl = m.group(2)
                        if tbl in src_to_cpy_tables:
                            problems.append(('node-sql', f'{key}/{nkey}',
                                f'SQL {m.group(1)} {tbl!r} → {src_to_cpy_tables[tbl]!r}'))

    if not problems:
        print(f'L5 workflows: PASS (src={len(src)}, cpy={len(cpy)})')
        sys.exit(0)
    print(f'L5 workflows: FAIL — {len(problems)} issue(s)')
    by_kind = {}
    for kind, where, msg in problems:
        by_kind.setdefault(kind, []).append((where, msg))
    for kind in sorted(by_kind):
        print(f'  [{kind}] x{len(by_kind[kind])}')
        for where, msg in by_kind[kind][:20]:
            print(f'    {where}: {msg}')
    sys.exit(1)

if __name__ == '__main__':
    main()
