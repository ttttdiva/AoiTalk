import * as Crypto from "expo-crypto";
import { and, asc, eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../db/sqlite-writer";
import type { LegacyMigrationStatus } from "./contract";
import { isSyncV3ManuallyStopped } from "../sync/v3/safety";

export const LEGACY_CAPTURE_ID = "sync-v3-legacy";
const UNKNOWN_OWNER_SCOPE = "legacy:unknown";
const DIRTY_TABLES = [
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_field_values",
] as const;

type DirtyTable = (typeof DIRTY_TABLES)[number];
export type DirtyCapture = {
  table: DirtyTable;
  entityId: string;
  row: Record<string, unknown>;
  ownerAuthScopes: string[];
  scopeKey: string | null;
  local: Record<string, unknown>;
  base: Record<string, unknown> | null;
  server: Record<string, unknown> | null;
};
export type LegacySnapshot = {
  version: 1;
  outbox: Array<Record<string, unknown>>;
  dirty: DirtyCapture[];
  conflicts: Array<Record<string, unknown>>;
  archived?: LegacySnapshot[];
};

type CaptureRow = typeof schema.syncV3LegacyCapture.$inferSelect;

let captureFlight: Promise<LegacyMigrationStatus> | null = null;

function sortedValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value as Record<string, unknown>).sort()
        .map((key) => [key, sortedValue((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

export function stableLegacyJson(value: unknown): string {
  return JSON.stringify(sortedValue(value));
}

function jsonObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function primaryId(table: DirtyTable, row: Record<string, unknown>): string {
  if (table === "knowledge_node_supertags") return `${String(row.nodeId)}:${String(row.supertagId)}`;
  if (table === "knowledge_field_values") return `${String(row.nodeId)}:${String(row.fieldId)}`;
  return String(row.id);
}

function ownerMembershipKey(table: DirtyTable, entityId: string): string {
  return entityId;
}

function valueOfField(row: Record<string, unknown>): unknown {
  if (row.valueJson !== null && row.valueJson !== undefined) return row.valueJson;
  if (row.valueText !== null && row.valueText !== undefined) return row.valueText;
  if (row.valueNumber !== null && row.valueNumber !== undefined) return row.valueNumber;
  if (row.valueDatetime !== null && row.valueDatetime !== undefined) return row.valueDatetime;
  return row.targetNodeId ?? null;
}

function apiLocalForDirty(
  table: DirtyTable,
  row: Record<string, unknown>,
  relatedFieldValues: Array<Record<string, unknown>> = [],
): Record<string, unknown> {
  switch (table) {
    case "knowledge_nodes":
      return {
        id: row.id,
        workspace_id: row.workspaceId ?? null,
        parent_id: row.parentId ?? null,
        root_page_id: row.rootPageId ?? null,
        project_id: row.projectId ?? null,
        source: row.source ?? null,
        access: row.access ?? null,
        read_only: Boolean(row.readOnly),
        system_key: row.systemKey ?? null,
        title: row.title,
        aliases: Array.isArray(row.aliases) ? row.aliases : [],
        description: row.description ?? null,
        body_json: row.bodyJson ?? null,
        body_text: row.bodyText ?? null,
        node_type: row.nodeType ?? "node",
        display_props: row.displayProps ?? null,
        query_json: row.queryJson ?? null,
        view_json: row.viewJson ?? null,
        day_date: row.dayDate ?? null,
        sort_order: row.sortOrder ?? null,
        created_by: row.createdBy ?? null,
        updated_by: row.updatedBy ?? null,
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
        archived_at: row.archivedAt ?? null,
        body: row.bodyJson ?? row.bodyText ?? null,
        fields: relatedFieldValues.map((field) => ({
          field_id: field.fieldId,
          value: valueOfField(field),
        })),
      };
    case "knowledge_supertags":
      return {
        id: row.id,
        workspace_id: row.workspaceId ?? null,
        parent_supertag_id: row.parentSupertagId ?? null,
        system_key: row.systemKey ?? null,
        name: row.name,
        base_type: row.baseType ?? null,
        description: row.description ?? null,
        icon: row.icon ?? null,
        color: row.color ?? null,
        template_json: row.templateJson ?? null,
        pinned_field_ids: Array.isArray(row.pinnedFieldIds) ? row.pinnedFieldIds : [],
        config_json: row.configJson ?? null,
        title_template: row.titleTemplate ?? null,
        ai_instructions: row.aiInstructions ?? null,
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
      };
    case "knowledge_node_supertags":
      return {
        node_id: row.nodeId,
        supertag_id: row.supertagId,
        created_at: row.createdAt ?? null,
        updated_at: row.updatedAt ?? null,
        created_by: row.createdBy ?? null,
      };
    case "knowledge_field_values":
      return {
        node_id: row.nodeId,
        field_id: row.fieldId,
        value: valueOfField(row),
        value_json: row.valueJson ?? null,
        value_text: row.valueText ?? null,
        value_number: row.valueNumber ?? null,
        value_datetime: row.valueDatetime ?? null,
        target_node_id: row.targetNodeId ?? null,
        updated_at: row.updatedAt ?? null,
      };
  }
}

function dirtyRowsInWriter(tx: SqliteHandle, table: DirtyTable): Array<Record<string, unknown>> {
  if (table === "knowledge_nodes") {
    return tx.select().from(schema.knowledgeNodes)
      .where(eq(schema.knowledgeNodes.dirty, true))
      .orderBy(asc(schema.knowledgeNodes.id)).all() as unknown as Array<Record<string, unknown>>;
  }
  if (table === "knowledge_supertags") {
    return tx.select().from(schema.knowledgeSupertags)
      .where(eq(schema.knowledgeSupertags.dirty, true))
      .orderBy(asc(schema.knowledgeSupertags.id)).all() as unknown as Array<Record<string, unknown>>;
  }
  if (table === "knowledge_node_supertags") {
    return tx.select().from(schema.knowledgeNodeSupertags)
      .where(eq(schema.knowledgeNodeSupertags.dirty, true))
      .orderBy(asc(schema.knowledgeNodeSupertags.nodeId), asc(schema.knowledgeNodeSupertags.supertagId))
      .all() as unknown as Array<Record<string, unknown>>;
  }
  return tx.select().from(schema.knowledgeFieldValues)
    .where(eq(schema.knowledgeFieldValues.dirty, true))
    .orderBy(asc(schema.knowledgeFieldValues.nodeId), asc(schema.knowledgeFieldValues.fieldId))
    .all() as unknown as Array<Record<string, unknown>>;
}

function currentSnapshotInWriter(tx: SqliteHandle): LegacySnapshot {
  const outbox = tx.select().from(schema.outbox)
    .orderBy(asc(schema.outbox.createdAt), asc(schema.outbox.opId)).all()
    .map((row) => ({ ...row })) as unknown as Array<Record<string, unknown>>;
  const conflicts: Array<Record<string, unknown>> = outbox
    .filter((row) => row.conflictPayload != null || String(row.lastError ?? "").startsWith("conflict:"))
    .map((row) => ({ source: "outbox", row }));
  const dirty: DirtyCapture[] = [];
  for (const table of DIRTY_TABLES) {
    const rows = dirtyRowsInWriter(tx, table);
    for (const row of rows) {
      const entityId = primaryId(table, row);
      const memberships = tx.select({
        authScope: schema.docsScopeMembership.authScope,
        scopeKey: schema.docsScopeMembership.scopeKey,
        state: schema.docsScopeMembership.state,
      }).from(schema.docsScopeMembership).where(and(
        eq(schema.docsScopeMembership.tableName, table),
        eq(schema.docsScopeMembership.entityKey, ownerMembershipKey(table, entityId)),
      )).orderBy(asc(schema.docsScopeMembership.authScope), asc(schema.docsScopeMembership.scopeKey)).all();
      const ownerAuthScopes = [...new Set(memberships
        .filter((membership) => membership.state === "active")
        .map((membership) => membership.authScope))].sort();
      const scopeKeys = [...new Set(memberships
        .filter((membership) => membership.state === "active")
        .map((membership) => membership.scopeKey))].sort();
      const relatedFieldValues = table === "knowledge_nodes"
        ? tx.select().from(schema.knowledgeFieldValues)
          .where(eq(schema.knowledgeFieldValues.nodeId, entityId))
          .orderBy(asc(schema.knowledgeFieldValues.fieldId)).all() as unknown as Array<Record<string, unknown>>
        : [];
      const local = apiLocalForDirty(table, row, relatedFieldValues);
      const conflict = jsonObject(row.conflictPayload);
      dirty.push({
        table,
        entityId,
        row,
        ownerAuthScopes,
        scopeKey: scopeKeys.length === 1 ? scopeKeys[0] : null,
        local,
        base: row.serverUpdatedAt ? { updated_at: row.serverUpdatedAt } : null,
        server: conflict,
      });
      if (conflict) conflicts.push({ source: "dirty", table, entityId, row, conflict });
    }
  }
  dirty.sort((a, b) => a.table.localeCompare(b.table) || a.entityId.localeCompare(b.entityId));
  conflicts.sort((a, b) => stableLegacyJson(a).localeCompare(stableLegacyJson(b)));
  return { version: 1, outbox, dirty, conflicts };
}

export function readCurrentDirtyCaptureInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  table: DirtyTable,
  entityId: string,
): DirtyCapture | null {
  assertActiveWriter(w);
  return currentSnapshotInWriter(tx).dirty.find((item) => item.table === table && item.entityId === entityId) ?? null;
}

function ownerUnknownCount(snapshot: LegacySnapshot): number {
  return snapshot.outbox.filter((row) => typeof row.authScope !== "string" || !row.authScope).length
    + snapshot.dirty.filter((row) => row.ownerAuthScopes.length !== 1).length;
}

function snapshotCounts(snapshot: LegacySnapshot) {
  const parts = flattenSnapshotParts(snapshot);
  const outbox = uniqueByStable(parts.flatMap((part) => part.outbox));
  const dirty = uniqueByStable(parts.flatMap((part) => part.dirty));
  const normalized: LegacySnapshot = { version: 1, outbox, dirty, conflicts: [] };
  return {
    captureCount: outbox.length + dirty.length,
    opCount: outbox.length,
    dirtyCount: dirty.length,
    ownerUnknownCount: ownerUnknownCount(normalized),
  };
}

function uniqueByStable<T>(values: T[]): T[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    const key = stableLegacyJson(value);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function activeSnapshot(snapshot: LegacySnapshot): LegacySnapshot {
  return { version: 1, outbox: snapshot.outbox, dirty: snapshot.dirty, conflicts: snapshot.conflicts };
}

function flattenSnapshotParts(snapshot: LegacySnapshot): LegacySnapshot[] {
  return [
    ...(snapshot.archived ?? []).flatMap(flattenSnapshotParts),
    activeSnapshot(snapshot),
  ];
}

function archivedMerge(current: LegacySnapshot, previous: LegacySnapshot): LegacySnapshot {
  return {
    ...current,
    archived: [...(previous.archived ?? []).flatMap(flattenSnapshotParts), activeSnapshot(previous)],
  };
}

function publicStatus(row: CaptureRow | null | undefined): LegacyMigrationStatus {
  if (!row) return {
    state: "not_needed", legacyCount: 0, ownerUnknownCount: 0,
    captureSha256: null, captureFile: null, errorCode: null,
  };
  return {
    state: row.state as LegacyMigrationStatus["state"],
    legacyCount: row.captureCount,
    ownerUnknownCount: row.ownerUnknownCount,
    captureSha256: row.captureSha256,
    captureFile: null,
    errorCode: row.errorCode,
  };
}

function getCaptureInWriter(tx: SqliteHandle): CaptureRow | null {
  return tx.select().from(schema.syncV3LegacyCapture)
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID)).all()[0] ?? null;
}

async function hashText(value: string): Promise<string> {
  return Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, value);
}

/** Capture the whole legacy queue and every dirty Docs row in a stable snapshot. */
export function captureLegacy(): Promise<LegacyMigrationStatus> {
  if (captureFlight) return captureFlight;
  captureFlight = captureLegacyOnce().finally(() => { captureFlight = null; });
  return captureFlight;
}

async function captureLegacyOnce(): Promise<LegacyMigrationStatus> {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const observed = await runForegroundSqliteWrite((w) => {
      return getDb().transaction((tx) => {
        const snapshot = currentSnapshotInWriter(tx);
        return { snapshot, canonical: stableLegacyJson(snapshot), existing: getCaptureInWriter(tx) };
      });
    }, "sync-v3");
    const existingSnapshot = observed.existing
      ? parseSnapshot(observed.existing.snapshotJson)
      : null;
    // Once the legacy queue is migrated, newly dirty Docs rows belong to the
    // normal outbox path. Do not turn every later edit into a new migration gate.
    if (observed.existing?.state === "migrated") return publicStatus(observed.existing);
    const previouslyCapturedDirty = existingSnapshot
      ? uniqueByStable(flattenSnapshotParts(existingSnapshot).flatMap((part) => part.dirty))
      : [];
    const hasNewDirty = existingSnapshot
      ? observed.snapshot.dirty.some((current) => !previouslyCapturedDirty.some((previous) =>
        stableLegacyJson(previous) === stableLegacyJson(current)))
      : observed.snapshot.dirty.length > 0;
    const priorCaptureStillMatches = observed.existing?.state === "awaiting_confirm"
      && existingSnapshot
      && currentMatchesCapture(observed.snapshot, existingSnapshot, observed.existing.sourceCleared);
    if (priorCaptureStillMatches) {
      return publicStatus(observed.existing);
    }
    const snapshotToSave = observed.existing?.state === "awaiting_confirm" && existingSnapshot
      ? archivedMerge(observed.snapshot, existingSnapshot)
      : observed.snapshot;
    const canonicalToSave = stableLegacyJson(snapshotToSave);
    const counts = snapshotCounts(snapshotToSave);
    const digest = await hashText(canonicalToSave);
    const saved = await runForegroundSqliteWrite((w) => {
      return getDb().transaction((tx) => {
        const live = currentSnapshotInWriter(tx);
        const liveCanonical = stableLegacyJson(live);
        if (liveCanonical !== observed.canonical) return { stale: true as const };
        const previous = getCaptureInWriter(tx);
        if (previous?.state === "migrated") {
          return { stale: false as const, row: previous };
        }
        if (counts.captureCount === 0 && !hasNewDirty) {
          const row = {
            id: LEGACY_CAPTURE_ID,
            generation: (previous?.generation ?? 0) + 1,
            captureSha256: digest,
            snapshotJson: canonicalToSave,
            ...counts,
            state: "migrated",
            sourceCleared: false,
            confirmedSha256: digest,
            updatedAt: Date.now(),
            errorCode: null,
          };
          tx.insert(schema.syncV3LegacyCapture).values(row)
            .onConflictDoUpdate({ target: schema.syncV3LegacyCapture.id, set: row }).run();
          return { stale: false as const, row: tx.select().from(schema.syncV3LegacyCapture)
            .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID)).all()[0] };
        }
        const row = {
          id: LEGACY_CAPTURE_ID,
          generation: (previous?.generation ?? 0) + 1,
          captureSha256: digest,
          snapshotJson: canonicalToSave,
          ...counts,
          state: "awaiting_confirm",
          sourceCleared: previous?.state === "awaiting_confirm" ? previous.sourceCleared : false,
          confirmedSha256: null,
          updatedAt: Date.now(),
          errorCode: null,
        };
        tx.insert(schema.syncV3LegacyCapture).values(row)
          .onConflictDoUpdate({ target: schema.syncV3LegacyCapture.id, set: row }).run();
        return { stale: false as const, row: tx.select().from(schema.syncV3LegacyCapture)
          .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID)).all()[0] };
      });
    }, "sync-v3");
    if (saved.stale) continue;
    return publicStatus(saved.row);
  }
  throw Object.assign(new Error("legacy_capture_unstable"), { code: "legacy_capture_unstable" });
}

export async function getLegacyMigrationStatus(): Promise<LegacyMigrationStatus> {
  const row = await getDb().select().from(schema.syncV3LegacyCapture)
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID));
  return publicStatus(row[0] ?? null);
}

export async function getSyncV3LegacyExport(): Promise<string | null> {
  const row = await getDb().select({ snapshotJson: schema.syncV3LegacyCapture.snapshotJson })
    .from(schema.syncV3LegacyCapture)
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID));
  return row[0]?.snapshotJson ?? null;
}

/** Called in the same transaction that clears account-scoped live caches. */
export function markLegacyCaptureSourceClearedInWriter(w: SqliteWriter, tx: SqliteHandle): void {
  assertActiveWriter(w);
  if (getCaptureInWriter(tx)?.state !== "awaiting_confirm") return;
  tx.update(schema.syncV3LegacyCapture).set({ sourceCleared: true, updatedAt: Date.now() })
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID)).run();
}

function parseSnapshot(value: string): LegacySnapshot {
  const parsed = JSON.parse(value) as LegacySnapshot;
  if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.outbox)
    || !Array.isArray(parsed.dirty) || !Array.isArray(parsed.conflicts)
    || (parsed.archived != null && (!Array.isArray(parsed.archived)
      || !parsed.archived.every((item) => item?.version === 1)))) {
    throw Object.assign(new Error("legacy_snapshot_invalid"), { code: "legacy_snapshot_invalid" });
  }
  return parsed;
}

function currentMatchesCapture(
  current: LegacySnapshot,
  captured: LegacySnapshot,
  sourceCleared: boolean,
): boolean {
  const active = activeSnapshot(captured);
  if (!sourceCleared) return stableLegacyJson(current) === stableLegacyJson(active);
  const sameOutbox = stableLegacyJson(current.outbox) === stableLegacyJson(active.outbox);
  const currentDirtySubset = current.dirty.every((item) => active.dirty.some((saved) =>
    stableLegacyJson(saved) === stableLegacyJson(item)));
  const currentConflictsSubset = current.conflicts.every((item) => active.conflicts.some((saved) =>
    stableLegacyJson(saved) === stableLegacyJson(item)));
  return sameOutbox && currentDirtySubset && currentConflictsSubset;
}

function matchingOp(dirty: DirtyCapture, snapshot: LegacySnapshot): Record<string, unknown> | null {
  if (dirty.ownerAuthScopes.length !== 1) return null;
  const owner = dirty.ownerAuthScopes[0];
  return snapshot.outbox.find((op) => op.authScope === owner
    && op.tableName === dirty.table && op.entityId === dirty.entityId) ?? null;
}

function deterministicConflictId(generation: number, index: number, dirty: DirtyCapture): string {
  return `legacy:${generation}:dirty:${index}:${dirty.table}:${encodeURIComponent(dirty.entityId)}`;
}

function insertMigratedConflictInWriter(
  tx: SqliteHandle,
  conflict: typeof schema.syncV3Conflicts.$inferInsert,
): void {
  tx.insert(schema.syncV3Conflicts).values(conflict).onConflictDoNothing().run();
  const saved = tx.select().from(schema.syncV3Conflicts)
    .where(eq(schema.syncV3Conflicts.id, conflict.id!)).all()[0];
  if (!saved
    || saved.authScope !== conflict.authScope
    || saved.tableName !== conflict.tableName
    || saved.entityId !== conflict.entityId
    || saved.scopeKey !== (conflict.scopeKey ?? null)
    || saved.sourceOpId !== (conflict.sourceOpId ?? null)
    || saved.sourceSnapshotJson !== conflict.sourceSnapshotJson
    || saved.localJson !== conflict.localJson) {
    throw Object.assign(new Error("legacy_conflict_id_collision"), { code: "legacy_conflict_id_collision" });
  }
}

function createMigratedConflictInWriter(
  tx: SqliteHandle,
  generation: number,
  index: number,
  dirty: DirtyCapture,
): void {
  const ownerScope = dirty.ownerAuthScopes.length === 1 ? dirty.ownerAuthScopes[0] : UNKNOWN_OWNER_SCOPE;
  const source = stableLegacyJson(dirty);
  const fields = Object.keys(dirty.local).sort();
  insertMigratedConflictInWriter(tx, {
    id: deterministicConflictId(generation, index, dirty),
    authScope: ownerScope,
    tableName: dirty.table,
    entityId: dirty.entityId,
    scopeKey: dirty.scopeKey,
    kind: "migrated_unresolved",
    localJson: stableLegacyJson(dirty.local),
    baseJson: dirty.base ? stableLegacyJson(dirty.base) : null,
    serverJson: dirty.server ? stableLegacyJson(dirty.server) : null,
    fieldsJson: JSON.stringify(fields),
    sourceOpId: null,
    sourceSnapshotJson: source,
    titleHint: String(dirty.local.title ?? dirty.local.name ?? dirty.entityId),
    createdAt: Date.now(),
    state: "open",
    reappliedOpId: null,
  });
}

function deterministicOperationConflictId(generation: number, index: number, operation: Record<string, unknown>): string {
  return `legacy:${generation}:op:${index}:${encodeURIComponent(String(operation.opId ?? operation.op_id ?? "unknown"))}`;
}

function createMigratedOperationConflictInWriter(
  tx: SqliteHandle,
  generation: number,
  index: number,
  operation: Record<string, unknown>,
  linkedDirty: DirtyCapture | null,
): void {
  const payload = jsonObject(operation.payload) ?? {};
  const local = linkedDirty
    ? { ...payload, ...linkedDirty.local, legacy_operation_payload: payload }
    : payload;
  const opId = String(operation.opId ?? operation.op_id ?? "");
  const owner = typeof operation.authScope === "string" && operation.authScope
    ? operation.authScope : UNKNOWN_OWNER_SCOPE;
  const base = jsonObject(operation.basePayload)
    ?? (typeof operation.baseUpdatedAt === "string" ? { updated_at: operation.baseUpdatedAt } : null);
  const server = jsonObject(operation.conflictPayload);
  insertMigratedConflictInWriter(tx, {
    id: deterministicOperationConflictId(generation, index, operation),
    authScope: owner,
    tableName: String(operation.tableName ?? "unknown"),
    entityId: String(operation.entityId ?? "unknown"),
    scopeKey: typeof operation.docsScopeKey === "string" ? operation.docsScopeKey : null,
    kind: "migrated_unresolved",
    localJson: stableLegacyJson(local),
    baseJson: base ? stableLegacyJson(base) : null,
    serverJson: server ? stableLegacyJson(server) : null,
    fieldsJson: JSON.stringify(Object.keys(local).sort()),
    sourceOpId: opId || null,
    sourceSnapshotJson: stableLegacyJson({ operation, linkedDirty }),
    titleHint: String(local.title ?? local.name ?? operation.entityId ?? opId),
    createdAt: Date.now(),
    state: "open",
    reappliedOpId: null,
  });
}

/**
 * Confirm the exact displayed capture.  The migration is one SQLite
 * transaction: all dirty rows are either linked to their same-owner op or
 * recorded as durable conflicts, old outbox rows/retry metadata are untouched,
 * and the capture marker commits only when accounting balances.
 */
export async function migrateLegacy(
  captureSha256: string,
): Promise<{ migrated: boolean; stale: boolean; status: LegacyMigrationStatus }> {
  const resultState: { state: LegacyMigrationStatus["state"] } = { state: "failed" };
  let stale = false;
  await runForegroundSqliteWrite((w) => getDb().transaction((tx) => {
    const capture = getCaptureInWriter(tx);
    if (isSyncV3ManuallyStopped()) {
      resultState.state = capture?.state === "migrated" ? "migrated" : "awaiting_confirm";
      return;
    }
    if (!capture || capture.state !== "awaiting_confirm" || capture.captureSha256 !== captureSha256) {
      resultState.state = capture?.state === "migrated" ? "migrated" : "failed";
      return;
    }
    const snapshot = parseSnapshot(capture.snapshotJson);
    const storedCounts = snapshotCounts(snapshot);
    if (capture.captureCount !== storedCounts.captureCount
      || capture.opCount !== storedCounts.opCount
      || capture.dirtyCount !== storedCounts.dirtyCount) {
      throw Object.assign(new Error("legacy_accounting_mismatch"), { code: "legacy_accounting_mismatch" });
    }
    const current = currentSnapshotInWriter(tx);
    if (!currentMatchesCapture(current, snapshot, capture.sourceCleared)) {
      stale = true;
      resultState.state = "awaiting_confirm";
      return;
    }
    const parts = flattenSnapshotParts(snapshot);
    const capturedOps = uniqueByStable(parts.flatMap((part) => part.outbox));
    const capturedDirty = uniqueByStable(parts.flatMap((part) => part.dirty));
    const operationsSnapshot: LegacySnapshot = { version: 1, outbox: capturedOps, dirty: [], conflicts: [] };
    const currentOps = new Set(current.outbox.map((operation) => stableLegacyJson(operation)));
    let opProtected = 0;
    let knownRecorded = 0;
    let unknown = 0;
    for (const [index, operation] of capturedOps.entries()) {
      const owner = operation.authScope;
      if (typeof owner !== "string" || !owner) {
        if (!currentOps.has(stableLegacyJson(operation))) {
          const linkedDirty = capturedDirty.find((item) => item.ownerAuthScopes.length !== 1
            && item.table === operation.tableName
            && item.entityId === operation.entityId) ?? null;
          createMigratedOperationConflictInWriter(tx, capture.generation, index, operation, linkedDirty);
        }
        unknown += 1;
        continue;
      }
      if (currentOps.has(stableLegacyJson(operation))) {
        opProtected += 1;
        continue;
      }
      const linkedDirty = capturedDirty.find((item) => item.ownerAuthScopes.length === 1
        && item.ownerAuthScopes[0] === owner
        && item.table === operation.tableName
        && item.entityId === operation.entityId) ?? null;
      createMigratedOperationConflictInWriter(tx, capture.generation, index, operation, linkedDirty);
      knownRecorded += 1;
    }
    for (const [index, item] of capturedDirty.entries()) {
      if (matchingOp(item, operationsSnapshot)) {
        opProtected += 1;
        continue;
      }
      createMigratedConflictInWriter(tx, capture.generation, index, item);
      if (item.ownerAuthScopes.length === 1) knownRecorded += 1;
      else unknown += 1;
    }
    const captureCount = capturedOps.length + capturedDirty.length;
    if (opProtected + knownRecorded + unknown !== captureCount) {
      throw Object.assign(new Error("legacy_accounting_mismatch"), { code: "legacy_accounting_mismatch" });
    }
    // Existing outbox conflicts are still represented by their original
    // operation. Existing dirty conflict payloads are copied into the durable
    // unresolved record above; neither path clears source rows.
    tx.update(schema.syncV3LegacyCapture).set({
      state: "migrated",
      confirmedSha256: captureSha256,
      ownerUnknownCount: unknown,
      updatedAt: Date.now(),
      errorCode: null,
    }).where(and(
      eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID),
      eq(schema.syncV3LegacyCapture.generation, capture.generation),
      eq(schema.syncV3LegacyCapture.captureSha256, captureSha256),
      eq(schema.syncV3LegacyCapture.state, "awaiting_confirm"),
    )).run();
    resultState.state = "migrated";
  }), "sync-v3");

  if (stale) {
    // Re-capture into a new generation. The old export remains represented by
    // the previous hash in UI until this refresh completes; no legacy row was
    // changed by the failed CAS transaction.
    const status = await recaptureChangedSnapshot();
    return { migrated: false, stale: true, status };
  }
  const status = await getLegacyMigrationStatus();
  return { migrated: resultState.state === "migrated" && !stale, stale, status };
}

async function recaptureChangedSnapshot(): Promise<LegacyMigrationStatus> {
  const row = await getDb().select().from(schema.syncV3LegacyCapture)
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID));
  if (row[0]?.sourceCleared) return publicStatus(row[0]);
  return captureLegacy();
}

export async function getLegacyCaptureRowForDiagnostics(): Promise<CaptureRow | null> {
  const row = await getDb().select().from(schema.syncV3LegacyCapture)
    .where(eq(schema.syncV3LegacyCapture.id, LEGACY_CAPTURE_ID));
  return row[0] ?? null;
}
