/**
 * Primary-key removal of a local row for Sync v3 delete/hide/eviction where no
 * repository tombstone function applies.
 *
 * - A row with a pending outbox operation is kept: that operation will be
 *   rejected by the server and quarantined, so no local edit is lost.
 * - No tombstone ledger is written: a later upsert of the same id shows the
 *   row again.
 * - The local schema declares no foreign keys (PRAGMA foreign_keys=ON has
 *   nothing to enforce), so a single-row delete cannot violate a constraint.
 *   Dependent rows are handled by their own changes/membership.
 */
import { and, eq } from "drizzle-orm";
import { schema } from "../../db/client";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../../db/sqlite-writer";
import { hasPendingOutboxInWriter } from "../../repositories/outbox";
import type { SyncV3Table } from "./types";

/** Split a composite key exactly like rows._docs_row_key ("<a>:<b>"). */
export function splitSyncV3CompositeKey(entityId: string): [string, string] | null {
  const index = entityId.indexOf(":");
  if (index <= 0 || index === entityId.length - 1) return null;
  const left = entityId.slice(0, index);
  const right = entityId.slice(index + 1);
  if (right.includes(":")) return null;
  return [left, right];
}

function deleteByKey(tx: SqliteHandle, table: SyncV3Table, entityId: string): void {
  switch (table) {
    case "spaces":
      tx.delete(schema.spaces).where(eq(schema.spaces.id, entityId)).run();
      return;
    case "projects":
      tx.delete(schema.projects).where(eq(schema.projects.id, entityId)).run();
      return;
    case "tasks":
      tx.delete(schema.tasks).where(eq(schema.tasks.id, entityId)).run();
      return;
    case "task_occurrences":
      tx.delete(schema.taskOccurrences).where(eq(schema.taskOccurrences.id, entityId)).run();
      return;
    case "time_entries":
      tx.delete(schema.timeEntries).where(eq(schema.timeEntries.id, entityId)).run();
      return;
    case "conversation_sessions":
      tx.delete(schema.conversationSessions).where(eq(schema.conversationSessions.id, entityId)).run();
      return;
    case "conversation_messages":
      tx.delete(schema.conversationMessages).where(eq(schema.conversationMessages.id, entityId)).run();
      return;
    case "record_tables":
      tx.delete(schema.recordTables).where(eq(schema.recordTables.id, entityId)).run();
      return;
    case "record_fields":
      tx.delete(schema.recordFields).where(eq(schema.recordFields.id, entityId)).run();
      return;
    case "record_rows":
      tx.delete(schema.recordRows).where(eq(schema.recordRows.id, entityId)).run();
      return;
    case "knowledge_nodes":
      tx.delete(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, entityId)).run();
      return;
    case "knowledge_supertags":
      tx.delete(schema.knowledgeSupertags).where(eq(schema.knowledgeSupertags.id, entityId)).run();
      return;
    case "knowledge_fields":
      tx.delete(schema.knowledgeFields).where(eq(schema.knowledgeFields.id, entityId)).run();
      return;
    case "knowledge_node_placements":
      tx.delete(schema.knowledgeNodePlacements)
        .where(eq(schema.knowledgeNodePlacements.id, entityId)).run();
      return;
    case "knowledge_edges":
      tx.delete(schema.knowledgeEdges).where(eq(schema.knowledgeEdges.id, entityId)).run();
      return;
    case "knowledge_node_supertags": {
      const key = splitSyncV3CompositeKey(entityId);
      if (!key) return;
      tx.delete(schema.knowledgeNodeSupertags).where(and(
        eq(schema.knowledgeNodeSupertags.nodeId, key[0]),
        eq(schema.knowledgeNodeSupertags.supertagId, key[1]),
      )).run();
      return;
    }
    case "knowledge_supertag_fields": {
      const key = splitSyncV3CompositeKey(entityId);
      if (!key) return;
      tx.delete(schema.knowledgeSupertagFields).where(and(
        eq(schema.knowledgeSupertagFields.supertagId, key[0]),
        eq(schema.knowledgeSupertagFields.fieldId, key[1]),
      )).run();
      return;
    }
    case "knowledge_field_values": {
      const key = splitSyncV3CompositeKey(entityId);
      if (!key) return;
      tx.delete(schema.knowledgeFieldValues).where(and(
        eq(schema.knowledgeFieldValues.nodeId, key[0]),
        eq(schema.knowledgeFieldValues.fieldId, key[1]),
      )).run();
      return;
    }
  }
}

/**
 * Delete one local row by primary key unless it has a pending outbox
 * operation for this account. Returns true when the row was removed (or did
 * not exist), false when it was kept for its pending operation.
 */
export function deleteSyncV3LocalRowInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
): boolean {
  assertActiveWriter(w);
  if (hasSyncV3ProtectedLocalRowInWriter(w, tx, authScope, table, entityId)) return false;
  deleteByKey(tx, table, entityId);
  return true;
}

/** Pending operation, durable conflict, and dirty Docs rows are all sweep protection. */
export function hasSyncV3ProtectedLocalRowInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  table: SyncV3Table,
  entityId: string,
): boolean {
  assertActiveWriter(w);
  if (hasPendingOutboxInWriter(w, tx, table, entityId, authScope)) return true;
  const conflicts = tx.select({ id: schema.syncV3Conflicts.id }).from(schema.syncV3Conflicts).where(and(
    eq(schema.syncV3Conflicts.authScope, authScope),
    eq(schema.syncV3Conflicts.tableName, table),
    eq(schema.syncV3Conflicts.entityId, entityId),
    eq(schema.syncV3Conflicts.state, "open"),
  )).limit(1).all();
  if (conflicts.length) return true;
  if (table === "knowledge_nodes") {
    return tx.select({ id: schema.knowledgeNodes.id }).from(schema.knowledgeNodes).where(and(
      eq(schema.knowledgeNodes.id, entityId), eq(schema.knowledgeNodes.dirty, true),
    )).limit(1).all().length > 0;
  }
  if (table === "knowledge_supertags") {
    return tx.select({ id: schema.knowledgeSupertags.id }).from(schema.knowledgeSupertags).where(and(
      eq(schema.knowledgeSupertags.id, entityId), eq(schema.knowledgeSupertags.dirty, true),
    )).limit(1).all().length > 0;
  }
  if (table === "knowledge_node_supertags" || table === "knowledge_field_values") {
    const key = splitSyncV3CompositeKey(entityId);
    if (!key) return false;
    return table === "knowledge_node_supertags"
      ? tx.select({ nodeId: schema.knowledgeNodeSupertags.nodeId }).from(schema.knowledgeNodeSupertags).where(and(
        eq(schema.knowledgeNodeSupertags.nodeId, key[0]),
        eq(schema.knowledgeNodeSupertags.supertagId, key[1]),
        eq(schema.knowledgeNodeSupertags.dirty, true),
      )).limit(1).all().length > 0
      : tx.select({ nodeId: schema.knowledgeFieldValues.nodeId }).from(schema.knowledgeFieldValues).where(and(
        eq(schema.knowledgeFieldValues.nodeId, key[0]),
        eq(schema.knowledgeFieldValues.fieldId, key[1]),
        eq(schema.knowledgeFieldValues.dirty, true),
      )).limit(1).all().length > 0;
  }
  return false;
}
