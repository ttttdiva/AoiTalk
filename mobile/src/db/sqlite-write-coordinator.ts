type SqliteWritePriority = "foreground" | "background";

type PendingSqliteWrite<T> = {
  task: () => Promise<T> | T;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

const foregroundQueue: Array<PendingSqliteWrite<unknown>> = [];
const backgroundQueue: Array<PendingSqliteWrite<unknown>> = [];

let active = false;

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

  void Promise.resolve()
    .then(() => pending.task())
    .then(
      (value) => {
        pending.resolve(value);
      },
      (error) => {
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
  task: () => Promise<T> | T,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const pending: PendingSqliteWrite<T> = {
      task,
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
  task: () => Promise<T> | T,
): Promise<T> {
  return enqueueSqliteWrite("foreground", task);
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
  task: () => Promise<T> | T,
): Promise<T> {
  return enqueueSqliteWrite("background", task);
}

/**
 * Serialize a background/sync SQLite write.
 *
 * Waiting foreground work takes priority after the currently active write
 * finishes. Background reads that can synchronously block against an
 * exclusive transaction use `runBackgroundSqliteAccess`.
 */
export function runBackgroundSqliteWrite<T>(
  task: () => Promise<T> | T,
): Promise<T> {
  return runBackgroundSqliteAccess(task);
}
