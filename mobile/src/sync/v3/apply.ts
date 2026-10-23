/**
 * Sync v3 page application inside one write unit / SQLite transaction.
 *
 * Every function here is synchronous and takes the caller's writer and
 * transaction. Values that need a token read are prepared before the unit
 * (`prepareSyncV3Apply`) with the same functions v2 applyPullResponse uses.
 *
 * Mapping (server rows.py load state → v2 pull wire → mobile function):
 * - general tables (projects, tasks, task_occurrences, time_entries,
 *   conversation_sessions, conversation_messages, record_*): the v3 loaders do
 *   not filter deleted_at, so a soft-deleted row is "row" with deleted_at set.
 *   v2 `_split_changes` sent it as a tombstone, so it goes to the table's
 *   *TombstonesInWriter; a live row goes to applyRemote*InWriter.
 * - a hard-deleted entity is "missing" → op=delete → the v2 tombstone function
 *   (tables without one: primary-key removal in local-delete.ts).
 * - Docs rows are never soft-deleted (archive is a normal row); an entity that
 *   is not visible in any scope is "hidden" → op=hide.
 */
import { and, eq, inArray, isNull, lt } from "drizzle-orm";
import { schema } from "../../db/client";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../../db/sqlite-writer";
import {
  applyProjectTombstonesInWriter,
  applyRemoteProjectsInWriter,
} from "../../repositories/projects";
import {
  applyRemoteTasksInWriter,
  applyTaskTombstonesInWriter,
  prepareTaskTombstoneLedgerKey,
} from "../../repositories/tasks";
import {
  applyOccurrenceTombstonesInWriter,
  applyRemoteOccurrencesInWriter,
  prepareOccurrenceApply,
  type OccurrenceApplyPrep,
} from "../../repositories/occurrences";
import {
  applyRemoteTimeEntriesInWriter,
  applyTimeEntryTombstonesInWriter,
  prepareTimeEntryApply,
  type TimeEntryApplyPrep,
} from "../../repositories/timeEntries";
import {
  applyConversationMessageTombstonesInWriter,
  applyConversationSessionTombstonesInWriter,
  applyRemoteConversationMessagesInWriter,
  applyRemoteConversationSessionsInWriter,
  prepareRemoteConversationSessions,
} from "../../repositories/conversations";
import {
  applyRecordFieldTombstonesInWriter,
  applyRecordRowTombstonesInWriter,
  applyRecordTableTombstonesInWriter,
  applyRemoteRecordFieldsInWriter,
  applyRemoteRecordRowsInWriter,
  applyRemoteRecordTablesInWriter,
} from "../../repositories/records";
import { applyRemoteSpacesInWriter, applySpaceTombstonesInWriter } from "../../repositories/spaces";
import {
  applyDocsNodeTombstonesInWriter,
  applyRemoteDocsEdgesInWriter,
  applyRemoteDocsFieldValuesInWriter,
  applyRemoteDocsFieldsInWriter,
  applyRemoteDocsNodeSupertagsInWriter,
  applyRemoteDocsNodesInWriter,
  applyRemoteDocsPlacementsInWriter,
  applyRemoteDocsSupertagFieldsInWriter,
  applyRemoteDocsSupertagsInWriter,
  deleteLocalDocsFieldValueInWriter,
  deleteLocalDocsNodeSupertagInWriter,
  removeDocsScopeMembershipInWriter,
  upsertDocsScopeMembershipInWriter,
} from "../../repositories/docs";
import { hasPendingOutboxInWriter, resolveAuthScope } from "../../repositories/outbox";
import type {
  ConversationMessage,
  ConversationSession,
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSupertag,
  DocsSupertagField,
  Project,
  RecordField,
  RecordRow,
  RecordTable,
  Space,
  Task,
  TaskOccurrence,
  TimeEntry,
} from "../../types/api";
import {
  deleteSyncV3LocalRowInWriter,
  hasSyncV3ProtectedLocalRowInWriter,
  splitSyncV3CompositeKey,
} from "./local-delete";
import {
  SYNC_V3_APPLY_ORDER,
  SYNC_V3_DOCS_TABLES,
  type SyncV3Change,
  type SyncV3ScopeState,
  type SyncV3Table,
} from "./types";

export type SyncV3ApplyEntry =
  | { kind: "upsert"; table: SyncV3Table; entityId: string; row: Record<string, unknown> }
  | { kind: "delete" | "hide"; table: SyncV3Table; entityId: string };

/** Values prepared before the write unit (token reads), exactly as v2 does. */
export type SyncV3ApplyPrep = {
  authScope: string;
  taskLedgerKey: string | null;
  occurrencePrep: OccurrenceApplyPrep | null;
  timeEntryPrep: TimeEntryApplyPrep | null;
  pendingPinSessionIds: ReadonlySet<string>;
  pendingPinOutboxScope: string | null;
};

export type SyncV3ApplyContext = {
  prep: SyncV3ApplyPrep;
  /** Scope states after this unit commits (generation + Docs metadata). */
  scopes: Readonly<Record<string, SyncV3ScopeState>>;
  now: string;
};

export type SyncV3ApplyResult = {
  reschedule: boolean;
  tables: Set<SyncV3Table>;
};

const GENERAL_SOFT_DELETE_TABLES = new Set<SyncV3Table>([
  "projects",
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
  "conversation_messages",
  "record_tables",
  "record_fields",
  "record_rows",
]);

const DOCS_TABLE_SET = new Set<SyncV3Table>(SYNC_V3_DOCS_TABLES);

function deletedAtOf(row: Record<string, unknown>): string | null {
  const value = row.deleted_at;
  return typeof value === "string" && value.trim() ? value : null;
}

function tombstoneOf(entityId: string, row: Record<string, unknown>) {
  const tombstone: { id: string; deleted_at?: string | null; deletion_batch_id?: string } = {
    id: entityId,
    deleted_at: deletedAtOf(row),
  };
  if (typeof row.deletion_batch_id === "string" && row.deletion_batch_id) {
    tombstone.deletion_batch_id = row.deletion_batch_id;
  }
  return tombstone;
}

export function changesToSyncV3Entries(changes: SyncV3Change[]): SyncV3ApplyEntry[] {
  return changes.map((change) =>
    change.op === "upsert" && change.row
      ? { kind: "upsert", table: change.table, entityId: change.entity_id, row: change.row }
      : { kind: change.op === "delete" ? "delete" : "hide", table: change.table, entityId: change.entity_id },
  );
}

/**
 * Prepare the token-dependent inputs of the v2 InWriter functions. Only the
 * tables present in `entries` are prepared (same conditions as v2).
 */
export async function prepareSyncV3Apply(
  authScope: string,
  entries: SyncV3ApplyEntry[],
): Promise<SyncV3ApplyPrep> {
  const tables = new Set(entries.map((entry) => entry.table));
  const sessionRows = entries
    .filter((entry): entry is Extract<SyncV3ApplyEntry, { kind: "upsert" }> =>
      entry.kind === "upsert" && entry.table === "conversation_sessions" && !deletedAtOf(entry.row))
    .map((entry) => entry.row as unknown as ConversationSession);
  const needsTaskLedger = entries.some((entry) =>
    entry.table === "tasks" && entry.kind !== "hide");
  const needsOccurrence = entries.some((entry) =>
    entry.table === "task_occurrences" && entry.kind !== "hide");
  const needsTimeEntry = entries.some((entry) =>
    entry.table === "time_entries" && entry.kind !== "hide");
  return {
    authScope,
    taskLedgerKey: needsTaskLedger ? await prepareTaskTombstoneLedgerKey() : null,
    occurrencePrep: needsOccurrence && tables.has("task_occurrences")
      ? await prepareOccurrenceApply()
      : null,
    timeEntryPrep: needsTimeEntry && tables.has("time_entries")
      ? await prepareTimeEntryApply()
      : null,
    pendingPinSessionIds: sessionRows.length
      ? await prepareRemoteConversationSessions(sessionRows)
      : new Set<string>(),
    pendingPinOutboxScope: sessionRows.length ? await resolveAuthScope() : null,
  };
}

/** Prep for eviction-only units: hide never needs a ledger or a token read. */
export function syncV3EvictionPrep(authScope: string): SyncV3ApplyPrep {
  return {
    authScope,
    taskLedgerKey: null,
    occurrencePrep: null,
    timeEntryPrep: null,
    pendingPinSessionIds: new Set<string>(),
    pendingPinOutboxScope: null,
  };
}

// ---------- upsert ----------

function applyUpsertsInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  prep: SyncV3ApplyPrep,
  table: SyncV3Table,
  entries: Array<Extract<SyncV3ApplyEntry, { kind: "upsert" }>>,
): boolean {
  if (!entries.length) return false;
  const live = GENERAL_SOFT_DELETE_TABLES.has(table)
    ? entries.filter((entry) => !deletedAtOf(entry.row))
    : entries;
  const dead = GENERAL_SOFT_DELETE_TABLES.has(table)
    ? entries.filter((entry) => deletedAtOf(entry.row)).map((entry) => tombstoneOf(entry.entityId, entry.row))
    : [];
  const rows = <T,>() => live.map((entry) => entry.row as unknown as T);
  switch (table) {
    case "spaces":
      applyRemoteSpacesInWriter(w, tx, rows<Space>());
      return false;
    case "projects":
      applyRemoteProjectsInWriter(w, tx, rows<Project>());
      applyProjectTombstonesInWriter(w, tx, dead);
      return false;
    case "tasks": {
      const key = requirePrep(prep.taskLedgerKey, table);
      const applied = applyRemoteTasksInWriter(w, tx, rows<Task>(), key);
      const tombstoned = applyTaskTombstonesInWriter(w, tx, dead, key);
      return applied || tombstoned;
    }
    case "task_occurrences": {
      const occurrencePrep = requirePrep(prep.occurrencePrep, table);
      const applied = applyRemoteOccurrencesInWriter(w, tx, rows<TaskOccurrence>(), occurrencePrep);
      const tombstoned = applyOccurrenceTombstonesInWriter(w, tx, dead, occurrencePrep.ledgerKey);
      return applied || tombstoned;
    }
    case "time_entries": {
      const timeEntryPrep = requirePrep(prep.timeEntryPrep, table);
      applyRemoteTimeEntriesInWriter(w, tx, rows<TimeEntry>(), timeEntryPrep);
      applyTimeEntryTombstonesInWriter(w, tx, dead, timeEntryPrep.ledgerKey);
      return false;
    }
    case "conversation_sessions":
      applyRemoteConversationSessionsInWriter(
        w, tx, rows<ConversationSession>(), prep.pendingPinSessionIds, prep.pendingPinOutboxScope);
      applyConversationSessionTombstonesInWriter(w, tx, dead);
      return false;
    case "conversation_messages":
      applyRemoteConversationMessagesInWriter(w, tx, rows<ConversationMessage>());
      applyConversationMessageTombstonesInWriter(w, tx, dead);
      return false;
    case "record_tables":
      applyRemoteRecordTablesInWriter(w, tx, rows<RecordTable>());
      applyRecordTableTombstonesInWriter(w, tx, dead);
      return false;
    case "record_fields":
      applyRemoteRecordFieldsInWriter(w, tx, rows<RecordField>());
      applyRecordFieldTombstonesInWriter(w, tx, dead);
      return false;
    case "record_rows":
      applyRemoteRecordRowsInWriter(w, tx, rows<RecordRow>());
      applyRecordRowTombstonesInWriter(w, tx, dead);
      return false;
    // Docs: never force; preservation of pending/dirty rows uses authScope.
    case "knowledge_nodes":
      applyRemoteDocsNodesInWriter(w, tx, rows<DocsNode>(), prep.authScope);
      return false;
    case "knowledge_supertags":
      applyRemoteDocsSupertagsInWriter(w, tx, rows<DocsSupertag>(), prep.authScope);
      return false;
    case "knowledge_fields":
      applyRemoteDocsFieldsInWriter(w, tx, rows<DocsField>());
      return false;
    case "knowledge_field_values":
      applyRemoteDocsFieldValuesInWriter(w, tx, rows<DocsFieldValue>(), undefined, prep.authScope);
      return false;
    case "knowledge_node_supertags":
      applyRemoteDocsNodeSupertagsInWriter(w, tx, rows<DocsNodeSupertag>(), undefined, prep.authScope);
      return false;
    case "knowledge_supertag_fields":
      applyRemoteDocsSupertagFieldsInWriter(w, tx, rows<DocsSupertagField>());
      return false;
    case "knowledge_node_placements":
      applyRemoteDocsPlacementsInWriter(w, tx, rows<DocsNodePlacement>());
      return false;
    case "knowledge_edges":
      applyRemoteDocsEdgesInWriter(w, tx, rows<DocsEdge>());
      return false;
  }
}

function requirePrep<T>(value: T | null, table: SyncV3Table): T {
  if (value == null) throw new Error(`sync v3 apply prep missing for ${table}`);
  return value;
}

// ---------- delete / hide ----------

function tombstoneSpacesInWriter(w: SqliteWriter, tx: SqliteHandle, ids: string[]): void {
  if (!ids.length) return;
  const remove = new Set(ids);
  const keep = new Set(
    tx.select({ id: schema.spaces.id }).from(schema.spaces).where(isNull(schema.spaces.deletedAt)).all()
      .map((row) => row.id)
      .filter((id) => !remove.has(id)),
  );
  applySpaceTombstonesInWriter(w, tx, keep);
}

function removeDocsRelationInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  table: "knowledge_node_supertags" | "knowledge_field_values",
  entityId: string,
): void {
  // A pending local relation edit is kept for its own (later rejected) op.
  if (hasPendingOutboxInWriter(w, tx, table, entityId, authScope)) return;
  const key = splitSyncV3CompositeKey(entityId);
  if (!key) return;
  if (table === "knowledge_node_supertags") {
    deleteLocalDocsNodeSupertagInWriter(w, tx, key[0], key[1]);
  } else {
    deleteLocalDocsFieldValueInWriter(w, tx, key[0], key[1]);
  }
}

/**
 * op=delete (server: missing) uses the v2 tombstone semantics. op=hide (lost
 * scope / eviction) never writes a tombstone ledger: the ledger tables use the
 * primary-key removal so a later upsert shows the row again.
 */
function applyRemovalsInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  prep: SyncV3ApplyPrep,
  table: SyncV3Table,
  ids: string[],
  kind: "delete" | "hide",
): boolean {
  if (!ids.length) return false;
  // Scope loss/eviction must not remove a local row protected by an outbox
  // operation, an unresolved durable conflict, or any legacy Docs dirty bit.
  const safeIds = kind === "hide"
    ? ids.filter((id) => !hasSyncV3ProtectedLocalRowInWriter(w, tx, prep.authScope, table, id))
    : ids;
  if (!safeIds.length) return false;
  const tombstones = safeIds.map((id) => ({ id }));
  switch (table) {
    case "spaces":
      tombstoneSpacesInWriter(w, tx, safeIds);
      return false;
    case "projects":
      applyProjectTombstonesInWriter(w, tx, tombstones);
      return false;
    case "tasks":
      if (kind === "delete") {
        return applyTaskTombstonesInWriter(w, tx, tombstones, requirePrep(prep.taskLedgerKey, table));
      }
      for (const id of safeIds) deleteSyncV3LocalRowInWriter(w, tx, prep.authScope, table, id);
      return true;
    case "task_occurrences":
      if (kind === "delete") {
        return applyOccurrenceTombstonesInWriter(
          w, tx, tombstones, requirePrep(prep.occurrencePrep, table).ledgerKey);
      }
      for (const id of safeIds) deleteSyncV3LocalRowInWriter(w, tx, prep.authScope, table, id);
      return true;
    case "time_entries":
      if (kind === "delete") {
        applyTimeEntryTombstonesInWriter(w, tx, tombstones, requirePrep(prep.timeEntryPrep, table).ledgerKey);
        return false;
      }
      for (const id of safeIds) deleteSyncV3LocalRowInWriter(w, tx, prep.authScope, table, id);
      return false;
    case "conversation_sessions":
      applyConversationSessionTombstonesInWriter(w, tx, tombstones);
      return false;
    case "conversation_messages":
      applyConversationMessageTombstonesInWriter(w, tx, tombstones);
      return false;
    case "record_tables":
      applyRecordTableTombstonesInWriter(w, tx, tombstones);
      return false;
    case "record_fields":
      applyRecordFieldTombstonesInWriter(w, tx, tombstones);
      return false;
    case "record_rows":
      applyRecordRowTombstonesInWriter(w, tx, tombstones);
      return false;
    case "knowledge_nodes":
      applyDocsNodeTombstonesInWriter(w, tx, tombstones, prep.authScope);
      return false;
    case "knowledge_node_supertags":
    case "knowledge_field_values":
      for (const id of safeIds) removeDocsRelationInWriter(w, tx, prep.authScope, table, id);
      return false;
    case "knowledge_supertags":
    case "knowledge_fields":
    case "knowledge_supertag_fields":
    case "knowledge_node_placements":
    case "knowledge_edges":
      for (const id of safeIds) deleteSyncV3LocalRowInWriter(w, tx, prep.authScope, table, id);
      return false;
  }
}

/** Apply entries grouped by table in the v2 applyPullResponse order. */
export function applySyncV3EntriesInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  prep: SyncV3ApplyPrep,
  entries: SyncV3ApplyEntry[],
): SyncV3ApplyResult {
  assertActiveWriter(w);
  let reschedule = false;
  const tables = new Set<SyncV3Table>();
  for (const table of SYNC_V3_APPLY_ORDER) {
    const forTable = entries.filter((entry) => entry.table === table);
    if (!forTable.length) continue;
    tables.add(table);
    const upserts = forTable.filter(
      (entry): entry is Extract<SyncV3ApplyEntry, { kind: "upsert" }> => entry.kind === "upsert");
    reschedule = applyUpsertsInWriter(w, tx, prep, table, upserts) || reschedule;
    const deletes = forTable.filter((entry) => entry.kind === "delete").map((entry) => entry.entityId);
    reschedule = applyRemovalsInWriter(w, tx, prep, table, deletes, "delete") || reschedule;
    const hides = forTable.filter((entry) => entry.kind === "hide").map((entry) => entry.entityId);
    reschedule = applyRemovalsInWriter(w, tx, prep, table, hides, "hide") || reschedule;
  }
  return { reschedule, tables };
}

// ---------- membership ----------

type MembershipKey = { table: SyncV3Table; entityId: string };

function docsScopeIdentity(
  scopeKey: string,
  scope: SyncV3ScopeState | undefined,
): { scopeId: string; projectId: string | null } | null {
  if (scope?.kind && scope.kind !== "docs") return null;
  if (scope?.id) return { scopeId: scope.id, projectId: scope.projectId ?? null };
  const match = /^(.+)\|project:(.*)$/.exec(scopeKey);
  if (!match) return null;
  return { scopeId: match[1], projectId: match[2] || null };
}

function projectDocsMembershipInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  context: SyncV3ApplyContext,
  table: SyncV3Table,
  entityId: string,
  scopeKey: string,
): void {
  if (!DOCS_TABLE_SET.has(table)) return;
  const scope = context.scopes[scopeKey];
  const identity = docsScopeIdentity(scopeKey, scope);
  if (!identity) return;
  upsertDocsScopeMembershipInWriter(w, tx, {
    authScope: context.prep.authScope,
    scopeKey,
    scopeId: identity.scopeId,
    projectId: identity.projectId,
    tableName: table,
    entityKey: entityId,
    state: "active",
    access: scope?.access ?? null,
    readOnly: scope?.readOnly ?? null,
    updatedAt: context.now,
  });
}

function removeDocsMembershipInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
  scopeKey: string,
): void {
  if (!DOCS_TABLE_SET.has(table)) return;
  removeDocsScopeMembershipInWriter(w, tx, authScope, scopeKey, table, entityId);
}

function membershipScopeKeysInWriter(
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
): string[] {
  return tx
    .select({ scopeKey: schema.syncV3Membership.scopeKey })
    .from(schema.syncV3Membership)
    .where(and(
      eq(schema.syncV3Membership.authScope, authScope),
      eq(schema.syncV3Membership.tableName, table),
      eq(schema.syncV3Membership.entityId, entityId),
    ))
    .all()
    .map((row) => row.scopeKey);
}

function deleteEntityMembershipInWriter(
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
): void {
  tx.delete(schema.syncV3Membership)
    .where(and(
      eq(schema.syncV3Membership.authScope, authScope),
      eq(schema.syncV3Membership.tableName, table),
      eq(schema.syncV3Membership.entityId, entityId),
    ))
    .run();
}

function insertMembershipInWriter(
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
  scopeKey: string,
  generation: number,
): void {
  tx.insert(schema.syncV3Membership)
    .values({ authScope, tableName: table, entityId, scopeKey, generation })
    .onConflictDoUpdate({
      target: [
        schema.syncV3Membership.authScope,
        schema.syncV3Membership.tableName,
        schema.syncV3Membership.entityId,
        schema.syncV3Membership.scopeKey,
      ],
      set: { generation },
    })
    .run();
}

function generationOf(context: SyncV3ApplyContext, scopeKey: string): number {
  return context.scopes[scopeKey]?.generation ?? 1;
}

/** Replace an entity's membership with exactly `scopeKeys` (changes upsert). */
function replaceMembershipInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  context: SyncV3ApplyContext,
  table: SyncV3Table,
  entityId: string,
  scopeKeys: string[],
): void {
  const authScope = context.prep.authScope;
  const next = new Set(scopeKeys);
  for (const previous of membershipScopeKeysInWriter(tx, authScope, table, entityId)) {
    if (!next.has(previous)) removeDocsMembershipInWriter(w, tx, authScope, table, entityId, previous);
  }
  deleteEntityMembershipInWriter(tx, authScope, table, entityId);
  for (const scopeKey of next) {
    insertMembershipInWriter(tx, authScope, table, entityId, scopeKey, generationOf(context, scopeKey));
    projectDocsMembershipInWriter(w, tx, context, table, entityId, scopeKey);
  }
}

function clearMembershipInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
): void {
  for (const previous of membershipScopeKeysInWriter(tx, authScope, table, entityId)) {
    removeDocsMembershipInWriter(w, tx, authScope, table, entityId, previous);
  }
  deleteEntityMembershipInWriter(tx, authScope, table, entityId);
}

/** One /changes page: rows, then membership, in one transaction. */
export function applySyncV3ChangesInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  context: SyncV3ApplyContext,
  changes: SyncV3Change[],
): SyncV3ApplyResult {
  assertActiveWriter(w);
  const result = applySyncV3EntriesInWriter(w, tx, context.prep, changesToSyncV3Entries(changes));
  for (const change of changes) {
    if (change.op === "upsert" && change.row) {
      replaceMembershipInWriter(w, tx, context, change.table, change.entity_id, change.scope_keys);
    } else {
      clearMembershipInWriter(w, tx, context.prep.authScope, change.table, change.entity_id);
    }
  }
  return result;
}

/** One /bootstrap page of one scope: rows and their membership at the scope generation. */
export function applySyncV3BootstrapInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  context: SyncV3ApplyContext,
  scopeKey: string,
  table: SyncV3Table,
  rows: Array<{ entity_id: string; row: Record<string, unknown> }>,
): SyncV3ApplyResult {
  assertActiveWriter(w);
  const entries: SyncV3ApplyEntry[] = rows.map((item) => ({
    kind: "upsert", table, entityId: item.entity_id, row: item.row,
  }));
  const result = applySyncV3EntriesInWriter(w, tx, context.prep, entries);
  const generation = generationOf(context, scopeKey);
  for (const item of rows) {
    insertMembershipInWriter(tx, context.prep.authScope, table, item.entity_id, scopeKey, generation);
    projectDocsMembershipInWriter(w, tx, context, table, item.entity_id, scopeKey);
  }
  return result;
}

export const SYNC_V3_SWEEP_BATCH_SIZE = 100;

/**
 * Remove the membership of one scope (all generations, or only those below
 * `belowGeneration`) and evict every entity left with no membership.
 */
export function dropSyncV3ScopeMembershipInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  context: SyncV3ApplyContext,
  scopeKey: string,
  belowGeneration: number | null,
): SyncV3ApplyResult & { remaining: boolean } {
  assertActiveWriter(w);
  const authScope = context.prep.authScope;
  const condition = and(
    eq(schema.syncV3Membership.authScope, authScope),
    eq(schema.syncV3Membership.scopeKey, scopeKey),
    ...(belowGeneration == null ? [] : [lt(schema.syncV3Membership.generation, belowGeneration)]),
  );
  const dropped = tx
    .select({ table: schema.syncV3Membership.tableName, entityId: schema.syncV3Membership.entityId })
    .from(schema.syncV3Membership)
    .where(condition)
    .limit(SYNC_V3_SWEEP_BATCH_SIZE)
    .all() as Array<{ table: SyncV3Table; entityId: string }>;
  if (!dropped.length) return { reschedule: false, tables: new Set(), remaining: false };
  for (const item of dropped) {
    tx.delete(schema.syncV3Membership).where(and(condition,
      eq(schema.syncV3Membership.tableName, item.table),
      eq(schema.syncV3Membership.entityId, item.entityId),
    )).run();
    removeDocsMembershipInWriter(w, tx, authScope, item.table, item.entityId, scopeKey);
  }
  const orphans = orphanedInWriter(tx, authScope, dropped);
  return { ...evictSyncV3EntitiesInWriter(w, tx, context.prep, orphans),
    remaining: dropped.length === SYNC_V3_SWEEP_BATCH_SIZE };
}

function orphanedInWriter(
  tx: SqliteHandle,
  authScope: string,
  candidates: MembershipKey[],
): MembershipKey[] {
  const byTable = new Map<SyncV3Table, Set<string>>();
  for (const item of candidates) {
    if (!byTable.has(item.table)) byTable.set(item.table, new Set());
    byTable.get(item.table)!.add(item.entityId);
  }
  const orphans: MembershipKey[] = [];
  for (const [table, idSet] of byTable) {
    const ids = [...idSet];
    for (let offset = 0; offset < ids.length; offset += 500) {
      const chunk = ids.slice(offset, offset + 500);
      const remaining = new Set(
        tx.select({ entityId: schema.syncV3Membership.entityId })
          .from(schema.syncV3Membership)
          .where(and(
            eq(schema.syncV3Membership.authScope, authScope),
            eq(schema.syncV3Membership.tableName, table),
            inArray(schema.syncV3Membership.entityId, chunk),
          ))
          .all()
          .map((row) => row.entityId),
      );
      for (const entityId of chunk) {
        if (!remaining.has(entityId)) orphans.push({ table, entityId });
      }
    }
  }
  return orphans;
}

/** Evict entities with no membership: identical to op=hide. */
export function evictSyncV3EntitiesInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  prep: SyncV3ApplyPrep,
  entities: MembershipKey[],
): SyncV3ApplyResult {
  return applySyncV3EntriesInWriter(
    w,
    tx,
    prep,
    entities.map((item) => ({ kind: "hide", table: item.table, entityId: item.entityId })),
  );
}
