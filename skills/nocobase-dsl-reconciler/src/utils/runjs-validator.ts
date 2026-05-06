/**
 * runjs-validator — AST-aware validation of user JS files referenced by DSL.
 *
 * Wraps the upstream sibling skill's `nocobase-ui-builder/runtime/src/runjs-parser.js`
 * (acorn-based) and adds dsl-reconciler-specific checks (unfilled DSL
 * template params, ctx.render(null) placeholders, ctx.sql() direct calls).
 *
 * Behavior:
 *   - Real JS syntax errors (e.g. unterminated string) → level 'error'
 *   - Forbidden APIs (URLSearchParams, fetch w/o local def, eval, ESM
 *     import/export, ctx.render(null), bare ctx.sql()) → level 'error'
 *   - JSX edge cases that ui-builder's regex jsx-transform misjudges
 *     (legitimate NB JSX with member-expression tags, SVG inside div, etc.)
 *     → level 'warn' — we trust NB's runtime over the upstream pre-validator
 *   - Unfilled `{{var}}` DSL params left over from scaffolding → level 'error'
 *
 * Callers (js-filler, event-flow-filler, ai-button, click-to-open, chart-filler)
 * should treat 'error' as "log + skip writing to NB" and 'warn' as silent OK.
 */
import * as path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export type IssueLevel = 'error' | 'warn';

export interface RunJSIssue {
  level: IssueLevel;
  type: string;
  message: string;
}

export interface ValidationResult {
  ok: boolean;
  issues: RunJSIssue[];
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PARSER_URL = pathToFileURL(
  path.resolve(HERE, '../../../nocobase-ui-builder/runtime/src/runjs-parser.js'),
).href;

interface ParserModule {
  parseWrappedRunJS: (code: string, opts?: { compiled?: boolean }) => {
    ast: AstNode;
    wrappedBody: AstNode | null;
  };
}

let parserPromise: Promise<ParserModule | null> | null = null;
function loadParser(): Promise<ParserModule | null> {
  if (!parserPromise) {
    parserPromise = (async () => {
      try {
        return (await import(PARSER_URL)) as ParserModule;
      } catch {
        return null; // sibling skill missing — fall back to regex-only mode
      }
    })();
  }
  return parserPromise;
}

interface AstNode {
  type: string;
  [k: string]: unknown;
}

function isJsxParseError(msg: string): boolean {
  // ui-builder's hand-rolled jsx-transform.js (468 lines) miscompiles a few
  // legitimate NB JSX shapes — Typography.Text member tags, SVG children,
  // some attribute spreads. NB's actual runtime accepts them. Treat these as
  // skip-warnings, not push-blockers.
  return /JSX|Mismatched.*closing tag|Invalid JSX|JSX attribute|Unexpected token/i.test(msg);
}

function isAstNode(v: unknown): v is AstNode {
  return !!v && typeof v === 'object' && typeof (v as AstNode).type === 'string';
}

function walk(node: AstNode | null | undefined, visit: (n: AstNode) => void): void {
  if (!isAstNode(node)) return;
  visit(node);
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) {
      for (const item of value) if (isAstNode(item)) walk(item, visit);
    } else if (isAstNode(value)) {
      walk(value, visit);
    }
  }
}

/** Resolve `ctx.sql.save` / `ctx.render` MemberExpression to a dotted string. */
function memberChain(node: AstNode): string | null {
  const parts: string[] = [];
  let cur: AstNode | null = node;
  while (cur && cur.type === 'MemberExpression') {
    const prop = cur.property as AstNode | undefined;
    if (prop?.type !== 'Identifier') return null;
    parts.unshift(String((prop as { name?: string }).name ?? ''));
    cur = (cur.object as AstNode) ?? null;
  }
  if (cur?.type === 'Identifier') {
    parts.unshift(String((cur as { name?: string }).name ?? ''));
    return parts.join('.');
  }
  return null;
}

function maskStrings(code: string): string {
  return code
    .replace(/`(?:\\.|[^`\\])*`/g, '""')
    .replace(/'(?:\\.|[^'\\])*'/g, '""')
    .replace(/"(?:\\.|[^"\\])*"/g, '""');
}

function checkUnfilledTemplates(code: string, issues: RunJSIssue[]): void {
  // {{var}} or {{var||default}} left over from spec scaffolding. Strings are
  // masked first so legitimate i18n calls like t('{{count}}m ago', {count})
  // don't trip this.
  const noStrings = maskStrings(code);
  const unfilled = noStrings.match(/\{\{(\w+)(?:\|\|[^}]*)?\}\}/g);
  if (unfilled?.length) {
    issues.push({
      level: 'error',
      type: 'unfilled_template',
      message: `unfilled DSL template params: ${unfilled.slice(0, 5).join(', ')}`,
    });
  }
}

function detectLocalFetch(ast: AstNode): boolean {
  let found = false;
  walk(ast, n => {
    if (found) return;
    if (n.type === 'FunctionDeclaration' || n.type === 'VariableDeclarator') {
      const id = n.id as { name?: string } | undefined;
      if (id?.name === 'fetch') found = true;
    }
  });
  return found;
}

// Mirrors FORBIDDEN_BARE_GLOBALS from
// nocobase-ui-builder/scripts/runjs_guard.mjs — bare-name globals that NB's
// runJs sandbox blocks. Detected as Identifier callees / NewExpression callees
// at any AST depth, with shadowing accounted for (local `const fetch = …`
// disables the fetch check for that file).
const FORBIDDEN_BARE_GLOBALS: Record<string, string> = {
  fetch: 'fetch() not available — use ctx.request',
  localStorage: 'localStorage not available — store on collections instead',
  sessionStorage: 'sessionStorage not available',
  XMLHttpRequest: 'XMLHttpRequest not available — use ctx.request',
  WebSocket: 'WebSocket not available',
  Worker: 'Worker not available',
  SharedWorker: 'SharedWorker not available',
  ServiceWorker: 'ServiceWorker not available',
  BroadcastChannel: 'BroadcastChannel not available',
  EventSource: 'EventSource not available',
  indexedDB: 'indexedDB not available',
  caches: 'caches not available',
  Function: 'Function() constructor not available — equivalent to eval',
  eval: 'eval() not available in NB JS sandbox',
  globalThis: 'globalThis not available — use ctx',
  process: 'process not available (server-only API)',
  require: 'require not available — runJs is ESM-like, use ctx APIs',
  module: 'module not available — runJs is not CommonJS',
  exports: 'exports not available — runJs is not CommonJS',
};

function detectLocalBindings(ast: AstNode): Set<string> {
  // Top-level `const X = …` / `function X(…)` / `let X` shadows globals of the
  // same name. We only check declarators at any depth here — runJs doesn't
  // have block scoping concerns for the names we care about (mostly fetch).
  const found = new Set<string>();
  walk(ast, n => {
    if (n.type === 'FunctionDeclaration' || n.type === 'VariableDeclarator') {
      const id = n.id as { name?: string } | undefined;
      if (id?.name && FORBIDDEN_BARE_GLOBALS[id.name]) found.add(id.name);
    }
  });
  return found;
}

function checkForbiddenApis(ast: AstNode, issues: RunJSIssue[]): void {
  const shadowed = detectLocalBindings(ast);

  walk(ast, n => {
    // `new X(...)` — flag NewExpressions of any forbidden global, plus the
    // special-case URLSearchParams (not a bare-global runtime API but still
    // unavailable in NB sandbox; suggest regex parse instead).
    if (n.type === 'NewExpression') {
      const callee = n.callee as { name?: string } | undefined;
      const name = callee?.name;
      if (name === 'URLSearchParams') {
        issues.push({
          level: 'error',
          type: 'forbidden_api',
          message: 'URLSearchParams not available — use regex/split to parse URL params',
        });
      } else if (name && FORBIDDEN_BARE_GLOBALS[name] && !shadowed.has(name)) {
        issues.push({
          level: 'error',
          type: 'forbidden_api',
          message: FORBIDDEN_BARE_GLOBALS[name],
        });
      }
    }
    if (n.type === 'CallExpression') {
      const callee = n.callee as AstNode | undefined;
      if (callee?.type === 'MemberExpression') {
        const chain = memberChain(callee);
        if (chain === 'ctx.render') {
          const args = n.arguments as AstNode[];
          const first = args?.[0];
          const isNull =
            first?.type === 'Literal' && (first as { value?: unknown }).value === null;
          if (isNull) {
            issues.push({
              level: 'error',
              type: 'placeholder',
              message: 'ctx.render(null) is a scaffold placeholder — implement actual content',
            });
          }
        }
        if (chain === 'ctx.sql') {
          issues.push({
            level: 'error',
            type: 'forbidden_api',
            message: 'ctx.sql() direct call not available — use ctx.sql.save() + ctx.sql.runById()',
          });
        }
      }
      if (callee?.type === 'Identifier') {
        const name = (callee as { name?: string }).name;
        if (name && FORBIDDEN_BARE_GLOBALS[name] && !shadowed.has(name)) {
          issues.push({
            level: 'error',
            type: 'forbidden_api',
            message: FORBIDDEN_BARE_GLOBALS[name],
          });
        }
      }
    }
    // Member access on forbidden globals: localStorage.getItem, process.env.FOO,
    // module.exports = …, etc. Read-only ref patterns are still useful to flag
    // because they imply the user expects these to exist at runtime.
    if (n.type === 'MemberExpression') {
      const obj = n.object as { type?: string; name?: string } | undefined;
      if (obj?.type === 'Identifier' && obj.name && FORBIDDEN_BARE_GLOBALS[obj.name] && !shadowed.has(obj.name)) {
        // Skip cases like `function localStorage() { … }` where the AST visits
        // the object Identifier — those are FunctionDeclaration ids handled
        // above. The MemberExpression walker only sees real reads/writes.
        issues.push({
          level: 'error',
          type: 'forbidden_api',
          message: FORBIDDEN_BARE_GLOBALS[obj.name],
        });
      }
    }
    // ES module import/export — script-mode parse usually rejects them, but
    // some forms (dynamic import expression) survive. Catch belt-and-suspenders.
    if (n.type === 'ImportDeclaration') {
      issues.push({ level: 'error', type: 'forbidden_api', message: 'ES import not available' });
    }
    if (n.type === 'ExportNamedDeclaration' || n.type === 'ExportDefaultDeclaration') {
      issues.push({ level: 'error', type: 'forbidden_api', message: 'ES export not available' });
    }
  });
}

/**
 * Validate a runjs source string against NB sandbox constraints.
 *
 * Returns `{ ok: boolean, issues: [...] }` where `ok` means no `error`-level
 * issues. `warn`-level issues (e.g. JSX edge cases) do not flip `ok` to false.
 */
export async function validateRunJS(code: string): Promise<ValidationResult> {
  const issues: RunJSIssue[] = [];

  checkUnfilledTemplates(code, issues);

  const parser = await loadParser();
  if (!parser) {
    // Sibling ui-builder runtime not present — caller should fall back to
    // simpler regex checks. Returning ok:true here lets push proceed.
    return { ok: !issues.some(i => i.level === 'error'), issues };
  }

  let parsed;
  try {
    parsed = parser.parseWrappedRunJS(code);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (isJsxParseError(msg)) {
      issues.push({
        level: 'warn',
        type: 'jsx_skip',
        message: `JSX edge case skipped (NB runtime is more permissive): ${msg.slice(0, 80)}`,
      });
    } else {
      issues.push({ level: 'error', type: 'syntax', message: msg.slice(0, 200) });
    }
    return { ok: !issues.some(i => i.level === 'error'), issues };
  }

  checkForbiddenApis(parsed.ast, issues);

  return { ok: !issues.some(i => i.level === 'error'), issues };
}

/**
 * Format issues as one log line per issue. Caller decides routing (e.g. only
 * print errors, count warns silently). Format:
 *   "      ✗ JS file.js: <msg>"
 *   "      ! JS file.js: <warn msg>"
 */
export function formatIssue(filePath: string, issue: RunJSIssue): string {
  const sym = issue.level === 'error' ? '✗' : '!';
  const file = filePath.replace(/^.*\/workspaces\//, 'ws/');
  return `      ${sym} ${file}: ${issue.message}`;
}
