/**
 * Sync v3 push transport: the outbox rows are sent to POST /api/sync/v3/ops
 * and the results are converted to v2 SyncPushResult so that pushOutbox's
 * existing result handling (CAS, rebase, conflict) is reused unchanged.
 */
import { asc, eq } from "drizzle-orm";
import { getDb, schema } from "../../db/client";
import { runForegroundSqliteWrite } from "../../db/sqlite-write-coordinator";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../../db/sqlite-writer";
import {
  allocateV3OpIdentitiesInWriter,
  outboxV3Digest,
} from "../../repositories/outbox";
import { pushSync, type SyncPushOperation, type SyncPushResult } from "../api";
import { postSyncV3Ops } from "./api";
import {
  SYNC_V3_MAX_OPS_BODY_BYTES,
  SYNC_V3_MAX_OPS_PER_REQUEST,
  SyncV3DisabledError,
  type SyncV3Op,
  type SyncV3OpAction,
  type SyncV3OpResult,
} from "./types";

type OutboxRow = typeof schema.outbox.$inferSelect;

/** Codes for which the same op_id is rejected forever (receipt is stored). */
const TERMINAL_REJECT_CODES = new Set(["forbidden", "entity_deleted", "invalid", "unsupported"]);

/** Durable hold for v2 fallback operations whose result is not authoritative. */
type V2FallbackHold = {
  opId: string;
  v3OpId: string;
  v3OpSeq: number;
  digest: string;
  retryCount: number;
  lastError: string | null;
  result?: SyncPushResult;
};

const SYNC_V3_V2_FALLBACK_HOLD_KEY_PREFIX = "sync-v3-v2-fallback-hold:";

export function syncV3V2FallbackHoldKey(authScope: string): string {
  return `${SYNC_V3_V2_FALLBACK_HOLD_KEY_PREFIX}${authScope}`;
}

function parseAuthoritativeV2Result(raw: unknown, expectedOpId: string): SyncPushResult | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const result = raw as Record<string, unknown>;
  if (result.op_id !== expectedOpId
    || (result.status !== "ok" && result.status !== "conflict" && result.status !== "error")) return null;
  if (result.entity !== undefined && result.entity !== null
    && (typeof result.entity !== "object" || Array.isArray(result.entity))) return null;
  if (result.server_updated_at !== undefined
    && result.server_updated_at !== null && typeof result.server_updated_at !== "string") return null;
  if ((result.reason !== undefined && typeof result.reason !== "string")
    || (result.reason_code !== undefined && typeof result.reason_code !== "string")) return null;
  return result as unknown as SyncPushResult;
}

function parseV2FallbackHolds(raw: unknown): V2FallbackHold[] {
  if (typeof raw !== "string") throw new TypeError("sync_v3_v2_fallback_hold_invalid");
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new TypeError("sync_v3_v2_fallback_hold_invalid");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("sync_v3_v2_fallback_hold_invalid");
  }
  const record = value as { version?: unknown; holds?: unknown };
  if ((record.version !== 1 && record.version !== 2) || !Array.isArray(record.holds)) {
    throw new TypeError("sync_v3_v2_fallback_hold_invalid");
  }
  const parsed: V2FallbackHold[] = [];
  for (const hold of record.holds) {
    if (!hold || typeof hold !== "object" || Array.isArray(hold)) {
      throw new TypeError("sync_v3_v2_fallback_hold_invalid");
    }
    const candidate = hold as Record<string, unknown>;
    const opId = candidate.opId;
    const v3OpId = candidate.v3OpId;
    const v3OpSeq = candidate.v3OpSeq;
    const digest = candidate.digest;
    if (typeof opId !== "string" || opId.length === 0) {
      throw new TypeError("sync_v3_v2_fallback_hold_invalid");
    }
    const retryCount = candidate.retryCount ?? (record.version === 1 ? 0 : undefined);
    const lastError = candidate.lastError === undefined && record.version === 1
      ? null
      : candidate.lastError;
    const result = candidate.result === undefined ? undefined : parseAuthoritativeV2Result(candidate.result, opId);
    if (typeof opId !== "string" || opId.length === 0
      || typeof v3OpId !== "string" || v3OpId.length === 0
      || typeof v3OpSeq !== "number" || !Number.isSafeInteger(v3OpSeq) || v3OpSeq < 0
      || typeof digest !== "string" || digest.length === 0
      || typeof retryCount !== "number" || !Number.isSafeInteger(retryCount) || retryCount < 0
      || (lastError !== null && typeof lastError !== "string")
      || (candidate.result !== undefined && result === null)) {
      throw new TypeError("sync_v3_v2_fallback_hold_invalid");
    }
    parsed.push({ opId, v3OpId, v3OpSeq, digest, retryCount, lastError, ...(result ? { result } : {}) });
  }
  if (new Set(parsed.map((hold) => hold.opId)).size !== parsed.length) {
    throw new TypeError("sync_v3_v2_fallback_hold_invalid");
  }
  return parsed;
}

function readV2FallbackHoldsInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
): V2FallbackHold[] {
  assertActiveWriter(w);
  const rows = tx.select({ cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, syncV3V2FallbackHoldKey(authScope)))
    .all();
  if (!rows.length) return [];
  return parseV2FallbackHolds(rows[0].cursor);
}

function writeV2FallbackHoldsInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  holds: V2FallbackHold[],
): void {
  assertActiveWriter(w);
  const key = syncV3V2FallbackHoldKey(authScope);
  if (!holds.length) {
    tx.delete(schema.syncState).where(eq(schema.syncState.tableName, key)).run();
    return;
  }
  tx.insert(schema.syncState)
    .values({
      tableName: key,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor: JSON.stringify({ version: 2, holds }),
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor: JSON.stringify({ version: 2, holds }) },
    })
    .run();
}

/** Same filter as listPendingOutbox (send rows only; reorder excluded). */
function isSendable(row: OutboxRow, authScope: string): boolean {
  return row.authScope === authScope
    && row.blockedReason == null
    && row.action !== "reorder"
    && !(row.tableName === "tasks" && (row.lastError ?? "").startsWith("conflict:")
      && (row.action !== "update" || row.conflictPayload != null))
    && (row.retryCount < 5 || (row.lastError ?? "").startsWith("conflict:"));
}

export function toV2Operation(row: OutboxRow): SyncPushOperation {
  return {
    op_id: row.opId,
    table: row.tableName,
    action: row.action as SyncPushOperation["action"],
    entity_id: row.entityId,
    payload: JSON.parse(row.payload || "{}") as Record<string, unknown>,
    base_updated_at: row.baseUpdatedAt ?? null,
  };
}

function toV3Op(row: OutboxRow): SyncV3Op {
  return {
    op_id: row.v3OpId!,
    op_seq: row.v3OpSeq!,
    table: row.tableName,
    action: row.action as SyncV3OpAction,
    entity_id: row.entityId,
    scope_key: null,
    patch: JSON.parse(row.payload || "{}") as Record<string, unknown>,
    base: { updated_at: row.baseUpdatedAt ?? null },
  };
}

/** The row was already sent under this exact v3 identity before this run. */
function hasSealedV3Identity(row: OutboxRow): boolean {
  return row.v3OpId != null
    && row.v3OpSeq != null
    && row.v3SentDigest === outboxV3Digest(row);
}

function toV2FallbackHold(row: OutboxRow): V2FallbackHold | null {
  if (!row.v3OpId || row.v3OpSeq == null || !row.v3SentDigest) return null;
  return {
    opId: row.opId,
    v3OpId: row.v3OpId,
    v3OpSeq: row.v3OpSeq,
    digest: row.v3SentDigest,
    retryCount: row.retryCount,
    lastError: row.lastError ?? null,
  };
}

function matchesV2FallbackHold(row: OutboxRow, hold: V2FallbackHold): boolean {
  return row.opId === hold.opId
    && row.v3OpId === hold.v3OpId
    && row.v3OpSeq === hold.v3OpSeq
    && row.v3SentDigest === hold.digest
    && outboxV3Digest(row) === hold.digest;
}

function isV2FallbackResultSettled(row: OutboxRow | undefined, hold: V2FallbackHold): boolean {
  if (!hold.result) return false;
  if (!row || !matchesV2FallbackHold(row, hold)) return true;
  return hold.result.status !== "ok" && row.retryCount > hold.retryCount;
}

async function saveV2FallbackHolds(authScope: string, rows: OutboxRow[]): Promise<void> {
  const incoming = rows.map(toV2FallbackHold).filter((hold): hold is V2FallbackHold => hold != null);
  if (!incoming.length) return;
  await runForegroundSqliteWrite((w) => getDb().transaction((tx) => {
    const merged = new Map(readV2FallbackHoldsInWriter(w, tx, authScope).map((hold) => [hold.opId, hold]));
    for (const hold of incoming) merged.set(hold.opId, hold);
    writeV2FallbackHoldsInWriter(w, tx, authScope, [...merged.values()]);
  }), "sync-v3");
}

async function persistV2FallbackResults(
  authScope: string,
  fallbackIds: Set<string>,
  rawResults: unknown,
): Promise<SyncPushResult[]> {
  if (!Array.isArray(rawResults)) return [];
  const counts = new Map<string, number>();
  const valid = new Map<string, SyncPushResult>();
  for (const raw of rawResults) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const opId = (raw as Record<string, unknown>).op_id;
    if (typeof opId !== "string" || !fallbackIds.has(opId)) continue;
    counts.set(opId, (counts.get(opId) ?? 0) + 1);
    const result = parseAuthoritativeV2Result(raw, opId);
    if (result) valid.set(opId, result);
    else valid.delete(opId);
  }
  const acceptedIds = new Set([...valid.keys()].filter((opId) => counts.get(opId) === 1));
  if (!acceptedIds.size) return [];
  const accepted = rawResults.flatMap((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const opId = (raw as Record<string, unknown>).op_id;
    return typeof opId === "string" && acceptedIds.has(opId) ? [valid.get(opId)!] : [];
  });
  await runForegroundSqliteWrite((w) => getDb().transaction((tx) => {
    const holds = readV2FallbackHoldsInWriter(w, tx, authScope);
    const byId = new Map(accepted.map((result) => [result.op_id, result]));
    writeV2FallbackHoldsInWriter(w, tx, authScope, holds.map((hold) => {
      const result = byId.get(hold.opId);
      return result ? { ...hold, result } : hold;
    }));
  }), "sync-v3");
  return accepted;
}

export async function settleV2FallbackHolds(
  authScope: string,
  resultIds: Set<string>,
  assertActive: () => void = () => {},
): Promise<void> {
  if (!resultIds.size) return;
  await runForegroundSqliteWrite((w) => getDb().transaction((tx) => {
    assertActiveWriter(w);
    assertActive();
    const holds = readV2FallbackHoldsInWriter(w, tx, authScope);
    const remaining = holds.filter((hold) => {
      if (!resultIds.has(hold.opId)) return true;
      const current = tx.select().from(schema.outbox)
        .where(eq(schema.outbox.opId, hold.opId)).get();
      return !isV2FallbackResultSettled(current, hold);
    });
    writeV2FallbackHoldsInWriter(w, tx, authScope, remaining);
  }), "sync-v3");
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Split into requests of at most 50 ops and 512KB, keeping order. */
export function chunkV3Ops(
  deviceId: string,
  floor: number,
  ops: SyncV3Op[],
): SyncV3Op[][] {
  const chunks: SyncV3Op[][] = [];
  let current: SyncV3Op[] = [];
  const size = (items: SyncV3Op[]) =>
    byteLength(JSON.stringify({ device_id: deviceId, retirement_floor: floor, ops: items }));
  for (const op of ops) {
    const candidate = [...current, op];
    if (current.length && (candidate.length > SYNC_V3_MAX_OPS_PER_REQUEST
      || size(candidate) > SYNC_V3_MAX_OPS_BODY_BYTES)) {
      chunks.push(current);
      current = [op];
    } else {
      current = candidate;
    }
  }
  if (current.length) chunks.push(current);
  return chunks;
}

export type ConvertedV3Result = {
  result: SyncPushResult;
  /** Set for a permanent rejection: the row is quarantined after markOutboxError. */
  quarantineCode: string | null;
};

/** Convert one v3 op result to the v2 SyncPushResult the engine already handles. */
export function convertV3Result(result: SyncV3OpResult, opId: string): ConvertedV3Result {
  const entity = result.entity ?? undefined;
  const serverUpdatedAt = typeof entity?.updated_at === "string" ? entity.updated_at : null;
  const reasonCode = typeof result.reason_code === "string" && result.reason_code ? result.reason_code : undefined;
  const base = { op_id: opId, entity, server_updated_at: serverUpdatedAt };
  switch (result.status) {
    case "applied":
      return { result: { ...base, status: "ok" }, quarantineCode: null };
    case "applied_partial":
      return {
        result: { ...base, status: result.code === "field_conflict" ? "conflict" : "error", reason_code: reasonCode },
        quarantineCode: null,
      };
    case "rejected": {
      if (result.code === "field_conflict") {
        return { result: { ...base, status: "conflict", reason_code: reasonCode }, quarantineCode: null };
      }
      const code = result.code ?? "invalid";
      return {
        result: { ...base, status: "error", reason_code: reasonCode ?? `sync_v3_${code}` },
        quarantineCode: TERMINAL_REJECT_CODES.has(code) ? code : null,
      };
    }
    case "retry":
    default:
      return {
        result: { op_id: opId, status: "error", reason_code: "sync_operation_failed" },
        quarantineCode: null,
      };
  }
}

export type V3PushOutcome = {
  /** The re-read send rows; the snapshot used by the result handling. */
  pending: OutboxRow[];
  operations: SyncPushOperation[];
  results: SyncPushResult[];
  /** Fallback receipts persisted for settlement by the engine. */
  fallbackResultIds: Set<string>;
  quarantine: Map<string, string>;
  requestIds: string[];
};

/**
 * Allocate identities in one foreground unit, send to /ops in order, and fall
 * back to v2 pushSync (same rows, outbox op ids) when /ops answers 503.
 */
export async function pushOutboxViaV3(
  authScope: string,
  signal: AbortSignal,
  assertActive: () => void,
  onRequestId?: (requestId: string) => void,
): Promise<V3PushOutcome> {
  assertActive();
  const allocation = await runForegroundSqliteWrite((w) => {
    assertActive();
    return getDb().transaction((tx) => {
      const allRows = tx.select().from(schema.outbox)
        .where(eq(schema.outbox.authScope, authScope))
        .orderBy(asc(schema.outbox.createdAt))
        .all();
      const rows = allRows.filter((row) => isSendable(row, authScope));
      const rowsById = new Map(allRows.map((row) => [row.opId, row]));
      const holds = readV2FallbackHoldsInWriter(w, tx, authScope);
      const replayRows: OutboxRow[] = [];
      const replayResults: SyncPushResult[] = [];
      const remainingHolds: V2FallbackHold[] = [];
      for (const hold of holds) {
        const current = rowsById.get(hold.opId);
        if (!current || (hold.result && isV2FallbackResultSettled(current, hold))) continue;
        remainingHolds.push(hold);
        if (hold.result && isSendable(current, authScope) && matchesV2FallbackHold(current, hold)) {
          replayRows.push(current);
          replayResults.push(hold.result);
        }
      }
      writeV2FallbackHoldsInWriter(w, tx, authScope, remainingHolds);
      const heldOpIds = new Set(remainingHolds.map((hold) => hold.opId));
      const sendRows = rows.filter((row) => !heldOpIds.has(row.opId));
      const sealedOpIds = new Set(
        sendRows.filter(hasSealedV3Identity).map((row) => row.opId),
      );
      return {
        ...allocateV3OpIdentitiesInWriter(w, tx, authScope, sendRows),
        sealedOpIds,
        replayRows,
        replayResults,
      };
    });
  }, "sync-v3");
  assertActive();
  const pending = [...allocation.rows, ...allocation.replayRows];
  const operations = pending.map(toV2Operation);
  const results: SyncPushResult[] = [...allocation.replayResults];
  const fallbackResultIds = new Set(allocation.replayResults.map((result) => result.op_id));
  const quarantine = new Map<string, string>();
  const requestIds: string[] = [];
  if (!pending.length) return { pending, operations, results, fallbackResultIds, quarantine, requestIds };
  const byV3Id = new Map(allocation.rows.map((row) => [row.v3OpId!, row]));
  const chunks = chunkV3Ops(allocation.deviceId, allocation.retirementFloor, allocation.rows.map(toV3Op));
  for (let index = 0; index < chunks.length; index += 1) {
    assertActive();
    let response;
    try {
      response = await postSyncV3Ops({
        device_id: allocation.deviceId,
        retirement_floor: allocation.retirementFloor,
        ops: chunks[index],
      }, signal, authScope, (requestId) => {
        requestIds.push(requestId);
        onRequestId?.(requestId);
      });
    } catch (error) {
      if (!(error instanceof SyncV3DisabledError)) throw error;
      const rest = new Set(chunks.slice(index).flat().map((op) => byV3Id.get(op.op_id)!.opId));
      // A previously sent identity must stay on v3: v2 has no op_id receipt
      // and replaying it could apply the same mutation twice. Rows allocated
      // in this run retain the existing fallback behavior.
      const fallbackRows = allocation.rows.filter((row) => rest.has(row.opId) && !allocation.sealedOpIds.has(row.opId));
      if (fallbackRows.length) {
        const fallbackIds = new Set(fallbackRows.map((row) => row.opId));
        await saveV2FallbackHolds(authScope, fallbackRows);
        try {
          const fallback = await pushSync(operations.filter((op) => fallbackIds.has(op.op_id)), signal);
          const fallbackResults = await persistV2FallbackResults(authScope, fallbackIds, fallback.results);
          results.push(...fallbackResults);
          for (const result of fallbackResults) fallbackResultIds.add(result.op_id);
        } catch (fallbackError) {
          // A v2 response does not carry a durable receipt keyed by the v3
          // identity. The sync_state hold was written before the request and
          // stays durable on exception, so auto-resolution never adopts or
          // discards this operation without an authoritative v2 result.
          throw fallbackError;
        }
      }
      break;
    }
    assertActive();
    for (const item of response.results ?? []) {
      const row = byV3Id.get(item.op_id);
      if (!row) continue;
      const converted = convertV3Result(item, row.opId);
      results.push(converted.result);
      if (converted.quarantineCode) quarantine.set(row.opId, converted.quarantineCode);
    }
  }
  return { pending, operations, results, fallbackResultIds, quarantine, requestIds };
}
