#!/usr/bin/env python3
"""Audit Copy-side TableBlockModels against source equivalents.

For every TableBlockModel whose collectionName ends in `_copy`, find the
corresponding source TableBlockModel (same coll minus `_copy`) and compare
column-by-column. Reports:

  - missing columns (fieldPath present in source, absent in Copy)
  - extra columns (in Copy but not source)
  - column drift (tableColumnSettings.model.use missing, width missing,
    fieldSettings.init.dataSourceKey missing)
  - inner DisplayFieldModel missing (column header renders, cells empty)

When multiple Copy tables share a collection (e.g. two `nb_crm_leads_copy`
table blocks), pair by containing-grid position. If pairing is ambiguous,
the audit prints "(ambiguous)" but still tries.

Usage:
    NB_URL=http://localhost:14000 NB_USER=admin@nocobase.com NB_PASSWORD=admin123 \\
      python3 scripts/copy-table-audit.py [--fix]

`--fix` (optional) auto-applies fixes for the safe cases:
  - sets tableColumnSettings.model.use from interface→display-model map
  - sets fieldSettings.init.{dataSourceKey, collectionName, fieldPath}

Adding missing columns is NOT auto-fixed — printed for review.

Exit code 0 when nothing wrong, 1 otherwise.
"""
import os, sys, json, argparse, urllib.request, urllib.error
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

TOK = resolve_token()
HDR = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}

# interface → display-model auto-fix table (subset; keep aligned with reconciler's
# DISPLAY_MODEL_MAP in src/deploy/display-model-fixer.ts).
INTERFACE_TO_DISPLAY = {
    'input': 'DisplayTextFieldModel', 'textarea': 'DisplayTextFieldModel',
    'email': 'DisplayTextFieldModel', 'phone': 'DisplayTextFieldModel',
    'password': 'DisplayTextFieldModel', 'sequence': 'DisplayTextFieldModel',
    'snowflakeId': 'DisplayTextFieldModel', 'uuid': 'DisplayTextFieldModel',
    'nanoid': 'DisplayTextFieldModel', 'icon': 'DisplayTextFieldModel',
    'url': 'DisplayURLFieldModel', 'richText': 'DisplayHtmlFieldModel',
    'vditor': 'DisplayTextFieldModel', 'markdown': 'DisplayTextFieldModel',
    'select': 'DisplayEnumFieldModel', 'radioGroup': 'DisplayEnumFieldModel',
    'multipleSelect': 'DisplayEnumFieldModel', 'checkboxGroup': 'DisplayEnumFieldModel',
    'checkbox': 'DisplayCheckboxFieldModel',
    'integer': 'DisplayNumberFieldModel', 'number': 'DisplayNumberFieldModel',
    'percent': 'DisplayPercentFieldModel', 'sort': 'DisplayNumberFieldModel',
    'date': 'DisplayDateTimeFieldModel', 'dateOnly': 'DisplayDateTimeFieldModel',
    'datetime': 'DisplayDateTimeFieldModel', 'datetimeNoTz': 'DisplayDateTimeFieldModel',
    'createdAt': 'DisplayDateTimeFieldModel', 'updatedAt': 'DisplayDateTimeFieldModel',
    'time': 'DisplayTimeFieldModel',
    'color': 'DisplayColorFieldModel', 'json': 'DisplayJSONFieldModel',
    'code': 'DisplayCodeFieldModel',
    'm2o': 'DisplayTextFieldModel', 'o2o': 'DisplayTextFieldModel',
    'obo': 'DisplayTextFieldModel', 'oho': 'DisplayTextFieldModel',
    'o2m': 'DisplayNumberFieldModel', 'm2m': 'DisplayNumberFieldModel',
    'attachment': 'DisplayNumberFieldModel', 'subTable': 'DisplayNumberFieldModel',
    'chinaRegion': 'DisplayEnumFieldModel', 'cascader': 'DisplayEnumFieldModel',
    'cascadeSelect': 'DisplayEnumFieldModel', 'formula': 'DisplayTextFieldModel',
    'roles': 'DisplayTextFieldModel',
    'createdBy': 'DisplaySubItemFieldModel', 'updatedBy': 'DisplaySubItemFieldModel',
}

ap = argparse.ArgumentParser()
ap.add_argument('--fix', action='store_true', help='auto-apply safe fixes (model.use, dataSourceKey)')
args = ap.parse_args()

conn = psycopg2.connect(DSN); cur = conn.cursor()

# ──────────────────────────────────────────────────────────────────────
# Pass A: collection metadata audit — system fields registration
# ──────────────────────────────────────────────────────────────────────
# duplicate-project creates Copy collections via collections:apply which
# auto-creates id/createdAt/updatedAt/createdBy/updatedBy PG columns but
# (depending on NB version) may skip registering them in the `fields`
# metadata table. Result: NB UI says "field may have been deleted" when
# rendering a column referencing updatedAt etc.
SYS_FIELDS = ['id', 'createdAt', 'updatedAt', 'createdBy', 'updatedBy']
cur.execute("SELECT DISTINCT \"collectionName\" FROM \"fields\" WHERE \"collectionName\" LIKE '%%_copy'")
copy_colls_with_any = sorted([r[0] for r in cur.fetchall()])
meta_problems = 0
meta_fixed = 0
for coll in copy_colls_with_any:
    src_coll = coll[:-5]
    cur.execute("SELECT name FROM \"fields\" WHERE \"collectionName\"=%s AND name = ANY(%s)", (coll, SYS_FIELDS))
    present = {r[0] for r in cur.fetchall()}
    missing_sys = [s for s in SYS_FIELDS if s not in present]
    if not missing_sys: continue
    # Pull source defs
    cur.execute("SELECT name, type, interface, options FROM \"fields\" WHERE \"collectionName\"=%s AND name = ANY(%s)", (src_coll, missing_sys))
    src_defs = cur.fetchall()
    src_names = {r[0] for r in src_defs}
    actually_missing = [n for n in missing_sys if n in src_names]
    if not actually_missing: continue
    meta_problems += len(actually_missing)
    if not args.fix:
        print(f"  ⚠ {coll}: missing system field registration {actually_missing}")
        continue
    for name, ftype, fiface, fopts in src_defs:
        if name not in actually_missing: continue
        fopts_d = fopts if isinstance(fopts, dict) else json.loads(fopts)
        payload = {'name': name, 'type': ftype, 'interface': fiface, **fopts_d}
        try:
            req = urllib.request.Request(
                f"{BASE}/api/collections/{coll}/fields:create",
                data=json.dumps(payload).encode(), headers=HDR, method="POST")
            urllib.request.urlopen(req)
            meta_fixed += 1
        except urllib.error.HTTPError:
            pass
print(f"=== collection meta audit: {meta_problems} missing system field registrations, fixed: {meta_fixed} ===\n")

# ──────────────────────────────────────────────────────────────────────
# Pass B: table column audit (existing)
# ──────────────────────────────────────────────────────────────────────
# ── 1. inventory ──
cur.execute("""
  SELECT uid, options
  FROM "flowModels"
  WHERE options->>'use' = 'TableBlockModel'
""")
all_tables = {}
for uid, opts in cur.fetchall():
    if isinstance(opts, str): opts = json.loads(opts)
    rs = ((opts.get('stepParams') or {}).get('resourceSettings') or {}).get('init') or {}
    coll = rs.get('collectionName')
    if not coll: continue
    all_tables[uid] = {'coll': coll, 'parent': opts.get('parentId')}

# Bucket source vs copy by collection
src_by_coll, cpy_by_coll = {}, {}
for uid, info in all_tables.items():
    coll = info['coll']
    if coll.endswith('_copy'):
        cpy_by_coll.setdefault(coll[:-5], []).append(uid)
    else:
        src_by_coll.setdefault(coll, []).append(uid)

# ── 2. per-table column detail ──
def column_meta(table_uid):
    """Return list of column dicts with field_path, model_use, has_inner."""
    cur.execute("""
      SELECT uid, options FROM "flowModels"
      WHERE options->>'parentId'=%s AND options->>'subKey'='columns'
      ORDER BY (options->>'sortIndex')::float NULLS LAST, uid
    """, (table_uid,))
    cols = []
    for cu, copts in cur.fetchall():
        co = copts if isinstance(copts, dict) else json.loads(copts)
        sp = co.get('stepParams') or {}
        init = ((sp.get('fieldSettings') or {}).get('init') or {})
        tcs = sp.get('tableColumnSettings') or {}
        # has any inner field model?
        cur.execute("SELECT options->>'use' FROM \"flowModels\" WHERE options->>'parentId'=%s LIMIT 1", (cu,))
        inner = cur.fetchone()
        cols.append({
            'uid': cu, 'use': co.get('use'),
            'field_path': init.get('fieldPath'),
            'dataSourceKey': init.get('dataSourceKey'),
            'collectionName': init.get('collectionName'),
            'model_use': (tcs.get('model') or {}).get('use'),
            'width': (tcs.get('width') or {}).get('width'),
            'sortIndex': co.get('sortIndex'),
            'inner_use': inner[0] if inner else None,
            'raw_stepParams': sp,
        })
    return cols

def field_interface(coll, field_path):
    """Look up field interface from collections table."""
    cur.execute("SELECT options FROM \"collections\" WHERE name=%s", (coll,))
    r = cur.fetchone()
    if not r: return None
    o = r[0] if isinstance(r[0], dict) else json.loads(r[0])
    # collections table is keyed differently; try via fields
    cur.execute("SELECT options FROM \"fields\" WHERE \"collectionName\"=%s AND name=%s", (coll, field_path))
    rr = cur.fetchone()
    if not rr: return None
    fo = rr[0] if isinstance(rr[0], dict) else json.loads(rr[0])
    return fo.get('interface')

def save(payload):
    req = urllib.request.Request(f"{BASE}/api/flowModels:save",
        data=json.dumps(payload).encode(), headers=HDR, method="POST")
    urllib.request.urlopen(req)

# ── 3. compare ──
problems = 0
fixed = 0
print(f"=== Copy table audit ({len(cpy_by_coll)} _copy collections) ===\n")
for coll, cpy_uids in sorted(cpy_by_coll.items()):
    src_uids = src_by_coll.get(coll, [])
    if not src_uids:
        print(f"⚠ {coll}: no source TableBlockModel found, skipping")
        continue
    # Pair by index (best-effort when count matches)
    if len(cpy_uids) != len(src_uids):
        print(f"! {coll}: source has {len(src_uids)} tables, copy has {len(cpy_uids)} (ambiguous pairing)")
    for i, cpy_uid in enumerate(cpy_uids):
        src_uid = src_uids[i] if i < len(src_uids) else src_uids[0]
        src_cols = column_meta(src_uid)
        cpy_cols = column_meta(cpy_uid)

        src_fields = {c['field_path']: c for c in src_cols if c['field_path']}
        cpy_fields = {c['field_path']: c for c in cpy_cols if c['field_path']}

        gaps = []
        # Missing fields
        missing = sorted(set(src_fields) - set(cpy_fields))
        if missing:
            gaps.append(f"missing columns: {','.join(missing)}")
            problems += len(missing)
        # Extra fields
        extra = sorted(set(cpy_fields) - set(src_fields))
        if extra:
            gaps.append(f"extra columns: {','.join(extra)}")
        # Drift on shared fields
        for fp in sorted(set(src_fields) & set(cpy_fields)):
            sc, cc = src_fields[fp], cpy_fields[fp]
            issues = []
            if sc['model_use'] and not cc['model_use']:
                issues.append(f"missing tableColumnSettings.model.use (need {sc['model_use']!r})")
            if sc['width'] and cc['width'] != sc['width']:
                issues.append(f"width drift {cc['width']!r} vs src {sc['width']!r}")
            if sc['inner_use'] and not cc['inner_use']:
                issues.append(f"missing inner {sc['inner_use']!r}")
            if not cc['dataSourceKey']:
                issues.append("missing fieldSettings.init.dataSourceKey")
            if issues:
                gaps.append(f"  {fp}: {'; '.join(issues)}")
                problems += len(issues)
                # Auto-fix safe cases
                if args.fix and sc['model_use'] and not cc['model_use']:
                    sp = cc['raw_stepParams']
                    sp.setdefault('fieldSettings', {})['init'] = {
                        'dataSourceKey': 'main',
                        'collectionName': coll + '_copy',
                        'fieldPath': fp,
                    }
                    tcs = sp.get('tableColumnSettings') or {}
                    tcs['model'] = {'use': sc['model_use']}
                    if sc['width']: tcs['width'] = {'width': sc['width']}
                    sp['tableColumnSettings'] = tcs
                    try:
                        save({'uid': cc['uid'], 'stepParams': sp})
                        fixed += 1
                    except urllib.error.HTTPError as e:
                        gaps.append(f"    ! fix HTTP {e.code}")
                # Auto-fix inner Display field: clone source's inner with copy collectionName
                if args.fix and sc['inner_use'] and not cc['inner_use']:
                    import random, string
                    new_inner_uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=11))
                    # Get source inner stepParams
                    cur.execute("SELECT options FROM \"flowModels\" WHERE options->>'parentId'=%s LIMIT 1", (sc['uid'],))
                    src_inner = cur.fetchone()
                    if src_inner:
                        sio = src_inner[0] if isinstance(src_inner[0], dict) else json.loads(src_inner[0])
                        inner_sp = sio.get('stepParams') or {}
                        # Rewrite collectionName refs in popupSettings.openView.collectionName
                        def deep_rewrite(obj):
                            if isinstance(obj, str):
                                if obj == coll: return coll + '_copy'
                                return obj
                            if isinstance(obj, dict): return {k: deep_rewrite(v) for k, v in obj.items()}
                            if isinstance(obj, list): return [deep_rewrite(x) for x in obj]
                            return obj
                        inner_sp = deep_rewrite(inner_sp)
                        try:
                            save({
                                'uid': new_inner_uid, 'name': new_inner_uid,
                                'use': sc['inner_use'],
                                'parentId': cc['uid'], 'subKey': 'field', 'subType': 'object',
                                'sortIndex': 0,
                                'stepParams': inner_sp,
                                'flowRegistry': sio.get('flowRegistry') or {},
                            })
                            fixed += 1
                        except urllib.error.HTTPError as e:
                            gaps.append(f"    ! inner-clone HTTP {e.code}")
        if not gaps: continue
        print(f"  {coll}_copy[{i}] (cpy={cpy_uid} ← src={src_uid}):")
        for g in gaps:
            print(f"    - {g}")

print(f"\n--- summary ---")
print(f"  problems: {problems}")
if args.fix: print(f"  auto-fixed: {fixed}")
sys.exit(0 if problems == 0 else 1)
