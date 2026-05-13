/**
 * Tests for printing-template round-trip:
 *   - simplifier (block-exporter side)
 *   - action-key stability for templatePrint
 *   - stepParams builder (deployer side)
 *   - model alias registration
 *
 * Matches the project's `test-workflow.ts` style: self-contained, no runner.
 *   npx tsx src/printing-templates/test.ts
 */
import { simplifyTemplatePrintAction } from '../export/simplifiers';
import { actionKey } from '../utils/action-key';
import {
  MODEL_TO_ACTION_TYPE,
  NON_COMPOSE_ACTION_TYPE_TO_MODEL,
} from '../utils/block-types';

let passed = 0;
let failed = 0;
const failures: string[] = [];

function assert(label: string, condition: boolean, detail?: string): void {
  if (condition) { passed++; console.log(`  PASS  ${label}`); }
  else {
    failed++;
    const msg = detail ? `${label} — ${detail}` : label;
    failures.push(msg);
    console.log(`  FAIL  ${msg}`);
  }
}

function section(name: string): void { console.log(`\n=== ${name} ===`); }

section('model registry');
assert(
  'MODEL_TO_ACTION_TYPE maps TemplatePrintRecordActionModel → templatePrint',
  MODEL_TO_ACTION_TYPE.TemplatePrintRecordActionModel === 'templatePrint',
);
assert(
  'MODEL_TO_ACTION_TYPE maps TemplatePrintCollectionActionModel → templatePrint (alias)',
  MODEL_TO_ACTION_TYPE.TemplatePrintCollectionActionModel === 'templatePrint',
);
assert(
  'NON_COMPOSE_ACTION_TYPE_TO_MODEL[templatePrint] = record model (canonical)',
  NON_COMPOSE_ACTION_TYPE_TO_MODEL.templatePrint === 'TemplatePrintRecordActionModel',
);

section('simplifyTemplatePrintAction — toolbar (collection) shape');
{
  // Mirrors the real NB payload for TemplatePrintCollectionActionModel.
  const actionSpec = {
    type: 'templatePrint',
    key: 'templatePrint_899syvngmfe',
    stepParams: {
      templatePrintActionSetting: {
        configTemplate: { templateName: '899syvngmfe' },
      },
    },
  };
  const s = simplifyTemplatePrintAction(actionSpec);
  assert('preserves type', s.type === 'templatePrint');
  assert('preserves key', s.key === 'templatePrint_899syvngmfe');
  assert('lifts templateName', s.templateName === '899syvngmfe');
  assert('drops stepParams', s.stepParams === undefined);
  assert('no spurious convertedToPDF', s.convertedToPDF === undefined);
}

section('simplifyTemplatePrintAction — record with buttonSettings');
{
  // Mirrors the real NB payload for TemplatePrintRecordActionModel on the
  // repair detail popup.
  const actionSpec = {
    type: 'templatePrint',
    key: 'templatePrint_v6dvqmd82ht',
    stepParams: {
      templatePrintActionSetting: {
        configTemplate: { templateName: 'v6dvqmd82ht' },
      },
      buttonSettings: {
        general: { type: 'default', title: '打印维修单', icon: 'PrinterOutlined' },
      },
    },
  };
  const s = simplifyTemplatePrintAction(actionSpec);
  assert('lifts title',   s.title === '打印维修单');
  assert('lifts icon',    s.icon === 'PrinterOutlined');
  assert('omits default style', s.style === undefined);
  assert('templateName kept', s.templateName === 'v6dvqmd82ht');
}

section('simplifyTemplatePrintAction — with convertedToPDF (nested)');
{
  const actionSpec = {
    type: 'templatePrint',
    stepParams: {
      templatePrintActionSetting: {
        configTemplate: { templateName: 'repair_order' },
        convertedToPDF: { convertedToPDF: true },
      },
    },
  };
  const s = simplifyTemplatePrintAction(actionSpec);
  assert('convertedToPDF hoisted', s.convertedToPDF === true);
}

section('simplifyTemplatePrintAction — convertedToPDF (flat bool)');
{
  const actionSpec = {
    type: 'templatePrint',
    stepParams: {
      templatePrintActionSetting: {
        configTemplate: { templateName: 'repair_order' },
        convertedToPDF: true,
      },
    },
  };
  const s = simplifyTemplatePrintAction(actionSpec);
  assert('convertedToPDF hoisted (flat)', s.convertedToPDF === true);
}

section('simplifyTemplatePrintAction — non-default style');
{
  const actionSpec = {
    type: 'templatePrint',
    stepParams: {
      templatePrintActionSetting: { configTemplate: { templateName: 't' } },
      buttonSettings: { general: { type: 'primary', title: 'x' } },
    },
  };
  const s = simplifyTemplatePrintAction(actionSpec);
  assert('style=primary preserved', s.style === 'primary');
}

section('simplifyTemplatePrintAction — empty stepParams');
{
  const s = simplifyTemplatePrintAction({ type: 'templatePrint' });
  assert('survives missing stepParams', s.type === 'templatePrint' && s.templateName === undefined);
}

section('actionKey — templatePrint');
{
  // Shorthand form: key derives from templateName
  assert(
    'shorthand templateName → templatePrint_<slug>',
    actionKey({ type: 'templatePrint', templateName: 'repair_order' }) === 'templatePrint_repair_order',
  );
  // Raw form (still carrying stepParams): same key
  assert(
    'raw stepParams templateName → same key',
    actionKey({
      type: 'templatePrint',
      stepParams: {
        templatePrintActionSetting: { configTemplate: { templateName: 'repair_order' } },
      },
    }) === 'templatePrint_repair_order',
  );
  // Different templates → different keys (so two print buttons on one block don't collide)
  const k1 = actionKey({ type: 'templatePrint', templateName: 'a' });
  const k2 = actionKey({ type: 'templatePrint', templateName: 'b' });
  assert('distinct templates → distinct keys', k1 !== k2);
  // No templateName fallback
  assert(
    'no templateName → bare type',
    actionKey({ type: 'templatePrint' }) === 'templatePrint',
  );
}

// ── Deploy-side stepParams builder (private, reimported via isolated import trick)
// We exercise the same logic by reading from the compiled module. Keep the
// builder exported from action-filler for test only? It's private today — the
// safer test is the round-trip: given simplified shorthand → build → simplify
// back should return the shorthand. That's an integration concern, not a unit
// here. Left for the e2e pull/push check.
section('Done');
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log('\nFailures:');
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
