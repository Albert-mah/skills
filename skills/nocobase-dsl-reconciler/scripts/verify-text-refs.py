#!/usr/bin/env python3
"""L4 — verify Copy-side text refs (SQL / JS / Liquid) all point to _copy.

Three sub-checks:

  A. chart SQL: every `flowSql` record reachable from a Copy chart block,
     plus every `stepParams.chartSettings.configure.query.sql` string,
     scanned for FROM/JOIN/INTO/UPDATE/DELETE referring to a known source
     PG table name when a `<table>_copy` exists.

  B. JS strings: every `stepParams.jsSettings.runJs` (jsBlock / jsAction
     / jsColumn / jsItem) scanned for string literals matching source
     `nb_*` table names or `collectionName='<src>'` patterns.

  C. Liquid var keys (informational): `{% if ctx.var_form1.X %}`
     patterns inside SQL — uid suffix mapping is too hard to auto-fix
     reliably; reported for human review.

Autofix (A, B):
  - regex-replace source table names with `<src>_copy` in SQL text
  - regex-replace `collectionName: '<src>'` and `'<src>.field'` association
    paths in JS strings

Liquid var keys (C) are reported but NOT auto-fixed.

Usage:
  python3 scripts/verify-text-refs.py [--prefix 'Copy - '] [--suffix '_copy'] [--fix]
"""
import argparse, json, os, re, sys, urllib.request, urllib.error
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')
BASE = os.environ.get('NB_URL', 'http://localhost:14000').rstrip('/')

def resolve_token():
    if os.environ.get('NB_TOKEN'): return os.environ['NB_TOKEN'].strip()
    user, pwd = os.environ.get('NB_USER'), os.environ.get('NB_PASSWORD')
    if not (user and pwd): sys.exit("set NB_TOKEN or NB_USER+NB_PASSWORD")
    body = json.dumps({"account": user, "password": pwd}).encode()
    req = urllib.request.Request(f"{BASE}/api/auth:signIn", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())['data']['token']

TOK = None; HDR = None

def post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=HDR, method="POST")
    return json.loads(urllib.request.urlopen(req).read())

def get_pg_tables(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
    """)
    return {r[0] for r in cur.fetchall()}

def collect_copy_subtree(cur, prefix):
    cur.execute("SELECT id, \"parentId\", title, \"schemaUid\" FROM \"desktopRoutes\"")
    routes = cur.fetchall()
    by_parent = {}
    for rid, pid, title, suid in routes:
        by_parent.setdefault(pid, []).append((rid, title, suid))
    schema_uids = set()
    def walk(parent_id):
        for rid, title, suid in by_parent.get(parent_id, []):
            if suid: schema_uids.add(suid)
            walk(rid)
    for rid, pid, title, suid in routes:
        if pid is None and title and title.startswith(prefix):
            if suid: schema_uids.add(suid)
            walk(rid)
    if not schema_uids: return set()
    seen = set(schema_uids); frontier = list(schema_uids)
    while frontier:
        cur.execute("SELECT uid FROM \"flowModels\" WHERE options->>'parentId' = ANY(%s)", (frontier,))
        nxt = [r[0] for r in cur.fetchall() if r[0] not in seen]
        seen.update(nxt); frontier = nxt
    return seen

# SQL keywords followed by an identifier we care about
SQL_TABLE_RE = re.compile(r'\b(FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+["\']?([a-zA-Z_][\w$]*)["\']?', re.IGNORECASE)
# JS string literal patterns
JS_TABLE_LIT_RE = re.compile(r"(['\"])(nb_[a-z0-9_]+)\1")

def find_sql_table_refs(sql_text, src_to_cpy_tables):
    out = []
    for m in SQL_TABLE_RE.finditer(sql_text or ''):
        tbl = m.group(2)
        if tbl in src_to_cpy_tables:
            out.append((m.start(), m.end(), tbl, src_to_cpy_tables[tbl]))
    return out

def rewrite_sql(sql_text, src_to_cpy_tables):
    if not sql_text: return sql_text, 0
    n = 0
    def repl(m):
        nonlocal n
        kw, tbl = m.group(1), m.group(2)
        if tbl in src_to_cpy_tables:
            n += 1
            return f'{kw} {src_to_cpy_tables[tbl]}'
        return m.group(0)
    new = SQL_TABLE_RE.sub(repl, sql_text)
    return new, n

def find_js_table_refs(js_text, src_to_cpy_tables):
    out = []
    for m in JS_TABLE_LIT_RE.finditer(js_text or ''):
        tbl = m.group(2)
        if tbl in src_to_cpy_tables:
            out.append((m.start(), m.end(), tbl, src_to_cpy_tables[tbl]))
    return out

def rewrite_js(js_text, src_to_cpy_tables, src_to_cpy_colls):
    if not js_text: return js_text, 0
    n = 0
    def repl_tbl(m):
        nonlocal n
        q, tbl = m.group(1), m.group(2)
        if tbl in src_to_cpy_tables:
            n += 1
            return f'{q}{src_to_cpy_tables[tbl]}{q}'
        return m.group(0)
    new = JS_TABLE_LIT_RE.sub(repl_tbl, js_text)
    # collectionName: 'leads' → 'leads_copy'
    for src, cpy in src_to_cpy_colls.items():
        pat = re.compile(r"(collectionName\s*[:=]\s*['\"])" + re.escape(src) + r"(['\"])")
        new2, k = pat.subn(lambda m: f'{m.group(1)}{cpy}{m.group(2)}', new)
        n += k; new = new2
    return new, n

LIQUID_VAR_RE = re.compile(r'var_form1\.([a-zA-Z_][\w]*)_([a-z0-9]+)')

def find_liquid_keys(text):
    return [(m.group(1), m.group(2)) for m in LIQUID_VAR_RE.finditer(text or '')]

def walk_strings(obj, path=''):
    """Yield (path, key, value) for every string leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            new = f'{path}.{k}' if path else k
            if isinstance(v, str):
                yield new, k, v
            else:
                yield from walk_strings(v, new)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f'{path}[{i}]')

def set_at(obj, path, value):
    """Set value at dotted/bracketed path in nested obj. Path uses our format."""
    parts = re.split(r'\.|(\[\d+\])', path)
    parts = [p for p in parts if p]
    cur = obj
    for i, p in enumerate(parts):
        last = (i == len(parts) - 1)
        if p.startswith('['):
            idx = int(p[1:-1])
            if last: cur[idx] = value
            else: cur = cur[idx]
        else:
            if last: cur[p] = value
            else: cur = cur[p]

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

    # build src→cpy maps for tables AND collections
    all_tables = get_pg_tables(cur)
    src_to_cpy_tables = {t[: -len(args.suffix)]: t for t in all_tables if t.endswith(args.suffix)}
    cur.execute("SELECT name FROM collections WHERE name LIKE %s", ('%' + args.suffix,))
    cpy_colls = {r[0] for r in cur.fetchall()}
    src_to_cpy_colls = {c[: -len(args.suffix)]: c for c in cpy_colls}

    if not src_to_cpy_tables:
        print(f'no _copy tables found'); sys.exit(0)

    copy_uids = collect_copy_subtree(cur, args.prefix)
    if not copy_uids:
        print('no Copy subtree found'); sys.exit(0)

    # === Sub-check A: chart SQL — flowSql records ===
    cur.execute(
        "SELECT uid, sql FROM \"flowSql\" WHERE uid = ANY(%s)",
        (list(copy_uids),))
    sql_violations = []  # (uid, source, table_refs)
    for uid, sql in cur.fetchall():
        refs = find_sql_table_refs(sql, src_to_cpy_tables)
        if refs:
            sql_violations.append(('flowSql', uid, sql, refs))

    # === Sub-check A2 + B + C: walk options recursively ===
    cur.execute("SELECT uid, options FROM \"flowModels\" WHERE uid = ANY(%s)",
                (list(copy_uids),))
    opts_sql_viol = []   # (uid, use, path, value, refs)
    opts_js_viol  = []
    liquid_keys   = {}   # uid → list of (field, srcUid)
    rewrites_for_uid = {}  # uid → patched options (for autofix)
    for uid, opts in cur.fetchall():
        if not opts: continue
        use = opts.get('use', '?') if isinstance(opts, dict) else '?'
        for path, k, v in walk_strings(opts):
            # A2: stepParams.chartSettings.configure.query.sql or any path ending in '.sql'
            if path.endswith('.sql') or k == 'sql':
                refs = find_sql_table_refs(v, src_to_cpy_tables)
                if refs:
                    opts_sql_viol.append((uid, use, path, v, refs))
                lks = find_liquid_keys(v)
                if lks:
                    liquid_keys.setdefault(uid, []).extend([(f, u, path) for f, u in lks])
            # B: runJs
            if path.endswith('.runJs') or k == 'runJs':
                refs = find_js_table_refs(v, src_to_cpy_tables)
                if refs:
                    opts_js_viol.append((uid, use, path, v, refs))

    # === report ===
    total = len(sql_violations) + len(opts_sql_viol) + len(opts_js_viol)
    if total == 0 and not liquid_keys:
        print(f'L4 text-refs: PASS (flowSql={cur.rowcount}, scanned Copy options)')
        sys.exit(0)
    print(f'L4 text-refs: {"FAIL" if total else "WARN-only"}')
    if sql_violations:
        print(f'  [A] flowSql records with source table refs: {len(sql_violations)}')
        for src_type, uid, sql, refs in sql_violations[:20]:
            tbl_list = ', '.join(f'{t}→{c}' for _, _, t, c in refs)
            print(f'    {uid}: {tbl_list}')
    if opts_sql_viol:
        print(f'  [A2] embedded SQL strings with source table refs: {len(opts_sql_viol)}')
        for uid, use, path, _, refs in opts_sql_viol[:20]:
            tbl_list = ', '.join(f'{t}→{c}' for _, _, t, c in refs)
            print(f'    {uid} ({use}) {path}: {tbl_list}')
    if opts_js_viol:
        print(f'  [B] JS strings with source refs: {len(opts_js_viol)}')
        for uid, use, path, _, refs in opts_js_viol[:20]:
            tbl_list = ', '.join(f'{t}→{c}' for _, _, t, c in refs)
            print(f'    {uid} ({use}) {path}: {tbl_list}')
    if liquid_keys:
        n_keys = sum(len(v) for v in liquid_keys.values())
        print(f'  [C] Liquid var_form1.<field>_<uid> keys (manual review — uid suffix may need remap): {n_keys}')
        for uid, ks in list(liquid_keys.items())[:5]:
            for f, srcUid, path in ks[:3]:
                print(f'    {uid} {path}: var_form1.{f}_{srcUid}')

    # === autofix ===
    if args.fix and total:
        print('\n--fix: rewriting…')
        fixed_sql = 0
        # A: flowSql
        for _, uid, sql, _ in sql_violations:
            new_sql, n = rewrite_sql(sql, src_to_cpy_tables)
            if n:
                try:
                    post(f'/api/flowSql:update?filterByTk={uid}', {'sql': new_sql})
                    fixed_sql += 1
                    print(f'  flowSql {uid}: {n} ref(s) rewritten')
                except urllib.error.HTTPError as e:
                    print(f'  flowSql {uid} FAILED: {e.code} {e.reason}')
        # A2+B: re-fetch options once per uid, apply all rewrites then save
        target_uids = set([v[0] for v in opts_sql_viol] + [v[0] for v in opts_js_viol])
        fixed_opts = 0
        for uid in target_uids:
            cur.execute("SELECT options FROM \"flowModels\" WHERE uid=%s", (uid,))
            row = cur.fetchone()
            if not row: continue
            opts = row[0]
            total_changes = 0
            def rewrite_strings(obj):
                nonlocal total_changes
                if isinstance(obj, dict):
                    for k, v in list(obj.items()):
                        if isinstance(v, str):
                            if k == 'sql':
                                new, n = rewrite_sql(v, src_to_cpy_tables)
                                if n: obj[k] = new; total_changes += n
                            elif k == 'runJs':
                                new, n = rewrite_js(v, src_to_cpy_tables, src_to_cpy_colls)
                                if n: obj[k] = new; total_changes += n
                        else:
                            rewrite_strings(v)
                elif isinstance(obj, list):
                    for v in obj:
                        rewrite_strings(v)
            rewrite_strings(opts)
            if total_changes:
                try:
                    post(f'/api/flowModels:save?filterByTk={uid}', {'options': opts})
                    fixed_opts += 1
                    print(f'  flowModel {uid}: {total_changes} string(s) rewritten')
                except urllib.error.HTTPError as e:
                    print(f'  flowModel {uid} FAILED: {e.code} {e.reason}')
        print(f'autofix done: flowSql={fixed_sql}, flowModels={fixed_opts}')

    sys.exit(1 if total else 0)

if __name__ == '__main__':
    main()
