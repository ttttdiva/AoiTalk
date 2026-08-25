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
  return (async () => {
    let result!: T;
    await asAsyncDatabase().withExclusiveTransactionAsync(async (tx) => {
      result = await task(tx);
    });
    return result;
  })();
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
