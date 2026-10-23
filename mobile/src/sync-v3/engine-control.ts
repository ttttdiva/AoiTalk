import { useSyncStatusStore } from "../stores/sync-status";
import { canAttemptAoiTalkServer } from "../stores/network";
import { runSync, setSyncExecutionActive } from "../sync/engine";
import { requestV3ScopeRebootstrap } from "../sync/v3/pull";
import {
  getSyncV3Engine,
  registerConflictStore,
  registerSyncV3Engine,
} from "./contract";
import type { SyncV3EngineControl, SyncV3Status, SyncTrigger } from "./contract";
import { getSyncV3Diagnostics } from "./diagnostics";
import { captureLegacy, getLegacyMigrationStatus, migrateLegacy } from "./legacy-migration";
import { createSyncV3ConflictStore } from "./conflict-store";
import {
  subscribeSyncV3RequestDiagnostics,
} from "./request-observer";
import { isSyncV3ManuallyStopped, setSyncV3ManuallyStopped } from "../sync/v3/safety";

const initialLane = () => ({ state: "idle" as const, lastSuccessAt: null, nextAttemptAt: null, lastErrorCode: null });
function initialStatus(): SyncV3Status {
  return {
    authScope: null,
    push: initialLane(),
    changes: initialLane(),
    bootstrap: { ...initialLane(), scopesPending: 0, seen: 0, total: null },
    queue: { queued: 0, backoff: 0, sent: 0 },
    openConflicts: 0,
    legacy: { state: "not_needed", legacyCount: 0, ownerUnknownCount: 0, captureSha256: null, captureFile: null, errorCode: null },
    disabledReason: null,
    overall: "offline",
    cursor: null,
    scopeSetRevision: null,
    scopes: [],
    lastSuccessAt: null,
    entityCounts: {},
    sweepError: null,
    recentRequests: [],
    writeUnits: { count: 0, p95Ms: null, maxMs: null, samples: 0 },
  };
}

function cloneStatus(value: SyncV3Status): SyncV3Status {
  return JSON.parse(JSON.stringify(value)) as SyncV3Status;
}

class SyncV3ProductEngine implements SyncV3EngineControl {
  private status = initialStatus();
  private authScope: string | null = null;
  private generation = 0;
  private startedAt: number | null = null;
  private confirming = false;
  private listeners = new Set<(status: SyncV3Status) => void>();
  private refreshQueued = false;
  private readonly removeRequestObserver = subscribeSyncV3RequestDiagnostics(() => this.queueRefresh());
  private readonly removeSyncStatus = useSyncStatusStore.subscribe(() => this.queueRefresh());

  private publish(status: SyncV3Status): void {
    this.status = status;
    for (const listener of [...this.listeners]) {
      try { listener(cloneStatus(status)); } catch { /* UI observers cannot alter sync */ }
    }
  }

  private queueRefresh(): void {
    if (this.refreshQueued) return;
    this.refreshQueued = true;
    queueMicrotask(() => {
      this.refreshQueued = false;
      void this.refresh();
    });
  }

  private async refresh(): Promise<void> {
    const authScope = this.authScope;
    const generation = this.generation;
    if (!authScope) return;
    try {
      const [diag, legacy] = await Promise.all([
        getSyncV3Diagnostics(authScope),
        getLegacyMigrationStatus(),
      ]);
      if (generation !== this.generation || authScope !== this.authScope) return;
      const stopped = isSyncV3ManuallyStopped();
      const syncState = useSyncStatusStore.getState();
      const pushLane = diag.lane.push;
      const changesLane = diag.lane.changes;
      const bootstrapLane = diag.lane.bootstrap;
      const push = { ...initialLane(), ...pushLane, nextAttemptAt: null };
      const changes = {
        ...initialLane(), ...changesLane,
        lastSuccessAt: changesLane.lastSuccessAt ?? diag.lastSuccessAt,
        nextAttemptAt: null,
      };
      const bootstrap = {
        ...initialLane(),
        ...bootstrapLane,
        nextAttemptAt: null,
        scopesPending: diag.scopesPending,
        seen: diag.bootstrapSeen,
        total: diag.bootstrapTotal,
        state: diag.scopesPending ? "running" as const : bootstrapLane.state,
      };
      const migrationPending = legacy.state !== "migrated" && legacy.state !== "not_needed";
      let overall: SyncV3Status["overall"];
      let disabledReason: string | null = null;
      if (migrationPending) overall = "migration_pending";
      else if (stopped) {
        overall = "disabled";
        disabledReason = "safe_stop";
      } else if (diag.sweepError) {
        overall = "degraded";
        disabledReason = diag.sweepError;
      } else if (!canAttemptAoiTalkServer()) overall = "offline";
      else if (this.startedAt != null
        && Date.now() - (changes.lastSuccessAt ?? diag.lastSuccessAt ?? this.startedAt) >= 10 * 60_000) {
        overall = "degraded";
      }
      else if (syncState.status === "syncing" || push.state === "running" || changes.state === "running"
        || bootstrap.state === "running") overall = "syncing";
      else if (diag.scopesPending || !diag.cursor) overall = "bootstrapping";
      else if (diag.openConflicts || diag.queue.queued || diag.queue.backoff || diag.queue.sent) overall = "pending";
      else overall = "synced";
      this.publish({
        authScope,
        push,
        changes,
        bootstrap,
        queue: diag.queue,
        openConflicts: diag.openConflicts,
        legacy,
        disabledReason,
        overall,
        cursor: diag.cursor,
        scopeSetRevision: diag.scopeSetRevision,
        scopes: diag.scopes.map(({ scopeKey, kind, bootstrap: active }) => ({ scopeKey, kind, bootstrap: active })),
        lastSuccessAt: diag.lastSuccessAt,
        entityCounts: diag.entityCounts,
        sweepError: diag.sweepError,
        recentRequests: diag.recentRequests,
        writeUnits: diag.writeUnits,
      });
    } catch (error) {
      if (generation !== this.generation) return;
      this.publish({
        ...this.status,
        authScope,
        legacy: await getLegacyMigrationStatus().catch(() => ({
          state: "failed", legacyCount: null, ownerUnknownCount: null,
          captureSha256: null, captureFile: null, errorCode: "legacy_status_unavailable",
        })),
        overall: "degraded",
        disabledReason: error instanceof Error ? error.name : "diagnostics_unavailable",
      });
    }
  }

  start(authScope: string): void {
    if (this.authScope === authScope) {
      this.queueRefresh();
      return;
    }
    if (this.authScope && this.authScope !== authScope) setSyncExecutionActive(false);
    this.generation += 1;
    this.authScope = authScope;
    this.startedAt = Date.now();
    setSyncV3ManuallyStopped(false);
    setSyncExecutionActive(true);
    this.publish({ ...initialStatus(), authScope, overall: "migration_pending" });
    const generation = this.generation;
    void captureLegacy().then(() => {
      if (generation === this.generation && this.authScope === authScope) return this.refresh();
      return undefined;
    }).catch(() => this.refresh());
  }

  async stop(): Promise<void> {
    this.generation += 1;
    this.authScope = null;
    this.startedAt = null;
    this.confirming = false;
    setSyncExecutionActive(false);
    this.publish(initialStatus());
  }

  requestSync(_reason: SyncTrigger): void {
    if (!this.authScope) return;
    setSyncV3ManuallyStopped(false);
    setSyncExecutionActive(true);
    const generation = this.generation;
    void runSync().finally(() => {
      if (generation === this.generation) void this.refresh();
    });
  }

  async safeStop(): Promise<void> {
    setSyncV3ManuallyStopped(true);
    setSyncExecutionActive(false);
    await this.refresh();
  }

  async requestRebootstrap(scopeKey: string): Promise<void> {
    const authScope = this.authScope;
    const generation = this.generation;
    if (!authScope || isSyncV3ManuallyStopped()) return;
    const legacy = await getLegacyMigrationStatus();
    if (legacy.state !== "migrated" && legacy.state !== "not_needed") return;
    const controller = new AbortController();
    const context = {
      signal: controller.signal,
      assertActive: () => {
        if (generation !== this.generation || authScope !== this.authScope || controller.signal.aborted) {
          throw new Error("sync_v3_control_stopped");
        }
      },
    };
    try {
      await requestV3ScopeRebootstrap(authScope, scopeKey, context);
      context.assertActive();
      this.requestSync("manual");
    } catch {
      if (generation === this.generation && authScope === this.authScope) await this.refresh();
    }
  }

  async confirmLegacyMigration(captureSha256: string): Promise<void> {
    const authScope = this.authScope;
    const generation = this.generation;
    if (!authScope || this.confirming || isSyncV3ManuallyStopped()) return;
    this.confirming = true;
    this.publish({ ...this.status, legacy: { ...this.status.legacy, state: "migrating" } });
    try {
      const result = await migrateLegacy(captureSha256);
      if (generation !== this.generation || authScope !== this.authScope) return;
      await this.refresh();
      if (result.migrated) this.requestSync("manual");
    } catch (error) {
      if (generation === this.generation && authScope === this.authScope) {
        this.publish({ ...this.status, legacy: {
          ...this.status.legacy,
          state: "failed",
          errorCode: error instanceof Error && typeof (error as Error & { code?: unknown }).code === "string"
            ? (error as Error & { code: string }).code
            : "legacy_migration_failed",
        } });
      }
    } finally {
      if (generation === this.generation) this.confirming = false;
    }
  }

  getStatus(): SyncV3Status { return cloneStatus(this.status); }

  subscribe(listener: (status: SyncV3Status) => void): () => void {
    this.listeners.add(listener);
    listener(this.getStatus());
    return () => this.listeners.delete(listener);
  }

  dispose(): void {
    this.removeRequestObserver();
    this.removeSyncStatus();
    this.listeners.clear();
  }
}

export const syncV3ProductEngine = new SyncV3ProductEngine();
let productRegistrationCount = 0;

/** App entry only: bridge and conflict store are registered once by the product lifecycle. */
export function registerSyncV3ProductRuntime(): () => void {
  if (productRegistrationCount === 0) {
    registerSyncV3Engine(syncV3ProductEngine);
    registerConflictStore(createSyncV3ConflictStore());
  }
  productRegistrationCount += 1;
  return () => {
    productRegistrationCount = Math.max(0, productRegistrationCount - 1);
    if (productRegistrationCount === 0 && getSyncV3Engine() === syncV3ProductEngine) {
      registerSyncV3Engine(null);
      registerConflictStore(null);
      void syncV3ProductEngine.stop();
    }
  };
}
