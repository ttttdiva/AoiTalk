import { and, eq, like } from "drizzle-orm";
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
  const [state, outbox, durableConflicts, outboxConflicts, legacy, members, safety, recentRequests] = await Promise.all([
    readSyncV3State(authScope),
    db.select().from(schema.outbox).where(eq(schema.outbox.authScope, authScope)),
    db.select({ id: schema.syncV3Conflicts.id, sourceOpId: schema.syncV3Conflicts.sourceOpId,
      tableName: schema.syncV3Conflicts.tableName, entityId: schema.syncV3Conflicts.entityId })
      .from(schema.syncV3Conflicts).where(and(
      eq(schema.syncV3Conflicts.authScope, authScope), eq(schema.syncV3Conflicts.state, "open"),
    )),
    db.select({ opId: schema.outbox.opId, tableName: schema.outbox.tableName, entityId: schema.outbox.entityId })
      .from(schema.outbox).where(and(
      eq(schema.outbox.authScope, authScope), like(schema.outbox.lastError, "conflict:%"),
    )),
    getLegacyMigrationStatus(),
    db.select({ table: schema.syncV3Membership.tableName, entityId: schema.syncV3Membership.entityId })
      .from(schema.syncV3Membership).where(eq(schema.syncV3Membership.authScope, authScope)),
    checkSyncV3CacheSafety(authScope),
    recentSyncV3Requests(authScope),
  ]);
  const saved = state ?? emptyState();
  const queue = { queued: 0, backoff: 0, sent: 0 };
  for (const item of outbox) {
    if (item.v3OpId) queue.sent += 1;
    else if (item.blockedReason || item.retryCount >= 5) queue.backoff += 1;
    else queue.queued += 1;
  }
  const entityCounts: Record<string, number> = {};
  const memberEntities = new Set(members.map((item) => `${item.table}:${item.entityId}`));
  for (const item of memberEntities) {
    const table = item.slice(0, item.indexOf(":"));
    entityCounts[table] = (entityCounts[table] ?? 0) + 1;
  }
  const conflictIdentities = new Set<string>();
  for (const item of durableConflicts) conflictIdentities.add(item.sourceOpId ?? `durable:${item.id}`);
  for (const item of outboxConflicts) {
    const durable = durableConflicts.some((conflict) => conflict.sourceOpId === item.opId);
    if (!durable) conflictIdentities.add(`outbox:${item.opId}`);
  }
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
    openConflicts: conflictIdentities.size,
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
