#!/usr/bin/env python3
"""Copy-mode integrity verifier — dispatcher.

Runs all known Copy-drift checks against a live NocoBase instance and
aggregates the results. Each layer is a standalone script that can also be
invoked directly; this dispatcher just shells out to them and prints a
combined report.

Goal: every check passes ⇔ the Copy is interchangeable with the source
(same operations produce same behavior, Copy side never points back at
source data/UIDs/resources).

Layers:
  L1  topology         — route tree, per-page block use, popup depth   (copy-diff.py)
  L2  collection-refs  — block/column/association/action collection refs all `_copy`
  L3  block-internals  — table columns 3-layer, form items, linkageRules, eventFlows (copy-table-audit.py + extensions)
  L4  text-refs        — chart SQL `FROM`/`JOIN`, JS hardcoded UIDs/colls, Liquid var keys
  L5  workflows        — every workflow cloned, nodes config/script use `_copy` tables
  L6  db-schema        — PG column types match source (esp. dateOnly), system fields registered
  L7  templates        — flowModelTemplates targetUid trees non-empty

Usage:
  NB_URL=http://localhost:14000 NB_USER=... NB_PASSWORD=... \\
    python3 scripts/verify-copy.py [--layer L2] [--fix] [--prefix 'Copy - '] [--suffix '_copy']

Exit 0 only when every selected layer passes.
"""
import argparse, os, subprocess, sys

LAYERS = [
    ('L1', 'topology',        ['copy-diff.py'],                              False),
    ('L2', 'collection-refs', ['verify-collection-refs.py'],                 True),
    ('L3', 'block-internals', ['copy-table-audit.py', 'verify-block-internals.py'], True),
    ('L4', 'text-refs',       ['verify-text-refs.py'],                       True),
    ('L5', 'workflows',       ['verify-workflows.py'],                       False),
    ('L6', 'db-schema',       ['verify-db-schema.py'],                       True),
    ('L7', 'templates',       ['verify-templates.py'],                       False),
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--layer', action='append', default=[], help='only run named layer(s), e.g. --layer L2')
    ap.add_argument('--fix', action='store_true', help='pass --fix to layers that support autofix')
    ap.add_argument('--prefix', default='Copy - ', help='Copy title prefix')
    ap.add_argument('--suffix', default='_copy', help='Copy collection suffix')
    ap.add_argument('--report', help='write summary to file instead of stdout')
    args = ap.parse_args()

    selected = set(l.upper() for l in args.layer) if args.layer else None
    out_lines = []
    results = []  # (id, name, status, exit_code, output)

    for lid, lname, scripts, fixable in LAYERS:
        if selected and lid not in selected:
            continue
        sub_results = []
        for script in scripts:
            path = os.path.join(SCRIPT_DIR, script)
            if not os.path.exists(path):
                sub_results.append((script, 'SKIP', -1, f'(missing: {script})'))
                continue
            cmd = ['python3', path]
            # pass --prefix everywhere it's accepted; --suffix only where it's accepted
            if script in ('copy-diff.py', 'verify-collection-refs.py', 'verify-text-refs.py',
                          'verify-workflows.py', 'verify-templates.py', 'verify-block-internals.py'):
                cmd += ['--prefix', args.prefix]
            if script in ('verify-collection-refs.py', 'verify-text-refs.py', 'verify-workflows.py',
                          'verify-db-schema.py', 'verify-block-internals.py'):
                cmd += ['--suffix', args.suffix]
            if args.fix and fixable and script != 'copy-diff.py':
                # copy-diff has no --fix; others under L3 (audit + block-internals) take it
                if script != 'verify-block-internals.py' and script != 'verify-templates.py' \
                   and script != 'verify-workflows.py':
                    cmd.append('--fix')
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
                sub_status = 'PASS' if r.returncode == 0 else 'FAIL'
                sub_results.append((script, sub_status, r.returncode,
                                    (r.stdout or '') + (r.stderr or '')))
            except Exception as e:
                sub_results.append((script, 'ERROR', -1, f'{type(e).__name__}: {e}'))
        # aggregate per-layer status (FAIL if any sub fails)
        layer_status = 'PASS'
        if any(s[1] == 'ERROR' for s in sub_results): layer_status = 'ERROR'
        elif any(s[1] == 'FAIL' for s in sub_results): layer_status = 'FAIL'
        elif all(s[1] == 'SKIP' for s in sub_results): layer_status = 'SKIP'
        combined_out = '\n'.join(
            f'-- {s[0]} ({s[1]}) --\n{s[3]}' for s in sub_results if s[1] != 'PASS')
        worst_code = max((s[2] for s in sub_results), default=0)
        results.append((lid, lname, layer_status, worst_code, combined_out))

    out_lines.append('# Copy-mode verification report\n')
    for lid, lname, status, code, output in results:
        out_lines.append(f'\n=== {lid} {lname} === {status}')
        if status != 'PASS':
            out_lines.append(output.rstrip())

    failures = [r for r in results if r[2] not in ('PASS', 'SKIP')]
    skips = [r for r in results if r[2] == 'SKIP']
    out_lines.append('\n--- summary ---')
    for lid, lname, status, _, _ in results:
        out_lines.append(f'  {lid} {lname:18} {status}')
    out_lines.append(f'  total: {len(results)} | fail: {len(failures)} | skip: {len(skips)}')

    text = '\n'.join(out_lines)
    if args.report:
        with open(args.report, 'w') as f: f.write(text)
        print(f'report → {args.report}')
    else:
        print(text)

    sys.exit(1 if failures else 0)

if __name__ == '__main__':
    main()
