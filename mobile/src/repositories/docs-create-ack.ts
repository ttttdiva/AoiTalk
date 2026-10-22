/** Carry authenticated Docs write receipts into their unsent local continuations. */
import { and, eq, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { getCachedToken, getTokenAuthScope } from "../lib/auth";
import { isExplicitBlankParagraph } from "../lib/docs-blank";
import { notifyMutationQueued } from "../sync/task-mutation-wake";
import type { SyncPushResult } from "../sync/api";
import { outboxMutationSnapshotCondition, validateOutboxConflictServerScope, type PendingOutboxSnapshot } from "./outbox";

type Operation = typeof schema.outbox.$inferSelect;
const MISSING_BASE = "Docs base_updated_at is required for an existing entity";
const DOCS_BASE_MISSING = "docs_base_missing";
const DOCS_CAS_STALE = "docs_cas_stale";
const CAS_STALE_REASON = "Docs entity was updated on the server";
const CONTENT_FIELDS = [
  "title", "aliases", "description", "body_json", "body_text", "node_type",
  "parent_id", "project_id", "day_date", "sort_order", "archived_at",
  "display_props", "query_json", "view_json",
] as const;

function object(value: unknown): Record<string, unknown> | null {
  if (typeof value === "string") {
    try { return object(JSON.parse(value)); } catch { return null; }
  }
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, nested]) => [key, canonical(nested)]),
  );
  return value ?? null;
}
function equal(a: unknown, b: unknown): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}
function sameSnapshot(current: Operation | undefined, expected: Operation): boolean {
  return Boolean(current && current.opId === expected.opId
    && current.authScope === expected.authScope && current.docsScopeKey === expected.docsScopeKey
    && current.tableName === expected.tableName && current.entityId === expected.entityId
    && current.action === expected.action && current.payload === expected.payload
    && current.baseUpdatedAt === expected.baseUpdatedAt
    && equal(current.basePayload, expected.basePayload)
    && equal(current.conflictPayload, expected.conflictPayload)
    && current.retryCount === expected.retryCount
    && current.lastError === expected.lastError
    && current.blockedReason === expected.blockedReason);
}
function mutationSnapshot(operation: Operation): PendingOutboxSnapshot {
  return {
    opId: operation.opId,
    tableName: operation.tableName,
    action: operation.action,
    entityId: operation.entityId,
    payload: operation.payload,
    baseUpdatedAt: operation.baseUpdatedAt,
    basePayload: operation.basePayload,
    conflictPayload: operation.conflictPayload,
    retryCount: operation.retryCount,
    lastError: operation.lastError,
    blockedReason: operation.blockedReason,
    authScope: operation.authScope,
    docsScopeKey: operation.docsScopeKey,
  };
}
function verifiedEntity(operation: Operation, result: SyncPushResult): Record<string, unknown> | null {
  if (!operation.authScope || !operation.docsScopeKey || !result.entity
    || result.entity.deleted === true || result.entity.archived_at != null
    || typeof result.server_updated_at !== "string" || !result.server_updated_at.trim()
    || result.entity.updated_at !== result.server_updated_at) return null;
  return validateOutboxConflictServerScope(operation, result.entity).ok ? result.entity : null;
}
function hasExpectedScope(operation: Operation, payload: Record<string, unknown>): boolean {
  return payload.workspace_id != null
    && `${String(payload.workspace_id)}|project:${payload.project_id ?? ""}` === operation.docsScopeKey
    && (!Object.prototype.hasOwnProperty.call(payload, "id") || payload.id === operation.entityId);
}
function sameContent(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  return CONTENT_FIELDS.every((key) => equal(a[key], b[key]));
}
function normalizeLocalContent(base: Record<string, unknown>): Record<string, unknown> {
  return { ...base, description: base.description ?? "", aliases: base.aliases ?? [],
    display_props: base.display_props ?? {}, view_json: base.view_json ?? {} };
}

/** Compare persisted editable content without applying create-only title/body defaults. */
export function matchesDocsStoredContent(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  return sameContent(normalizeLocalContent(a), normalizeLocalContent(b));
}

/**
 * Compare a create with its canonical stored content. These are the exact
 * create serializer defaults, not a general null/empty equivalence. Include
 * parent/content fields so a moved, edited or archived row is not adopted.
 */
export function matchesDocsCreateContent(
  payload: Record<string, unknown>, entity: Record<string, unknown>,
): boolean {
  return entity.deleted !== true && entity.archived_at == null
    && sameContent({ ...normalizeLocalContent(payload),
      body_text: payload.title ?? "", body_json: payload.body_json ?? {} }, entity);
}

/** Compare exact server microseconds, treating naive Python timestamps as UTC. */
export function compareDocsVersions(a: string, b: string): number | null {
  const parse = (value: string): [number, string] | null => {
    const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$/.exec(value);
    if (!match) return null;
    const seconds = Date.parse(`${match[1]}${match[3] ?? "Z"}`);
    return Number.isFinite(seconds) ? [seconds, (match[2] ?? "").padEnd(9, "0")] : null;
  };
  const left = parse(a), right = parse(b);
  if (!left || !right) return null;
  return left[0] === right[0] ? (left[1] === right[1] ? 0 : left[1] < right[1] ? -1 : 1)
    : left[0] < right[0] ? -1 : 1;
}

/** Keep a newer durable server conflict when an earlier HTTP receipt arrives. */
export function isDocsResultOlderThanRecordedConflict(operation: Operation, result: SyncPushResult): boolean {
  if (operation.tableName !== "knowledge_nodes" || !operation.conflictPayload) return false;
  const stored = object(operation.conflictPayload), incoming = result.entity;
  if (!stored || !incoming || !operation.docsScopeKey) return true;
  if (!validateOutboxConflictServerScope(operation, stored).ok
    || !validateOutboxConflictServerScope(operation, incoming).ok) return true;
  const before = stored.updated_at, after = result.server_updated_at ?? incoming.updated_at;
  if (typeof before !== "string" || typeof after !== "string") return true;
  const order = compareDocsVersions(after, before);
  return order === null || order < 0 || (order === 0 && !sameContent(stored, incoming));
}

function currentActor(authScope: string, assertActive: () => void): void {
  assertActive();
  if (getTokenAuthScope(getCachedToken()) !== authScope) throw new Error("Docs receipt auth scope changed");
}

/**
 * Updates may share a batch with their create or be queued during HTTP. For a
 * sent update, require the real missing-base conflict to witness exactly the
 * acknowledged version/content. Different remote edits, deletes, missing or
 * duplicate receipts, other scopes and newer local snapshots remain untouched.
 */
export async function rebaseDocsWriteFollowups(
  authScope: string, pending: readonly Operation[], results: readonly SyncPushResult[],
  assertActive: () => void,
): Promise<{ updates: Set<string>; receipts: Set<string> }> {
  const rebased = { updates: new Set<string>(), receipts: new Set<string>() };
  const sources = pending.filter((op) => op.tableName === "knowledge_nodes"
    && ["create", "update"].includes(op.action) && op.authScope === authScope && op.docsScopeKey);
  if (!sources.length) return rebased;
  const sent = new Map(pending.map((op) => [op.opId, op]));
  const positions = new Map(pending.map((op, index) => [op.opId, index]));
  const uniqueResult = (id: string) => {
    const matches = results.filter((result) => result.op_id === id);
    return matches.length === 1 ? matches[0] : null;
  };
  await runForegroundSqliteWrite(() => {
    currentActor(authScope, assertActive);
    getDb().transaction((tx) => {
      for (const source of sources) {
        const ack = uniqueResult(source.opId);
        const acknowledgesWrite = ack?.status === "ok" || (source.action === "create"
          && ack?.status === "conflict" && ack.reason_code === DOCS_BASE_MISSING
          && ack.reason === MISSING_BASE);
        const entity = acknowledgesWrite && ack ? verifiedEntity(source, ack) : null;
        const payload = object(source.payload);
        if (!ack || !entity || !payload) continue;
        if (source.action === "create" && (payload.id !== source.entityId || !hasExpectedScope(source, payload)
          || entity.created_by !== authScope.slice("auth:".length)
          || !matchesDocsCreateContent(payload, entity))) continue;
        const currentSource = tx.select().from(schema.outbox).where(eq(schema.outbox.opId, source.opId)).get();
        const snapshotMatches = sameSnapshot(currentSource, source);
        let createFollowup: { latest: Record<string, unknown>; changed: string[] } | null = null;
        if (!snapshotMatches && source.action === "create" && currentSource
          && sameSnapshot({ ...currentSource, payload: source.payload }, source)) {
          const latest = object(currentSource.payload);
          const changed = latest ? [...new Set([...Object.keys(payload), ...Object.keys(latest)])]
            .filter((key) => !equal(payload[key], latest[key])) : [];
          if (latest && changed.length && changed.every((key) =>
            ["title", "description", "body_json", "body_text", "sort_order", "source_refs"].includes(key))) {
            createFollowup = { latest, changed };
          }
        }
        // Do not project metadata from an old receipt until the complete
        // lifecycle snapshot matches. The payload-only create followup is the
        // sole narrow exception and is converted atomically below.
        if (!snapshotMatches && !createFollowup) continue;
        if (source.action === "create" && currentSource
          && currentSource.authScope === authScope && currentSource.docsScopeKey === source.docsScopeKey) {
          // A confirmed create receipt supplies server-clock provenance even
          // when its content is retained by a newer in-flight local edit.
          const live = tx.select().from(schema.knowledgeNodes).where(eq(schema.knowledgeNodes.id, source.entityId)).get();
          if (live && live.workspaceId === entity.workspace_id && (live.projectId ?? null) === (entity.project_id ?? null)) {
            const order = live.serverUpdatedAt ? compareDocsVersions(ack.server_updated_at!, live.serverUpdatedAt) : 1;
            tx.update(schema.knowledgeNodes).set({
              createdBy: String(entity.created_by),
              ...(typeof entity.created_at === "string" ? { createdAt: entity.created_at } : {}),
              ...(order !== null && order >= 0 ? { serverUpdatedAt: ack.server_updated_at! } : {}),
            }).where(eq(schema.knowledgeNodes.id, source.entityId)).run();
          }
        }
        if (!snapshotMatches) {
          // An edit during create HTTP merges into the same operation ID.
          // Once that create is acknowledged it must become an UPDATE, never
          // insert the same node again. Preserve only the newer editable delta.
          if (source.action === "create" && currentSource && createFollowup) {
            const changedRow = tx.update(schema.outbox).set({
              action: "update", payload: JSON.stringify({ workspace_id: entity.workspace_id, project_id: entity.project_id,
                ...Object.fromEntries(createFollowup.changed.map((key) => [key, createFollowup.latest[key]])) }),
              baseUpdatedAt: ack.server_updated_at!, basePayload: entity,
              conflictPayload: null, lastError: null, retryCount: 0,
            }).where(and(
              outboxMutationSnapshotCondition(source.opId, mutationSnapshot(currentSource)),
              isNull(schema.outbox.blockedReason),
            )).run();
            if (Number(changedRow?.changes ?? 0) <= 0) continue;
            rebased.updates.add(source.opId); rebased.receipts.add(source.opId);
          }
          continue;
        }
        const updates = tx.select().from(schema.outbox).where(and(
          eq(schema.outbox.authScope, authScope), eq(schema.outbox.tableName, "knowledge_nodes"),
          eq(schema.outbox.entityId, source.entityId), eq(schema.outbox.docsScopeKey, source.docsScopeKey!),
          eq(schema.outbox.action, "update"),
          isNull(schema.outbox.blockedReason),
        )).all();
        for (const update of updates) {
          if (update.opId === source.opId || update.createdAt < source.createdAt
            || (positions.has(update.opId) && positions.get(update.opId)! <= positions.get(source.opId)!)) continue;
          const base = object(update.basePayload), sourceBase = object(source.basePayload);
          if (source.action === "create" ? update.baseUpdatedAt !== null
            : !(base && ((update.baseUpdatedAt === source.baseUpdatedAt && sourceBase
              && sameContent(normalizeLocalContent(base), normalizeLocalContent(sourceBase)))
              || (base.id === update.entityId && sameContent(normalizeLocalContent(base), entity))))) continue;
          const updatePayload = object(update.payload);
          if (!updatePayload || ["workspace_id", "workspaceId"].some((key) => key in updatePayload && updatePayload[key] !== entity.workspace_id)
            || ["project_id", "projectId"].some((key) => key in updatePayload && updatePayload[key] !== entity.project_id)
            || ("id" in updatePayload && updatePayload.id !== update.entityId)) continue;
          const prior = sent.get(update.opId);
          if (prior) {
            const rejection = uniqueResult(update.opId);
            const current = rejection && verifiedEntity(update, rejection);
            if (!sameSnapshot(update, prior) || rejection?.status !== "conflict"
              || !((rejection.reason_code === DOCS_BASE_MISSING && rejection.reason === MISSING_BASE)
                || (rejection.reason_code === DOCS_CAS_STALE && rejection.reason === CAS_STALE_REASON))
              || !current
              || rejection.server_updated_at !== ack.server_updated_at || !sameContent(entity, current)) continue;
          } else if (update.lastError || update.retryCount !== 0 || update.conflictPayload) continue;
          // An already-recorded different remote snapshot must not be erased
          // by an older create receipt, even when its update still has no base.
          if (isDocsResultOlderThanRecordedConflict(update, ack)) continue;
          const changedRow = tx.update(schema.outbox).set({
            baseUpdatedAt: ack.server_updated_at!, basePayload: entity,
            conflictPayload: null, lastError: null, retryCount: 0,
          }).where(and(
            outboxMutationSnapshotCondition(update.opId, mutationSnapshot(update)),
            isNull(schema.outbox.blockedReason),
          )).run();
          if (Number(changedRow?.changes ?? 0) <= 0) continue;
          rebased.updates.add(update.opId); rebased.receipts.add(source.opId);
        }
      }
      currentActor(authScope, assertActive);
    });
  });
  if (rebased.updates.size) notifyMutationQueued(authScope);
  return rebased;
}

/**
 * 232 can already have removed the create receipt. Recover only its specific
 * Personal-root blank draft baseline, using a fresh server missing-base
 * response whose complete editable content still equals that baseline.
 * Never use cached conflict data as a current server version or change text.
 */
export async function rebaseLegacyBlankDocsUpdate(
  operation: Operation, result: SyncPushResult, authScope: string, assertActive: () => void,
): Promise<boolean> {
  if (operation.tableName !== "knowledge_nodes" || operation.action !== "update"
    || operation.authScope !== authScope || operation.baseUpdatedAt != null
    || result.status !== "conflict" || result.reason_code !== DOCS_BASE_MISSING
    || result.reason !== MISSING_BASE) return false;
  const entity = verifiedEntity(operation, result);
  const base = object(operation.basePayload), payload = object(operation.payload);
  if (!entity || !base || !payload || !hasExpectedScope(operation, payload)
    || base.id !== operation.entityId || base.workspace_id !== null
    || base.project_id !== null || base.parent_id !== null || base.root_page_id !== null
    || base.source !== "personal" || base.access !== "write" || base.read_only !== false
    || base.created_by !== null || base.updated_by !== null || base.archived_at !== null
    || base.title !== "" || base.body_text !== ""
    || base.node_type !== "node" || !isExplicitBlankParagraph(base.title, base.body_json, base.node_type)
    || entity.created_by !== authScope.slice("auth:".length)
    || entity.updated_by !== authScope.slice("auth:".length)
    || entity.project_id !== null || entity.parent_id !== null
    // The actual create serializer canonicalizes these absent local fields.
    || !sameContent(normalizeLocalContent(base), entity)
    || Object.keys(payload).some((key) => ![
      "workspace_id", "project_id", "title", "description", "body_json", "body_text", "sort_order",
    ].includes(key))) return false;
  const observed = object(operation.conflictPayload);
  if (observed && (observed.updated_at !== result.server_updated_at || !sameContent(observed, entity))) return false;
  const rebased = await runForegroundSqliteWrite(() => {
    currentActor(authScope, assertActive);
    return getDb().transaction((tx) => {
      const current = tx.select().from(schema.outbox).where(eq(schema.outbox.opId, operation.opId)).get();
      if (!sameSnapshot(current, operation)) return false;
      const changedRow = tx.update(schema.outbox).set({
        baseUpdatedAt: result.server_updated_at!, basePayload: entity,
        conflictPayload: null, lastError: null, retryCount: 0,
      }).where(and(
        outboxMutationSnapshotCondition(operation.opId, mutationSnapshot(operation)),
        isNull(schema.outbox.blockedReason),
      )).run();
      currentActor(authScope, assertActive);
      return Number(changedRow?.changes ?? 0) > 0;
    });
  });
  if (rebased) notifyMutationQueued(authScope);
  return rebased;
}
