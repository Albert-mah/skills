/**
 * Exporter for the `printingTemplates` collection owned by
 * @nocobase/plugin-action-template-print.
 *
 * Pulls metadata rows + the uploaded .docx/.xlsx/.pptx files onto disk so
 * they live alongside the DSL they're referenced by (templateName in
 * `- type: templatePrint` entries).
 *
 * Layout on disk:
 *   workspaces/<proj>/print-templates/
 *     index.yaml                       # ordered list of {name, title, collectionName, rootDataType, dataSource, filename}
 *     <name>.docx                      # file per template; filename matches .name (uid PK)
 *
 * Scope rule: when --group is used, only export templates whose
 * `collectionName` is in the scoped collection set. Unscoped → everything.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { NocoBaseClient } from '../client';
import { dumpYaml } from '../utils/yaml';
import { catchSwallow } from '../utils/swallow';

export interface PrintingTemplateEntry {
  name: string;
  title: string;
  collectionName: string;
  rootDataType: 'array' | 'map';
  dataSource: string;
  filename: string;
}

export async function exportPrintingTemplates(
  nb: NocoBaseClient,
  outDir: string,
  scopedColls?: Set<string>,
): Promise<void> {
  let rows: PrintingTemplateEntry[] = [];
  try {
    const resp = await nb.http.get(`${nb.baseUrl}/api/printingTemplates:list`, {
      params: { paginate: 'false' },
    });
    rows = (resp.data?.data || []) as PrintingTemplateEntry[];
  } catch (e) {
    // Plugin not installed, or the collection doesn't exist — silent skip.
    catchSwallow(e, 'plugin not installed');
    return;
  }

  // Scope filter
  const filtered = scopedColls
    ? rows.filter(r => r.collectionName && scopedColls.has(r.collectionName))
    : rows;

  if (!filtered.length) return;

  const dir = path.join(outDir, 'print-templates');
  fs.mkdirSync(dir, { recursive: true });

  // Detect existing local-only files (user-authored docx like my
  // hand-built 刀具维修单.docx) so we don't clobber them. Files named
  // after a pulled template *will* be overwritten — that's the expected
  // round-trip behavior.
  const pulledFilenames = new Set<string>();

  const entries: PrintingTemplateEntry[] = [];
  for (const r of filtered) {
    if (!r.name || !r.filename) continue;
    const ext = path.extname(r.filename) || '.docx';
    const onDiskName = `${r.name}${ext}`;
    const filePath = path.join(dir, onDiskName);

    try {
      const binResp = await nb.http.get(`${nb.baseUrl}/api/printingTemplates:download`, {
        params: { filterByTk: r.name },
        responseType: 'arraybuffer',
      });
      fs.writeFileSync(filePath, Buffer.from(binResp.data as ArrayBuffer));
      pulledFilenames.add(onDiskName);
    } catch (e) {
      // File missing on the NB host or download permission denied —
      // emit the entry anyway (metadata only) so push can at least
      // re-upload when someone restores the file locally.
      console.log(`  ! printing-template download failed for "${r.name}": ${
        e instanceof Error ? e.message.slice(0, 80) : e
      }`);
    }

    entries.push({
      name: r.name,
      title: r.title,
      collectionName: r.collectionName,
      rootDataType: r.rootDataType,
      dataSource: r.dataSource || 'main',
      filename: onDiskName,  // DSL-side filename is normalized to `<name>.<ext>`
    });
  }

  // Stable ordering by name for clean diffs
  entries.sort((a, b) => a.name.localeCompare(b.name));

  fs.writeFileSync(path.join(dir, 'index.yaml'), dumpYaml({ templates: entries }));
  console.log(`  + ${entries.length} printing-template${entries.length === 1 ? '' : 's'}`);
}
