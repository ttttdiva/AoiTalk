import { and, asc, eq, like } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { assertActiveWriter } from "../db/sqlite-writer";
import { getCachedToken, getToken, getTokenAuthScope } from "../lib/auth";
import {
  listOutboxConflicts,
  randomId,
  type OutboxConflictReference,
} from "../repositories/outbox";
import { rebaseOutboxConflict } from "../repositories/outbox";
import { notifySyncMutation } from "./contract";
import type { ConflictRecord, ConflictStore, ConflictKind, SyncTableV3 } from "./contract";
import { readCurrentDirtyCaptureInWriter, stableLegacyJson } from "./legacy-migration";

function asObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown> : null;
  } catch { return null; }
}

function stringArray(value: unknown): string[] {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) as unknown : value;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch { return []; }
}

type DurableReapply = { action: "create" | "update"; payload: Record<string, unknown> };

function durableReapplyFor(
  table: string,
  local: Record<string, unknown>,
  base: Record<string, unknown> | null,
): DurableReapply | null {
  if (typeof base?.updated_at !== "string" || !base.updated_at) return null;
  if (table === "knowledge_nodes") {
    // This is the editable subset accepted by the Docs update endpoint. Cache
    // metadata and denormalized fields are not valid patch fields.
    if (local.body_json == null && local.body_text != null) return null;
    const payload: Record<string, unknown> = {};
    for (const key of ["title", "description", "body_json", "parent_id", "sort_order", "day_date"] as const) {
      if (Object.prototype.hasOwnProperty.call(local, key)
        && (key !== "body_json" || local.body_json != null)
        && (key !== "sort_order" || local.sort_order != null)) {
        payload[key] = local[key];
      }
    }
    return Object.keys(payload).length ? { action: "update", payload } : null;
  }
  if (table === "knowledge_node_supertags"
    && typeof local.node_id === "string" && local.node_id
    && typeof local.supertag_id === "string" && local.supertag_id) {
    // A dirty local association is present in the cache, so the supported
    // v2/v3 operation is create (idempotent add), not update.
    return {
      action: "create",
      payload: { node_id: local.node_id, supertag_id: local.supertag_id },
    };
  }
  if (table === "knowledge_field_values"
    && typeof local.node_id === "string" && local.node_id
    && typeof local.field_id === "string" && local.field_id
    && Object.prototype.hasOwnProperty.call(local, "value")) {
    return {
      action: "update",
      payload: { node_id: local.node_id, field_id: local.field_id, value: local.value },
    };
  }
  // The current contract only supports creating supertags. A migrated dirty
  // supertag may be an edit to an existing row, which cannot safely be replayed.
  return null;
}

function isSyncV3Table(value: string): value is SyncTableV3 {
  return ["spaces", "projects", "tasks", "task_occurrences", "time_entries",
    "conversation_sessions", "conversation_messages", "record_tables", "record_fields", "record_rows",
    "knowledge_nodes", "knowledge_supertags", "knowledge_node_supertags", "knowledge_supertag_fields",
    "knowledge_fields", "knowledge_field_values", "knowledge_node_placements", "knowledge_edges"].includes(value);
}

function conflictKind(lastError: string | null): ConflictKind {
  const code = (lastError ?? "").toLowerCase();
  if (code.includes("deleted")) return "entity_deleted";
  if (code.includes("forbidden")) return "forbidden";
  if (code.includes("invalid")) return "invalid";
  return "field_conflict";
}

function fromOutbox(row: Awaited<ReturnType<typeof listOutboxConflicts>>[number], authScope: string): ConflictRecord | null {
  if (!isSyncV3Table(row.tableName) || row.authScope !== authScope) return null;
  const local = asObject(row.payload) ?? {};
  const base = asObject(row.basePayload) ?? (row.baseUpdatedAt ? { updated_at: row.baseUpdatedAt } : null);
  const server = asObject(row.conflictPayload);
  const fields = [...new Set([...Object.keys(local), ...Object.keys(server ?? {})])].sort();
  return {
    id: `outbox:${row.opId}`,
    auth_scope: authScope,
    table: row.tableName,
    entity_id: row.entityId,
    scope_key: row.docsScopeKey ?? null,
    kind: conflictKind(row.lastError),
    local,
    base,
    server,
    fields,
    source_op_id: row.opId,
    title_hint: String(local.title ?? local.name ?? row.entityId),
    created_at: row.createdAt,
    state: "open",
    can_reapply: row.tableName.startsWith("knowledge_")
      && Boolean(row.docsScopeKey)
      && Boolean(asObject(row.conflictPayload)?.updated_at),
  };
}

function fromDurable(row: typeof schema.syncV3Conflicts.$inferSelect): ConflictRecord | null {
  if (!isSyncV3Table(row.tableName)) return null;
  const local = asObject(row.localJson) ?? {};
  const base = asObject(row.baseJson);
  return {
    id: row.id,
    auth_scope: row.authScope,
    table: row.tableName,
    entity_id: row.entityId,
    scope_key: row.scopeKey,
    kind: row.kind as ConflictKind,
    local,
    base,
    server: asObject(row.serverJson),
    fields: stringArray(row.fieldsJson),
    source_op_id: row.sourceOpId,
    title_hint: row.titleHint,
    created_at: row.createdAt,
    state: row.state as ConflictRecord["state"],
    can_reapply: row.sourceOpId == null
      && row.kind === "migrated_unresolved"
      && row.authScope !== "legacy:unknown"
      && Boolean(row.scopeKey)
      && durableReapplyFor(row.tableName, local, base) !== null,
  };
}

function currentAuthScope(token: string | null | undefined, expected: string): boolean {
  return Boolean(token && getTokenAuthScope(token) === expected);
}

function outboxReference(row: Awaited<ReturnType<typeof listOutboxConflicts>>[number]): OutboxConflictReference {
  return {
    opId: row.opId,
    tableName: row.tableName,
    action: row.action,
    entityId: row.entityId,
    payload: row.payload,
    baseUpdatedAt: row.baseUpdatedAt,
    basePayload: row.basePayload,
    conflictPayload: row.conflictPayload,
    retryCount: row.retryCount,
    lastError: row.lastError,
    blockedReason: row.blockedReason,
    authScope: row.authScope!,
    docsScopeKey: row.docsScopeKey,
  };
}

async function reapplyDurableConflict(id: string, authScope: string): Promise<string | null> {
  const token = await getToken();
  if (!currentAuthScope(token, authScope)) return null;
  const found = await getDb().select().from(schema.syncV3Conflicts).where(and(
    eq(schema.syncV3Conflicts.id, id),
    eq(schema.syncV3Conflicts.authScope, authScope),
    eq(schema.syncV3Conflicts.state, "open"),
  ));
  const conflict = found[0];
  if (!conflict || !conflict.scopeKey || !isSyncV3Table(conflict.tableName)) return null;
  const local = asObject(conflict.localJson);
  if (!local) return null;
  const table = conflict.tableName;
  const membership = await getDb().select().from(schema.docsScopeMembership).where(and(
    eq(schema.docsScopeMembership.authScope, authScope),
    eq(schema.docsScopeMembership.scopeKey, conflict.scopeKey),
    eq(schema.docsScopeMembership.tableName, table),
    eq(schema.docsScopeMembership.entityKey, conflict.entityId),
    eq(schema.docsScopeMembership.state, "active"),
  ));
  if (!membership.some((item) => item.access !== "read" && item.readOnly !== true)) return null;

  const db = getDb();
  const result = await runForegroundSqliteWrite((writer) => db.transaction((tx) => {
    assertActiveWriter(writer);
    if (!currentAuthScope(getCachedToken(), authScope)) return null;
    const latest = tx.select().from(schema.syncV3Conflicts).where(and(
      eq(schema.syncV3Conflicts.id, id),
      eq(schema.syncV3Conflicts.authScope, authScope),
      eq(schema.syncV3Conflicts.state, "open"),
    )).all()[0];
    if (!latest || latest.sourceSnapshotJson !== conflict.sourceSnapshotJson) return null;
    const reapply = durableReapplyFor(table, local, asObject(latest.baseJson));
    if (!reapply) return null;
    const currentDirty = readCurrentDirtyCaptureInWriter(writer, tx, table as never, latest.entityId);
    if (!currentDirty || stableLegacyJson(currentDirty.local) !== latest.localJson
      || stableLegacyJson(currentDirty) !== latest.sourceSnapshotJson) return null;
    const latestMembership = tx.select().from(schema.docsScopeMembership).where(and(
      eq(schema.docsScopeMembership.authScope, authScope),
      eq(schema.docsScopeMembership.scopeKey, conflict.scopeKey!),
      eq(schema.docsScopeMembership.tableName, table),
      eq(schema.docsScopeMembership.entityKey, conflict.entityId),
      eq(schema.docsScopeMembership.state, "active"),
    )).all();
    if (!latestMembership.some((item) => item.access !== "read" && item.readOnly !== true)) return null;
    const nodeId = table === "knowledge_field_values" || table === "knowledge_node_supertags"
      ? latest.entityId.split(":")[0]
      : table === "knowledge_nodes" ? latest.entityId : null;
    if (nodeId) {
      const node = tx.select({
        access: schema.knowledgeNodes.access,
        readOnly: schema.knowledgeNodes.readOnly,
        archivedAt: schema.knowledgeNodes.archivedAt,
        systemKey: schema.knowledgeNodes.systemKey,
      }).from(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, nodeId)).all()[0];
      const systemKey = node?.systemKey?.trim() ?? "";
      if (!node || node.readOnly === true || node.access === "read" || node.archivedAt != null
        || systemKey === "project_information_root" || systemKey.startsWith("project_information:")) return null;
    }
    const duplicate = tx.select({ opId: schema.outbox.opId }).from(schema.outbox).where(and(
      eq(schema.outbox.authScope, authScope),
      eq(schema.outbox.tableName, table),
      eq(schema.outbox.entityId, conflict.entityId),
    )).all()[0];
    if (duplicate) return null;
    const baseUpdatedAt = latest.baseJson
      ? (asObject(latest.baseJson)?.updated_at as string | undefined) ?? null
      : null;
    if (!baseUpdatedAt) return null;
    const opId = latest.reappliedOpId ?? randomId();
    tx.insert(schema.outbox).values({
      opId,
      createdAt: Date.now(),
      tableName: table,
      action: reapply.action,
      entityId: latest.entityId,
      payload: stableLegacyJson(reapply.payload),
      authScope,
      docsScopeKey: latest.scopeKey,
      blockedReason: null,
      baseUpdatedAt,
      basePayload: null,
      conflictPayload: null,
      retryCount: 0,
      lastError: null,
      v3OpId: null,
      v3OpSeq: null,
      v3SentDigest: null,
    }).run();
    tx.update(schema.syncV3Conflicts).set({ state: "reapplied", reappliedOpId: opId })
      .where(and(
        eq(schema.syncV3Conflicts.id, id),
        eq(schema.syncV3Conflicts.authScope, authScope),
        eq(schema.syncV3Conflicts.state, "open"),
        eq(schema.syncV3Conflicts.sourceSnapshotJson, conflict.sourceSnapshotJson),
      )).run();
    return opId;
  }), "sync-v3");
  if (result) notifySyncMutation();
  return result;
}

export function createSyncV3ConflictStore(): ConflictStore {
  return {
    async listOpen(authScope) {
      const token = await getToken();
      if (!currentAuthScope(token, authScope)) return [];
      const durable = await getDb().select().from(schema.syncV3Conflicts).where(and(
        eq(schema.syncV3Conflicts.authScope, authScope),
        eq(schema.syncV3Conflicts.state, "open"),
      )).orderBy(asc(schema.syncV3Conflicts.createdAt));
      const outbox = await listOutboxConflicts(authScope);
      const latestToken = await getToken();
      if (!currentAuthScope(latestToken, authScope)) return [];
      return [
        ...durable.map(fromDurable).filter((item): item is ConflictRecord => item !== null),
        ...outbox.map((row) => fromOutbox(row, authScope)).filter((item): item is ConflictRecord => item !== null),
      ].sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
    },
    async countOpen(authScope) {
      return (await this.listOpen(authScope)).length;
    },
    async reapply(id) {
      if (id.startsWith("outbox:")) {
        const opId = id.slice("outbox:".length);
        const rows = await listOutboxConflicts();
        const row = rows.find((item) => item.opId === opId);
        if (!row?.authScope || !currentAuthScope(await getToken(), row.authScope)) return null;
        const result = await rebaseOutboxConflict(outboxReference(row));
        if (!result.ok) return null;
        notifySyncMutation();
        return opId;
      }
      const token = await getToken();
      const authScope = token ? getTokenAuthScope(token) : null;
      return authScope ? reapplyDurableConflict(id, authScope) : null;
    },
    async dismiss(id) {
      if (id.startsWith("outbox:")) {
        const opId = id.slice("outbox:".length);
        const token = await getToken();
        const authScope = token ? getTokenAuthScope(token) : null;
        if (!authScope) return;
        await runForegroundSqliteWrite((writer) => getDb().transaction((tx) => {
          assertActiveWriter(writer);
          if (!currentAuthScope(getCachedToken(), authScope)) return;
          const row = tx.select().from(schema.outbox).where(and(
            eq(schema.outbox.opId, opId),
            eq(schema.outbox.authScope, authScope),
            like(schema.outbox.lastError, "conflict:%"),
          )).all()[0];
          if (!row) return;
          tx.update(schema.outbox).set({
            blockedReason: row.blockedReason ?? "user_dismissed_conflict",
            lastError: `dismissed:${row.lastError ?? "conflict"}`,
          }).where(and(eq(schema.outbox.opId, opId), eq(schema.outbox.authScope, authScope),
            like(schema.outbox.lastError, "conflict:%"))).run();
        }), "sync-v3");
        return;
      }
      const token = await getToken();
      const authScope = token ? getTokenAuthScope(token) : null;
      if (!authScope) return;
      await runForegroundSqliteWrite((writer) => getDb().transaction((tx) => {
        assertActiveWriter(writer);
        if (!currentAuthScope(getCachedToken(), authScope)) return;
        tx.update(schema.syncV3Conflicts).set({ state: "dismissed" }).where(and(
          eq(schema.syncV3Conflicts.id, id),
          eq(schema.syncV3Conflicts.authScope, authScope),
          eq(schema.syncV3Conflicts.state, "open"),
        )).run();
      }), "sync-v3");
    },
  };
}
