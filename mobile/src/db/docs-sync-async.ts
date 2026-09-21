/**
 * Raw asynchronous SQLite primitives used by the production Docs sync path.
 *
 * Drizzle's Expo adapter is useful for ordinary repository work, but its
 * transaction builder executes synchronously when it is backed by the
 * `openDatabaseSync` connection used by the mobile client.  Large Docs pulls
 * therefore use the native async transaction API directly.  Keep this module
 * deliberately small: callers own the SQL and every statement executed by a
 * transaction must use the transaction object passed by Expo.
 */

import { getSqlite } from "./client";
import {
  hasPendingForegroundSqliteWrite,
  runBackgroundSqliteWrite,
} from "./sqlite-write-coordinator";

export type DocsSqliteAsyncStatement = {
  executeAsync: (params?: unknown[] | Record<string, unknown>) => Promise<unknown>;
  finalizeAsync: () => Promise<void>;
};

export type DocsSqliteAsyncTransaction = {
  getAllAsync: <T>(source: string, ...params: unknown[]) => Promise<T[]>;
  getFirstAsync: <T>(source: string, ...params: unknown[]) => Promise<T | null>;
  runAsync: (source: string, ...params: unknown[]) => Promise<unknown>;
  prepareAsync?: (source: string) => Promise<DocsSqliteAsyncStatement>;
};

type DocsSqliteAsyncDatabase = {
  withExclusiveTransactionAsync: <T>(
    task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
  ) => Promise<T>;
};

function asAsyncDatabase(): DocsSqliteAsyncDatabase {
  const database = getSqlite() as unknown as Partial<DocsSqliteAsyncDatabase>;
  if (typeof database.withExclusiveTransactionAsync !== "function") {
    throw new Error("Docs同期にはasync SQLite exclusive transactionが必要です");
  }
  return database as DocsSqliteAsyncDatabase;
}

/** True when the native async transaction API is available. */
export function docsSqliteAsyncAvailable(): boolean {
  try {
    const database = getSqlite() as unknown as {
      withExclusiveTransactionAsync?: unknown;
    };
    return typeof database.withExclusiveTransactionAsync === "function";
  } catch {
    return false;
  }
}

/** Run a Docs operation in one native SQLite transaction. */
export function withDocsExclusiveTransaction<T>(
  task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
): Promise<T> {
  return runBackgroundSqliteWrite(async () => {
    let result!: T;
    await asAsyncDatabase().withExclusiveTransactionAsync(async (tx) => {
      result = await task(tx);
    });
    return result;
  });
}

class ForegroundSqliteWriteRequestedError extends Error {
  constructor() {
    super("foreground SQLite write requested");
    this.name = "ForegroundSqliteWriteRequestedError";
  }
}

function yieldIfForegroundWriteIsWaiting(): void {
  if (hasPendingForegroundSqliteWrite()) {
    throw new ForegroundSqliteWriteRequestedError();
  }
}

function foregroundYieldingTransaction(
  tx: DocsSqliteAsyncTransaction,
): DocsSqliteAsyncTransaction {
  const yielding: DocsSqliteAsyncTransaction = {
    getAllAsync: <T>(
      source: string,
      ...params: unknown[]
    ): Promise<T[]> => {
      yieldIfForegroundWriteIsWaiting();
      return tx.getAllAsync<T>(source, ...params);
    },
    getFirstAsync: <T>(
      source: string,
      ...params: unknown[]
    ): Promise<T | null> => {
      yieldIfForegroundWriteIsWaiting();
      return tx.getFirstAsync<T>(source, ...params);
    },
    runAsync: (
      source: string,
      ...params: unknown[]
    ): Promise<unknown> => {
      yieldIfForegroundWriteIsWaiting();
      return tx.runAsync(source, ...params);
    },
  };

  if (tx.prepareAsync) {
    yielding.prepareAsync = async (
      source: string,
    ): Promise<DocsSqliteAsyncStatement> => {
      yieldIfForegroundWriteIsWaiting();
      const statement = await tx.prepareAsync!(source);
      return {
        executeAsync: (
          params?: unknown[] | Record<string, unknown>,
        ): Promise<unknown> => {
          yieldIfForegroundWriteIsWaiting();
          return statement.executeAsync(params);
        },
        // Resource cleanup must never be skipped merely because foreground
        // work arrived while the statement was active.
        finalizeAsync: () => statement.finalizeAsync(),
      };
    };
  }

  return yielding;
}

/**
 * Run a retry-safe long Docs transaction while allowing queued interactive
 * writes to take priority.
 *
 * A foreground request causes the current native transaction to throw and
 * roll back at the next SQLite statement boundary. The coordinator then
 * executes the queued foreground write before this background transaction is
 * re-enqueued. No two SQLite writes run concurrently.
 *
 * Use only for callbacks whose externally visible effects are contained in
 * the SQLite transaction and therefore safe to repeat after rollback.
 */
export async function withForegroundYieldingDocsExclusiveTransaction<T>(
  task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
): Promise<T> {
  for (;;) {
    try {
      return await withDocsExclusiveTransaction(async (tx) => {
        const result = await task(foregroundYieldingTransaction(tx));
        // Close the small window between the final SQL statement and commit.
        yieldIfForegroundWriteIsWaiting();
        return result;
      });
    } catch (error) {
      if (!(error instanceof ForegroundSqliteWriteRequestedError)) {
        throw error;
      }
      // The failed exclusive transaction has rolled back. Re-enqueue the
      // complete background transaction; coordinator priority lets the
      // already-waiting foreground operation run first.
    }
  }
}

/** Drizzle's `text(..., { mode: "json" })` wire representation. */
export function encodeDocsJson(value: unknown): string | null {
  return value == null ? null : JSON.stringify(value);
}

/** SQLite INTEGER representation used by Drizzle's boolean mode. */
export function encodeDocsBoolean(value: unknown): number | null {
  return value == null ? null : value ? 1 : 0;
}

/** Prepare one statement for a bounded batch and always finalize it. */
export async function executeDocsPreparedBatch(
  tx: DocsSqliteAsyncTransaction,
  source: string,
  params: readonly (readonly unknown[])[],
): Promise<void> {
  if (tx.prepareAsync) {
    const statement = await tx.prepareAsync(source);
    try {
      for (const row of params) {
        await statement.executeAsync([...row]);
      }
    } finally {
      await statement.finalizeAsync();
    }
    return;
  }
  // Small test doubles and older rolling-upgrade clients may expose only the
  // convenience method.  The production Expo SQLite 16 connection supports
  // prepareAsync, so this is a compatibility fallback rather than the hot
  // path.
  for (const row of params) {
    await tx.runAsync(source, ...row);
  }
}
