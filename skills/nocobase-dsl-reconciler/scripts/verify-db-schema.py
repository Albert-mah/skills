#!/usr/bin/env python3
"""L6 — verify _copy collection PG schema matches source.

Checks per `<src>_copy` table:
  A. column set: every column in `<src>` table exists in `<src>_copy`
  B. column type: data_type matches between src and cpy
     (catches the known `dateOnly → varchar` Copy drift bug)
  C. system fields registration in `fields` metadata table
     (id/createdAt/updatedAt/createdBy/updatedBy)

Autofix:
  - B (column type drift): `ALTER TABLE ... ALTER COLUMN ... TYPE <src_type>
    USING NULLIF(col, '')::<src_type>`  — limited to date-family types only
    (safer subset; other types might need lossy conversion)
  - C (missing field row): POST /api/collections/<coll>/fields:create
    cloning the source row.

Column-missing (A) is reported but NOT auto-fixed — adding columns might
require backfill decisions.

Usage:
  python3 scripts/verify-db-schema.py [--suffix '_copy'] [--fix]
"""
import argparse, json, os, sys, urllib.request, urllib.error
import psycopg2

DSN = os.environ.get('PG_DSN', 'dbname=nocobase user=nocobase password=nocobase host=localhost port=5435')
BASE = os.environ.get('NB_URL', 'http://localhost:14000').rstrip('/')

SYS_FIELDS = ['id', 'createdAt', 'updatedAt', 'createdBy', 'updatedBy']

ALTER_SAFE_TYPES = {'date', 'timestamp with time zone', 'timestamp without time zone',
                    'time with time zone', 'time without time zone'}

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

def main():
    global TOK, HDR
    ap = argparse.ArgumentParser()
    ap.add_argument('--suffix', default='_copy')
    ap.add_argument('--fix', action='store_true')
    args = ap.parse_args()

    TOK = resolve_token()
    HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
    conn = psycopg2.connect(DSN); cur = conn.cursor()

    # collect _copy tables in collections
    cur.execute("SELECT name FROM collections WHERE name LIKE %s", ('%' + args.suffix,))
    cpy_colls = sorted(r[0] for r in cur.fetchall())
    if not cpy_colls:
        print(f'no _copy collections'); sys.exit(0)

    a_missing = []      # (coll, col)
    b_drift   = []      # (coll, col, src_type, cpy_type)
    c_missing = []      # (coll, field)

    for cpy in cpy_colls:
        src = cpy[: -len(args.suffix)]
        # check both tables exist
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
        """, (src,))
        src_cols = dict(cur.fetchall())
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
        """, (cpy,))
        cpy_cols = dict(cur.fetchall())
        if not src_cols or not cpy_cols: continue
        # A. column set
        for col in src_cols:
            if col not in cpy_cols:
                a_missing.append((cpy, col))
            elif src_cols[col] != cpy_cols[col]:
                b_drift.append((cpy, col, src_cols[col], cpy_cols[col]))
        # C. fields registration
        cur.execute("SELECT name FROM \"fields\" WHERE \"collectionName\"=%s AND name=ANY(%s)",
                    (cpy, SYS_FIELDS))
        present = {r[0] for r in cur.fetchall()}
        for f in SYS_FIELDS:
            if f not in present and f in src_cols:
                c_missing.append((cpy, f))

    total = len(a_missing) + len(b_drift) + len(c_missing)
    if total == 0:
        print(f'L6 db-schema: PASS ({len(cpy_colls)} _copy collections checked)')
        sys.exit(0)
    print(f'L6 db-schema: FAIL — {total} issue(s)')
    if a_missing:
        print(f'  [A] missing columns: {len(a_missing)}')
        for c, col in a_missing[:20]: print(f'    {c}: column {col!r} not in copy')
    if b_drift:
        print(f'  [B] column type drift: {len(b_drift)}')
        for c, col, s, t in b_drift[:30]: print(f'    {c}.{col}: src={s} cpy={t}')
    if c_missing:
        print(f'  [C] system fields not registered: {len(c_missing)}')
        for c, f in c_missing[:30]: print(f'    {c}: missing fields-row for {f}')

    # autofix
    if args.fix:
        print('\n--fix:')
        # B: ALTER TABLE for safe date-family types
        fixed_b = 0
        for c, col, s, t in b_drift:
            if s in ALTER_SAFE_TYPES and t == 'character varying':
                sql = f'ALTER TABLE "{c}" ALTER COLUMN "{col}" TYPE {s} USING NULLIF("{col}", \'\')::{s}'
                try:
                    cur.execute(sql); conn.commit()
                    fixed_b += 1
                    print(f'  ALTER {c}.{col}: varchar → {s}')
                except Exception as e:
                    conn.rollback()
                    print(f'  ALTER {c}.{col} FAILED: {e}')
            else:
                print(f'  skip {c}.{col}: src={s} cpy={t} (not in safe set)')
        # C: clone src field rows → POST fields:create
        fixed_c = 0
        for c, f in c_missing:
            src = c[: -len(args.suffix)]
            cur.execute("""
                SELECT type, interface, options
                FROM "fields" WHERE "collectionName"=%s AND name=%s
            """, (src, f))
            row = cur.fetchone()
            if not row:
                print(f'  skip {c}.{f}: no source row to clone'); continue
            stype, iface, soptions = row
            payload = {
                'name': f, 'type': stype, 'interface': iface,
                'collectionName': c, 'options': soptions or {},
            }
            try:
                post(f'/api/collections/{c}/fields:create', payload)
                fixed_c += 1
                print(f'  fields:create {c}.{f}')
            except urllib.error.HTTPError as e:
                print(f'  fields:create {c}.{f} FAILED: {e.code} {e.reason}')
        print(f'autofix: B={fixed_b}/{len(b_drift)}  C={fixed_c}/{len(c_missing)}')

    sys.exit(1)

if __name__ == '__main__':
    main()
