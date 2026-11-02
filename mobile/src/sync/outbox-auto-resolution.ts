import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { assertActiveWriter } from "../db/sqlite-writer";
import { applyRemoteTasks, applyTaskTombstones } from "../repositories/tasks";
import {
  discardOutboxRow,
  hasPendingOutboxInWriter,
  listStuckOutboxRows,
  purgeExpiredOutboxRecords,
  type OutboxResolutionReadSnapshot,
} from "../repositories/outbox";
import {
  createOutboxResolutionFacade,
} from "../repositories/outbox-resolution";
import type { ResolutionPreview, ResolutionResultReason } from "../repositories/outbox-resolution-types";
import type { Task } from "../types/api";

// Created on first use: the sync engine imports this module, and the facade's
// default wiring must not run while that import graph is initializing.
let lazyResolutionFacade: ReturnType<typeof createOutboxResolutionFacade> | null = null;
function getResolutionFacade() {
  lazyResolutionFacade ??= createOutboxResolutionFacade({
    syncState: () => ({ status: "idle" }),
  });
  return lazyResolutionFacade;
}

const TEMPORARY_REASONS = new Set<ResolutionResultReason>([
  "editor_changed",
  "sync_inflight",
  "server_unavailable",
  "context_changed",
  "auth_scope_changed",
  "local_snapshot_changed",
  "local_snapshot_unavailable",
  "related_snapshot_changed",
  "cache_snapshot_changed",
  "membership_changed",
  "transaction_failed",
  "journal_unavailable",
  "outbox_replaced",
  "server_changed",
  "revision_mismatch",
]);
const RESOLUTION_REASONS = new Set<string>([
  "origin_confirmation_required",
  "invalid_input",
  "journal_unavailable",
  "record_not_found",
  "revision_mismatch",
  "context_changed",
  "auth_scope_changed",
  "server_changed",
  "server_unavailable",
  "server_not_found",
  "server_forbidden",
  "server_version_missing",
  "local_snapshot_changed",
  "local_snapshot_unavailable",
  "related_snapshot_changed",
  "membership_changed",
  "cache_snapshot_changed",
  "editor_changed",
  "sync_inflight",
  "duplicate_entity",
  "acl_changed",
  "scope_changed",
  "unsupported",
  "not_prepareable",
  "not_adoptable",
  "outbox_replaced",
  "draft_missing",
  "transaction_failed",
  "export_unavailable",
]);

export type OutboxAutoResolutionResult = {
  adopted: number;
  discarded: number;
  deferred: number;
};

type Step<T> = { value: T } | { error: unknown };

async function safely<T>(operation: () => Promise<T>): Promise<Step<T>> {
  try {
    return { value: await operation() };
  } catch (error) {
    return { error };
  }
}

function reasonFrom(value: unknown): ResolutionResultReason {
  let candidate: unknown = null;
  if (value && typeof value === "object" && "reason" in value) {
    candidate = (value as { reason?: unknown }).reason;
  }
  if (candidate == null && value instanceof Error) candidate = value.message;
  return typeof candidate === "string" && RESOLUTION_REASONS.has(candidate)
    ? candidate as ResolutionResultReason
    : "journal_unavailable";
}

function objectValue(value: unknown): Record<string, unknown> | null {
  let current = value;
  if (typeof current === "string") {
    try {
      current = JSON.parse(current) as unknown;
    } catch {
      return null;
    }
  }
  return current && typeof current === "object" && !Array.isArray(current)
    ? current as Record<string, unknown>
    : null;
}

function matchingEntity(
  row: OutboxResolutionReadSnapshot,
  preview: ResolutionPreview | null,
): Record<string, unknown> | null {
  const inspected = preview?.capture.server.state === "present"
    ? objectValue(preview.capture.server.value)
    : null;
  const conflict = objectValue(row.conflictPayload);
  for (const candidate of [inspected, conflict]) {
    if (candidate && String(candidate.id ?? "") === row.entityId) return candidate;
  }
  return null;
}

function incrementReason(reasons: Map<string, number>, reason: string): void {
  reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
}

async function clearMissingDocsDirty(
  row: OutboxResolutionReadSnapshot,
  authScope: string,
): Promise<void> {
  await runForegroundSqliteWrite((writer) => getDb().transaction((tx) => {
    assertActiveWriter(writer);
    // An edit may have been enqueued after the old snapshot was discarded.
    // In that case the new local write owns the dirty bit.
    if (hasPendingOutboxInWriter(writer, tx, "knowledge_nodes", row.entityId, authScope)) {
      return false;
    }
    tx.update(schema.knowledgeNodes)
      .set({ dirty: false })
      .where(eq(schema.knowledgeNodes.id, row.entityId))
      .run();
    return true;
  }));
}

async function applyServerStateAfterDiscard(
  row: OutboxResolutionReadSnapshot,
  authScope: string,
  reason: ResolutionResultReason,
  preview: ResolutionPreview | null,
): Promise<void> {
  if (row.tableName === "tasks") {
    if (reason === "server_not_found") {
      await applyTaskTombstones([{
        id: row.entityId,
        deleted_at: new Date().toISOString(),
      }]);
      return;
    }
    const entity = matchingEntity(row, preview);
    if (entity) {
      await applyRemoteTasks([entity as unknown as Task]);
    }
    return;
  }

  if (row.tableName === "knowledge_nodes") {
    const entity = matchingEntity(row, preview);
    if (entity) {
      // Lazy require avoids an initialization cycle: the sync engine imports
      // this module for push-stage orchestration.
      const { applyDocsPushEntity } = require("./engine") as {
        applyDocsPushEntity: (
          table: string | undefined,
          value: Record<string, unknown> | undefined,
          scope?: string,
          preservePending?: boolean,
        ) => Promise<void>;
      };
      await applyDocsPushEntity("knowledge_nodes", entity, authScope, false);
    } else {
      await clearMissingDocsDirty(row, authScope);
    }
  }
}

let recordsPurgedAt = 0;
const RECORD_PURGE_INTERVAL_MS = 24 * 60 * 60 * 1000;

/** Internal records expire on their own; failure here must not affect sync. */
async function purgeRecordsOccasionally(): Promise<void> {
  const now = Date.now();
  if (now - recordsPurgedAt < RECORD_PURGE_INTERVAL_MS) return;
  recordsPurgedAt = now;
  await safely(() => purgeExpiredOutboxRecords(now));
}

/** Resolve stale outbox rows around the regular sender without asking the user. */
export async function autoResolveStuckOutbox(
  authScope: string,
  assertActive: () => void,
): Promise<OutboxAutoResolutionResult> {
  const result: OutboxAutoResolutionResult = { adopted: 0, discarded: 0, deferred: 0 };
  const reasonCounts = new Map<string, number>();
  assertActive();
  await purgeRecordsOccasionally();
  assertActive();
  const listed = await safely(() => listStuckOutboxRows(authScope));
  assertActive();
  if (!("value" in listed)) {
    result.deferred = 1;
    incrementReason(reasonCounts, "local_snapshot_unavailable");
  } else if (listed.value.length > 0) {
    const resolutionFacade = getResolutionFacade();
    for (const row of listed.value) {
      assertActive();
      let preview: ResolutionPreview | null = null;
      let capability: object | null = null;
      let failure: ResolutionResultReason | null = null;

      const requested = await safely(() =>
        resolutionFacade.requestOutboxResolutionTarget(row.opId, authScope));
      assertActive();
      if (!("value" in requested)) {
        failure = reasonFrom(requested.error);
      } else {
        const requestResult = requested.value;
        if (requestResult.ok === false) {
          failure = requestResult.reason;
        } else {
        const confirmed = await safely(() =>
          resolutionFacade.confirmOutboxResolutionTarget(requestResult.value.confirmation));
        assertActive();
        if (!("value" in confirmed)) {
          failure = reasonFrom(confirmed.error);
        } else if (confirmed.value.ok === false) {
          failure = confirmed.value.reason;
        } else {
          capability = confirmed.value.value;
          const inspected = await safely(() =>
            resolutionFacade.inspectOutboxResolution(row.opId, authScope, capability!));
          assertActive();
          if (!("value" in inspected)) {
            failure = reasonFrom(inspected.error);
          } else if (inspected.value.ok === false) {
            failure = inspected.value.reason;
          } else {
            preview = inspected.value.value;
            if (preview.view.canAdopt) {
              const prepared = await safely(() =>
                resolutionFacade.prepareOutboxResolution(preview!));
              assertActive();
              if (!("value" in prepared)) {
                failure = reasonFrom(prepared.error);
              } else if (prepared.value.ok === false) {
                failure = prepared.value.reason;
              } else {
                const preparedRecord = prepared.value.value;
                if (preparedRecord.item.state !== "prepared") {
                  failure = "not_prepareable";
                } else {
                const adopted = await safely(() => resolutionFacade.adoptOutboxServer(
                  preparedRecord.item.id,
                  authScope,
                  preparedRecord.item.revision,
                  capability!,
                ));
                assertActive();
                if (!("value" in adopted)) {
                  failure = reasonFrom(adopted.error);
                } else if (adopted.value.ok === false) {
                  failure = adopted.value.reason;
                } else if (adopted.value.value.item.state !== "adopted") {
                  failure = "not_adoptable";
                } else {
                  result.adopted += 1;
                  continue;
                }
                }
              }
            } else {
              failure = typeof preview.view.reason === "string"
                && RESOLUTION_REASONS.has(preview.view.reason)
                ? preview.view.reason as ResolutionResultReason
                : "not_adoptable";
            }
          }
        }
        }
      }

      const reason = failure ?? "not_adoptable";
      incrementReason(reasonCounts, reason);
      if (TEMPORARY_REASONS.has(reason)) {
        result.deferred += 1;
        continue;
      }

      assertActive();
      // An update whose target is gone on the server has nothing to reconcile
      // with, so it is dropped rather than retained.
      const keepRecord = reason !== "server_not_found"
        && !/not found/i.test(row.lastError ?? "");
      const discarded = await safely(() => discardOutboxRow(row, reason, { keepRecord }));
      assertActive();
      if (!("value" in discarded) || !discarded.value) {
        result.deferred += 1;
        incrementReason(reasonCounts, "outbox_replaced");
        continue;
      }
      result.discarded += 1;

      assertActive();
      const localApply = await safely(() =>
        applyServerStateAfterDiscard(row, authScope, reason, preview));
      assertActive();
      if (!("value" in localApply)) {
        result.deferred += 1;
        incrementReason(reasonCounts, "transaction_failed");
      }
    }
  }

  if (result.adopted || result.discarded || result.deferred) {
    try {
      console.info("[sync] outbox auto resolution", JSON.stringify({
        adopted: result.adopted,
        discarded: result.discarded,
        deferred: result.deferred,
        reasons: Object.fromEntries(reasonCounts),
      }));
    } catch {
      // Diagnostics must not affect the sync result.
    }
  }
  return result;
}
