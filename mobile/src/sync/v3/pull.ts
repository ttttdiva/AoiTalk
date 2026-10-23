/**
 * Sync v3 pull: per-scope bootstrap and cursor-based changes.
 *
 * Each HTTP page is applied in exactly one background write unit and one
 * SQLite transaction together with its membership and the durable position
 * (bootstrap position or changes cursor). A failure therefore leaves the
 * previous position, and the next run resumes from the same page.
 *
 * No unconditional full eviction is allowed. An account with unclassified
 * record rows is stopped before v3 HTTP; account-owned queue/conflict rows are
 * protected at scope sweep. A fresh/classified cache can complete with a
 * verified no-op sweep and persist that fact.
 */
import { and, eq } from "drizzle-orm";
import { getDb, schema } from "../../db/client";
import { runBackgroundSqliteWrite } from "../../db/sqlite-write-coordinator";
import type { SqliteHandle, SqliteWriter } from "../../db/sqlite-writer";
import { isApiHttpError } from "../../lib/api-client";
import { rescheduleLocalTaskNotificationsFromCache } from "../../lib/local-notifications";
import { getSyncV3Bootstrap, getSyncV3Changes, getSyncV3Scopes } from "./api";
import {
  SYNC_V3_SWEEP_BATCH_SIZE,
  applySyncV3BootstrapInWriter,
  applySyncV3ChangesInWriter,
  changesToSyncV3Entries,
  dropSyncV3ScopeMembershipInWriter,
  prepareSyncV3Apply,
  syncV3EvictionPrep,
  type SyncV3ApplyEntry,
  type SyncV3ApplyPrep,
  type SyncV3ApplyResult,
} from "./apply";
import {
  emptySyncV3State,
  readSyncV3State,
  readSyncV3StateInWriter,
  writeSyncV3StateInWriter,
} from "./state";
import {
  SYNC_V3_DOCS_TABLES,
  SyncV3CursorExpiredError,
  isSyncV3Table,
  type SyncV3Scope,
  type SyncV3ScopeState,
  type SyncV3ScopesResponse,
  type SyncV3State,
  type SyncV3Table,
} from "./types";
import { markSyncV3RequestWriteFailed } from "../../sync-v3/request-observer";
import { checkSyncV3CacheSafety } from "./safety";

/** Engine-provided run guard. `assertActive` throws when the run was invalidated. */
export type SyncV3RunContext = {
  signal: AbortSignal;
  assertActive: () => void;
  onPage?: (page: number) => void;
};

export type SyncV3PullResult = {
  /** Tables that received at least one applied row/removal in this run. */
  appliedTables: Set<SyncV3Table>;
};

/** Bound on scope/cursor restarts within one run (corrupt server protection). */
const MAX_RESTARTS = 16;
export const SYNC_V3_PULL_MAX_PAGES = 64;
export const SYNC_V3_PULL_BUDGET_MS = 90_000;

export class SyncV3PullBudgetError extends Error {
  constructor() {
    super("同期v3は次のflightで保存済み位置から再開します");
    this.name = "SyncV3PullBudgetError";
  }
}

type BootstrapPositionWithTarget = NonNullable<SyncV3ScopeState["bootstrap"]> & {
  /** Fingerprint this bootstrap is collecting (saved on completion). */
  fingerprint?: string;
};

function cloneState(state: SyncV3State): SyncV3State {
  return JSON.parse(JSON.stringify(state)) as SyncV3State;
}

function userIdOf(authScope: string): string {
  return authScope.startsWith("auth:") ? authScope.slice("auth:".length) : authScope;
}

function docsSource(scope: SyncV3Scope): string {
  if (scope.project_id) return "project";
  return scope.access === "owner" ? "personal" : "shared";
}

function docsWorkspaceKey(authScope: string, scope: { id: string; projectId: string | null }): string {
  return `docs_workspace:v2:${userIdOf(authScope)}:scope:${scope.id}${scope.projectId ? `:project:${scope.projectId}` : ""}`;
}

function putSyncState(tx: SqliteHandle, tableName: string, cursor: string): void {
  tx.insert(schema.syncState)
    .values({ tableName, lastPulledAt: null, lastPushedAt: null, cursor })
    .onConflictDoUpdate({ target: schema.syncState.tableName, set: { cursor } })
    .run();
}

/**
 * Keep the existing Docs read/create projections current from the v3 scope
 * set: `docs_scopes:v2:<user>` (create destinations) and
 * `docs_workspace:v2:<user>…` (visible workspaces for local reads).
 */
function projectDocsScopesInWriter(
  tx: SqliteHandle,
  authScope: string,
  scopes: SyncV3Scope[],
  removed: Array<{ id: string; projectId: string | null }>,
): void {
  const docs = scopes.filter((scope) => scope.kind === "docs");
  putSyncState(tx, `docs_scopes:v2:${userIdOf(authScope)}`, JSON.stringify(docs.map((scope) => ({
    workspace_id: scope.id,
    source: docsSource(scope),
    access: scope.access,
    read_only: scope.read_only,
    project_id: scope.project_id,
  }))));
  for (const scope of docs) {
    putSyncState(tx, docsWorkspaceKey(authScope, { id: scope.id, projectId: scope.project_id }), scope.id);
  }
  const keep = new Set(docs.map((scope) => docsWorkspaceKey(authScope, { id: scope.id, projectId: scope.project_id })));
  for (const item of removed) {
    const key = docsWorkspaceKey(authScope, item);
    if (!keep.has(key)) tx.delete(schema.syncState).where(eq(schema.syncState.tableName, key)).run();
  }
}

function scopeStateFrom(scope: SyncV3Scope, generation: number, previousFingerprint: string): SyncV3ScopeState {
  const tables = scope.tables.filter(isSyncV3Table);
  const bootstrap: BootstrapPositionWithTarget = {
    tableIndex: 0, after: null, fingerprint: scope.fingerprint, seen: 0, total: null,
  };
  return {
    fingerprint: previousFingerprint,
    generation,
    tables,
    bootstrap,
    kind: scope.kind,
    id: scope.id,
    projectId: scope.project_id,
    access: scope.access,
    readOnly: scope.read_only,
  };
}

class SyncV3PullRunner {
  private state: SyncV3State;
  private readonly appliedTables = new Set<SyncV3Table>();
  private page = 0;

  constructor(
    private readonly authScope: string,
    private readonly context: SyncV3RunContext,
    initial: SyncV3State,
    private resetMembership: boolean,
  ) {
    this.state = initial;
    this.resetMembership ||= initial.membershipResetPending === true;
  }

  private nextPage(): void {
    this.page += 1;
    if (this.page > SYNC_V3_PULL_MAX_PAGES) throw new SyncV3PullBudgetError();
    try {
      this.context.onPage?.(this.page);
    } catch {
      // A progress observer must not change the pull.
    }
  }

  /**
   * Run one write unit: `body` receives a scratch copy of the state and the
   * transaction; the copy is persisted in the same transaction and adopted
   * in memory only after the commit.
   */
  private async unit(
    body: (w: SqliteWriter, tx: SqliteHandle, next: SyncV3State) => SyncV3ApplyResult | void,
    requestId?: string,
  ): Promise<void> {
    this.context.assertActive();
    const next = cloneState(this.state);
    const authScope = this.authScope;
    const assertActive = this.context.assertActive;
    let result: SyncV3ApplyResult | null;
    try {
      result = await runBackgroundSqliteWrite((w) => {
        assertActive();
        return getDb().transaction((tx) => {
          const applied = body(w, tx, next);
          writeSyncV3StateInWriter(w, tx, authScope, next);
          return applied ?? null;
        });
      }, "sync-v3");
    } catch (error) {
      if (requestId) await markSyncV3RequestWriteFailed(requestId);
      throw error;
    }
    this.context.assertActive();
    // Adopt the scratch state only after its transaction committed.
    this.state = next;
    if (result) {
      for (const table of result.tables) this.appliedTables.add(table);
      if (result.reschedule) void rescheduleLocalTaskNotificationsFromCache();
    }
  }

  private context_(prep: SyncV3ApplyPrep, next: SyncV3State) {
    return { prep, scopes: next.scopes, now: new Date().toISOString() };
  }

  /** Step 2: reconcile the server scope set (one transaction). */
  private async reconcileScopes(response: SyncV3ScopesResponse, requestId?: string): Promise<void> {
    const prep = syncV3EvictionPrep(this.authScope);
    if (this.resetMembership) {
      let remaining = true;
      while (remaining) {
        await this.unit((_w, tx, next) => {
          const rows = tx.select().from(schema.syncV3Membership)
            .where(eq(schema.syncV3Membership.authScope, this.authScope))
            .limit(SYNC_V3_SWEEP_BATCH_SIZE).all();
          for (const row of rows) {
            tx.delete(schema.syncV3Membership).where(and(
              eq(schema.syncV3Membership.authScope, this.authScope),
              eq(schema.syncV3Membership.tableName, row.tableName),
              eq(schema.syncV3Membership.entityId, row.entityId),
              eq(schema.syncV3Membership.scopeKey, row.scopeKey),
            )).run();
          }
          remaining = rows.length === SYNC_V3_SWEEP_BATCH_SIZE;
          next.membershipResetPending = remaining;
        }, requestId);
        await this.yieldBetweenUnits();
      }
    }
    // Retain the scope's durable state until its last chunk is committed.
    // On cancellation, remaining membership itself is the resumable position.
    const serverKeys = new Set(response.scopes.map((scope) => scope.scope_key));
    for (const scopeKey of Object.keys(this.state.scopes)) {
      if (serverKeys.has(scopeKey)) continue;
      let remaining: boolean;
      do {
        remaining = false;
        await this.unit((w, tx, next) => {
          next.scopes[scopeKey].evictionPending = true;
          const result = dropSyncV3ScopeMembershipInWriter(w, tx, this.context_(prep, next), scopeKey, null);
          remaining = result.remaining;
          return result;
        }, requestId);
        await this.yieldBetweenUnits();
      } while (remaining);
    }
    await this.unit((w, tx, next) => {
      const tables = new Set<SyncV3Table>();
      const reschedule = false;
      const serverKeys = new Set(response.scopes.map((scope) => scope.scope_key));
      const removed: Array<{ id: string; projectId: string | null }> = [];
      for (const [scopeKey, scope] of Object.entries(next.scopes)) {
        if (serverKeys.has(scopeKey)) continue;
        if (scope.kind === "docs" && scope.id) removed.push({ id: scope.id, projectId: scope.projectId ?? null });
        delete next.scopes[scopeKey];
      }
      for (const scope of response.scopes) {
        const saved = next.scopes[scope.scope_key];
        if (!saved) {
          next.scopes[scope.scope_key] = scopeStateFrom(scope, 1, scope.fingerprint);
          continue;
        }
        const pending = saved.bootstrap as BootstrapPositionWithTarget | null;
        // Metadata (access/read-only) always follows the latest server value.
        Object.assign(saved, {
          kind: scope.kind,
          id: scope.id,
          projectId: scope.project_id,
          access: scope.access,
          readOnly: scope.read_only,
        });
        if (saved.evictionPending) {
          next.scopes[scope.scope_key] = scopeStateFrom(scope, saved.generation + 1, saved.fingerprint);
          continue;
        }
        if (pending && (pending.fingerprint ?? saved.fingerprint) === scope.fingerprint) continue;
        if (!pending && saved.fingerprint === scope.fingerprint) continue;
        next.scopes[scope.scope_key] = scopeStateFrom(scope, saved.generation + 1, saved.fingerprint);
      }
      if (next.cursor == null) next.cursor = response.head_cursor;
      next.scopeSetRevision = response.scope_set_revision;
      projectDocsScopesInWriter(tx, this.authScope, response.scopes, removed);
      return { tables, reschedule };
    }, requestId);
    this.resetMembership = false;
  }

  private async yieldBetweenUnits(): Promise<void> {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    this.context.assertActive();
  }

  private pendingScopeKeys(): string[] {
    return Object.keys(this.state.scopes)
      .filter((key) => this.state.scopes[key].bootstrap != null)
      .sort();
  }

  /** Step 3: bootstrap every scope whose bootstrap position is set. Returns false when scopes must be refetched. */
  private async bootstrapPending(): Promise<boolean> {
    for (const scopeKey of this.pendingScopeKeys()) {
      for (;;) {
        const scope = this.state.scopes[scopeKey];
        const position = scope?.bootstrap as BootstrapPositionWithTarget | null | undefined;
        if (!scope || !position) break;
        const table = scope.tables[position.tableIndex];
        let rows: Array<{ entity_id: string; row: Record<string, unknown> }> = [];
        let nextAfter: string | null = null;
        let requestId: string | undefined;
        let totalCount: number | null = position.total ?? null;
        if (table) {
          this.context.assertActive();
          this.nextPage();
          try {
            const response = await getSyncV3Bootstrap(
              { scopeKey, table, after: position.after },
              this.context.signal,
              this.authScope,
              (value) => { requestId = value; },
            );
            rows = response.rows;
            nextAfter = response.next_after;
            if (typeof response.total_count === "number") totalCount = response.total_count;
          } catch (error) {
            // The scope disappeared between /scopes and /bootstrap.
            if (isApiHttpError(error) && error.status === 404) return false;
            throw error;
          }
          this.context.assertActive();
        }
        const entries: SyncV3ApplyEntry[] = rows.map((item) => ({
          kind: "upsert", table: table!, entityId: item.entity_id, row: item.row,
        }));
        const prep = entries.length
          ? await prepareSyncV3Apply(this.authScope, entries)
          : syncV3EvictionPrep(this.authScope);
        await this.unit((w, tx, next) => {
          const target = next.scopes[scopeKey];
          const ctx = this.context_(prep, next);
          const tables = new Set<SyncV3Table>();
          let reschedule = false;
          if (table && rows.length) {
            const applied = applySyncV3BootstrapInWriter(w, tx, ctx, scopeKey, table, rows);
            applied.tables.forEach((item) => tables.add(item));
            reschedule ||= applied.reschedule;
          }
          const current = target.bootstrap as BootstrapPositionWithTarget;
          const seen = (current.seen ?? 0) + rows.length;
          const nextPosition: BootstrapPositionWithTarget = table && nextAfter
            ? { ...current, after: nextAfter, seen, total: totalCount }
            : { ...current, tableIndex: current.tableIndex + 1, after: null, seen: 0, total: null };
          if (nextPosition.tableIndex >= target.tables.length) {
            // Scope complete: drop older generations and evict orphans.
            const dropped = dropSyncV3ScopeMembershipInWriter(w, tx, ctx, scopeKey, target.generation);
            dropped.tables.forEach((item) => tables.add(item));
            reschedule ||= dropped.reschedule;
            if (dropped.remaining) {
              target.bootstrap = { ...nextPosition, tableIndex: target.tables.length, seen, total: totalCount };
            } else {
              target.fingerprint = current.fingerprint ?? target.fingerprint;
              target.lastBootstrap = { seen, total: totalCount };
              target.bootstrap = null;
            }
          } else {
            target.bootstrap = nextPosition;
          }
          return { tables, reschedule };
        }, requestId);
        await this.yieldBetweenUnits();
      }
    }
    return true;
  }

  private async fetchAndReconcile(): Promise<void> {
    this.context.assertActive();
    this.nextPage();
    let requestId: string | undefined;
    const scopes = await getSyncV3Scopes(this.context.signal, this.authScope, (value) => { requestId = value; });
    this.context.assertActive();
    await this.reconcileScopes(scopes, requestId);
  }

  /** Step 5: read changes until has_more=false. Returns "rescope" on a scope-set change. */
  private async readChanges(): Promise<"done" | "rescope"> {
    for (;;) {
      const cursor = this.state.cursor;
      if (cursor == null) return "rescope";
      this.context.assertActive();
      this.nextPage();
      let requestId: string | undefined;
      const response = await getSyncV3Changes(cursor, this.context.signal, undefined, this.authScope,
        (value) => { requestId = value; });
      this.context.assertActive();
      const entries = changesToSyncV3Entries(response.changes);
      const prep = entries.length
        ? await prepareSyncV3Apply(this.authScope, entries)
        : syncV3EvictionPrep(this.authScope);
      await this.unit((w, tx, next) => {
        const applied = response.changes.length
          ? applySyncV3ChangesInWriter(w, tx, this.context_(prep, next), response.changes)
          : undefined;
        next.cursor = response.cursor;
        return applied;
      }, requestId);
      await this.yieldBetweenUnits();
      if (response.scope_set_revision !== this.state.scopeSetRevision) return "rescope";
      if (!response.has_more) return "done";
    }
  }

  private async expireCursor(): Promise<void> {
    await this.unit((_w, _tx, next) => {
      for (const scope of Object.values(next.scopes)) {
        const position: BootstrapPositionWithTarget = {
          tableIndex: 0,
          after: null,
          seen: 0,
          total: null,
          fingerprint: (scope.bootstrap as BootstrapPositionWithTarget | null)?.fingerprint ?? scope.fingerprint,
        };
        scope.generation += 1;
        scope.bootstrap = position;
      }
      next.cursor = null;
      next.fullEvictPending = true;
    });
  }

  async run(): Promise<SyncV3PullResult> {
    const beforeSafety = await checkSyncV3CacheSafety(this.authScope);
    if (!beforeSafety.safe) {
      const code = beforeSafety.reason ?? "scope_sweep_unclassified_cache";
      throw Object.assign(new Error(code), {
        code,
      });
    }
    let needScopes = this.state.cursor == null || this.state.scopeSetRevision == null;
    for (let restart = 0; restart <= MAX_RESTARTS; restart += 1) {
      try {
        if (needScopes) await this.fetchAndReconcile();
        needScopes = false;
        if (!(await this.bootstrapPending())) {
          needScopes = true;
          continue;
        }
        if ((await this.readChanges()) === "rescope") {
          needScopes = true;
          continue;
        }
        const afterSafety = await checkSyncV3CacheSafety(this.authScope);
        if (!afterSafety.safe) {
          const code = afterSafety.reason ?? "scope_sweep_unclassified_cache";
          throw Object.assign(new Error(code), {
            code,
          });
        }
        await this.unit((_w, _tx, next) => {
          next.fullEvictPending = false;
          next.lastSuccessAt = Date.now();
        });
        return { appliedTables: this.appliedTables };
      } catch (error) {
        if (error instanceof SyncV3CursorExpiredError) {
          await this.expireCursor();
          needScopes = true;
          continue;
        }
        throw error;
      }
    }
    throw new Error("同期v3のscope再取得が上限回数を超えました");
  }
}

/** Pull with v3 for one auth scope. Throws SyncV3DisabledError on 503 sync_disabled. */
export async function runV3Pull(
  authScope: string,
  context: SyncV3RunContext,
): Promise<SyncV3PullResult> {
  context.assertActive();
  const controller = new AbortController();
  const expiresAt = Date.now() + SYNC_V3_PULL_BUDGET_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let abortParent: (() => void) | undefined;
  const child: SyncV3RunContext = {
    ...context,
    signal: controller.signal,
    assertActive: () => {
      context.assertActive();
      if (controller.signal.aborted) throw Object.assign(new Error("sync_v3_pull_interrupted"), { name: "AbortError" });
      if (Date.now() >= expiresAt) throw new SyncV3PullBudgetError();
    },
  };
  const stopped = new Promise<never>((_, reject) => {
    abortParent = () => {
      controller.abort();
      try { context.assertActive(); } catch (error) { reject(error); return; }
      reject(Object.assign(new Error("sync_v3_pull_interrupted"), { name: "AbortError" }));
    };
    if (context.signal.aborted) abortParent();
    else context.signal.addEventListener("abort", abortParent, { once: true });
    timer = setTimeout(() => {
      controller.abort();
      reject(new SyncV3PullBudgetError());
    }, SYNC_V3_PULL_BUDGET_MS);
  });
  const flight = (async () => {
    child.assertActive();
    const saved = await readSyncV3State(authScope);
    child.assertActive();
    return new SyncV3PullRunner(authScope, child, saved ?? emptySyncV3State(), saved == null).run();
  })();
  // A transport that ignores abort can settle later; child guards prohibit a
  // subsequent write and its rejection is consumed after the queue is released.
  void flight.catch(() => undefined);
  try {
    return await Promise.race([flight, stopped]);
  } finally {
    controller.abort();
    if (timer !== undefined) clearTimeout(timer);
    if (abortParent) context.signal.removeEventListener("abort", abortParent);
  }
}

/**
 * Mark every Docs scope for a fresh bootstrap (next generation). Rows are not
 * removed until each scope's re-bootstrap completes. Returns whether a v3
 * state existed (without one the next pull bootstraps everything anyway).
 */
export async function requestV3DocsRebootstrap(
  authScope: string,
  context: SyncV3RunContext,
): Promise<boolean> {
  context.assertActive();
  const existed = await runBackgroundSqliteWrite((w) => {
    context.assertActive();
    return getDb().transaction((tx) => {
      const state = readSyncV3StateInWriter(w, tx, authScope);
      if (!state) return false;
      for (const scope of Object.values(state.scopes)) {
        const isDocs = scope.kind === "docs"
          || (scope.kind == null && scope.tables.some((table) => SYNC_V3_DOCS_TABLES.includes(table)));
        if (!isDocs) continue;
        const position: BootstrapPositionWithTarget = {
          tableIndex: 0,
          after: null,
          seen: 0,
          total: null,
          fingerprint: (scope.bootstrap as BootstrapPositionWithTarget | null)?.fingerprint ?? scope.fingerprint,
        };
        scope.generation += 1;
        scope.bootstrap = position;
      }
      writeSyncV3StateInWriter(w, tx, authScope, state);
      return true;
    });
  }, "sync-v3");
  context.assertActive();
  return existed;
}

/** Mark one concrete scope for an authenticated, resumable re-bootstrap. */
export async function requestV3ScopeRebootstrap(
  authScope: string,
  scopeKey: string,
  context: SyncV3RunContext,
): Promise<boolean> {
  context.assertActive();
  const safety = await checkSyncV3CacheSafety(authScope);
  if (!safety.safe) throw Object.assign(new Error(safety.reason ?? "scope_sweep_unclassified_cache"), { code: safety.reason });
  const existed = await runBackgroundSqliteWrite((w) => {
    context.assertActive();
    return getDb().transaction((tx) => {
      const state = readSyncV3StateInWriter(w, tx, authScope);
      const scope = state?.scopes[scopeKey];
      if (!state || !scope) return false;
      const position: BootstrapPositionWithTarget = {
        tableIndex: 0,
        after: null,
        seen: 0,
        total: null,
        fingerprint: (scope.bootstrap as BootstrapPositionWithTarget | null)?.fingerprint ?? scope.fingerprint,
      };
      scope.generation += 1;
      scope.bootstrap = position;
      state.fullEvictPending = true;
      writeSyncV3StateInWriter(w, tx, authScope, state);
      return true;
    });
  }, "sync-v3");
  context.assertActive();
  return existed;
}
