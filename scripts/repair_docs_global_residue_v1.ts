/** Markdown/Wikilink断片・空node・壊れたplacementを通常の編集可能nodeへ修復する。 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { and, asc, eq, inArray, isNull, or, sql } from "../frontend/node_modules/drizzle-orm/index.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f";
const RUN_ROOT = resolve(ROOT, "artifacts", "foam_curation", "global_residue_v1");
const PLAN_PATH = resolve(RUN_ROOT, "latest-plan.json");
type Json = Record<string, any>;
type TitleRepair = { id: string; original_title: string; replacement_title: string };
type ArchiveTarget = { id: string; original_title: string };
type Plan = { schema_version: "docs-global-residue/v1"; fingerprint: string; title_repairs: TitleRepair[]; archive_targets: ArchiveTarget[]; orphan_placement_ids: string[]; counts: Record<string, number> };
type BackupReceipt = { schema_version: "docs-global-residue-backup/v1"; dump_path: string; dump_sha256: string; plan_sha256: string; fingerprint: string };
const sha256 = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
const ALLOWED_EMPTY_HOLDER_BODY_KEYS = new Set(["format", "block_type"]);
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value as Json).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
  return JSON.stringify(value) ?? "null";
}
function loadEnv() {
  for (const envPath of [resolve(ROOT, ".env"), resolve(ROOT, "frontend", ".env")]) {
    if (!existsSync(envPath)) continue;
    for (const line of readFileSync(envPath, "utf8").split(/\r?\n/)) {
      const match = line.match(/^\s*([^#=]+)=(.*)$/); if (!match) continue;
      const key = match[1].trim(); if (!(key in process.env)) process.env[key] = match[2].trim().replace(/^['"]|['"]$/g, "");
    }
  }
  if (process.env.DATABASE_URL?.startsWith("postgresql+asyncpg://")) process.env.DATABASE_URL = process.env.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://");
}
function dbEnv() {
  const url = process.env.DATABASE_URL ? new URL(process.env.DATABASE_URL) : null;
  return { ...process.env, PGHOST: process.env.POSTGRES_HOST || url?.hostname || "127.0.0.1", PGPORT: process.env.POSTGRES_PORT || url?.port || "5432", PGDATABASE: process.env.POSTGRES_DB || url?.pathname.slice(1) || "aoitalk_memory", PGUSER: process.env.POSTGRES_USER || (url ? decodeURIComponent(url.username) : "aoitalk"), PGPASSWORD: process.env.POSTGRES_PASSWORD || (url ? decodeURIComponent(url.password) : "") };
}
function dumpDatabase(path: string) {
  const exe = existsSync("C:/Program Files/PostgreSQL/16/bin/pg_dump.exe") ? "C:/Program Files/PostgreSQL/16/bin/pg_dump.exe" : "pg_dump";
  const result = spawnSync(exe, ["--format=custom", "--file", path], { env: dbEnv(), encoding: "utf8" });
  if (result.status !== 0) throw new Error(`pg_dump failed: ${result.stderr || result.stdout}`);
}
function writeJson(path: string, value: unknown) { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8"); }
function readJson<T>(path: string) { return JSON.parse(readFileSync(path, "utf8")) as T; }
function receiptPath(dumpPath: string) { return `${dumpPath}.receipt.json`; }
function arg(name: string) { const index = process.argv.indexOf(name); return index < 0 ? null : process.argv[index + 1] ?? null; }
function stripWiki(value: string) {
  return value.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2").replace(/\[\[([^\]]+)\]\]/g, "$1").replace(/\[\[(?:file:)?/g, "").replace(/\]\]/g, "");
}
function cleanCell(value: string) {
  return stripWiki(value.trim())
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_all, label, url) => `${label || "画像"}: ${url}`)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)")
    .replace(/<br\s*\/?>/gi, " / ").replace(/\s+/g, " ").trim();
}
function tableReplacement(title: string) {
  const trimmed = title.trim();
  const decorated = trimmed.match(/^[-=]{3,}\s*(.*?)\s*[-=]{3,}$/);
  if (decorated) return cleanCell(decorated[1]) || null;
  if (/^[-=]{3,}$/.test(trimmed)) return null;
  const cells = trimmed.replace(/^\|/, "").replace(/\|$/, "").split("|").map(cleanCell);
  if (cells.length && cells.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
  return cells.filter(Boolean).join(" - ");
}
function holderHasOwnContent(row: { bodyJson: unknown; description?: string | null; aliases?: unknown }, decrypt: (value: unknown) => Json) {
  const body = decrypt(row.bodyJson);
  return Object.keys(body).some((key) => !ALLOWED_EMPTY_HOLDER_BODY_KEYS.has(key))
    || (typeof row.description === "string" && row.description.trim().length > 0)
    || (Array.isArray(row.aliases) && row.aliases.length > 0);
}
async function archiveRelations(db: any, schema: any, archiveIds: string[]) {
  if (!archiveIds.length) return { supertags: [], fieldValues: [], attachments: [], nodePlacements: [] };
  const [supertags, fieldValues, attachments, nodePlacements] = await Promise.all([
    db.select().from(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, archiveIds)).orderBy(asc(schema.knowledgeNodeSupertags.nodeId)),
    db.select().from(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, archiveIds)).orderBy(asc(schema.knowledgeFieldValues.nodeId)),
    db.select().from(schema.knowledgeAttachments).where(inArray(schema.knowledgeAttachments.nodeId, archiveIds)).orderBy(asc(schema.knowledgeAttachments.nodeId)),
    db.select().from(schema.knowledgeNodePlacements).where(or(
      inArray(schema.knowledgeNodePlacements.nodeId, archiveIds),
      inArray(schema.knowledgeNodePlacements.parentNodeId, archiveIds),
    )).orderBy(asc(schema.knowledgeNodePlacements.id)),
  ]);
  return { supertags, fieldValues, attachments, nodePlacements };
}
async function fingerprint(db: any, schema: any, ids: string[], placementIds: string[], archiveIds: string[]) {
  const rows = ids.length ? await db.select().from(schema.knowledgeNodes).where(inArray(schema.knowledgeNodes.id, ids)).orderBy(asc(schema.knowledgeNodes.id)) : [];
  const placements = placementIds.length || archiveIds.length ? await db.select().from(schema.knowledgeNodePlacements).where(or(
    ...(placementIds.length ? [inArray(schema.knowledgeNodePlacements.id, placementIds)] : []),
    ...(archiveIds.length ? [inArray(schema.knowledgeNodePlacements.nodeId, archiveIds), inArray(schema.knowledgeNodePlacements.parentNodeId, archiveIds)] : []),
  )).orderBy(asc(schema.knowledgeNodePlacements.id)) : [];
  const relations = await archiveRelations(db, schema, archiveIds);
  return sha256([
    rows.map((row: any) => canonical(row)).join("\n"),
    relations.supertags.map((row: any) => canonical(row)).join("\n"),
    relations.fieldValues.map((row: any) => canonical(row)).join("\n"),
    relations.attachments.map((row: any) => canonical(row)).join("\n"),
    placements.map((row: any) => canonical(row)).join("\n"),
  ].join("\n--relation--\n"));
}
async function buildPlan(): Promise<Plan> {
  const { db } = await import("@/db"); const schema = await import("@/db/schema");
  const { decryptDocsNodeBodyJson } = await import("@/lib/server/docs-node-writer");
  const rows = await db.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.workspaceId, WORKSPACE_ID), isNull(schema.knowledgeNodes.archivedAt), sql`(
    ((${schema.knowledgeNodes.title} like '%[[%' and ${schema.knowledgeNodes.title} not like '%[[node:%'))
    or ${schema.knowledgeNodes.title} ~ '^\\s*\\|.*\\|\\s*$'
    or ${schema.knowledgeNodes.title} ~ '^\\s*\\|?\\s*:?-{3,}'
    or (btrim(${schema.knowledgeNodes.title})='' and ${schema.knowledgeNodes.systemKey} not like 'docs_manual_curation_v2:blank:%')
    or ${schema.knowledgeNodes.title}='Untitled'
  )`)).orderBy(asc(schema.knowledgeNodes.id));
  const titleRepairs: TitleRepair[] = []; const archiveTargets: ArchiveTarget[] = [];
  for (const row of rows) {
    const body = decryptDocsNodeBodyJson(row.bodyJson);
    // Exact code/config/prompt lines may legitimately contain TOML arrays such as
    // [[datasets]]. They are editable source text, not unresolved Foam wikilinks.
    if (body.curation_exact === true && String(row.systemKey ?? "").startsWith("docs_followup_20260720:anima:")) continue;
    let replacement: string | null = row.title;
    if (!row.title.trim() || row.title === "Untitled") replacement = null;
    else if (/^\s*\|.*\|\s*$/.test(row.title) || /^\s*\|?\s*:?-{3,}/.test(row.title)) replacement = tableReplacement(row.title);
    if (replacement !== null) replacement = stripWiki(replacement).trim();
    if (!replacement) archiveTargets.push({ id: String(row.id), original_title: row.title });
    else if (replacement !== row.title) titleRepairs.push({ id: String(row.id), original_title: row.title, replacement_title: replacement });
  }
  const archiveIds = archiveTargets.map((item) => item.id);
  const archiveRowsById = new Map(rows.filter((row) => archiveIds.includes(String(row.id))).map((row) => [String(row.id), row]));
  const relations = await archiveRelations(db, schema, archiveIds);
  const relatedNodeIds = new Set([
    ...relations.supertags.map((row: any) => String(row.nodeId)),
    ...relations.fieldValues.map((row: any) => String(row.nodeId)),
    ...relations.attachments.map((row: any) => String(row.nodeId)),
    ...relations.nodePlacements.flatMap((row: any) => [String(row.nodeId), String(row.parentNodeId)]),
  ]);
  const unsafeArchiveIds = archiveIds.filter((id) => {
    const row = archiveRowsById.get(id);
    return !row || holderHasOwnContent(row, decryptDocsNodeBodyJson) || relatedNodeIds.has(id);
  });
  if (unsafeArchiveIds.length) throw new Error(`空title/Untitled nodeに内容または関連データがあるため自動archiveできません: ${unsafeArchiveIds.join(", ")}`);
  const orphanPlacements = await db.execute(sql`select placement.id::text from knowledge_node_placements placement left join knowledge_nodes node on node.id=placement.node_id left join knowledge_nodes parent on parent.id=placement.parent_node_id where node.id is null or parent.id is null or node.archived_at is not null or parent.archived_at is not null order by placement.id`) as Array<{ id: string }>;
  const ids = [...titleRepairs.map((item) => item.id), ...archiveTargets.map((item) => item.id)];
  return { schema_version: "docs-global-residue/v1", fingerprint: await fingerprint(db, schema, ids, orphanPlacements.map((row) => row.id), archiveIds), title_repairs: titleRepairs, archive_targets: archiveTargets, orphan_placement_ids: orphanPlacements.map((row) => row.id), counts: { title_repairs: titleRepairs.length, archived_blank_or_separator: archiveTargets.length, orphan_placements: orphanPlacements.length } };
}
async function audit() { const plan = await buildPlan(); writeJson(PLAN_PATH, plan); console.log(JSON.stringify({ plan_path: PLAN_PATH, plan_sha256: sha256(readFileSync(PLAN_PATH)), ...plan.counts }, null, 2)); }
async function backup() {
  if (!existsSync(PLAN_PATH)) await audit();
  const approved = readJson<Plan>(PLAN_PATH); const current = await buildPlan();
  if (canonical(approved) !== canonical(current)) throw new Error("plan or DB changed after audit; rerun audit before backup");
  const path = resolve(RUN_ROOT, `pre-apply-${new Date().toISOString().replace(/[:.]/g, "-")}.dump`);
  dumpDatabase(path);
  const receipt: BackupReceipt = { schema_version: "docs-global-residue-backup/v1", dump_path: path, dump_sha256: sha256(readFileSync(path)), plan_sha256: sha256(readFileSync(PLAN_PATH)), fingerprint: approved.fingerprint };
  writeJson(receiptPath(path), receipt);
  console.log(JSON.stringify({ backup_path: path, receipt_path: receiptPath(path), sha256: receipt.dump_sha256 }, null, 2));
}
async function apply() {
  const backupPath = arg("--backup"); if (!backupPath || !existsSync(backupPath)) throw new Error("--backup <existing pg_dump> is required");
  if (!existsSync(PLAN_PATH)) throw new Error("audit plan is missing");
  const approved = readJson<Plan>(PLAN_PATH);
  const backupReceiptPath = receiptPath(backupPath); if (!existsSync(backupReceiptPath)) throw new Error(`backup receipt is missing: ${backupReceiptPath}`);
  const receipt = readJson<BackupReceipt>(backupReceiptPath);
  if (receipt.schema_version !== "docs-global-residue-backup/v1" || resolve(receipt.dump_path) !== resolve(backupPath) || receipt.dump_sha256 !== sha256(readFileSync(backupPath)) || receipt.plan_sha256 !== sha256(readFileSync(PLAN_PATH)) || receipt.fingerprint !== approved.fingerprint) throw new Error("backup receipt does not match the approved plan, DB fingerprint, or dump");
  const current = await buildPlan(); if (canonical(approved) !== canonical(current)) throw new Error("plan or DB changed after audit");
  const { db } = await import("@/db"); const schema = await import("@/db/schema");
  const writer = await import("@/lib/server/docs-node-writer"); const utils = await import("@/lib/server/knowledge-docs-utils");
  await db.transaction(async (tx) => {
    await tx.execute(sql`set transaction isolation level serializable`);
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext('docs-workspace-write'), hashtext(${WORKSPACE_ID}))`);
    await tx.execute(sql`lock table knowledge_nodes, knowledge_attachments, knowledge_node_supertags, knowledge_field_values, knowledge_supertags, knowledge_fields, knowledge_edges, knowledge_search_index, knowledge_node_placements, knowledge_revisions, knowledge_import_jobs, users in share row exclusive mode`);
    const ids = [...approved.title_repairs.map((item) => item.id), ...approved.archive_targets.map((item) => item.id)];
    const archiveIds = approved.archive_targets.map((item) => item.id);
    if (await fingerprint(tx, schema, ids, approved.orphan_placement_ids, archiveIds) !== approved.fingerprint) throw new Error("DB changed after audit; aborting before write");
    for (const target of approved.title_repairs) {
      const [row] = await tx.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.id, target.id), isNull(schema.knowledgeNodes.archivedAt)));
      if (!row || row.title !== target.original_title) throw new Error(`title changed: ${target.id}`);
      const updated = await writer.updateDocsNode(tx, target.id, { title: target.replacement_title, updatedBy: OWNER_ID });
      await utils.upsertKnowledgeSearchIndex(tx, updated, ""); await utils.syncKnowledgeNodeReferenceEdges(tx, updated, OWNER_ID); await utils.appendKnowledgeRevision(tx, updated, OWNER_ID, "Markdown断片を編集可能な通常nodeへ変換", []);
    }
    for (const target of approved.archive_targets) {
      const [row] = await tx.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.id, target.id), isNull(schema.knowledgeNodes.archivedAt)));
      if (!row || row.title !== target.original_title) throw new Error(`archive target changed: ${target.id}`);
      const relations = await archiveRelations(tx, schema, [row.id]);
      if (holderHasOwnContent(row, writer.decryptDocsNodeBodyJson)
        || relations.supertags.length
        || relations.fieldValues.length
        || relations.attachments.length
        || relations.nodePlacements.length) {
        throw new Error(`空title/Untitled nodeに内容または関連データがあるためarchiveを中止します: ${row.id}`);
      }
      const children = await tx.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.parentId, row.id), isNull(schema.knowledgeNodes.archivedAt)));
      for (const child of children) await writer.updateDocsNode(tx, child.id, { parentId: row.parentId, rootPageId: row.rootPageId, projectId: row.projectId, updatedBy: OWNER_ID });
      await tx.delete(schema.knowledgeNodePlacements).where(eq(schema.knowledgeNodePlacements.parentNodeId, row.id));
      const archived = await writer.updateDocsNode(tx, row.id, { archivedAt: new Date(), updatedBy: OWNER_ID }); await tx.delete(schema.knowledgeSearchIndex).where(eq(schema.knowledgeSearchIndex.nodeId, row.id)); await utils.appendKnowledgeRevision(tx, archived, OWNER_ID, "空行またはMarkdown区切りを除去", []);
    }
    if (approved.orphan_placement_ids.length) await tx.delete(schema.knowledgeNodePlacements).where(inArray(schema.knowledgeNodePlacements.id, approved.orphan_placement_ids));
  });
  console.log(JSON.stringify({ applied: true, backup_path: backupPath, backup_sha256: sha256(readFileSync(backupPath)), ...approved.counts }, null, 2));
}
async function verify() { const plan = await buildPlan(); console.log(JSON.stringify(plan.counts, null, 2)); if (Object.values(plan.counts).some((value) => value !== 0)) process.exitCode = 1; }
async function main() { loadEnv(); const command = process.argv[2] ?? "audit"; if (command === "audit") return audit(); if (command === "backup") return backup(); if (command === "apply") return apply(); if (command === "verify") return verify(); throw new Error(`unknown command: ${command}`); }
main().then(() => process.exit(process.exitCode ?? 0), (error) => { console.error(error instanceof Error ? error.stack : error); process.exit(1); });
