/**
 * Deploy JS items (inside detail/form grid) and JS columns (table).
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { BlockSpec } from '../../types/spec';
import type { BlockState } from '../../types/state';
import type { DeployContext } from './types';
import { ensureJsHeader, replaceJsUids } from '../../utils/js-utils';
import { generateUid } from '../../utils/uid';
import { validateRunJS } from '../../utils/runjs-validator';

/**
 * Run AST validation against runjs source. Returns true when the file is OK
 * to push to NB. On error-level issues, logs them and returns false so the
 * caller can `continue` and skip this entry. Warning-level issues (e.g. JSX
 * edge cases ui-builder's parser misjudges) are silent — NB's runtime is the
 * authority on what JSX shapes work.
 */
async function preflightJs(
  code: string,
  filePath: string,
  log: (msg: string) => void,
): Promise<boolean> {
  const r = await validateRunJS(code);
  for (const issue of r.issues) {
    if (issue.level === 'error') {
      log(`      ✗ ${filePath}: ${issue.message}`);
    }
  }
  return r.ok;
}

/**
 * Deploy JS items into a form/details grid.
 */
export async function deployJsItems(
  ctx: DeployContext,
  gridUid: string,
  bs: BlockSpec,
  coll: string,
  modDir: string,
  blockState: BlockState,
  allBlocksState: Record<string, BlockState>,
): Promise<void> {
  const { nb, log } = ctx;
  const jsItems = bs.js_items || [];
  if (!jsItems.length || !gridUid) return;

  const specKeys = new Set<string>();
  for (const jsSpec of jsItems) {
    if (!jsSpec.file) continue;
    specKeys.add(jsSpec.key);
    const jsPath = path.join(modDir, jsSpec.file);
    if (!fs.existsSync(jsPath)) continue;

    let code = fs.readFileSync(jsPath, 'utf8');
    if (!(await preflightJs(code, `JS item ${jsSpec.file}`, log))) continue;
    code = ensureJsHeader(code, { desc: jsSpec.desc, jsType: 'JSItemModel', coll });
    code = replaceJsUids(code, allBlocksState);

    const existing = blockState.js_items?.[jsSpec.key];
    if (existing?.uid) {
      await nb.updateModel(existing.uid, {
        jsSettings: { runJs: { code, version: 'v1' } },
      });
    } else {
      const newUid = generateUid();
      await nb.models.save({
        uid: newUid, use: 'JSItemModel',
        parentId: gridUid, subKey: 'items', subType: 'array',
        sortIndex: 0, flowRegistry: {},
        stepParams: { jsSettings: { runJs: { code, version: 'v1' } } },
      });
      if (!blockState.js_items) blockState.js_items = {};
      blockState.js_items[jsSpec.key] = { uid: newUid };
    }
    log(`      ~ JS item: ${jsSpec.desc || jsSpec.key}`);
  }

  // Clean up orphaned JS items (state keys not in current spec)
  if (blockState.js_items) {
    for (const [key, entry] of Object.entries(blockState.js_items)) {
      if (specKeys.has(key)) continue;
      const uid = (entry as { uid?: string })?.uid;
      if (uid) {
        try {
          await nb.http.post(`${nb.baseUrl}/api/flowModels:destroy`, {}, { params: { filterByTk: uid } });
          log(`      - JS item orphan removed: ${key}`);
        } catch { /* skip */ }
      }
      delete blockState.js_items[key];
    }
  }
}

/**
 * Deploy JS columns into a table block.
 */
export async function deployJsColumns(
  ctx: DeployContext,
  blockUid: string,
  bs: BlockSpec,
  coll: string,
  modDir: string,
  blockState: BlockState,
  allBlocksState: Record<string, BlockState>,
): Promise<void> {
  const { nb, log } = ctx;
  const jsCols = bs.js_columns || [];
  const specKeys = new Set(jsCols.map(j => j.key));

  // Prune js_columns whose keys are no longer in DSL (applies regardless of
  // whether new cols exist — pure-delete case also needs cleanup).
  if (bs.type === 'table' && blockState.js_columns) {
    for (const [key, entry] of Object.entries(blockState.js_columns)) {
      if (specKeys.has(key)) continue;
      const uid = (entry as { uid?: string })?.uid;
      if (uid) {
        try {
          await nb.http.post(`${nb.baseUrl}/api/flowModels:destroy`, {}, { params: { filterByTk: uid } });
          log(`      - JS column orphan removed: ${key}`);
        } catch { /* skip */ }
      }
      delete blockState.js_columns[key];
    }
  }

  if (!jsCols.length || bs.type !== 'table') return;

  for (const jsSpec of jsCols) {
    if (!jsSpec.file) continue;
    const jsPath = path.join(modDir, jsSpec.file);
    if (!fs.existsSync(jsPath)) continue;

    let code = fs.readFileSync(jsPath, 'utf8');
    if (!(await preflightJs(code, `JS col ${jsSpec.file}`, log))) continue;
    code = ensureJsHeader(code, { desc: jsSpec.desc, jsType: 'JSColumnModel', coll });

    const existing = blockState.js_columns?.[jsSpec.key];
    if (existing?.uid) {
      const colUpdate: Record<string, unknown> = {
        jsSettings: { runJs: { code, version: 'v1' } },
      };
      if (jsSpec.title) colUpdate.tableColumnSettings = { title: { title: jsSpec.title } };
      await nb.updateModel(existing.uid, colUpdate);
    } else {
      const newUid = generateUid();
      const colStepParams: Record<string, unknown> = {
        jsSettings: { runJs: { code, version: 'v1' } },
        fieldSettings: { init: { fieldPath: jsSpec.field } },
      };
      if (jsSpec.title) {
        colStepParams.tableColumnSettings = { title: { title: jsSpec.title } };
      }
      await nb.models.save({
        uid: newUid, use: 'JSColumnModel',
        parentId: blockUid, subKey: 'columns', subType: 'array',
        sortIndex: 0, flowRegistry: {},
        stepParams: colStepParams,
      });
      if (!blockState.js_columns) blockState.js_columns = {};
      blockState.js_columns[jsSpec.key] = { uid: newUid };
    }
    log(`      ~ JS col: ${jsSpec.desc || jsSpec.key}`);
  }
}
