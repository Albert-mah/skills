/**
 * Workflow module tests — validator rules, normalize (apply/strip defaults),
 * graph parsing + topological sort. Matches the project's existing test-phase1.ts
 * pattern: self-contained, no test runner, run with `tsx`.
 *
 *   npx tsx src/workflow/test-workflow.ts
 *
 * Exits non-zero on any failure.
 */
import { validateWorkflow } from './validator';
import { applySpecDefaults, stripSpecDefaults, applyNodeDefaults } from './normalize';
import type { WorkflowSpec } from './types';

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

function makeSpec(overrides: Partial<WorkflowSpec> = {}): WorkflowSpec {
  return {
    title: 'Test',
    type: 'collection',
    trigger: { collection: 'nb_x', mode: 1 },
    graph: ['a'],
    nodes: { a: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [] } } } } },
    ...overrides,
  };
}

// ══════════════════════════════════════════════════════════════════
// Validator — type lists
// ══════════════════════════════════════════════════════════════════
section('validator: node + trigger type lists');
{
  const spec = makeSpec({
    nodes: { a: { type: 'multi-condition', config: { branches: [] } }, b: { type: 'end', config: {} } },
    graph: ['a', 'a --> b'],
  });
  const r = validateWorkflow(spec);
  assert('multi-condition is a known node type',
    !r.errors.some(e => /unknown type "multi-condition"/.test(e.message)));
  assert('end is a known node type',
    !r.errors.some(e => /unknown type "end"/.test(e.message)));
}
{
  const spec = makeSpec({ type: 'webhook', trigger: { path: '/hook' }, nodes: { a: { type: 'response', config: {} } }, graph: ['a'] });
  const r = validateWorkflow(spec);
  assert('webhook is a known trigger type',
    !r.errors.some(e => /unknown trigger type "webhook"/.test(e.message)));
}
{
  const spec = makeSpec({ type: 'form-event' });
  const r = validateWorkflow(spec);
  assert('form-event trigger now rejected as unknown',
    r.errors.some(e => /unknown trigger type "form-event"/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — filter root lint
// ══════════════════════════════════════════════════════════════════
section('validator: filter/condition root lint');
{
  const spec = makeSpec({
    trigger: { collection: 'nb_x', mode: 1, condition: { owner_id: { $empty: true } } },
  });
  const r = validateWorkflow(spec);
  assert('flat trigger.condition root errors',
    r.errors.some(e => e.level === 'error' && /must be wrapped in \$and or \$or/.test(e.message)));
}
{
  const spec = makeSpec({
    trigger: { collection: 'nb_x', mode: 1, condition: { $and: [{ owner_id: { $empty: true } }] } },
  });
  const r = validateWorkflow(spec);
  assert('$and-rooted trigger.condition passes',
    !r.errors.some(e => e.level === 'error' && /must be wrapped in \$and/.test(e.message)));
}
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', params: { filter: { name: { $eq: 'x' } } } } },
    },
  });
  const r = validateWorkflow(spec);
  assert('flat node filter root errors',
    r.errors.some(e => e.level === 'error' && /must be wrapped in \$and or \$or/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — condition calculator names
// ══════════════════════════════════════════════════════════════════
section('validator: condition calculator names');
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'condition', config: { calculation: { group: { type: 'and', calculations: [
        { calculator: 'greaterThan', operands: [1, 0] },
      ] } } } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('unknown calculator "greaterThan" errors',
    r.errors.some(e => e.level === 'error' && /unknown calculator "greaterThan"/.test(e.message)));
}
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'condition', config: { calculation: { group: { type: 'and', calculations: [
        { calculator: 'gt', operands: [1, 0] },
      ] } } } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('known calculator "gt" passes',
    !r.errors.some(e => /unknown calculator/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — request node headers/params must be arrays
// ══════════════════════════════════════════════════════════════════
section('validator: request node array guards');
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'request', config: { method: 'GET', url: 'https://x', params: { foo: 'bar' } } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('request.params as object errors',
    r.errors.some(e => e.level === 'error' && /params must be an array/.test(e.message)));
}
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'request', config: {
        method: 'POST', url: 'https://x',
        params: [{ name: 'q', value: 'x' }],
        headers: [{ name: 'Authorization', value: 'Bearer ...' }],
      } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('request.params/headers as arrays pass',
    !r.errors.some(e => e.level === 'error' && /must be an array/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — approval-node / approval-trigger coherence
// ══════════════════════════════════════════════════════════════════
section('validator: approval node requires approval trigger');
{
  const spec = makeSpec({
    type: 'collection',
    trigger: { collection: 'nb_x', mode: 1 },
    nodes: {
      a: { type: 'approval', config: { branchMode: true, assignees: [1] } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('approval node in collection-triggered workflow errors',
    r.errors.some(e => e.level === 'error' && /requires the workflow.*type.*approval/.test(e.message)));
}
{
  const spec = makeSpec({
    type: 'approval',
    trigger: { collection: 'nb_x', approvalUid: 'abc', taskCardUid: 'def' },
    nodes: {
      a: { type: 'approval', config: { branchMode: true, assignees: [1] } },
    },
    graph: ['a'],
  });
  const r = validateWorkflow(spec);
  assert('approval node in approval-triggered workflow passes',
    !r.errors.some(e => e.level === 'error' && /approval node/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — graph (merge points + orphans)
// ══════════════════════════════════════════════════════════════════
section('validator: graph structure');
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [] } } } },
      b: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [] } } } },
      c: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [] } } } },
    },
    graph: ['a', 'a --> c', 'b --> c'],
  });
  const r = validateWorkflow(spec);
  assert('merge-point (two sources → one target) errors',
    r.errors.some(e => e.level === 'error' && /has 2 incoming edges/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — variable namespaces
// ══════════════════════════════════════════════════════════════════
section('validator: variable namespaces');
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [{ id: { $eq: '{{$system.now}}' } }] } } } },
    },
  });
  const r = validateWorkflow(spec);
  assert('$system is recognized (no warn)',
    !r.errors.some(e => /\$system/.test(e.message)));
}
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', params: { filter: { $and: [{ id: { $eq: '{{$bogus.x}}' } }] } } } },
    },
  });
  const r = validateWorkflow(spec);
  assert('unknown namespace warns',
    r.errors.some(e => e.level === 'warn' && /unknown namespace "\$bogus"/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// Validator — duplicate titles
// ══════════════════════════════════════════════════════════════════
section('validator: duplicate node titles');
{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', title: 'Same', config: { collection: 'nb_x', params: { filter: { $and: [] } } } },
      b: { type: 'query', title: 'Same', config: { collection: 'nb_x', params: { filter: { $and: [] } } } },
    },
    graph: ['a', 'a --> b'],
  });
  const r = validateWorkflow(spec);
  assert('duplicate node title warns (deployer disambiguates)',
    r.errors.some(e => e.level === 'warn' && /duplicate node title/.test(e.message)));
}

// ══════════════════════════════════════════════════════════════════
// normalize: applySpecDefaults
// ══════════════════════════════════════════════════════════════════
section('normalize: apply defaults');
{
  const spec = makeSpec();
  const out = applySpecDefaults(spec);
  assert('workflow options.stackLimit defaulted to 1',
    (out.options as Record<string, unknown>)?.stackLimit === 1);
  assert('workflow options.deleteExecutionOnStatus defaulted to []',
    Array.isArray((out.options as Record<string, unknown>)?.deleteExecutionOnStatus));
  assert('trigger.appends defaulted to []',
    Array.isArray((out.trigger as Record<string, unknown>)?.appends));
  const query = out.nodes.a.config as Record<string, unknown>;
  assert('query.dataSource defaulted to main', query.dataSource === 'main');
  assert('query.multiple defaulted to false', query.multiple === false);
  const params = query.params as Record<string, unknown>;
  assert('query params.sort defaulted to []', Array.isArray(params.sort));
  assert('query params.page defaulted to 1', params.page === 1);
  assert('query params.pageSize defaulted to 20', params.pageSize === 20);
}

{
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', multiple: true, params: { filter: { $and: [] }, pageSize: 100 } } },
    },
  });
  const out = applySpecDefaults(spec);
  const q = out.nodes.a.config as Record<string, unknown>;
  const params = q.params as Record<string, unknown>;
  assert('user-provided multiple=true wins over default', q.multiple === true);
  assert('user-provided pageSize=100 wins', params.pageSize === 100);
  assert('unset params.sort still defaults', Array.isArray(params.sort));
}

{
  const cond = applyNodeDefaults({ type: 'condition', config: { calculation: { group: {} } } });
  assert('condition.engine defaulted to basic',
    (cond.config as Record<string, unknown>).engine === 'basic');
  assert('condition.rejectOnFalse defaulted to false',
    (cond.config as Record<string, unknown>).rejectOnFalse === false);
}

{
  // $ref configs should not be normalized inline (defaults apply post-resolve)
  const ref = applyNodeDefaults({ type: 'query', config: { $ref: 'components/q.yaml' } as unknown as Record<string, unknown> });
  assert('$ref config skipped during apply',
    (ref.config as Record<string, unknown>).$ref === 'components/q.yaml'
      && (ref.config as Record<string, unknown>).dataSource === undefined);
}

// ══════════════════════════════════════════════════════════════════
// normalize: create-node assignFormSchema auto-synth + non-value nodes
// ══════════════════════════════════════════════════════════════════
section('normalize: create also gets schema; query/destroy do NOT');
{
  const node = applyNodeDefaults({
    type: 'create',
    config: {
      collection: 'nb_demo',
      params: { values: { name: 'x', amount: 10 } },
    },
  });
  const c = node.config as Record<string, unknown>;
  assert('create node: usingAssignFormSchema=true',
    c.usingAssignFormSchema === true);
  assert('create node: has assignFormSchema', !!c.assignFormSchema);
}
for (const t of ['query', 'destroy']) {
  const node = applyNodeDefaults({
    type: t,
    config: { collection: 'nb_demo', params: { filter: { $and: [] } } },
  });
  const c = node.config as Record<string, unknown>;
  assert(`${t}: no synthesized schema (UI doesn't need it)`,
    !('assignFormSchema' in c) && !('usingAssignFormSchema' in c));
}

// ══════════════════════════════════════════════════════════════════
// normalize: update-node assignFormSchema synthesis
// ══════════════════════════════════════════════════════════════════
section('normalize: update assignFormSchema auto-synth');
{
  const node = applyNodeDefaults({
    type: 'update',
    config: {
      collection: 'nb_x',
      params: {
        filter: { $and: [{ id: { $eq: 'x' } }] },
        values: { status: 'done', comment: 'ok' },
      },
    },
  });
  const c = node.config as Record<string, unknown>;
  assert('usingAssignFormSchema set to true',
    c.usingAssignFormSchema === true);
  const schema = c.assignFormSchema as Record<string, unknown>;
  assert('assignFormSchema x-component is Grid',
    schema['x-component'] === 'Grid');
  assert('schema has a single Grid.Row',
    Object.values(schema.properties as Record<string, unknown>).some((r: any) => r['x-component'] === 'Grid.Row'));
  // Walk to find AssignedField nodes
  const fields: string[] = [];
  const walk = (obj: unknown): void => {
    if (!obj || typeof obj !== 'object') return;
    const rec = obj as Record<string, unknown>;
    if (rec['x-component'] === 'AssignedField' && typeof rec.name === 'string') fields.push(rec.name);
    for (const v of Object.values(rec)) walk(v);
  };
  walk(schema);
  assert('AssignedField per values key',
    fields.length === 2 && fields.includes('status') && fields.includes('comment'));
  assert('x-collection-field prefix uses collection',
    JSON.stringify(schema).includes('nb_x.status'));
}
{
  // User-provided assignFormSchema must survive unchanged
  const userSchema = { marker: 'user', 'x-component': 'CustomForm' };
  const node = applyNodeDefaults({
    type: 'update',
    config: {
      collection: 'nb_x',
      params: { values: { status: 'done' } },
      assignFormSchema: userSchema,
      usingAssignFormSchema: true,
    },
  });
  assert('user-provided assignFormSchema wins',
    (node.config as Record<string, unknown>).assignFormSchema === userSchema);
}
{
  // Round-trip: apply + strip should drop the synthesized schema
  const original: any = {
    type: 'update',
    config: {
      collection: 'nb_x',
      params: { filter: { $and: [] }, values: { status: 'done' } },
    },
  };
  const applied = applyNodeDefaults(original);
  const stripped = stripSpecDefaults(makeSpec({ nodes: { a: applied }, graph: ['a'] })).nodes.a;
  assert('strip removes synthesized assignFormSchema',
    !('assignFormSchema' in (stripped.config as Record<string, unknown>)));
  assert('strip removes usingAssignFormSchema',
    !('usingAssignFormSchema' in (stripped.config as Record<string, unknown>)));
  assert('params.values preserved post-strip',
    ((stripped.config as any).params.values.status) === 'done');
}

// ══════════════════════════════════════════════════════════════════
// normalize: stripSpecDefaults (export direction)
// ══════════════════════════════════════════════════════════════════
section('normalize: strip defaults');
{
  const spec = makeSpec();
  const withDefaults = applySpecDefaults(spec);
  const stripped = stripSpecDefaults(withDefaults);
  assert('strip drops workflow-level options entirely when all defaults',
    stripped.options === undefined);
  const q = stripped.nodes.a.config as Record<string, unknown>;
  assert('strip drops query.dataSource=main',
    q.dataSource === undefined);
  assert('strip drops query.multiple=false',
    q.multiple === undefined);
  assert('strip keeps params.filter (user intent)',
    !!(q.params as Record<string, unknown>)?.filter);
}

{
  // User-overridden values must survive strip
  const spec = makeSpec({
    nodes: {
      a: { type: 'query', config: { collection: 'nb_x', multiple: true, params: { filter: { $and: [] }, pageSize: 100 } } },
    },
  });
  const roundtrip = stripSpecDefaults(applySpecDefaults(spec));
  const q = roundtrip.nodes.a.config as Record<string, unknown>;
  const params = q.params as Record<string, unknown>;
  assert('strip preserves user override multiple=true',
    q.multiple === true);
  assert('strip preserves user override pageSize=100',
    params.pageSize === 100);
}

// ══════════════════════════════════════════════════════════════════
// Summary
// ══════════════════════════════════════════════════════════════════
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.log('\nfailures:');
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
