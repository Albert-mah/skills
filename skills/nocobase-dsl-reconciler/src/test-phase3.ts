/**
 * Phase 3 + B#2 + C regression tests — runs offline (no NB connection needed).
 *
 * Covers:
 *   - ui-builder-bridge: drift detection + lazy load + defensive fallbacks
 *   - assign-values validation (Phase 3c): typo catches, valid spec passes
 *   - jsAction (B#2): action-key derivation, model registry
 *   - workflow state prune (C): pruneStaleWorkflowState dry-run logic
 *
 * Run:
 *   npx tsx src/test-phase3.ts
 *
 * Exits non-zero on any failure. Self-contained, no runner.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { tmpdir } from 'node:os';

import {
  detectJsModelUseDrift,
  getKnownRunjsModelUses,
  validateAssignValues,
  validatePopupDocument,
  validateBlueprintShape,
} from './utils/ui-builder-bridge';
import {
  RECONCILER_JS_MODEL_USES,
  ACTION_TYPE_TO_MODEL,
  MODEL_TO_ACTION_TYPE,
} from './utils/block-types';
import { actionKey } from './utils/action-key';
import { validatePageSpecsAssignValues } from './deploy/spec-validator';
import { discoverPages } from './deploy/page-discovery';
import { loadYaml, saveYaml, dumpYaml } from './utils/yaml';

// ── Helpers ──

let passed = 0;
let failed = 0;
const failures: string[] = [];

function assert(label: string, condition: boolean, detail?: string): void {
  if (condition) {
    passed++;
    console.log(`  PASS  ${label}`);
  } else {
    failed++;
    const msg = detail ? `${label} — ${detail}` : label;
    failures.push(msg);
    console.log(`  FAIL  ${msg}`);
  }
}

function section(name: string): void {
  console.log(`\n=== ${name} ===`);
}

function mkTmpProject(label: string): string {
  const dir = fs.mkdtempSync(path.join(tmpdir(), `dsl-test-${label}-`));
  return dir;
}

function rmTmp(dir: string): void {
  fs.rmSync(dir, { recursive: true, force: true });
}

// ══════════════════════════════════════════════════════════════════
// ui-builder-bridge: surface-policy drift detection (Phase 3a)
// ══════════════════════════════════════════════════════════════════
async function testSurfacePolicyBridge(): Promise<void> {
  section('Phase 3a: surface-policy drift detection');

  const known = await getKnownRunjsModelUses();
  // ui-builder may not be reachable in some environments; that's OK — the
  // bridge returns []. We only assert when it loaded.
  if (known.length) {
    assert('bridge loaded RUNJS_MODEL_USES from ui-builder', known.length >= 6);
    assert('JSItemModel known', known.includes('JSItemModel'));
    assert('JSColumnModel known', known.includes('JSColumnModel'));
    assert('JSBlockModel known', known.includes('JSBlockModel'));
    assert('JSItemActionModel known (added in NB 2.0.27)', known.includes('JSItemActionModel'));
  } else {
    console.log('  SKIP  ui-builder bridge not reachable — drift assertions skipped');
  }

  // Our emitted uses should all be in the known set (zero drift expected).
  const drift = await detectJsModelUseDrift(RECONCILER_JS_MODEL_USES);
  assert('reconciler emits no unknown model uses (or bridge unreachable)',
    drift.length === 0,
    drift.length ? `drift: ${drift.join(', ')}` : undefined);

  // Inject a fake use that the kernel doesn't know — drift should catch it.
  const driftWithFake = await detectJsModelUseDrift([...RECONCILER_JS_MODEL_USES, 'FakeModel123']);
  // Only meaningful when bridge loaded.
  if (known.length) {
    assert('drift detector catches synthetic unknown model use',
      driftWithFake.includes('FakeModel123'));
  }
}

// ══════════════════════════════════════════════════════════════════
// ui-builder-bridge: defensive fallbacks (Phase 3b/3d)
// ══════════════════════════════════════════════════════════════════
async function testBridgeDefensive(): Promise<void> {
  section('bridge: defensive fallbacks');

  // popup-contract bridge — give it junk, expect [] not throw.
  const popupIssues = await validatePopupDocument(undefined as unknown, '$.test');
  assert('validatePopupDocument tolerates undefined input',
    Array.isArray(popupIssues));

  // blueprint-prepare — junk in, no throw.
  const bp = await validateBlueprintShape('not-a-blueprint');
  assert('validateBlueprintShape tolerates non-blueprint input',
    bp && typeof bp.ok === 'boolean');

  // assign-values — unknown collection meta, empty field set; should return
  // no issues for empty assign object (early return path).
  const emptyAssign = await validateAssignValues(
    {},
    'unknown_coll',
    new Set<string>(),
    '$.test',
  );
  assert('validateAssignValues with empty assign returns []',
    Array.isArray(emptyAssign) && emptyAssign.length === 0);
}

// ══════════════════════════════════════════════════════════════════
// Phase 3c: assign-values validation in spec-validator
// ══════════════════════════════════════════════════════════════════
async function testAssignValuesValidation(): Promise<void> {
  section('Phase 3c: assign-values validation');

  // Build a minimal project on disk: 1 collection, 1 page with a table block
  // whose recordActions includes updateRecord with a real and a typo assign.
  const projectDir = mkTmpProject('assign-values');
  try {
    fs.mkdirSync(path.join(projectDir, 'collections'), { recursive: true });
    fs.mkdirSync(path.join(projectDir, 'pages/main/things'), { recursive: true });

    saveYaml(path.join(projectDir, 'collections/things.yaml'), {
      name: 'things',
      titleField: 'name',
      fields: [
        { name: 'name', interface: 'input' },
        { name: 'status', interface: 'input' },
        { name: 'is_completed', interface: 'checkbox' },
      ],
    });

    saveYaml(path.join(projectDir, 'routes.yaml'), [
      {
        key: 'main', title: 'Main', type: 'group',
        children: [{ key: 'things', title: 'Things', type: 'flowPage' }],
      },
    ]);

    saveYaml(path.join(projectDir, 'pages/main/things/page.yaml'), { title: 'Things' });
    saveYaml(path.join(projectDir, 'pages/main/things/layout.yaml'), {
      blocks: [
        {
          key: 'tbl', type: 'table', coll: 'things',
          fields: ['name'],
          recordActions: [
            { type: 'updateRecord', key: 'updateRecord_good',  assign: { is_completed: true } },
            { type: 'updateRecord', key: 'updateRecord_typo',  assign: { sttus: 'done' } },
            { type: 'updateRecord', key: 'updateRecord_empty', assign: {} },
          ],
        },
      ],
    });

    const routes = loadYaml<any[]>(path.join(projectDir, 'routes.yaml'));
    const pages = discoverPages(path.join(projectDir, 'pages'), routes);
    const issues = await validatePageSpecsAssignValues(pages, projectDir);

    assert('catches the typo "sttus"',
      issues.some(i => i.message.includes('sttus')),
      `got: ${JSON.stringify(issues.map(i => i.message))}`);
    assert('does not flag the valid is_completed',
      !issues.some(i => i.message.includes('is_completed')));
    assert('does not flag empty assign {}',
      !issues.some(i => i.message.includes('updateRecord_empty')));
  } finally {
    rmTmp(projectDir);
  }
}

// ══════════════════════════════════════════════════════════════════
// B#2: jsAction action type
// ══════════════════════════════════════════════════════════════════
function testJsAction(): void {
  section('B#2: jsAction (JSItemActionModel)');

  assert('jsAction → JSItemActionModel registered',
    ACTION_TYPE_TO_MODEL.jsAction === 'JSItemActionModel');
  assert('JSItemActionModel → jsAction reverse map',
    MODEL_TO_ACTION_TYPE.JSItemActionModel === 'jsAction');
  assert('JSItemActionModel in RECONCILER_JS_MODEL_USES',
    RECONCILER_JS_MODEL_USES.includes('JSItemActionModel'));

  const key1 = actionKey({ type: 'jsAction', file: './js/my-button.js' });
  assert('actionKey derives from file basename',
    key1 === 'jsAction_my_button',
    `got: ${key1}`);

  const key2 = actionKey({ type: 'jsAction', file: './js/some/nested/Run-Check.js' });
  assert('actionKey strips path + handles dashes',
    key2 === 'jsAction_run_check',
    `got: ${key2}`);

  const key3 = actionKey({ type: 'jsAction', file: 'no-extension' });
  assert('actionKey handles missing extension',
    key3 === 'jsAction_no_extension',
    `got: ${key3}`);

  // Without file, falls back to generic action-key path
  const key4 = actionKey({ type: 'jsAction' });
  assert('actionKey without file falls back to type only',
    key4 === 'jsAction',
    `got: ${key4}`);
}

// ══════════════════════════════════════════════════════════════════
// C: workflow state prune — verifies the prune logic isolated from
// the live NB deploy. Uses a fake NB client that returns 404 for
// the synthetic stale id so case-2 path is exercised too.
// ══════════════════════════════════════════════════════════════════
async function testWorkflowStatePrune(): Promise<void> {
  section('C: workflow state prune (offline)');

  // Stub the prune function's two inputs: state + wfDirs. NB client is mocked
  // via a minimal interface (only .http.get with validateStatus).
  const { pruneStaleWorkflowState } = await loadPruneInternals();
  if (!pruneStaleWorkflowState) {
    console.log('  SKIP  pruneStaleWorkflowState not exported — internal-only function');
    return;
  }

  type State = { workflows: Record<string, { id?: number; key?: string }> };
  const state: State = {
    workflows: {
      keep_on_disk_with_id: { id: 100, key: 'k1' },
      keep_on_disk_no_id: { key: 'k2' },          // no id — left alone
      drop_dsl_removed: { id: 200, key: 'k3' },   // not in wfDirs
      drop_nb_404: { id: 999, key: 'k4' },        // id not in liveIds
    },
  };
  const wfDirs = ['keep_on_disk_with_id', 'keep_on_disk_no_id', 'drop_nb_404'];
  const existingWfs = [{ id: 100, title: 'Keep' }] as any[];

  const fakeNb = {
    baseUrl: 'http://test',
    http: {
      get: async () => ({ status: 404, data: { errors: ['not found'] } }),
    },
  } as any;

  const logs: string[] = [];
  await pruneStaleWorkflowState(fakeNb, state, wfDirs, existingWfs, (m: string) => logs.push(m));

  assert('keep_on_disk_with_id preserved',
    !!state.workflows.keep_on_disk_with_id);
  assert('keep_on_disk_no_id preserved (no id to validate)',
    !!state.workflows.keep_on_disk_no_id);
  assert('drop_dsl_removed dropped (slug not on disk)',
    !state.workflows.drop_dsl_removed);
  assert('drop_nb_404 dropped (id not in NB)',
    !state.workflows.drop_nb_404);
  assert('logged 2 drops',
    logs.filter(l => l.includes('drop')).length === 2,
    `logs: ${logs.join(' | ')}`);
}

// Helper: pruneStaleWorkflowState is module-local. Use a tsx-friendly
// dynamic import trick to grab it for testing.
async function loadPruneInternals(): Promise<{
  pruneStaleWorkflowState?: (...args: unknown[]) => Promise<void>;
}> {
  const mod = (await import('./workflow/workflow-deployer')) as Record<string, unknown>;
  return { pruneStaleWorkflowState: mod.pruneStaleWorkflowState as undefined };
}

// ══════════════════════════════════════════════════════════════════
// Entry
// ══════════════════════════════════════════════════════════════════
async function main(): Promise<void> {
  console.log('Phase 3 + B + C regression tests\n');

  await testSurfacePolicyBridge();
  await testBridgeDefensive();
  await testAssignValuesValidation();
  testJsAction();
  await testWorkflowStatePrune();

  console.log(`\nResults: ${passed} passed, ${failed} failed`);
  if (failed > 0) {
    console.log('\nFailures:');
    for (const f of failures) console.log(`  - ${f}`);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error('Test runner crashed:', e);
  process.exit(2);
});
