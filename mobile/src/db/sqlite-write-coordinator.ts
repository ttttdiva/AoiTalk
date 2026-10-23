import {
  closeSqliteWriter,
  openSqliteWriter,
  type SqliteWriter,
} from "./sqlite-writer";

type SqliteWritePriority = "foreground" | "background";

type PendingSqliteWrite<T> = {
  task: (writer: SqliteWriter) => Promise<T> | T;
  source: "app" | "sync-v3";
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

const foregroundQueue: Array<PendingSqliteWrite<unknown>> = [];
const backgroundQueue: Array<PendingSqliteWrite<unknown>> = [];

let active = false;
const syncV3WriteSamples: number[] = [];
let syncV3WriteCount = 0;

export type SyncV3WriteUnitStats = { count: number; p95Ms: number | null; maxMs: number | null; samples: number };

export function getSyncV3WriteUnitStats(): SyncV3WriteUnitStats {
  const sorted = [...syncV3WriteSamples].sort((a, b) => a - b);
  return {
    count: syncV3WriteCount,
    p95Ms: sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)] : null,
    maxMs: sorted.length ? sorted[sorted.length - 1] : null,
    samples: sorted.length,
  };
}

function recordFinishedWrite(source: "app" | "sync-v3", startedAt: number): void {
  if (source !== "sync-v3") return;
  syncV3WriteCount += 1;
  syncV3WriteSamples.push(Math.max(0, Date.now() - startedAt));
  if (syncV3WriteSamples.length > 20) syncV3WriteSamples.shift();
}

/**
 * True while interactive SQLite work is queued behind the currently active
 * write. Background work may use this only as a cooperative yield signal;
 * it does not authorize concurrent SQLite writes.
 */
export function hasPendingForegroundSqliteWrite(): boolean {
  return foregroundQueue.length > 0;
}

function nextPendingWrite(): PendingSqliteWrite<unknown> | undefined {
  // An already-running write is never preempted. Once it finishes, prefer
  // interactive work over background work that is still waiting.
  return foregroundQueue.shift() ?? backgroundQueue.shift();
}

function drainSqliteWriteQueue(): void {
  if (active) return;

  const pending = nextPendingWrite();
  if (!pending) return;

  active = true;
  const writer = openSqliteWriter();
  const startedAt = Date.now();

  void Promise.resolve()
    .then(() => pending.task(writer))
    .then(
      (value) => {
        // Invalidate before the caller can observe completion.
        closeSqliteWriter(writer);
        recordFinishedWrite(pending.source, startedAt);
        pending.resolve(value);
      },
      (error) => {
        closeSqliteWriter(writer);
        recordFinishedWrite(pending.source, startedAt);
        pending.reject(error);
      },
    )
    .finally(() => {
      active = false;
      drainSqliteWriteQueue();
    });
}

function enqueueSqliteWrite<T>(
  priority: SqliteWritePriority,
  task: (writer: SqliteWriter) => Promise<T> | T,
  source: "app" | "sync-v3" = "app",
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const pending: PendingSqliteWrite<T> = {
      task,
      source,
      resolve,
      reject,
    };

    if (priority === "foreground") {
      foregroundQueue.push(pending as PendingSqliteWrite<unknown>);
    } else {
      backgroundQueue.push(pending as PendingSqliteWrite<unknown>);
    }

    drainSqliteWriteQueue();
  });
}

/**
 * Serialize an interactive SQLite write.
 *
 * Callers must submit the complete write unit as one callback rather than
 * awaiting another coordinator callback from inside it.
 */
export function runForegroundSqliteWrite<T>(
  task: (writer: SqliteWriter) => Promise<T> | T,
  source: "app" | "sync-v3" = "app",
): Promise<T> {
  return enqueueSqliteWrite("foreground", task, source);
}

/**
 * Serialize a background SQLite access that must not overlap a coordinated
 * exclusive write.
 *
 * Ordinary lightweight reads do not need this queue. Use this entrypoint for
 * background reads implemented through synchronous Expo/Drizzle primitives
 * when entering SQLite while a native exclusive transaction is active could
 * block the JavaScript thread.
 *
 * This remains background priority: merely waiting here does not set the
 * foreground-pending signal used by yielding Docs promotion.
 */
export function runBackgroundSqliteAccess<T>(
  task: (writer: SqliteWriter) => Promise<T> | T,
  source: "app" | "sync-v3" = "app",
): Promise<T> {
  return enqueueSqliteWrite("background", task, source);
}

/**
 * Serialize a background/sync SQLite write.
 *
 * Waiting foreground work takes priority after the currently active write
 * finishes. Background reads that can synchronously block against an
 * exclusive transaction use `runBackgroundSqliteAccess`.
 */
export function runBackgroundSqliteWrite<T>(
  task: (writer: SqliteWriter) => Promise<T> | T,
  source: "app" | "sync-v3" = "app",
): Promise<T> {
  return runBackgroundSqliteAccess(task, source);
}
