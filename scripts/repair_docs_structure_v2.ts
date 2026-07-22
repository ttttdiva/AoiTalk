/**
 * Docsの読み取り専用本文・親子同名holder・画像生成直下の複合名を、
 * 現在DBを維持したまま通常の編集可能nodeへ修復する。
 *
 * Usage:
 *   frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_structure_v2.ts audit
 *   frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_structure_v2.ts backup
 *   frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_structure_v2.ts apply --backup <dump>
 *   frontend/node_modules/.bin/tsx --tsconfig frontend/tsconfig.json scripts/repair_docs_structure_v2.ts verify
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { and, asc, eq, inArray, isNull, ne, or, sql } from "../frontend/node_modules/drizzle-orm/index.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WORKSPACE_ID = "3e73f8c6-5870-4902-821d-241d3653c4ea";
const OWNER_ID = "118eb287-43cf-43aa-bb10-c8cabaf21b0f";
const RUN_ROOT = resolve(ROOT, "artifacts", "foam_curation", "structure_repair_v2");
const PLAN_PATH = resolve(RUN_ROOT, "latest-plan.json");
const MAX_TITLE = 19_000;

type Json = Record<string, any>;
type NodeRow = {
  id: string;
  workspaceId: string;
  parentId: string | null;
  rootPageId: string | null;
  projectId: string | null;
  systemKey: string | null;
  title: string;
  bodyJson: unknown;
  nodeType: string | null;
  sortOrder: number | null;
  createdBy: string | null;
  updatedBy: string | null;
  archivedAt: Date | null;
};
type VerbatimTarget = { id: string; contentSha256: string; segmentCount: number };
type DuplicateTarget = { childId: string; parentId: string };
type SplitTarget = { id: string; originalTitle: string; baseTitle: string; suffix: string | null };
type RepairPlan = {
  schema_version: "docs-structure-repair/v2";
  workspace_id: string;
  fingerprint: string;
  image_root_id: string;
  verbatim: VerbatimTarget[];
  duplicates: DuplicateTarget[];
  image_heading_ids: string[];
  direct_splits: SplitTarget[];
  counts: Record<string, number>;
};
type BackupReceipt = {
  schema_version: "docs-structure-repair-backup/v1";
  dump_path: string;
  dump_sha256: string;
  plan_sha256: string;
  fingerprint: string;
};

const sha256 = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
const ALLOWED_EMPTY_HOLDER_BODY_KEYS = new Set(["format", "block_type"]);
function holderHasOwnContent(row: { bodyJson: unknown; description?: string | null; aliases?: unknown }, decrypt: (value: unknown) => Json) {
  const body = decrypt(row.bodyJson);
  return Object.keys(body).some((key) => !ALLOWED_EMPTY_HOLDER_BODY_KEYS.has(key))
    || (typeof row.description === "string" && row.description.trim().length > 0)
    || (Array.isArray(row.aliases) && row.aliases.length > 0);
}
function stableId(value: string) {
  const chars = sha256(value).slice(0, 32).split("");
  chars[12] = "4";
  chars[16] = ((parseInt(chars[16], 16) & 3) | 8).toString(16);
  const valueHex = chars.join("");
  return `${valueHex.slice(0, 8)}-${valueHex.slice(8, 12)}-${valueHex.slice(12, 16)}-${valueHex.slice(16, 20)}-${valueHex.slice(20)}`;
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Json).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}
function actionablePlan(plan: RepairPlan) {
  return {
    schema_version: plan.schema_version,
    workspace_id: plan.workspace_id,
    fingerprint: plan.fingerprint,
    image_root_id: plan.image_root_id,
    verbatim: plan.verbatim,
    duplicates: plan.duplicates,
    image_heading_ids: plan.image_heading_ids,
    direct_splits: plan.direct_splits,
  };
}
function loadEnv() {
  for (const envPath of [resolve(ROOT, ".env"), resolve(ROOT, "frontend", ".env")]) {
    if (!existsSync(envPath)) continue;
    for (const line of readFileSync(envPath, "utf8").split(/\r?\n/)) {
      const match = line.match(/^\s*([^#=]+)=(.*)$/);
      if (!match) continue;
      const key = match[1].trim();
      if (key in process.env) continue;
      process.env[key] = match[2].trim().replace(/^['"]|['"]$/g, "");
    }
  }
  if (process.env.DATABASE_URL?.startsWith("postgresql+asyncpg://")) {
    process.env.DATABASE_URL = process.env.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://");
  }
}
function dbEnv() {
  const url = process.env.DATABASE_URL ? new URL(process.env.DATABASE_URL) : null;
  return {
    ...process.env,
    PGHOST: process.env.POSTGRES_HOST || url?.hostname || "127.0.0.1",
    PGPORT: process.env.POSTGRES_PORT || url?.port || "5432",
    PGDATABASE: process.env.POSTGRES_DB || url?.pathname.slice(1) || "aoitalk_memory",
    PGUSER: process.env.POSTGRES_USER || (url ? decodeURIComponent(url.username) : "aoitalk"),
    PGPASSWORD: process.env.POSTGRES_PASSWORD || (url ? decodeURIComponent(url.password) : ""),
  };
}
function writeJson(path: string, value: unknown) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}
function readJson<T>(path: string): T { return JSON.parse(readFileSync(path, "utf8")) as T; }
function receiptPath(dumpPath: string) { return `${dumpPath}.receipt.json`; }
function arg(name: string) { const index = process.argv.indexOf(name); return index < 0 ? null : process.argv[index + 1] ?? null; }
function dumpDatabase(path: string) {
  const exe = existsSync("C:/Program Files/PostgreSQL/16/bin/pg_dump.exe")
    ? "C:/Program Files/PostgreSQL/16/bin/pg_dump.exe"
    : "pg_dump";
  const result = spawnSync(exe, ["--format=custom", "--file", path], { env: dbEnv(), encoding: "utf8" });
  if (result.status !== 0) throw new Error(`pg_dump failed: ${result.stderr || result.stdout}`);
}

function editableSegments(content: string) {
  if (!/[\r\n]/.test(content)) return content.length <= MAX_TITLE
    ? [{ text: content, lineIndex: 0, lineCount: 1, chunkIndex: 0, chunkCount: 1 }]
    : Array.from({ length: Math.ceil(content.length / MAX_TITLE) }, (_, chunkIndex, all) => ({
        text: content.slice(chunkIndex * MAX_TITLE, (chunkIndex + 1) * MAX_TITLE),
        lineIndex: 0,
        lineCount: 1,
        chunkIndex,
        chunkCount: all.length,
      }));
  const lines = content.split(/\r\n|\n|\r/);
  return lines.flatMap((line, lineIndex) => {
    if (line.length === 0) return [];
    const chunkCount = Math.ceil(line.length / MAX_TITLE);
    return Array.from({ length: chunkCount }, (_, chunkIndex) => ({
      text: line.slice(chunkIndex * MAX_TITLE, (chunkIndex + 1) * MAX_TITLE),
      lineIndex,
      lineCount: lines.length,
      chunkIndex,
      chunkCount,
    }));
  });
}

function splitDirectTitle(title: string): { baseTitle: string; suffix: string | null } | null {
  const link = title.match(/^\[\[node:[^|]+\|([^\]]+)\]\]$/);
  if (link) return { baseTitle: link[1] === "プロンプト" ? "プロンプト倉庫" : link[1].trim(), suffix: null };
  if (/^AI[｜|]controlnet$/i.test(title.trim())) return { baseTitle: "ControlNet", suffix: null };
  const delimiters = [" - ", "｜", "|"];
  for (const delimiter of delimiters) {
    const index = title.indexOf(delimiter);
    if (index <= 0) continue;
    const baseTitle = title.slice(0, index).trim();
    const suffix = title.slice(index + delimiter.length).trim();
    if (baseTitle && suffix) return { baseTitle, suffix };
  }
  return null;
}

async function buildPlan(): Promise<RepairPlan> {
  const { db } = await import("@/db");
  const schema = await import("@/db/schema");
  const { knowledgeNodes } = schema;
  const { decryptDocsNodeBodyJson } = await import("@/lib/server/docs-node-writer");
  const [imageRoot] = await db.select().from(knowledgeNodes).where(and(
    eq(knowledgeNodes.workspaceId, WORKSPACE_ID),
    eq(knowledgeNodes.title, "画像生成"),
    eq(knowledgeNodes.nodeType, "page"),
    isNull(knowledgeNodes.archivedAt),
  ));
  if (!imageRoot) throw new Error("画像生成 root is missing");
  const imageIdRows = await db.execute(sql`
    with recursive descendants as (
      select id from knowledge_nodes where id = ${imageRoot.id}
      union all
      select child.id
      from knowledge_nodes child
      join descendants parent on child.parent_id = parent.id
      where child.archived_at is null
    )
    select id::text from descendants order by id
  `) as Array<{ id: string }>;
  const imageRows = await db.select().from(knowledgeNodes).where(
    inArray(knowledgeNodes.id, imageIdRows.map((row) => row.id)),
  ).orderBy(asc(knowledgeNodes.id)) as NodeRow[];
  const duplicateRows = await db.execute(sql`
    select child.id::text as child_id, parent.id::text as parent_id
    from knowledge_nodes child
    join knowledge_nodes parent on parent.id = child.parent_id
    where child.workspace_id = ${WORKSPACE_ID}
      and child.archived_at is null
      and parent.archived_at is null
      and btrim(child.title) = btrim(parent.title)
    order by child.id
  `) as Array<{ child_id: string; parent_id: string }>;
  const activeCountRows = await db.execute(sql`
    select count(*)::int as count
    from knowledge_nodes
    where workspace_id = ${WORKSPACE_ID} and archived_at is null
  `) as Json[];
  // verbatim_contentは移行namespaceを持つnodeだけに生成される。
  // 全10万nodeの暗号化bodyを復号せず、移行管理nodeだけを対象にする。
  const managedRows = await db.select().from(knowledgeNodes).where(and(
    eq(knowledgeNodes.workspaceId, WORKSPACE_ID),
    isNull(knowledgeNodes.archivedAt),
    sql`(
      ${knowledgeNodes.systemKey} like 'docs_semantic_overlay_v1:%'
      or ${knowledgeNodes.systemKey} like 'docs_illustration_repair_pilot_v1:%'
      or ${knowledgeNodes.systemKey} like 'docs_manual_repair_v1:%'
    )`,
  )).orderBy(asc(knowledgeNodes.id)) as NodeRow[];
  const byId = new Map(imageRows.map((row) => [row.id, row]));
  const children = new Map<string, NodeRow[]>();
  for (const row of imageRows) if (row.parentId) children.set(row.parentId, [...(children.get(row.parentId) ?? []), row]);
  const imageIds = new Set<string>();
  const stack = [imageRoot.id];
  while (stack.length) {
    const id = stack.pop()!;
    if (imageIds.has(id)) continue;
    imageIds.add(id);
    stack.push(...(children.get(id) ?? []).map((child) => child.id));
  }
  const verbatim: VerbatimTarget[] = [];
  const imageHeadingIds: string[] = [];
  for (const row of managedRows) {
    const body = decryptDocsNodeBodyJson(row.bodyJson);
    const content = typeof body.verbatim_content === "string" ? body.verbatim_content : null;
    if (content !== null) verbatim.push({ id: row.id, contentSha256: sha256(content), segmentCount: editableSegments(content).length });
  }
  for (const row of imageRows) {
    const body = decryptDocsNodeBodyJson(row.bodyJson);
    if (/^heading_[123]$/.test(String(body.block_type ?? ""))) imageHeadingIds.push(row.id);
  }
  const duplicateChildIds = duplicateRows.map((row) => row.child_id);
  const duplicateChildren = duplicateChildIds.length
    ? await db.select().from(knowledgeNodes).where(inArray(knowledgeNodes.id, duplicateChildIds))
    : [];
  const duplicateChildById = new Map(duplicateChildren.map((row) => [row.id, row]));
  const [duplicateSupertags, duplicateFieldValues, duplicateAttachments, duplicatePlacements] = duplicateChildIds.length
    ? await Promise.all([
        db.select().from(schema.knowledgeNodeSupertags).where(inArray(schema.knowledgeNodeSupertags.nodeId, duplicateChildIds)),
        db.select().from(schema.knowledgeFieldValues).where(inArray(schema.knowledgeFieldValues.nodeId, duplicateChildIds)),
        db.select().from(schema.knowledgeAttachments).where(inArray(schema.knowledgeAttachments.nodeId, duplicateChildIds)),
        db.select().from(schema.knowledgeNodePlacements).where(or(
          inArray(schema.knowledgeNodePlacements.nodeId, duplicateChildIds),
          inArray(schema.knowledgeNodePlacements.parentNodeId, duplicateChildIds),
        )),
      ])
    : [[], [], [], []];
  const relatedIds = (rows: Array<{ nodeId: string }>) => new Set(rows.map((row) => row.nodeId));
  const supertagNodeIds = relatedIds(duplicateSupertags);
  const fieldNodeIds = relatedIds(duplicateFieldValues);
  const attachmentNodeIds = relatedIds(duplicateAttachments);
  const placementNodeIds = new Set(duplicatePlacements.flatMap((row) => [row.nodeId, row.parentNodeId]));
  const unsafeDuplicates: string[] = [];
  const duplicates = duplicateRows.flatMap((row) => {
    const child = duplicateChildById.get(row.child_id);
    if (!child) {
      unsafeDuplicates.push(`${row.child_id}:missing`);
      return [];
    }
    const hasRelations = supertagNodeIds.has(child.id)
      || fieldNodeIds.has(child.id)
      || attachmentNodeIds.has(child.id)
      || placementNodeIds.has(child.id);
    if (holderHasOwnContent(child, decryptDocsNodeBodyJson) || hasRelations) {
      unsafeDuplicates.push(child.id);
      return [];
    }
    return [{ childId: row.child_id, parentId: row.parent_id }];
  });
  if (unsafeDuplicates.length) {
    throw new Error(`親子同名nodeに内容または関連データがあるため自動統合できません: ${unsafeDuplicates.join(", ")}`);
  }
  const directSplits = (children.get(imageRoot.id) ?? []).flatMap((row) => {
    const split = splitDirectTitle(row.title);
    return split ? [{ id: row.id, originalTitle: row.title, ...split }] : [];
  });
  const fingerprintIds = [...new Set([
    ...verbatim.map((item) => item.id),
    ...duplicates.flatMap((item) => [item.childId, item.parentId]),
    ...imageHeadingIds,
    ...directSplits.map((item) => item.id),
  ])];
  const fingerprintRows = fingerprintIds.length ? await db.select().from(knowledgeNodes).where(inArray(knowledgeNodes.id, fingerprintIds)).orderBy(asc(knowledgeNodes.id)) as NodeRow[] : [];
  const fingerprint = sha256(fingerprintRows.map((row) => canonical({ id: row.id, parentId: row.parentId, rootPageId: row.rootPageId, projectId: row.projectId, systemKey: row.systemKey, title: row.title, body: decryptDocsNodeBodyJson(row.bodyJson), archivedAt: row.archivedAt })).join("\n"));
  return {
    schema_version: "docs-structure-repair/v2",
    workspace_id: WORKSPACE_ID,
    fingerprint,
    image_root_id: imageRoot.id,
    verbatim,
    duplicates,
    image_heading_ids: imageHeadingIds,
    direct_splits: directSplits,
    counts: {
      active_nodes: Number(activeCountRows[0]?.count ?? 0),
      read_only_nodes: verbatim.length,
      parent_child_same_title: duplicates.length,
      image_headings: imageHeadingIds.length,
      image_direct_compound_titles: directSplits.length,
    },
  };
}

async function audit() {
  const plan = await buildPlan();
  writeJson(PLAN_PATH, plan);
  const planSha256 = sha256(readFileSync(PLAN_PATH));
  console.log(JSON.stringify({ plan_path: PLAN_PATH, plan_sha256: planSha256, ...plan.counts }, null, 2));
}

async function backup() {
  if (!existsSync(PLAN_PATH)) await audit();
  const approved = readJson<RepairPlan>(PLAN_PATH);
  const current = await buildPlan();
  if (canonical(actionablePlan(approved)) !== canonical(actionablePlan(current))) {
    throw new Error("DB changed after audit; rerun audit before backup");
  }
  mkdirSync(RUN_ROOT, { recursive: true });
  const path = resolve(RUN_ROOT, `pre-apply-${new Date().toISOString().replace(/[:.]/g, "-")}.dump`);
  dumpDatabase(path);
  const receipt: BackupReceipt = {
    schema_version: "docs-structure-repair-backup/v1",
    dump_path: path,
    dump_sha256: sha256(readFileSync(path)),
    plan_sha256: sha256(readFileSync(PLAN_PATH)),
    fingerprint: approved.fingerprint,
  };
  writeJson(receiptPath(path), receipt);
  console.log(JSON.stringify({ backup_path: path, receipt_path: receiptPath(path), sha256: receipt.dump_sha256 }, null, 2));
}

async function apply() {
  const backupPath = arg("--backup");
  if (!backupPath || !existsSync(backupPath)) throw new Error("--backup <existing pg_dump> is required");
  if (!existsSync(PLAN_PATH)) throw new Error("audit plan is missing");
  const approved = readJson<RepairPlan>(PLAN_PATH);
  const backupReceiptPath = receiptPath(backupPath);
  if (!existsSync(backupReceiptPath)) throw new Error(`backup receipt is missing: ${backupReceiptPath}`);
  const receipt = readJson<BackupReceipt>(backupReceiptPath);
  if (receipt.schema_version !== "docs-structure-repair-backup/v1"
    || resolve(receipt.dump_path) !== resolve(backupPath)
    || receipt.dump_sha256 !== sha256(readFileSync(backupPath))
    || receipt.plan_sha256 !== sha256(readFileSync(PLAN_PATH))
    || receipt.fingerprint !== approved.fingerprint) {
    throw new Error("backup receipt does not match the approved plan, DB fingerprint, or dump");
  }
  const current = await buildPlan();
  if (canonical(actionablePlan(approved)) !== canonical(actionablePlan(current))) {
    throw new Error("DB changed after audit; rerun audit and backup");
  }
  const { db } = await import("@/db");
  const schema = await import("@/db/schema");
  const {
    decryptDocsNodeBodyJson,
    insertDocsNode,
    updateDocsNode,
  } = await import("@/lib/server/docs-node-writer");
  const {
    appendKnowledgeRevision,
    syncKnowledgeNodeReferenceEdges,
    upsertKnowledgeSearchIndex,
  } = await import("@/lib/server/knowledge-docs-utils");
  await db.transaction(async (tx) => {
    await tx.execute(sql`set transaction isolation level serializable`);
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext('docs-workspace-write'), hashtext(${WORKSPACE_ID}))`);
    await tx.execute(sql`lock table knowledge_nodes, knowledge_attachments, knowledge_node_supertags, knowledge_field_values, knowledge_supertags, knowledge_fields, knowledge_edges, knowledge_search_index, knowledge_node_placements, knowledge_revisions, knowledge_import_jobs, users in share row exclusive mode`);
    const ids = [...new Set([
      ...approved.verbatim.map((item) => item.id),
      ...approved.duplicates.flatMap((item) => [item.childId, item.parentId]),
      ...approved.image_heading_ids,
      ...approved.direct_splits.map((item) => item.id),
    ])];
    const targetRows = ids.length
      ? await tx.select().from(schema.knowledgeNodes).where(inArray(schema.knowledgeNodes.id, ids)) as NodeRow[]
      : [];
    const lockedFingerprint = sha256([...targetRows].sort((a, b) => a.id.localeCompare(b.id)).map((row) => canonical({ id: row.id, parentId: row.parentId, rootPageId: row.rootPageId, projectId: row.projectId, systemKey: row.systemKey, title: row.title, body: decryptDocsNodeBodyJson(row.bodyJson), archivedAt: row.archivedAt })).join("\n"));
    if (lockedFingerprint !== approved.fingerprint) throw new Error("DB changed after audit; aborting before write");
    const byId = new Map(targetRows.map((row) => [row.id, row]));

    for (const target of approved.verbatim) {
      const currentRow = byId.get(target.id);
      if (!currentRow || currentRow.archivedAt) continue;
      const body = decryptDocsNodeBodyJson(currentRow.bodyJson);
      const content = typeof body.verbatim_content === "string" ? body.verbatim_content : null;
      if (content === null || sha256(content) !== target.contentSha256) throw new Error(`verbatim changed: ${target.id}`);
      const segments = editableSegments(content);
      const existingChildren = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.parentId, currentRow.id),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      const existingTitles = new Set(existingChildren.map((child) => child.title));
      const newlineKinds = [...content.matchAll(/\r\n|\n|\r/g)].map((match) => match[0]);
      for (let index = 0; index < segments.length; index += 1) {
        const segment = segments[index];
        if (!segment.text || segment.text === currentRow.title || existingTitles.has(segment.text)) continue;
        const systemKey = `docs_structure_repair_v2:editable:${currentRow.id}:${index}`;
        const id = stableId(systemKey);
        const inserted = await insertDocsNode(tx, {
          id,
          workspaceId: currentRow.workspaceId,
          parentId: currentRow.id,
          rootPageId: currentRow.rootPageId,
          projectId: currentRow.projectId,
          systemKey,
          title: segment.text,
          aliases: [],
          description: "",
          bodyJson: {
            format: "doc_block",
            block_type: "paragraph",
            source_content_sha256: target.contentSha256,
            source_line_index: segment.lineIndex,
            source_line_count: segment.lineCount,
            source_chunk_index: segment.chunkIndex,
            source_chunk_count: segment.chunkCount,
          },
          displayProps: {},
          queryJson: null,
          viewJson: {},
          dayDate: null,
          sortOrder: (currentRow.sortOrder ?? 0) + (index + 1) / 10_000,
          nodeType: "node",
          createdBy: currentRow.createdBy ?? OWNER_ID,
          updatedBy: OWNER_ID,
        });
        await upsertKnowledgeSearchIndex(tx, inserted, "");
        await appendKnowledgeRevision(tx, inserted, OWNER_ID, "読み取り専用本文を編集可能nodeへ変換", [{ content_sha256: target.contentSha256 }]);
      }
      delete body.verbatim_content;
      body.source_content_sha256 = target.contentSha256;
      if (newlineKinds.length) body.source_newline_kinds = newlineKinds;
      const updated = await updateDocsNode(tx, currentRow.id, { bodyJson: body, updatedBy: OWNER_ID });
      await upsertKnowledgeSearchIndex(tx, updated, "");
      await appendKnowledgeRevision(tx, updated, OWNER_ID, "読み取り専用本文を通常nodeへ展開", [{ content_sha256: target.contentSha256 }]);
    }

    for (const duplicate of approved.duplicates) {
      const [child] = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.id, duplicate.childId),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      const [parent] = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.id, duplicate.parentId),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      if (!child || !parent || child.title.trim() !== parent.title.trim()) continue;
      const [childSupertags, childFieldValues, childAttachments, childPlacements] = await Promise.all([
        tx.select().from(schema.knowledgeNodeSupertags).where(eq(schema.knowledgeNodeSupertags.nodeId, child.id)),
        tx.select().from(schema.knowledgeFieldValues).where(eq(schema.knowledgeFieldValues.nodeId, child.id)),
        tx.select().from(schema.knowledgeAttachments).where(eq(schema.knowledgeAttachments.nodeId, child.id)),
        tx.select().from(schema.knowledgeNodePlacements).where(or(
          eq(schema.knowledgeNodePlacements.nodeId, child.id),
          eq(schema.knowledgeNodePlacements.parentNodeId, child.id),
        )),
      ]);
      if (holderHasOwnContent(child, decryptDocsNodeBodyJson)
        || childSupertags.length
        || childFieldValues.length
        || childAttachments.length
        || childPlacements.length) {
        throw new Error(`親子同名nodeに内容または関連データがあるため統合を中止します: ${child.id}`);
      }
      const grandchildren = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.parentId, child.id),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      for (const grandchild of grandchildren) {
        const moved = await updateDocsNode(tx, grandchild.id, {
          parentId: parent.id,
          rootPageId: parent.rootPageId,
          projectId: parent.projectId,
          updatedBy: OWNER_ID,
        });
        await appendKnowledgeRevision(tx, moved, OWNER_ID, "親子同名holderを除去して子孫を繰り上げ", []);
      }
      await tx.update(schema.knowledgeNodePlacements)
        .set({ parentNodeId: parent.id })
        .where(eq(schema.knowledgeNodePlacements.parentNodeId, child.id));
      const archived = await updateDocsNode(tx, child.id, { archivedAt: new Date(), updatedBy: OWNER_ID });
      await tx.delete(schema.knowledgeSearchIndex).where(eq(schema.knowledgeSearchIndex.nodeId, child.id));
      await appendKnowledgeRevision(tx, archived, OWNER_ID, "親と同名の重複nodeを統合", []);
    }

    for (const id of approved.image_heading_ids) {
      const [row] = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.id, id),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      if (!row) continue;
      const body = decryptDocsNodeBodyJson(row.bodyJson);
      body.block_type = row.id === approved.image_root_id ? "page" : "paragraph";
      const updated = await updateDocsNode(tx, row.id, { bodyJson: body, displayProps: {}, updatedBy: OWNER_ID });
      await appendKnowledgeRevision(tx, updated, OWNER_ID, "画像生成配下の見出し表示を解除", []);
    }

    for (const target of approved.direct_splits) {
      const [row] = await tx.select().from(schema.knowledgeNodes).where(and(
        eq(schema.knowledgeNodes.id, target.id),
        isNull(schema.knowledgeNodes.archivedAt),
      ));
      if (!row || row.title !== target.originalTitle) continue;
      const updated = await updateDocsNode(tx, row.id, { title: target.baseTitle, updatedBy: OWNER_ID });
      await upsertKnowledgeSearchIndex(tx, updated, "");
      await syncKnowledgeNodeReferenceEdges(tx, updated, OWNER_ID);
      await appendKnowledgeRevision(tx, updated, OWNER_ID, "画像生成直下の分類名を単純化", []);
      if (target.suffix) {
        const existing = await tx.select().from(schema.knowledgeNodes).where(and(
          eq(schema.knowledgeNodes.parentId, row.id),
          eq(schema.knowledgeNodes.title, target.suffix),
          isNull(schema.knowledgeNodes.archivedAt),
        ));
        if (!existing.length) {
          const systemKey = `docs_structure_repair_v2:direct-suffix:${row.id}`;
          const inserted = await insertDocsNode(tx, {
            id: stableId(systemKey),
            workspaceId: row.workspaceId,
            parentId: row.id,
            rootPageId: row.rootPageId,
            projectId: row.projectId,
            systemKey,
            title: target.suffix,
            aliases: [],
            description: "",
            bodyJson: { format: "doc_block", block_type: "paragraph" },
            displayProps: {},
            queryJson: null,
            viewJson: {},
            dayDate: null,
            sortOrder: -1,
            nodeType: "node",
            createdBy: row.createdBy ?? OWNER_ID,
            updatedBy: OWNER_ID,
          });
          await upsertKnowledgeSearchIndex(tx, inserted, "");
          await appendKnowledgeRevision(tx, inserted, OWNER_ID, "複合分類名から個別話題を復元", []);
        }
      }
    }

    const orphanRows = await tx.execute(sql`
      select count(*)::int as count
      from knowledge_nodes child
      left join knowledge_nodes parent on parent.id = child.parent_id
      where child.workspace_id = ${WORKSPACE_ID}
        and child.archived_at is null
        and child.parent_id is not null
        and (parent.id is null or parent.archived_at is not null)
    `) as Json[];
    if (Number(orphanRows[0]?.count ?? 0) !== 0) throw new Error(`active orphan remains: ${orphanRows[0]?.count}`);
  });
  console.log(JSON.stringify({ applied: true, backup_path: backupPath, backup_sha256: sha256(readFileSync(backupPath)) }, null, 2));
}

async function verify() {
  const plan = await buildPlan();
  const result = {
    read_only_nodes: plan.verbatim.length,
    parent_child_same_title: plan.duplicates.length,
    image_headings: plan.image_heading_ids.length,
    image_direct_compound_titles: plan.direct_splits.length,
  };
  console.log(JSON.stringify(result, null, 2));
  if (Object.values(result).some((value) => value !== 0)) process.exitCode = 1;
}

async function main() {
  loadEnv();
  const command = process.argv[2] ?? "audit";
  if (command === "audit") return audit();
  if (command === "backup") return backup();
  if (command === "apply") return apply();
  if (command === "verify") return verify();
  throw new Error(`unknown command: ${command}`);
}

main().then(
  () => process.exit(process.exitCode ?? 0),
  (error) => {
    console.error(error instanceof Error ? error.stack : error);
    process.exit(1);
  },
);
