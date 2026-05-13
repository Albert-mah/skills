/**
 * ui-builder-bridge — lazy, defensive import of sibling skill's runtime contracts.
 *
 * Wraps `nocobase-ui-builder/runtime/src/{surface-policy,popup-contract}.js`
 * so reconciler can consult kernel-aware data (canonical JS model uses, popup
 * document shape rules) without hard-failing when the sibling skill is
 * missing or its API drifts.
 *
 * Failure modes that must NOT crash the reconciler:
 *   - sibling skill not installed at the relative path
 *   - export shape changed (renamed / removed function)
 *   - export returned an unexpected type
 *
 * On any of those, the corresponding bridge function returns a safe empty
 * value (empty array, null, false) and callers proceed with their fallback.
 *
 * Cache: each module loads once per process. Failed loads are remembered as
 * null so we don't re-attempt on every call.
 */
import * as path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

function resolveUbModule(rel: string): string {
  return pathToFileURL(path.resolve(HERE, '../../../nocobase-ui-builder/runtime/src/', rel)).href;
}

// ─── surface-policy.js ───

interface SurfacePolicyModule {
  RUNJS_MODEL_USES?: readonly string[];
  RUNJS_RENDER_MODEL_USES?: readonly string[];
  RUNJS_ACTION_MODEL_USES?: readonly string[];
  RUNJS_SURFACE_IDS?: readonly string[];
  getRunJSSurfaceAllowedModelUses?: (surface: string) => readonly string[];
  getRunJSSurfaceExtraAllowedRoots?: (surface: string) => readonly string[];
  getRunJSEffectStyle?: (surface: string) => string | null;
}

let surfacePolicyPromise: Promise<SurfacePolicyModule | null> | null = null;
function loadSurfacePolicy(): Promise<SurfacePolicyModule | null> {
  if (!surfacePolicyPromise) {
    surfacePolicyPromise = (async () => {
      try {
        const mod = (await import(resolveUbModule('surface-policy.js'))) as SurfacePolicyModule;
        // Sanity check — bridge is only useful if at least the data exports exist
        if (!Array.isArray(mod.RUNJS_MODEL_USES)) return null;
        return mod;
      } catch {
        return null;
      }
    })();
  }
  return surfacePolicyPromise;
}

/** All JS model uses the kernel currently recognizes. Empty array on load failure. */
export async function getKnownRunjsModelUses(): Promise<readonly string[]> {
  const mod = await loadSurfacePolicy();
  return mod?.RUNJS_MODEL_USES ?? [];
}

/** Allowed extra ctx.X roots for a surface (event-flow has a full list; others empty). */
export async function getSurfaceAllowedRoots(surface: string): Promise<readonly string[]> {
  const mod = await loadSurfacePolicy();
  const fn = mod?.getRunJSSurfaceExtraAllowedRoots;
  if (typeof fn !== 'function') return [];
  try {
    const roots = fn(surface);
    return Array.isArray(roots) ? roots : [];
  } catch {
    return [];
  }
}

/** Allowed JS model uses for a surface (e.g. js-model.render allows render models only). */
export async function getSurfaceAllowedModelUses(surface: string): Promise<readonly string[]> {
  const mod = await loadSurfacePolicy();
  const fn = mod?.getRunJSSurfaceAllowedModelUses;
  if (typeof fn !== 'function') return [];
  try {
    const uses = fn(surface);
    return Array.isArray(uses) ? uses : [];
  } catch {
    return [];
  }
}

// ─── Shared shape for kernel-aware findings ───

/**
 * Issue shape both `popup-contract` and `assign-values-validation` return.
 * `code` is an optional discriminator some ui-builder modules add.
 */
export interface BridgeRuleIssue {
  path: string;
  ruleId: string;
  message: string;
  code?: string;
}

// Backwards-compat alias — callers from the popup-contract era used this name.
export type PopupContractIssue = BridgeRuleIssue;

// ─── popup-contract.js ───

interface PopupContractModule {
  collectPopupDocumentContractIssues?: (
    popup: unknown,
    path: string,
    opts?: { normalizeText?: (v: unknown) => string },
  ) => BridgeRuleIssue[];
  hasTemplateDocument?: (template: unknown) => boolean;
}

let popupContractPromise: Promise<PopupContractModule | null> | null = null;
function loadPopupContract(): Promise<PopupContractModule | null> {
  if (!popupContractPromise) {
    popupContractPromise = (async () => {
      try {
        const mod = (await import(resolveUbModule('popup-contract.js'))) as PopupContractModule;
        if (typeof mod.collectPopupDocumentContractIssues !== 'function') return null;
        return mod;
      } catch {
        return null;
      }
    })();
  }
  return popupContractPromise;
}

/**
 * Run kernel-aware contract check on a popup runtime payload (`{ template,
 * blocks, layout, tryTemplate, saveAsTemplate, ... }`).
 *
 * Currently UNCALLED. Reserved for future use — reconciler today stores
 * popup template binding via flat `popupSettings.openView.popupTemplateUid`
 * rather than the nested `{ template: { uid } }` shape this contract
 * validates. When/if the kernel unifies popup runtime payloads, wire this
 * into template-deployer.ts before save.
 *
 * Returns empty array when the sibling module can't load or the export is
 * missing. Caller should treat unknown-failure as "no issues".
 */
export async function validatePopupDocument(
  popup: unknown,
  path: string,
): Promise<BridgeRuleIssue[]> {
  const mod = await loadPopupContract();
  const fn = mod?.collectPopupDocumentContractIssues;
  if (typeof fn !== 'function') return [];
  try {
    const issues = fn(popup, path);
    return Array.isArray(issues) ? issues : [];
  } catch {
    return [];
  }
}

// ─── assign-values-validation.js ───

interface AssignValuesCollectionMeta {
  fieldsByName: Set<string>;
}

interface AssignValuesValidationModule {
  collectAssignValuesValidationIssues?: (opts: {
    assignValues: unknown;
    path: string;
    collectionName: string;
    collectionMeta: AssignValuesCollectionMeta | null;
    normalizeName?: (v: unknown) => string;
    valueLabel?: string;
    metadataValueLabel?: string;
    includeDetails?: boolean;
  }) => BridgeRuleIssue[];
}

let assignValuesPromise: Promise<AssignValuesValidationModule | null> | null = null;
function loadAssignValuesValidation(): Promise<AssignValuesValidationModule | null> {
  if (!assignValuesPromise) {
    assignValuesPromise = (async () => {
      try {
        const mod = (await import(
          resolveUbModule('assign-values-validation.js')
        )) as AssignValuesValidationModule;
        if (typeof mod.collectAssignValuesValidationIssues !== 'function') return null;
        return mod;
      } catch {
        return null;
      }
    })();
  }
  return assignValuesPromise;
}

/**
 * Validate a DSL `assign: {...}` object against the target collection's known
 * field names. Catches typos like `assign: { sttus: 'done' }` (should be
 * `status`) before deploy emits an unusable updateRecord action.
 *
 * Pass `valueLabel` for the human-facing prefix (e.g. "recordActions[edit].assign").
 * Returns empty array on bridge load failure — caller should fall back to no check.
 */
export async function validateAssignValues(
  assignValues: unknown,
  collectionName: string,
  fieldNames: ReadonlySet<string>,
  path: string,
  valueLabel = 'assign',
): Promise<BridgeRuleIssue[]> {
  const mod = await loadAssignValuesValidation();
  const fn = mod?.collectAssignValuesValidationIssues;
  if (typeof fn !== 'function') return [];
  try {
    const issues = fn({
      assignValues,
      path,
      collectionName,
      collectionMeta: { fieldsByName: fieldNames as Set<string> },
      valueLabel,
    });
    return Array.isArray(issues) ? issues : [];
  } catch {
    return [];
  }
}

// ─── Diagnostics ───

/**
 * Compare reconciler's emitted JS model uses against kernel's canonical list.
 * Returns names we emit that the kernel does NOT recognize — likely a
 * kernel-side rename. Empty array when bridge loads cleanly with our set as
 * a subset, OR when bridge fails to load.
 */
export async function detectJsModelUseDrift(
  reconcilerUses: readonly string[],
): Promise<readonly string[]> {
  const known = await getKnownRunjsModelUses();
  if (!known.length) return []; // bridge unavailable — caller should not treat as drift
  const knownSet = new Set(known);
  return reconcilerUses.filter(use => !knownSet.has(use));
}

// ─── Once-per-process drift report ───

let driftReported = false;

/**
 * Print a one-time drift report at the start of a deploy. Idempotent — safe
 * to call from multiple entry points; subsequent invocations are no-ops.
 *
 * Drift findings are warn-level only: they do not block deploy. If the
 * bridge can't load (sibling skill missing or API renamed), nothing prints.
 */
export async function runStartupDriftChecks(
  reconcilerJsModelUses: readonly string[],
  log: (msg: string) => void,
): Promise<void> {
  if (driftReported) return;
  driftReported = true;
  const drift = await detectJsModelUseDrift(reconcilerJsModelUses);
  if (drift.length) {
    log(`  ⚠ ui-builder drift: reconciler emits JS model uses absent from kernel surface-policy: ${drift.join(', ')}`);
    log(`    Likely a NocoBase kernel rename. Check packages/core/flow-engine/src/runjs-context/ for canonical names.`);
  }
}
