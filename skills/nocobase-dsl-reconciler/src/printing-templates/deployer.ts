/**
 * Deployer for the `printingTemplates` collection.
 *
 * Pushes entries from `workspaces/<proj>/print-templates/index.yaml` onto
 * a live NB instance:
 *   - Upload the .docx file (multer endpoint returns a NB-assigned filename)
 *   - Upsert the metadata row, swapping the stored filename in for the
 *     newly uploaded one so the render action finds the right file.
 *
 * Idempotent: runs on every push. Existing rows are updated; new ones
 * created. Rows present on NB but absent from the local index are NOT
 * deleted — the print-templates dir can be shared across projects with
 * different scopes, so auto-delete would be overreach.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { NocoBaseClient } from '../client';
import { loadYaml } from '../utils/yaml';
import { catchSwallow } from '../utils/swallow';

interface PrintingTemplateEntry {
  name: string;
  title: string;
  collectionName: string;
  rootDataType: 'array' | 'map';
  dataSource: string;
  filename: string;
}

interface IndexFile {
  templates: PrintingTemplateEntry[];
}

export async function deployPrintingTemplates(
  nb: NocoBaseClient,
  projectDir: string,
  log: (msg: string) => void,
): Promise<void> {
  const indexFile = path.join(projectDir, 'print-templates', 'index.yaml');
  if (!fs.existsSync(indexFile)) return;

  const parsed = loadYaml<IndexFile>(indexFile);
  const entries = parsed?.templates || [];
  if (!entries.length) return;

  log('\n  ── Printing templates ──');

  // Fetch existing rows once for create-or-update dispatch.
  let existing = new Map<string, PrintingTemplateEntry>();
  try {
    const resp = await nb.http.get(`${nb.baseUrl}/api/printingTemplates:list`, {
      params: { paginate: 'false' },
    });
    for (const r of (resp.data?.data || []) as PrintingTemplateEntry[]) {
      existing.set(r.name, r);
    }
  } catch (e) {
    catchSwallow(e, 'plugin likely not installed');
    log('  ! printingTemplates:list failed — plugin not installed? skipping.');
    return;
  }

  for (const e of entries) {
    const localFile = path.join(projectDir, 'print-templates', e.filename);
    const fileExists = fs.existsSync(localFile);

    // 1. Upload docx if we have it. The plugin renames to a random
    //    filename; capture that into `serverFilename` for the metadata row.
    let serverFilename = existing.get(e.name)?.filename;
    if (fileExists) {
      try {
        // Use native fetch — axios+Blob doesn't negotiate the multer
        // boundary reliably; fetch+FormData does the right thing out of
        // the box and matches what the browser client does.
        const fd = new FormData();
        const buf = fs.readFileSync(localFile);
        fd.append(
          'file',
          new Blob([new Uint8Array(buf)]),
          path.basename(localFile),
        );
        const authz = (nb.http.defaults.headers?.common?.Authorization as string)
          || (nb.http.defaults.headers?.Authorization as string)
          || '';
        const res = await fetch(`${nb.baseUrl}/api/printingTemplates:upload`, {
          method: 'POST',
          body: fd,
          headers: authz ? { Authorization: authz } : {},
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json() as { data?: { filename?: string } };
        serverFilename = json?.data?.filename || serverFilename;
      } catch (err) {
        log(`  ! upload "${e.filename}": ${err instanceof Error ? err.message.slice(0, 80) : err}`);
        continue;
      }
    } else if (!serverFilename) {
      log(`  ! skip "${e.name}" — no local file AND no existing server file`);
      continue;
    }

    // 2. Upsert metadata row
    const payload = {
      name: e.name,
      title: e.title,
      collectionName: e.collectionName,
      rootDataType: e.rootDataType,
      dataSource: e.dataSource || 'main',
      filename: serverFilename,
    };
    try {
      if (existing.has(e.name)) {
        await nb.http.post(
          `${nb.baseUrl}/api/printingTemplates:update`,
          payload,
          { params: { filterByTk: e.name } },
        );
        log(`    ~ ${e.name} (${e.collectionName}/${e.rootDataType})`);
      } else {
        await nb.http.post(
          `${nb.baseUrl}/api/printingTemplates:create`,
          payload,
        );
        log(`    + ${e.name} (${e.collectionName}/${e.rootDataType})`);
      }
    } catch (err) {
      log(`  ! upsert "${e.name}": ${err instanceof Error ? err.message.slice(0, 80) : err}`);
    }
  }
}
