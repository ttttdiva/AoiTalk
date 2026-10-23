import { and, eq, like, sql, countDistinct, notExists } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getSyncV3WriteUnitStats } from "../db/sqlite-write-coordinator";
import { readSyncV3State } from "../sync/v3/state";
import type { SyncV3State, SyncV3Table } from "../sync/v3/types";
import { checkSyncV3CacheSafety } from "../sync/v3/safety";
import { getSyncV3LaneSnapshot, recentSyncV3Requests } from "./request-observer";
import { getLegacyMigrationStatus } from "./legacy-migration";

export type SyncV3DiagnosticsSnapshot = {
  authScope: string;
  cursor: string | null;
  scopeSetRevision: string | null;
  scopes: Array<{ scopeKey: string; kind: string | null; bootstrap: boolean; seen: number; total: number | null }>;
  scopesPending: number;
  bootstrapSeen: number;
  bootstrapTotal: number | null;
  lastSuccessAt: number | null;
  entityCounts: Record<string, number>;
  queue: { queued: number; backoff: number; sent: number };
  openConflicts: number;
  legacy: Awaited<ReturnType<typeof getLegacyMigrationStatus>>;
  sweepError: string | null;
  unclassifiedRecords: number;
  lane: ReturnType<typeof getSyncV3LaneSnapshot>;
  recentRequests: Awaited<ReturnType<typeof recentSyncV3Requests>>;
  writeUnits: ReturnType<typeof getSyncV3WriteUnitStats>;
};

function emptyState(): SyncV3State {
  return { v: 1, cursor: null, scopeSetRevision: null, fullEvictPending: true, lastSuccessAt: null, scopes: {} };
}

export async function getSyncV3Diagnostics(authScope: string): Promise<SyncV3DiagnosticsSnapshot> {
  const db = getDb();
  const [state, queueCounts, durableConflicts, outboxConflicts, legacy, members, safety, recentRequests] = await Promise.all([
    readSyncV3State(authScope),
    db.select({
      queued: sql<number>`coalesce(sum(case when ${schema.outbox.v3OpId} is null and ${schema.outbox.blockedReason} is null and coalesce(${schema.outbox.retryCount}, 0) < 5 then 1 else 0 end), 0)`,
      backoff: sql<number>`coalesce(sum(case when ${schema.outbox.v3OpId} is null and (${schema.outbox.blockedReason} is not null or ${schema.outbox.retryCount} >= 5) then 1 else 0 end), 0)`,
      sent: sql<number>`coalesce(sum(case when ${schema.outbox.v3OpId} is not null then 1 else 0 end), 0)`,
    }).from(schema.outbox).where(eq(schema.outbox.authScope, authScope)),
    db.select({ value: countDistinct(sql`coalesce(${schema.syncV3Conflicts.sourceOpId}, 'durable:' || ${schema.syncV3Conflicts.id})`) })
      .from(schema.syncV3Conflicts).where(and(
        eq(schema.syncV3Conflicts.authScope, authScope), eq(schema.syncV3Conflicts.state, "open"),
      )),
    db.select({ value: sql<number>`count(*)` }).from(schema.outbox).where(and(
      eq(schema.outbox.authScope, authScope), like(schema.outbox.lastError, "conflict:%"),
      notExists(db.select({ one: sql`1` }).from(schema.syncV3Conflicts).where(and(
        eq(schema.syncV3Conflicts.authScope, authScope), eq(schema.syncV3Conflicts.state, "open"),
        eq(schema.syncV3Conflicts.sourceOpId, schema.outbox.opId),
      ))),
    )),
    getLegacyMigrationStatus(),
    db.select({ table: schema.syncV3Membership.tableName, value: countDistinct(schema.syncV3Membership.entityId) })
      .from(schema.syncV3Membership).where(eq(schema.syncV3Membership.authScope, authScope))
      .groupBy(schema.syncV3Membership.tableName),
    checkSyncV3CacheSafety(authScope),
    recentSyncV3Requests(authScope),
  ]);
  const saved = state ?? emptyState();
  const queue = {
    queued: Number(queueCounts[0]?.queued ?? 0),
    backoff: Number(queueCounts[0]?.backoff ?? 0),
    sent: Number(queueCounts[0]?.sent ?? 0),
  };
  const entityCounts: Record<string, number> = Object.fromEntries(
    members.map((item) => [item.table, Number(item.value)]),
  );
  const openConflicts = Number(durableConflicts[0]?.value ?? 0) + Number(outboxConflicts[0]?.value ?? 0);
  const scopes = Object.entries(saved.scopes).sort(([a], [b]) => a.localeCompare(b)).map(([scopeKey, value]) => ({
    scopeKey,
    kind: value.kind ?? null,
    bootstrap: value.bootstrap !== null,
    seen: value.bootstrap?.seen ?? value.lastBootstrap?.seen ?? 0,
    total: value.bootstrap?.total ?? value.lastBootstrap?.total ?? null,
  }));
  const pending = scopes.filter((scope) => scope.bootstrap);
  const progressScopes = pending.length
    ? pending
    : scopes.filter((scope) => scope.seen > 0 || scope.total !== null);
  const bootstrapSeen = progressScopes.reduce((sum, scope) => sum + scope.seen, 0);
  const bootstrapTotal = progressScopes.length === 0 || progressScopes.some((scope) => scope.total == null)
    ? null
    : progressScopes.reduce((sum, scope) => sum + (scope.total ?? 0), 0);
  return {
    authScope,
    cursor: saved.cursor,
    scopeSetRevision: saved.scopeSetRevision,
    scopes,
    scopesPending: pending.length,
    bootstrapSeen,
    bootstrapTotal,
    lastSuccessAt: saved.lastSuccessAt ?? null,
    entityCounts,
    queue,
    openConflicts,
    legacy,
    sweepError: safety.safe ? null : safety.reason,
    unclassifiedRecords: safety.unclassifiedRecords,
    lane: getSyncV3LaneSnapshot(authScope),
    recentRequests,
    writeUnits: getSyncV3WriteUnitStats(),
  };
}

export function entityCountForTable(
  counts: Record<string, number>,
  table: SyncV3Table,
): number {
  return counts[table] ?? 0;
}
