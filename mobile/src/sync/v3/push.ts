/**
 * Sync v3 push transport: the outbox rows are sent to POST /api/sync/v3/ops
 * and the results are converted to v2 SyncPushResult so that pushOutbox's
 * existing result handling (CAS, rebase, conflict) is reused unchanged.
 */
import { asc, eq } from "drizzle-orm";
import { getDb, schema } from "../../db/client";
import { runForegroundSqliteWrite } from "../../db/sqlite-write-coordinator";
import { allocateV3OpIdentitiesInWriter } from "../../repositories/outbox";
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
      const rows = tx.select().from(schema.outbox)
        .where(eq(schema.outbox.authScope, authScope))
        .orderBy(asc(schema.outbox.createdAt))
        .all()
        .filter((row) => isSendable(row, authScope));
      return allocateV3OpIdentitiesInWriter(w, tx, authScope, rows);
    });
  }, "sync-v3");
  assertActive();
  const pending = allocation.rows;
  const operations = pending.map(toV2Operation);
  const results: SyncPushResult[] = [];
  const quarantine = new Map<string, string>();
  const requestIds: string[] = [];
  if (!pending.length) return { pending, operations, results, quarantine, requestIds };
  const byV3Id = new Map(pending.map((row) => [row.v3OpId!, row]));
  const chunks = chunkV3Ops(allocation.deviceId, allocation.retirementFloor, pending.map(toV3Op));
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
      const fallback = await pushSync(operations.filter((op) => rest.has(op.op_id)), signal);
      results.push(...fallback.results);
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
  return { pending, operations, results, quarantine, requestIds };
}
