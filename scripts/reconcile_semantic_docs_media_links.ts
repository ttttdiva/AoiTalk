/**
 * 承認済み semantic Docs に、source-grounded 添付と wikilink を安全に投影する。
 * Foam 原本・外部 API は読まない。曖昧な所有者・参照先は更新せず失敗する。
 */
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { and, eq, inArray, isNull, sql } from "../frontend/node_modules/drizzle-orm/index.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f";
const SOURCE_KEY = "foam_source_grounded_v1";
const SEMANTIC_KEY = "docs_semantic_overlay_v1";
const MIGRATION_KEY = "docs_semantic_media_links_v1";
const APPROVAL_TOKEN = "approved_by_user_instruction_2026-07-19";
const EXPECTED_SOURCE_ATTACHMENTS = 179;
const EXPECTED_SEMANTIC_ATTACHMENTS = 166;
const EXPECTED_PILOT_ATTACHMENTS = 7;
const EXPECTED_DAILY_ATTACHMENTS = 6;
const PILOT_SOURCE_KEY = "source.イラスト修正テク";
const APPROVED_PATH = resolve(ROOT, "artifacts/foam_curation/phase3_source_v1/semantic_overlay_runs/approved_semantic_manifest.json");
const PHASE3_PATH = resolve(ROOT, "artifacts/foam_curation/phase3_source_v1/manifest.json");
const PHASE3_PROVENANCE_PATH = resolve(ROOT, "artifacts/foam_curation/phase3_source_v1/provenance.private.json");
const TANA_MANIFEST_PATH = resolve(ROOT, "artifacts/foam_curation/tana_recovery_v1/import_manifest.json");
const TANA_PROVENANCE_PATH = resolve(ROOT, "artifacts/foam_curation/tana_recovery_v1/provenance.private.json");
const RUN_ROOT = resolve(ROOT, "artifacts/foam_curation/phase3_source_v1/semantic_media_link_runs");
const SEMANTIC_MEDIA_ROOT = resolve(ROOT, "workspaces/_docs/attachments/semantic", MIGRATION_KEY);
const IMAGE_RE = /!\[([^\]]*)\]\(([^)]+)\)/g;
const RAW_WIKILINK_RE = /(?<!!)\[\[([^\]\n]+)\]\]/g;
const DOCS_LINK_RE = /^node:[0-9a-f-]{36}\|/i;

type Json = Record<string, any>;
type ApprovedNode = { key: string; systemKey: string; title: string; content?: string; fields?: Array<{ name: string; value: string }>; parentKey: string; aliases?: string[]; sourceKeys?: string[]; sourceKind?: string };
type ApprovedManifest = { schema_version: string; approval: string; import_allowed: boolean; nodes: ApprovedNode[] };
type Phase3Manifest = { schema_version: string; nodes: Array<{ key: string; title: string; description?: string }>; placements: Array<{ node_key: string; parent_key: string | null }> };
type Phase3Provenance = { schema_version: string; sources: Array<{ node_key: string; path: string }> };
type TanaManifest = { schema_version: string; nodes: Array<{ key: string; title: string; content?: string }>; placements: Array<{ node_key: string; parent_key: string | null }> };
type TanaBlock = { source_file?: string; node_key?: string; note_node_keys?: string[] };
type ExactBlock = { pointer: string; source_path: string; content_sha256: string };
type TanaProvenance = {
  schema_version: string;
  blocks: Record<string, TanaBlock>;
  verbatim?: Array<{ source_file: string; node_key: string }>;
  exact?: { documents?: Array<{ uid?: string; source_path: string }>; blocks?: ExactBlock[] };
};
type AttachmentPlan = {
  source_attachment_id: string; source_node_key: string; target_node_key: string; target_node_id: string;
  target_attachment_id: string; original_reference: string; file_name: string; source_file_path: string;
  target_file_path: string; file_sha256: string; mime_type: string | null; size_bytes: number | null;
};
type LinkPlan = { source_node_key: string; source_node_id: string; raw: string; target: string; label: string; target_node_key: string; target_node_id: string };
type LinkCleanupPlan = { source_node_key: string; raw: string; label: string };
type ImageCleanupPlan = { target_node_key: string; references: string[] };
type Plan = {
  schema_version: "docs-semantic-media-links-plan/v2"; approved_sha256: string; phase3_sha256: string;
  phase3_provenance_sha256: string; tana_manifest_sha256: string; tana_provenance_sha256: string;
  database_fingerprint: string; source_attachment_count: number; local_image_reference_count: number;
  remote_image_reference_count: number; pilot_attachment_count: number; pilot_fingerprint: string; daily_attachment_count: number; daily_fingerprint: string;
  attachment_plans: AttachmentPlan[]; image_cleanup_plans: ImageCleanupPlan[]; link_plans: LinkPlan[]; link_cleanup_plans: LinkCleanupPlan[]; node_patch_count: number;
};

const sha = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
function stableId(value: string) { const h = sha(value).slice(0, 32).split(""); h[12] = "4"; h[16] = ((parseInt(h[16], 16) & 3) | 8).toString(16); const s = h.join(""); return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`; }
const MIGRATION_JOB_ID = stableId(`${MIGRATION_KEY}:registry:${WORKSPACE_ID}`);
function canonical(value: unknown): string { if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`; if (value && typeof value === "object") return `{${Object.entries(value as Json).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`; return JSON.stringify(value) ?? "null"; }
const readJson = <T>(path: string) => JSON.parse(readFileSync(path, "utf8")) as T;
function writeJson(path: string, value: unknown) { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8"); }
function arg(name: string) { const index = process.argv.indexOf(name); return index < 0 ? null : process.argv[index + 1] ?? null; }
function fail(message: string): never { throw new Error(message); }
function loadEnv() { const path = resolve(ROOT, ".env"); if (!existsSync(path)) return; for (const line of readFileSync(path, "utf8").split(/\r?\n/)) { const match = line.match(/^\s*([^#=]+)=(.*)$/); if (match && !(match[1].trim() in process.env)) process.env[match[1].trim()] = match[2].trim().replace(/^['"]|['"]$/g, ""); } if (process.env.DATABASE_URL?.startsWith("postgresql+asyncpg://")) process.env.DATABASE_URL = process.env.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"); }
function dbEnv() { const url = process.env.DATABASE_URL ? new URL(process.env.DATABASE_URL) : null; return { ...process.env, PGHOST: process.env.POSTGRES_HOST || url?.hostname || "127.0.0.1", PGPORT: process.env.POSTGRES_PORT || url?.port || "5432", PGDATABASE: process.env.POSTGRES_DB || url?.pathname.slice(1) || "aoitalk_memory", PGUSER: process.env.POSTGRES_USER || (url ? decodeURIComponent(url.username) : "aoitalk"), PGPASSWORD: process.env.POSTGRES_PASSWORD || (url ? decodeURIComponent(url.password) : "") }; }
function dumpDatabase(path: string) { const exe = existsSync("C:/Program Files/PostgreSQL/16/bin/pg_dump.exe") ? "C:/Program Files/PostgreSQL/16/bin/pg_dump.exe" : "pg_dump"; const result = spawnSync(exe, ["--format=custom", "--file", path], { env: dbEnv(), encoding: "utf8" }); if (result.status !== 0) fail(`pg_dump failed: ${result.stderr || result.stdout}`); return sha(readFileSync(path)); }
function normalized(value: string) { return value.normalize("NFKC").trim().replace(/\s+/g, " ").toLocaleLowerCase("ja-JP"); }
function originalReference(metadata: unknown) { return String((metadata && typeof metadata === "object" ? (metadata as Json).original_reference : "") ?? "").trim().replace(/\\/g, "/"); }
export function isRemoteImageReference(reference: string) { return /^(?:https?:|data:|blob:)/i.test(reference.trim()); }
export function isPilotSourceNode(sourceNodeKey: string) { return sourceNodeKey === PILOT_SOURCE_KEY || sourceNodeKey.startsWith(`${PILOT_SOURCE_KEY}.`); }
export function imageReferences(value: unknown) { if (typeof value !== "string") return [] as string[]; return [...value.matchAll(IMAGE_RE)].map((match) => match[2].trim().replace(/^<|>$/g, "").replace(/\\/g, "/")); }
function localImageReferences(value: unknown) { return imageReferences(value).filter((reference) => !isRemoteImageReference(reference)); }
function rawWikilinks(value: unknown) { if (typeof value !== "string") return [] as Array<{ raw: string; target: string; label: string }>; const out = []; for (const match of value.matchAll(RAW_WIKILINK_RE)) { if (DOCS_LINK_RE.test(match[1])) continue; const [rawTarget, ...labelParts] = match[1].split("|"); const target = rawTarget.trim(); if (!target) fail(`empty wikilink target: ${match[0]}`); out.push({ raw: match[0], target, label: labelParts.join("|").trim() || target }); } return out; }
function sourceDocument(nodeKey: string, sources: Phase3Provenance["sources"]) { const matches = sources.filter((source) => nodeKey === source.node_key || nodeKey.startsWith(`${source.node_key}.`)).sort((a, b) => b.node_key.length - a.node_key.length); if (matches.length && matches[1]?.node_key.length === matches[0].node_key.length) fail(`ambiguous phase3 provenance: ${nodeKey}`); return matches[0] ?? null; }
function fileStem(path: string) { return basename(path, extname(path)).toLocaleLowerCase("ja-JP"); }
function addLookup(lookup: Map<string, Set<string>>, value: unknown, key: string) { if (typeof value !== "string" || !value.trim()) return; const norm = normalized(value); const set = lookup.get(norm) ?? new Set<string>(); set.add(key); lookup.set(norm, set); }
function nearestActiveAncestor(key: string, parents: Map<string, string | null>, active: Set<string>): string | null { const seen = new Set<string>(); let cursor: string | null | undefined = key; while (cursor) { if (seen.has(cursor)) fail(`artifact hierarchy cycle: ${key}`); seen.add(cursor); if (active.has(cursor)) return cursor; cursor = parents.get(cursor); } return null; }
function topmostActiveAncestor(key: string, parents: Map<string, string | null>, active: Set<string>): string | null { const seen = new Set<string>(); let cursor: string | null | undefined = key; let owner: string | null = null; while (cursor) { if (seen.has(cursor)) fail(`artifact hierarchy cycle: ${key}`); seen.add(cursor); if (active.has(cursor)) owner = cursor; cursor = parents.get(cursor); } return owner; }

export function resolveAttachmentOwner(input: {
  sourceNodeKey: string;
  directOwners: string[];
  documentedOwners: string[];
  dailyOwner?: string;
  ancestorCandidates: Array<{ ancestorKey: string; owners: string[] }>;
  exactOwners: string[];
}): string {
  if (input.directOwners.length > 1) fail(`${input.sourceNodeKey}: direct owners=${input.directOwners.join(",")}`);
  if (input.directOwners.length === 1) {
    if (!input.documentedOwners.includes(input.directOwners[0])) fail(`${input.sourceNodeKey}: direct owner ${input.directOwners[0]} is outside source provenance`);
    return input.directOwners[0];
  }
  if (input.dailyOwner) return input.dailyOwner;
  for (const ancestor of input.ancestorCandidates) {
    if (ancestor.owners.length > 1) fail(`${input.sourceNodeKey}: ancestor ${ancestor.ancestorKey} owners=${ancestor.owners.join(",")}`);
    if (ancestor.owners.length === 1) return ancestor.owners[0];
  }
  if (input.exactOwners.length > 1) fail(`${input.sourceNodeKey}: exact owners=${input.exactOwners.join(",")}`);
  if (input.exactOwners.length === 1) return input.exactOwners[0];
  if (input.documentedOwners.length === 1) return input.documentedOwners[0];
  fail(`${input.sourceNodeKey}: no unique semantic owner`);
}

function artifactLookups(approved: ApprovedManifest, tanaManifest: TanaManifest, tana: TanaProvenance) {
  const active = new Set(approved.nodes.map((node) => node.key));
  const parents = new Map(tanaManifest.placements.map((placement) => [placement.node_key, placement.parent_key]));
  const contentHashToKeys = new Map<string, Set<string>>();
  for (const node of tanaManifest.nodes) if (typeof node.content === "string" && node.key.startsWith("exactblock.")) {
    const hash = sha(node.content); const set = contentHashToKeys.get(hash) ?? new Set<string>(); set.add(node.key); contentHashToKeys.set(hash, set);
  }
  const exactKeysByFile = new Map<string, Set<string>>();
  const exactNodeKeysByFile = new Map<string, Set<string>>();
  for (const block of tana.exact?.blocks ?? []) {
    const matches = [...(contentHashToKeys.get(block.content_sha256) ?? [])];
    if (!matches.length) fail(`exact block provenance is missing: ${block.content_sha256}`);
    const stem = fileStem(block.source_path);
    for (const match of matches) {
      if (active.has(match)) { const nodes = exactNodeKeysByFile.get(stem) ?? new Set<string>(); nodes.add(match); exactNodeKeysByFile.set(stem, nodes); }
      const owner = topmostActiveAncestor(parents.get(match) ?? "", parents, active);
      if (!owner) continue;
      const set = exactKeysByFile.get(stem) ?? new Set<string>(); set.add(owner); exactKeysByFile.set(stem, set);
    }
  }
  const lookup = new Map<string, Set<string>>();
  for (const document of tana.exact?.documents ?? []) {
    const owners = exactKeysByFile.get(fileStem(document.source_path)) ?? new Set<string>();
    for (const owner of owners) { addLookup(lookup, document.uid, owner); addLookup(lookup, fileStem(document.source_path), owner); }
  }
  for (const [stem, owners] of exactKeysByFile) for (const owner of owners) addLookup(lookup, stem, owner);
  return { lookup, parents, exactNodeKeysByFile };
}

async function snapshot(client?: any): Promise<{ db: any; schema: any; plan: Plan; rowsByKey: Map<string, any>; approvedByKey: Map<string, ApprovedNode> }> {
  for (const path of [APPROVED_PATH, PHASE3_PATH, PHASE3_PROVENANCE_PATH, TANA_MANIFEST_PATH, TANA_PROVENANCE_PATH]) if (!existsSync(path)) fail(`required artifact is missing: ${path}`);
  const approvedRaw = readFileSync(APPROVED_PATH); const approved = JSON.parse(approvedRaw.toString("utf8")) as ApprovedManifest;
  if (approved.schema_version !== "docs-approved-semantic-manifest/v1" || approved.approval !== APPROVAL_TOKEN || approved.import_allowed !== true) fail("semantic manifest approval token does not match semantic overlay gate");
  const phase3Raw = readFileSync(PHASE3_PATH); const phase3 = JSON.parse(phase3Raw.toString("utf8")) as Phase3Manifest;
  const phase3ProvenanceRaw = readFileSync(PHASE3_PROVENANCE_PATH); const phase3Provenance = JSON.parse(phase3ProvenanceRaw.toString("utf8")) as Phase3Provenance;
  const tanaManifestRaw = readFileSync(TANA_MANIFEST_PATH); const tanaManifest = JSON.parse(tanaManifestRaw.toString("utf8")) as TanaManifest;
  const tanaProvenanceRaw = readFileSync(TANA_PROVENANCE_PATH); const tanaProvenance = JSON.parse(tanaProvenanceRaw.toString("utf8")) as TanaProvenance;
  const byKey = new Map<string, ApprovedNode>(); for (const node of approved.nodes) { if (!node.key || byKey.has(node.key)) fail(`duplicate/empty approved node key: ${node.key}`); byKey.set(node.key, node); }
  const expectedSystemKeys = new Set(approved.nodes.map((node) => node.systemKey));
  const dbModule = await import("@/db"); const schema = await import("@/db/schema"); const target = client ?? dbModule.db;
  const semanticRows = await target.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.workspaceId, WORKSPACE_ID), isNull(schema.knowledgeNodes.archivedAt), sql`coalesce(system_key,'') like ${`${SEMANTIC_KEY}:node:%`}`));
  const rowsByKey = new Map<string, any>(); for (const row of semanticRows) { const key = String(row.systemKey).slice(`${SEMANTIC_KEY}:node:`.length); if (rowsByKey.has(key)) fail(`duplicate active semantic node: ${key}`); rowsByKey.set(key, row); }
  const missingSemantic = approved.nodes.filter((node) => !rowsByKey.has(node.key)); const unexpectedSemantic = semanticRows.filter((row: any) => !expectedSystemKeys.has(String(row.systemKey)));
  if (missingSemantic.length || unexpectedSemantic.length) fail(`approved semantic nodes are not applied exactly (missing=${missingSemantic.length}, unexpected=${unexpectedSemantic.length})`);
  const dailyRows = await target.select().from(schema.knowledgeNodes).where(and(eq(schema.knowledgeNodes.workspaceId, WORKSPACE_ID), isNull(schema.knowledgeNodes.archivedAt), sql`day_date is not null`));
  for (const row of dailyRows) { const key = String(row.systemKey ?? "").startsWith(`${SOURCE_KEY}:node:`) ? String(row.systemKey).slice(`${SOURCE_KEY}:node:`.length) : `journal.day.${row.dayDate}`; if (rowsByKey.has(key)) fail(`duplicate Docs owner key: ${key}`); rowsByKey.set(key, row); }
  const phase3Parents = new Map(phase3.placements.map((placement) => [placement.node_key, placement.parent_key]));
  const dailyOwnerFor = (sourceNodeKey: string) => { let cursor: string | null | undefined = sourceNodeKey; const seen = new Set<string>(); while (cursor) { if (seen.has(cursor)) fail(`phase3 hierarchy cycle while resolving attachment: ${sourceNodeKey}`); seen.add(cursor); if (rowsByKey.has(cursor) && !byKey.has(cursor)) return cursor; cursor = phase3Parents.get(cursor); } return null; };
  const sourceAttachments = await target.execute(sql`
    select a.id::text, a.node_id::text, a.file_name, a.file_path, a.mime_type, a.size_bytes, a.attachment_metadata, n.system_key
    from knowledge_attachments a join knowledge_nodes n on n.id=a.node_id
    where n.workspace_id=${WORKSPACE_ID} and n.system_key like ${`${SOURCE_KEY}:node:%`}
    order by n.system_key, a.id
  `) as Json[];
  if (sourceAttachments.length !== EXPECTED_SOURCE_ATTACHMENTS) fail(`source attachment count drift: ${sourceAttachments.length}/${EXPECTED_SOURCE_ATTACHMENTS}`);
  for (const row of sourceAttachments) if (!originalReference(row.attachment_metadata)) fail(`attachment has no original_reference: ${row.id}`);
  const localOwners = new Map<string, Set<string>>(); let remoteImageReferenceCount = 0;
  for (const node of approved.nodes) for (const value of [node.title, node.content]) for (const reference of imageReferences(value)) {
    if (isRemoteImageReference(reference)) { remoteImageReferenceCount++; continue; }
    const owners = localOwners.get(reference) ?? new Set<string>(); owners.add(node.key); localOwners.set(reference, owners);
  }
  const lineageOwnersBySourceKey = new Map<string, ApprovedNode[]>();
  for (const node of approved.nodes) for (const sourceKey of node.sourceKeys ?? []) {
    if (node.sourceKind !== "editable_image") continue;
    lineageOwnersBySourceKey.set(sourceKey, [...(lineageOwnersBySourceKey.get(sourceKey) ?? []), node]);
  }
  const pilotAttachments = sourceAttachments.filter((row) => isPilotSourceNode(String(row.system_key).slice(`${SOURCE_KEY}:node:`.length)));
  const dailyAttachments = sourceAttachments.filter((row) => dailyOwnerFor(String(row.system_key).slice(`${SOURCE_KEY}:node:`.length)) !== null);
  const pilotNodeRows = await target.execute(sql`select id::text,system_key,title,parent_id::text,root_page_id::text,sort_order,aliases,body_json,updated_at from knowledge_nodes where workspace_id=${WORKSPACE_ID} and archived_at is null and system_key like ${`${SOURCE_KEY}:node:${PILOT_SOURCE_KEY}%`} order by system_key,id`) as Json[];
  const pilotFingerprint = sha(canonical({ nodes: pilotNodeRows, attachments: pilotAttachments.map((row) => ({ id: String(row.id), node_id: String(row.node_id), system_key: row.system_key, file_name: row.file_name, file_path: row.file_path, mime_type: row.mime_type, size_bytes: row.size_bytes, attachment_metadata: row.attachment_metadata, file_sha256: existsSync(resolve(String(row.file_path))) ? sha(readFileSync(resolve(String(row.file_path)))) : "missing" })) }));
  const dailyFingerprint = sha(canonical({ nodes: dailyRows.map((row: any) => ({ id: String(row.id), system_key: row.systemKey, title: row.title, body_json: row.bodyJson, updated_at: row.updatedAt })).sort((left: Json, right: Json) => String(left.id).localeCompare(String(right.id))), attachments: dailyAttachments.map((row) => ({ id: String(row.id), node_id: String(row.node_id), system_key: row.system_key, file_name: row.file_name, file_path: row.file_path, mime_type: row.mime_type, size_bytes: row.size_bytes, attachment_metadata: row.attachment_metadata, file_sha256: existsSync(resolve(String(row.file_path))) ? sha(readFileSync(resolve(String(row.file_path)))) : "missing" })) }));
  const activeKeys = new Set(byKey.keys());
  const artifactProvenance = artifactLookups(approved, tanaManifest, tanaProvenance);
  const activeTanaByTitle = new Map<string, Set<string>>();
  for (const node of tanaManifest.nodes) if (activeKeys.has(node.key)) { const title = normalized(node.title); const keys = activeTanaByTitle.get(title) ?? new Set<string>(); keys.add(node.key); activeTanaByTitle.set(title, keys); }
  const documentOwnerFor = (documentKey: string) => { const title = phase3.nodes.find((node) => node.key === documentKey)?.title; if (!title) return null; const candidates = [...(activeTanaByTitle.get(normalized(title)) ?? [])]; if (candidates.length > 1) fail(`source document owner is ambiguous: ${documentKey}:${candidates.join(",")}`); return candidates[0] ?? null; };
  const ownerFailures: string[] = [];
  const attachmentPlans: AttachmentPlan[] = [];
  for (const row of sourceAttachments) {
    const reference = originalReference(row.attachment_metadata); const sourceNodeKey = String(row.system_key).slice(`${SOURCE_KEY}:node:`.length);
    if (isPilotSourceNode(sourceNodeKey) || dailyOwnerFor(sourceNodeKey)) continue;
    const document = sourceDocument(sourceNodeKey, phase3Provenance.sources); if (!document) { ownerFailures.push(`${sourceNodeKey}: source provenance missing`); continue; }
    const lineageOwners = (lineageOwnersBySourceKey.get(sourceNodeKey) ?? []).filter((node) => localImageReferences(node.content).includes(reference));
    if (lineageOwners.length > 1) { ownerFailures.push(`${sourceNodeKey}: source-lineage image owners=${lineageOwners.map((node) => node.key).join(",")}`); continue; }
    const targetKey = lineageOwners[0]?.key ?? dailyOwnerFor(sourceNodeKey) ?? documentOwnerFor(document.node_key);
    if (!targetKey) { const referenceOwners = [...(localOwners.get(reference) ?? [])].map((key) => { const node = byKey.get(key); return `${key}[${node?.sourceKind ?? ""}:${(node?.sourceKeys ?? []).join("|")}]`; }); ownerFailures.push(`${sourceNodeKey}: no artifact-grounded owner; reference=${reference}; content-owners=${referenceOwners.join(",") || "missing"}`); continue; }
    const targetRow = rowsByKey.get(targetKey); if (!targetRow) fail(`semantic image target missing in DB: ${targetKey}`);
    const sourceFilePath = resolve(String(row.file_path)); if (!existsSync(sourceFilePath) || !statSync(sourceFilePath).isFile()) fail(`source attachment file is missing: ${sourceFilePath}`);
    const targetAttachmentId = stableId(`${MIGRATION_KEY}:attachment:${row.id}:${targetRow.id}`);
    const targetFilePath = resolve(SEMANTIC_MEDIA_ROOT, targetAttachmentId, basename(String(row.file_name)));
    if (!targetFilePath.startsWith(`${SEMANTIC_MEDIA_ROOT}\\`)) fail(`unsafe semantic attachment path: ${targetFilePath}`);
    attachmentPlans.push({ source_attachment_id: String(row.id), source_node_key: sourceNodeKey, target_node_key: targetKey, target_node_id: String(targetRow.id), target_attachment_id: targetAttachmentId, original_reference: reference, file_name: basename(String(row.file_name)), source_file_path: sourceFilePath, target_file_path: targetFilePath, file_sha256: sha(readFileSync(sourceFilePath)), mime_type: row.mime_type == null ? null : String(row.mime_type), size_bytes: row.size_bytes == null ? null : Number(row.size_bytes) });
  }
  if (pilotAttachments.length !== EXPECTED_PILOT_ATTACHMENTS || dailyAttachments.length !== EXPECTED_DAILY_ATTACHMENTS || ownerFailures.length || attachmentPlans.length !== EXPECTED_SEMANTIC_ATTACHMENTS) fail(`attachment owner resolution failed (pilot=${pilotAttachments.length}/${EXPECTED_PILOT_ATTACHMENTS}, daily=${dailyAttachments.length}/${EXPECTED_DAILY_ATTACHMENTS}, failures=${ownerFailures.length}, planned=${attachmentPlans.length}/${EXPECTED_SEMANTIC_ATTACHMENTS}):\n${ownerFailures.join("\n")}`);
  const lineageOwnerByReference = new Map(attachmentPlans.map((item) => [item.original_reference, item.target_node_key]));
  const imageCleanupPlans: ImageCleanupPlan[] = [];
  for (const [reference, owners] of localOwners) for (const owner of owners) {
    if (owner === lineageOwnerByReference.get(reference)) continue;
    const existing = imageCleanupPlans.find((item) => item.target_node_key === owner);
    if (existing) existing.references.push(reference); else imageCleanupPlans.push({ target_node_key: owner, references: [reference] });
  }
  for (const item of imageCleanupPlans) item.references = [...new Set(item.references)].sort();

  const lookup = new Map<string, Set<string>>();
  for (const node of approved.nodes) { addLookup(lookup, node.title, node.key); addLookup(lookup, node.key, node.key); for (const alias of node.aliases ?? []) addLookup(lookup, alias, node.key); const row = rowsByKey.get(node.key); for (const alias of row?.aliases ?? []) addLookup(lookup, alias, node.key); if (row?.dayDate) addLookup(lookup, String(row.dayDate), node.key); }
  const artifacts = artifactProvenance;
  for (const [term, keys] of artifacts.lookup) for (const key of keys) addLookup(lookup, term, key);
  const linkPlans: LinkPlan[] = [];
  const linkCleanupPlans: LinkCleanupPlan[] = [];
  for (const node of approved.nodes) {
    const row = rowsByKey.get(node.key); if (!row) continue;
    const unique = new Map<string, { raw: string; target: string; label: string }>(); for (const link of [...rawWikilinks(node.title), ...rawWikilinks(node.content)]) unique.set(`${link.raw}\0${link.target}\0${link.label}`, link);
    for (const link of unique.values()) {
      const targetCandidates = new Set(lookup.get(normalized(link.target)) ?? []); targetCandidates.delete(node.key);
      if (targetCandidates.size > 1) fail(`wikilink target is ambiguous: ${node.key}:${link.raw} (${[...targetCandidates].join(",")})`);
      const candidates = targetCandidates.size === 1 ? targetCandidates : new Set(lookup.get(normalized(link.label)) ?? []); candidates.delete(node.key);
      if (candidates.size > 1) fail(`wikilink label is ambiguous: ${node.key}:${link.raw} (${[...candidates].join(",")})`);
      if (candidates.size === 0 && node.key.startsWith("exactblock.")) {
        const collection = nearestActiveAncestor(artifacts.parents.get(node.key) ?? "", artifacts.parents, activeKeys);
        if (collection && collection !== node.key) { candidates.clear(); candidates.add(collection); }
      }
      if (candidates.size === 0) { linkCleanupPlans.push({ source_node_key: node.key, raw: link.raw, label: link.label }); continue; }
      if (candidates.size !== 1) fail(`wikilink target unresolved/ambiguous: ${node.key}:${link.raw} (${[...candidates].join(",")})`);
      const targetKey = [...candidates][0]; const targetRow = rowsByKey.get(targetKey); if (!targetRow) fail(`wikilink target is not active: ${targetKey}`);
      linkPlans.push({ source_node_key: node.key, source_node_id: String(row.id), raw: link.raw, target: link.target, label: link.label, target_node_key: targetKey, target_node_id: String(targetRow.id) });
    }
  }
  const fingerprintRows = [...semanticRows, ...dailyRows].map((row: any) => ({ id: String(row.id), system_key: row.systemKey, title: row.title, aliases: row.aliases, body_json: row.bodyJson, updated_at: row.updatedAt })).sort((left, right) => left.id.localeCompare(right.id));
  const plan: Plan = { schema_version: "docs-semantic-media-links-plan/v2", approved_sha256: sha(approvedRaw), phase3_sha256: sha(phase3Raw), phase3_provenance_sha256: sha(phase3ProvenanceRaw), tana_manifest_sha256: sha(tanaManifestRaw), tana_provenance_sha256: sha(tanaProvenanceRaw), database_fingerprint: sha(canonical(fingerprintRows)), source_attachment_count: sourceAttachments.length, local_image_reference_count: [...localOwners.values()].reduce((count, owners) => count + owners.size, 0), remote_image_reference_count: remoteImageReferenceCount, pilot_attachment_count: pilotAttachments.length, pilot_fingerprint: pilotFingerprint, daily_attachment_count: dailyAttachments.length, daily_fingerprint: dailyFingerprint, attachment_plans: attachmentPlans, image_cleanup_plans: imageCleanupPlans, link_plans: linkPlans, link_cleanup_plans: linkCleanupPlans, node_patch_count: new Set([...attachmentPlans.map((item) => item.target_node_key), ...imageCleanupPlans.map((item) => item.target_node_key), ...linkPlans.map((item) => item.source_node_key), ...linkCleanupPlans.map((item) => item.source_node_key)]).size };
  return { db: target, schema, plan, rowsByKey, approvedByKey: byKey };
}

function planHash(plan: Plan) { return sha(canonical(plan)); }
async function audit() { const { plan, approvedByKey } = await snapshot(); mkdirSync(RUN_ROOT, { recursive: true }); const path = resolve(RUN_ROOT, "latest-audit-plan.json"); writeJson(path, plan); const patchKeys = new Set([...plan.attachment_plans.map((item) => item.target_node_key), ...plan.image_cleanup_plans.map((item) => item.target_node_key), ...plan.link_plans.map((item) => item.source_node_key), ...plan.link_cleanup_plans.map((item) => item.source_node_key)]); const dailyPatches = [...patchKeys].filter((key) => !approvedByKey.has(key)).length; console.log(JSON.stringify({ plan: path, plan_sha256: planHash(plan), source_attachments: plan.source_attachment_count, semantic_copies: plan.attachment_plans.length, pilot_preserved: plan.pilot_attachment_count, daily_preserved: plan.daily_attachment_count, remote_images_preserved: plan.remote_image_reference_count, wikilinks: plan.link_plans.length, unresolved_wikilinks_flattened: plan.link_cleanup_plans.length, node_patches: plan.node_patch_count, daily_node_patches: dailyPatches }, null, 2)); }
async function backup() { const { plan } = await snapshot(); const directory = resolve(RUN_ROOT, new Date().toISOString().replace(/[:.]/g, "-")); mkdirSync(directory, { recursive: true }); const dump = resolve(directory, "database-before-semantic-media-links.dump"); const dumpSha = dumpDatabase(dump); const receipt = { schema_version: "docs-semantic-media-links-backup/v2", plan, plan_sha256: planHash(plan), database_dump: { file: basename(dump), sha256: dumpSha } }; const path = resolve(directory, "backup.json"); writeJson(path, receipt); console.log(JSON.stringify({ backup: path, dump, plan_sha256: receipt.plan_sha256, dump_sha256: dumpSha }, null, 2)); }

export function replaceMediaAndLinks(value: string, attachments: AttachmentPlan[], links: Array<Pick<LinkPlan, "raw" | "label"> & { target_node_id?: string }>, cleanupReferences: string[] = []) {
  const references = new Set([...attachments.map((item) => item.original_reference), ...cleanupReferences]);
  let next = value.replace(IMAGE_RE, (raw, _alt: string, rawReference: string) => { const reference = rawReference.trim().replace(/^<|>$/g, "").replace(/\\/g, "/"); return !isRemoteImageReference(reference) && references.has(reference) ? "" : raw; });
  for (const link of links) next = next.replaceAll(link.raw, link.target_node_id ? `[[node:${link.target_node_id}|${link.label.replace(/[\]|\n]/g, " ")}]]` : link.label);
  return next;
}
function webUrls(value: string) { return [...value.matchAll(/https?:\/\/[^\s<>"']+/giu)].map((match) => match[0]); }
function assertUrlsUnchanged(before: string, after: string, nodeKey: string) { if (canonical(webUrls(before)) !== canonical(webUrls(after))) fail(`URL changed while converting media/link syntax: ${nodeKey}`); }
function searchableBody(node: ApprovedNode, content: unknown = node.content) { return [typeof content === "string" ? content : undefined, ...(node.fields ?? []).map((field) => `${field.name}: ${field.value}`)].filter((value): value is string => typeof value === "string" && value.length > 0).join("\n"); }
async function searchableBodyFromStoredFields(client: any, schema: any, nodeId: string, content: unknown) { const fields = await client.select({ name: schema.knowledgeFields.name, valueText: schema.knowledgeFieldValues.valueText }).from(schema.knowledgeFieldValues).innerJoin(schema.knowledgeFields, eq(schema.knowledgeFieldValues.fieldId, schema.knowledgeFields.id)).where(eq(schema.knowledgeFieldValues.nodeId, nodeId)); const lines = fields.map((field: any) => `${field.name}: ${field.valueText ?? ""}`).sort(); return [typeof content === "string" ? content : undefined, ...lines].filter((value): value is string => typeof value === "string" && value.length > 0).join("\n"); }
function copySemanticAttachment(item: AttachmentPlan) { const sourceHash = sha(readFileSync(item.source_file_path)); if (sourceHash !== item.file_sha256) fail(`source attachment changed after audit: ${item.source_file_path}`); mkdirSync(dirname(item.target_file_path), { recursive: true }); const created = !existsSync(item.target_file_path); if (created) copyFileSync(item.source_file_path, item.target_file_path); if (sha(readFileSync(item.target_file_path)) !== item.file_sha256) fail(`semantic attachment copy checksum mismatch: ${item.target_file_path}`); return created; }

async function apply() {
  const backupPath = arg("--backup"); const confirmed = arg("--confirm-plan-sha256"); if (!backupPath || !confirmed) fail("apply requires --backup and --confirm-plan-sha256");
  const receipt = readJson<Json>(resolve(backupPath)); const before = await snapshot(); if (receipt.schema_version !== "docs-semantic-media-links-backup/v2" || receipt.plan_sha256 !== confirmed || planHash(before.plan) !== confirmed) fail("backup/plan mismatch"); const dump = resolve(dirname(resolve(backupPath)), String(receipt.database_dump.file)); if (sha(readFileSync(dump)) !== receipt.database_dump.sha256) fail("backup dump checksum mismatch");
  const createdFiles: string[] = [];
  try { for (const item of before.plan.attachment_plans) if (copySemanticAttachment(item)) createdFiles.push(item.target_file_path); }
  catch (error) { for (const path of createdFiles.reverse()) rmSync(path, { force: true }); throw error; }
  const { db, schema } = before; const { decryptDocsNodeBodyJson, updateDocsNode } = await import("@/lib/server/docs-node-writer"); const { appendKnowledgeRevision, upsertKnowledgeSearchIndex } = await import("@/lib/server/knowledge-docs-utils"); let verification: Json | null = null;
  try { await db.transaction(async (tx: any) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`${WORKSPACE_ID}:${MIGRATION_KEY}`}))`);
    await tx.execute(sql`select id from knowledge_nodes where workspace_id=${WORKSPACE_ID} and archived_at is null and (system_key like ${`${SEMANTIC_KEY}:node:%`} or day_date is not null) order by id for update`);
    await tx.execute(sql`select id from knowledge_nodes where workspace_id=${WORKSPACE_ID} and archived_at is null and system_key like ${`${SOURCE_KEY}:node:${PILOT_SOURCE_KEY}%`} order by id for share`);
    await tx.execute(sql`select a.id from knowledge_attachments a join knowledge_nodes n on n.id=a.node_id where n.workspace_id=${WORKSPACE_ID} and n.system_key like ${`${SOURCE_KEY}:node:%`} order by a.id for share`);
    const live = await snapshot(tx); if (planHash(live.plan) !== confirmed) fail("Docs or source attachments changed after backup");
    const attachmentsByNode = new Map<string, AttachmentPlan[]>(); for (const item of live.plan.attachment_plans) attachmentsByNode.set(item.target_node_key, [...(attachmentsByNode.get(item.target_node_key) ?? []), item]);
    const cleanupByNode = new Map(live.plan.image_cleanup_plans.map((item) => [item.target_node_key, item.references]));
    const linksByNode = new Map<string, Array<Pick<LinkPlan, "raw" | "label"> & { target_node_id?: string }>>(); for (const item of live.plan.link_plans) linksByNode.set(item.source_node_key, [...(linksByNode.get(item.source_node_key) ?? []), item]); for (const item of live.plan.link_cleanup_plans) linksByNode.set(item.source_node_key, [...(linksByNode.get(item.source_node_key) ?? []), item]);
    for (const key of new Set([...attachmentsByNode.keys(), ...cleanupByNode.keys(), ...linksByNode.keys()])) {
      const row = live.rowsByKey.get(key); if (!row) fail(`node disappeared during apply: ${key}`); const body = decryptDocsNodeBodyJson(row.bodyJson) as Json;
      const attachments = attachmentsByNode.get(key) ?? []; const links = linksByNode.get(key) ?? []; const cleanupReferences = cleanupByNode.get(key) ?? [];
      const originalTitle = String(row.title ?? ""); const convertedTitle = replaceMediaAndLinks(originalTitle, attachments, links, cleanupReferences); assertUrlsUnchanged(originalTitle, convertedTitle, key);
      const title = convertedTitle.trim() ? convertedTitle : (attachments[0]?.file_name ?? row.title);
      const nextBody = { ...body, format: "doc_block", attachment_ids: [...new Set([...(Array.isArray(body.attachment_ids) ? body.attachment_ids.map(String) : []), ...attachments.map((item) => item.target_attachment_id)])] } as Json;
      if (typeof body.verbatim_content === "string") { nextBody.verbatim_content = replaceMediaAndLinks(body.verbatim_content, attachments, links, cleanupReferences); assertUrlsUnchanged(body.verbatim_content, nextBody.verbatim_content, key); }
      const approvedNode = live.approvedByKey.get(key); const searchBody = approvedNode ? searchableBody(approvedNode, nextBody.verbatim_content) : await searchableBodyFromStoredFields(tx, schema, String(row.id), nextBody.verbatim_content);
      const updated = await updateDocsNode(tx, row.id, { title, bodyJson: nextBody, updatedBy: OWNER_ID }); await upsertKnowledgeSearchIndex(tx, updated, searchBody); await appendKnowledgeRevision(tx, updated, OWNER_ID, "画像添付とwikilinkをDocs参照へ変換", []);
    }
    for (const item of live.plan.attachment_plans) await tx.insert(schema.knowledgeAttachments).values({ id: item.target_attachment_id, nodeId: item.target_node_id, fileName: item.file_name, filePath: item.target_file_path, mimeType: item.mime_type, sizeBytes: item.size_bytes, attachmentMetadata: { migration_key: MIGRATION_KEY, source_attachment_id: item.source_attachment_id, source_node_key: item.source_node_key, original_reference: item.original_reference, file_sha256: item.file_sha256, editable_block: true }, createdBy: OWNER_ID }).onConflictDoUpdate({ target: schema.knowledgeAttachments.id, set: { nodeId: item.target_node_id, fileName: item.file_name, filePath: item.target_file_path, mimeType: item.mime_type, sizeBytes: item.size_bytes, attachmentMetadata: { migration_key: MIGRATION_KEY, source_attachment_id: item.source_attachment_id, source_node_key: item.source_node_key, original_reference: item.original_reference, file_sha256: item.file_sha256, editable_block: true } } });
    for (const item of live.plan.link_plans) await tx.insert(schema.knowledgeEdges).values({ id: stableId(`${MIGRATION_KEY}:edge:${item.source_node_id}:${item.target_node_id}`), sourceNodeId: item.source_node_id, targetNodeId: item.target_node_id, relationType: "inline_ref", confidence: 1, createdBy: OWNER_ID }).onConflictDoUpdate({ target: schema.knowledgeEdges.id, set: { sourceNodeId: item.source_node_id, targetNodeId: item.target_node_id, relationType: "inline_ref", confidence: 1 } });
    const attachmentIds = live.plan.attachment_plans.map((item) => item.target_attachment_id).sort();
    const edgeIds = [...new Set(live.plan.link_plans.map((item) => stableId(`${MIGRATION_KEY}:edge:${item.source_node_id}:${item.target_node_id}`)))].sort();
    await tx.insert(schema.knowledgeImportJobs).values({ id: MIGRATION_JOB_ID, workspaceId: WORKSPACE_ID, projectId: null, sourceType: "docs_semantic_media_links", sourceName: "Docs semantic media/link reconciliation v2", status: "completed", optionsJson: { migration_key: MIGRATION_KEY, approved_sha256: live.plan.approved_sha256 }, summaryJson: { attachments: live.plan.attachment_plans.length, wikilinks: live.plan.link_plans.length, attachment_ids: attachmentIds, edge_ids: edgeIds }, createdBy: OWNER_ID }).onConflictDoUpdate({ target: schema.knowledgeImportJobs.id, set: { status: "completed", optionsJson: { migration_key: MIGRATION_KEY, approved_sha256: live.plan.approved_sha256 }, summaryJson: { attachments: live.plan.attachment_plans.length, wikilinks: live.plan.link_plans.length, attachment_ids: attachmentIds, edge_ids: edgeIds }, updatedAt: new Date() } });
    verification = await verify(live.plan, tx);
  }); } catch (error) { for (const path of createdFiles.reverse()) rmSync(path, { force: true }); throw error; }
  console.log(JSON.stringify({ status: "completed", verification }, null, 2));
}

async function verify(expectedPlan?: Plan, client?: any) {
  const { db, schema, plan, rowsByKey, approvedByKey } = await snapshot(client); const targetPlan = expectedPlan ?? plan;
  const attachmentRows = await db.execute(sql`select id::text,node_id::text,file_name,file_path,mime_type,size_bytes,attachment_metadata,created_by::text from knowledge_attachments where attachment_metadata->>'migration_key'=${MIGRATION_KEY} order by id`) as Json[];
  const expectedAttachments = new Map(targetPlan.attachment_plans.map((item) => [item.target_attachment_id, item])); let attachmentMismatch = 0;
  for (const row of attachmentRows) { const item = expectedAttachments.get(String(row.id)); if (!item || String(row.node_id) !== item.target_node_id || String(row.file_name) !== item.file_name || String(row.file_path) !== item.target_file_path || (row.mime_type == null ? null : String(row.mime_type)) !== item.mime_type || (row.size_bytes == null ? null : Number(row.size_bytes)) !== item.size_bytes || String(row.created_by) !== OWNER_ID || String(row.attachment_metadata?.migration_key) !== MIGRATION_KEY || String(row.attachment_metadata?.source_attachment_id) !== item.source_attachment_id || String(row.attachment_metadata?.source_node_key) !== item.source_node_key || String(row.attachment_metadata?.original_reference) !== item.original_reference || String(row.attachment_metadata?.file_sha256) !== item.file_sha256 || row.attachment_metadata?.editable_block !== true || !existsSync(item.target_file_path) || sha(readFileSync(item.target_file_path)) !== item.file_sha256) attachmentMismatch++; }
  const missingAttachments = [...expectedAttachments.keys()].filter((id) => !attachmentRows.some((row) => String(row.id) === id));
  const expectedEdgeIdList = targetPlan.link_plans.map((item) => stableId(`${MIGRATION_KEY}:edge:${item.source_node_id}:${item.target_node_id}`));
  const edgeRows = expectedEdgeIdList.length ? await db.select().from(schema.knowledgeEdges).where(inArray(schema.knowledgeEdges.id, [...new Set(expectedEdgeIdList)])) : [];
  const expectedEdges = new Map(targetPlan.link_plans.map((item) => [stableId(`${MIGRATION_KEY}:edge:${item.source_node_id}:${item.target_node_id}`), item])); let edgeMismatch = 0;
  for (const row of edgeRows) { const item = expectedEdges.get(String(row.id)); if (!item || String(row.sourceNodeId) !== item.source_node_id || String(row.targetNodeId) !== item.target_node_id || row.relationType !== "inline_ref" || Number(row.confidence) !== 1 || String(row.createdBy) !== OWNER_ID) edgeMismatch++; }
  const missingEdges = [...expectedEdges.keys()].filter((id) => !edgeRows.some((row: any) => String(row.id) === id));
  const { decryptDocsNodeBodyJson } = await import("@/lib/server/docs-node-writer"); let localMarkdownImages = 0; const localMarkdownImageExamples: Json[] = []; let remoteMarkdownImages = 0; let rawWikilinksRemaining = 0; let bodyAttachmentMismatch = 0; let searchIndexMismatch = 0;
  const patchedKeys = new Set([...targetPlan.attachment_plans.map((item) => item.target_node_key), ...targetPlan.image_cleanup_plans.map((item) => item.target_node_key), ...targetPlan.link_plans.map((item) => item.source_node_key), ...targetPlan.link_cleanup_plans.map((item) => item.source_node_key)]); const patchedIds = [...patchedKeys].map((key) => rowsByKey.get(key)?.id).filter(Boolean).map(String); const searchRows = patchedIds.length ? await db.select().from(schema.knowledgeSearchIndex).where(inArray(schema.knowledgeSearchIndex.nodeId, patchedIds)) : []; const searchByNodeId = new Map<string, any>(searchRows.map((row: any) => [String(row.nodeId), row]));
  const expectedIdsByNode = new Map<string, Set<string>>(); for (const item of targetPlan.attachment_plans) { const set = expectedIdsByNode.get(item.target_node_key) ?? new Set<string>(); set.add(item.target_attachment_id); expectedIdsByNode.set(item.target_node_key, set); }
  for (const [key, row] of rowsByKey) { const body = decryptDocsNodeBodyJson(row.bodyJson) as Json; if (approvedByKey.has(key)) for (const value of [row.title, body.verbatim_content]) { const localReferences = localImageReferences(value); localMarkdownImages += localReferences.length; if (localReferences.length) localMarkdownImageExamples.push({ key, references: localReferences }); remoteMarkdownImages += imageReferences(value).filter(isRemoteImageReference).length; rawWikilinksRemaining += rawWikilinks(value).length; } const actual = new Set(Array.isArray(body.attachment_ids) ? body.attachment_ids.map(String) : []); for (const id of expectedIdsByNode.get(key) ?? []) if (!actual.has(id)) bodyAttachmentMismatch++; if (patchedKeys.has(key)) { const approvedNode = approvedByKey.get(key); const expectedSearchBody = approvedNode ? searchableBody(approvedNode, body.verbatim_content) : await searchableBodyFromStoredFields(db, schema, String(row.id), body.verbatim_content); const search = searchByNodeId.get(String(row.id)); if (!search || search.titleText !== row.title || search.bodyTextPlain !== expectedSearchBody) searchIndexMismatch++; } }
  const registryRows = await db.execute(sql`select status,options_json,summary_json from knowledge_import_jobs where id=${MIGRATION_JOB_ID}`) as Json[];
  const registry = registryRows[0];
  const registryAttachmentIds = Array.isArray(registry?.summary_json?.attachment_ids) ? registry.summary_json.attachment_ids.map(String).sort() : [];
  const registryEdgeIds = Array.isArray(registry?.summary_json?.edge_ids) ? registry.summary_json.edge_ids.map(String).sort() : [];
  const registryMismatch = registryRows.length !== 1 || registry?.status !== "completed" || registry?.options_json?.migration_key !== MIGRATION_KEY || registry?.options_json?.approved_sha256 !== targetPlan.approved_sha256 || canonical(registryAttachmentIds) !== canonical([...expectedAttachments.keys()].sort()) || canonical(registryEdgeIds) !== canonical([...expectedEdges.keys()].sort());
  const pilotUnchanged = plan.pilot_attachment_count === targetPlan.pilot_attachment_count && plan.pilot_fingerprint === targetPlan.pilot_fingerprint; const dailyUnchanged = plan.daily_attachment_count === targetPlan.daily_attachment_count && plan.daily_fingerprint === targetPlan.daily_fingerprint;
  const result = { source_attachment_count: plan.source_attachment_count, semantic_copy_count: targetPlan.attachment_plans.length, pilot_attachment_count: plan.pilot_attachment_count, pilot_unchanged: pilotUnchanged, daily_attachment_count: plan.daily_attachment_count, daily_unchanged: dailyUnchanged, expected_attachment_links: targetPlan.attachment_plans.length, stored_attachment_links: attachmentRows.length, missing_attachments: missingAttachments.length, attachment_mismatch: attachmentMismatch, expected_wikilinks: targetPlan.link_plans.length, stored_edges: edgeRows.length, missing_edges: missingEdges.length, edge_mismatch: edgeMismatch, registry_mismatch: registryMismatch, body_attachment_mismatch: bodyAttachmentMismatch, search_index_mismatch: searchIndexMismatch, local_markdown_images_remaining: localMarkdownImages, local_markdown_image_examples: localMarkdownImageExamples.slice(0, 10), remote_markdown_images_preserved: remoteMarkdownImages, raw_wikilinks_remaining: rawWikilinksRemaining };
  if (plan.source_attachment_count !== EXPECTED_SOURCE_ATTACHMENTS || targetPlan.attachment_plans.length !== EXPECTED_SEMANTIC_ATTACHMENTS || attachmentRows.length !== expectedAttachments.size || plan.pilot_attachment_count !== EXPECTED_PILOT_ATTACHMENTS || plan.daily_attachment_count !== EXPECTED_DAILY_ATTACHMENTS || !pilotUnchanged || !dailyUnchanged || missingAttachments.length || attachmentMismatch || edgeRows.length !== expectedEdges.size || missingEdges.length || edgeMismatch || registryMismatch || bodyAttachmentMismatch || searchIndexMismatch || localMarkdownImages || remoteMarkdownImages !== targetPlan.remote_image_reference_count || rawWikilinksRemaining) fail(`semantic media/link verification failed: ${JSON.stringify(result)}`); return result;
}

async function main() { loadEnv(); const command = process.argv[2] ?? "audit"; if (["-h", "--help"].includes(command)) return console.log("usage: reconcile_semantic_docs_media_links.ts <audit|backup|apply|verify>"); if (command === "audit") return audit(); if (command === "backup") return backup(); if (command === "apply") return apply(); if (command === "verify") return console.log(JSON.stringify(await verify(), null, 2)); fail(`unknown command: ${command}`); }
if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) main().then(() => process.exit(0)).catch((error) => { console.error(error instanceof Error ? error.stack ?? error.message : error); process.exit(1); });
