import { and, desc, eq, sql } from "drizzle-orm";
import type { ApiRequestLifecycle } from "../lib/api-client";
import { createApiRequestId, isApiHttpError } from "../lib/api-client";
import { getDb, schema } from "../db/client";
import { runBackgroundSqliteWrite } from "../db/sqlite-write-coordinator";
import type { SyncV3RequestDiagnostic, SyncV3RequestOutcome } from "./contract";

export type SyncV3RequestLane = SyncV3RequestDiagnostic["lane"];
export type SyncV3LaneSnapshot = {
  push: { state: "idle" | "running" | "backoff"; lastSuccessAt: number | null; lastErrorCode: string | null };
  changes: { state: "idle" | "running" | "backoff"; lastSuccessAt: number | null; lastErrorCode: string | null };
  bootstrap: { state: "idle" | "running" | "backoff"; lastSuccessAt: number | null; lastErrorCode: string | null };
};

type RequestObserver = {
  requestId: string;
  lifecycle: ApiRequestLifecycle;
  finish: (outcome: SyncV3RequestOutcome, errorCode?: string | null) => Promise<void>;
  fail: (error: unknown) => Promise<void>;
};
type LaneRuntime = { state: "idle" | "running" | "backoff"; lastSuccessAt: number | null; lastErrorCode: string | null };
const laneByScope = new Map<string, Record<SyncV3RequestLane, LaneRuntime>>();
const listeners = new Set<() => void>();

function laneState(scope: string): Record<SyncV3RequestLane, LaneRuntime> {
  let lanes = laneByScope.get(scope);
  if (!lanes) {
    lanes = {
      push: { state: "idle", lastSuccessAt: null, lastErrorCode: null },
      changes: { state: "idle", lastSuccessAt: null, lastErrorCode: null },
      bootstrap: { state: "idle", lastSuccessAt: null, lastErrorCode: null },
      scopes: { state: "idle", lastSuccessAt: null, lastErrorCode: null },
      capabilities: { state: "idle", lastSuccessAt: null, lastErrorCode: null },
    };
    laneByScope.set(scope, lanes);
  }
  return lanes;
}

function emit(): void {
  for (const listener of [...listeners]) {
    try { listener(); } catch { /* diagnostics listeners are observers only */ }
  }
}

export function subscribeSyncV3RequestDiagnostics(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSyncV3LaneSnapshot(authScope: string): SyncV3LaneSnapshot {
  const lanes = laneState(authScope);
  return {
    push: { ...lanes.push },
    changes: { ...lanes.changes },
    bootstrap: { ...lanes.bootstrap },
  };
}

export function beginSyncV3Request(
  authScope: string,
  lane: SyncV3RequestLane,
): RequestObserver {
  const requestId = createApiRequestId(`sync-v3-${lane}`);
  const startedAt = Date.now();
  let attempts = 0;
  let status: number | null = null;
  let finished = false;
  const runtime = laneState(authScope)[lane];
  runtime.state = "running";
  runtime.lastErrorCode = null;
  emit();
  const observer: RequestObserver = {
    requestId,
    lifecycle: {
      requestId,
      onAttempt: () => { attempts += 1; status = null; },
      onResponse: ({ status: nextStatus }) => { status = nextStatus; },
    },
    async finish(outcome, errorCode = null) {
      if (finished) return;
      finished = true;
      const occurredAt = Date.now();
      const diagnostic: SyncV3RequestDiagnostic = {
        requestId,
        lane,
        outcome,
        status,
        occurredAt,
        durationMs: Math.max(0, occurredAt - startedAt),
        errorCode: errorCode ?? null,
      };
      if (outcome === "ok") {
        runtime.state = "idle";
        runtime.lastSuccessAt = occurredAt;
        runtime.lastErrorCode = null;
      } else {
        runtime.state = "backoff";
        runtime.lastErrorCode = errorCode ?? outcome;
      }
      emit();
      try {
        await runBackgroundSqliteWrite((writer) => {
          const db = getDb();
          db.insert(schema.syncV3RequestLogs).values({
            requestId,
            authScope,
            lane,
            outcome,
            status,
            occurredAt,
            durationMs: diagnostic.durationMs,
            errorCode: diagnostic.errorCode,
          }).onConflictDoUpdate({
            target: schema.syncV3RequestLogs.requestId,
            set: {
              outcome,
              status,
              occurredAt,
              durationMs: diagnostic.durationMs,
              errorCode: diagnostic.errorCode,
            },
          }).run();
          // Retain at most 500 canonical request records across scopes.
          db.run(sql`DELETE FROM sync_v3_request_logs WHERE request_id NOT IN (
            SELECT request_id FROM sync_v3_request_logs ORDER BY occurred_at DESC LIMIT 500
          )`);
          void writer;
        }, "sync-v3");
      } catch {
        // Persistence of telemetry never changes the sync request result.
      }
    },
    async fail(error) {
      const code = isApiHttpError(error)
        ? `http_${error.status}`
        : attempts === 0
          ? "not_sent"
          : status === null
            ? "no_response"
            : "response_invalid";
      const outcome: SyncV3RequestOutcome = attempts === 0
        ? "not_sent"
        : status === null
          ? "no_response"
          : "http_error";
      await observer.finish(outcome, code);
    },
  };
  return observer;
}

export async function markSyncV3RequestWriteFailed(requestId: string, errorCode = "write_failed"): Promise<void> {
  const rows = await getDb().select({ authScope: schema.syncV3RequestLogs.authScope,
    lane: schema.syncV3RequestLogs.lane, outcome: schema.syncV3RequestLogs.outcome })
    .from(schema.syncV3RequestLogs).where(eq(schema.syncV3RequestLogs.requestId, requestId));
  if (!rows[0] || rows[0].outcome !== "ok") return;
  const runtime = laneState(rows[0].authScope)[rows[0].lane as SyncV3RequestLane];
  if (runtime) {
    runtime.state = "backoff";
    runtime.lastErrorCode = errorCode;
  }
  await runBackgroundSqliteWrite((writer) => {
    const db = getDb();
    db.update(schema.syncV3RequestLogs).set({ outcome: "write_failed", errorCode })
      .where(and(eq(schema.syncV3RequestLogs.requestId, requestId), eq(schema.syncV3RequestLogs.outcome, "ok"))).run();
    void writer;
  }, "sync-v3").catch(() => undefined);
  emit();
}

export async function markLatestSyncV3RequestWriteFailed(
  authScope: string,
  lane: SyncV3RequestLane,
  errorCode = "write_failed",
): Promise<void> {
  const latest = await getDb().select({ requestId: schema.syncV3RequestLogs.requestId })
    .from(schema.syncV3RequestLogs).where(and(
      eq(schema.syncV3RequestLogs.authScope, authScope),
      eq(schema.syncV3RequestLogs.lane, lane),
      eq(schema.syncV3RequestLogs.outcome, "ok"),
    )).orderBy(desc(schema.syncV3RequestLogs.occurredAt)).limit(1);
  if (latest[0]) await markSyncV3RequestWriteFailed(latest[0].requestId, errorCode);
}

export async function recentSyncV3Requests(authScope: string): Promise<SyncV3RequestDiagnostic[]> {
  const rows = await getDb().select().from(schema.syncV3RequestLogs)
    .where(eq(schema.syncV3RequestLogs.authScope, authScope))
    .orderBy(desc(schema.syncV3RequestLogs.occurredAt)).limit(20);
  return rows.reverse().map((row) => ({
    requestId: row.requestId,
    lane: row.lane as SyncV3RequestLane,
    outcome: row.outcome as SyncV3RequestOutcome,
    status: row.status,
    occurredAt: row.occurredAt,
    durationMs: row.durationMs,
    errorCode: row.errorCode,
  }));
}

export function resetSyncV3RequestRuntime(authScope?: string): void {
  if (authScope) laneByScope.delete(authScope);
  else laneByScope.clear();
  emit();
}
