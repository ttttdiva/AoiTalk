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
  runForegroundSqliteWrite,
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

type DocsSqliteOperation = "get_all" | "get_first" | "run" | "prepare" | "execute" | "finalize";
type DocsSqliteOperationCounters = Record<DocsSqliteOperation, number>;

/** Safe, bounded checkpoint: never retain SQL text, bindings or row contents. */
export type DocsSqliteProgress = {
  awaiting: "writer_queue" | "transaction_begin" | "transaction_body" | "sql" | "native_settlement" | "settled";
  operation: DocsSqliteOperation | null;
  table: string | null;
  /** Cumulative API operations across rollback/retry, never a row count. */
  statements: number;
  completedStatements: number;
  attempts: number;
  operationCounts: DocsSqliteOperationCounters;
  operationElapsedMs: DocsSqliteOperationCounters;
  nativeActive: boolean;
  startedAt: number;
};

type DocsSqliteObserver = (progress: DocsSqliteProgress) => void;

function docsStatementTable(source: string): string {
  // Only fixed schema names may cross the diagnostic boundary.
  return [
    "docs_scope_membership", "docs_sync_staging", "docs_sync_runs", "sync_state", "outbox",
    "knowledge_nodes", "knowledge_supertags", "knowledge_node_supertags",
    "knowledge_supertag_fields", "knowledge_fields", "knowledge_field_values",
    "knowledge_node_placements", "knowledge_edges",
  ].find((table) => new RegExp("\\b" + table + "\\b", "i").test(source)) ?? "other";
}

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

async function executeDocsExclusiveTransaction<T>(
  task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
): Promise<T> {
  let result!: T;
  await asAsyncDatabase().withExclusiveTransactionAsync(async (tx) => {
    result = await task(tx);
  });
  return result;
}

/** Run a Docs operation in one native SQLite transaction. */
export function withDocsExclusiveTransaction<T>(
  task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
): Promise<T> {
  return runBackgroundSqliteWrite(() => executeDocsExclusiveTransaction(task));
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

function guardedDocsTransaction(
  tx: DocsSqliteAsyncTransaction,
  checkActive: () => void,
  report?: (operation: DocsSqliteProgress["operation"], source: string | null) => void,
): DocsSqliteAsyncTransaction {
  const invoke = async <T>(
    operation: DocsSqliteProgress["operation"],
    source: string,
    task: () => Promise<T>,
  ): Promise<T> => {
    report?.(operation, source);
    try { return await task(); }
    finally { report?.(null, null); }
  };
  const yielding: DocsSqliteAsyncTransaction = {
    getAllAsync: <T>(
      source: string,
      ...params: unknown[]
    ): Promise<T[]> => {
      checkActive();
      return invoke("get_all", source, () => tx.getAllAsync<T>(source, ...params));
    },
    getFirstAsync: <T>(
      source: string,
      ...params: unknown[]
    ): Promise<T | null> => {
      checkActive();
      return invoke("get_first", source, () => tx.getFirstAsync<T>(source, ...params));
    },
    runAsync: (
      source: string,
      ...params: unknown[]
    ): Promise<unknown> => {
      checkActive();
      return invoke("run", source, () => tx.runAsync(source, ...params));
    },
  };

  if (tx.prepareAsync) {
    yielding.prepareAsync = async (
      source: string,
    ): Promise<DocsSqliteAsyncStatement> => {
      checkActive();
      const statement = await invoke("prepare", source, () => tx.prepareAsync!(source));
      return {
        executeAsync: (
          params?: unknown[] | Record<string, unknown>,
        ): Promise<unknown> => {
          checkActive();
          return invoke("execute", source, () => statement.executeAsync(params));
        },
        // Resource cleanup must never be skipped merely because foreground
        // work arrived while the statement was active.
        finalizeAsync: () => invoke("finalize", source, () => statement.finalizeAsync()),
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
 * executes the waiting foreground writes before one non-yielding retry. That
 * retry joins the foreground FIFO: later polling writes cannot keep it from
 * starting or repeatedly roll back its progress. No writes run concurrently.
 * Interactive writes arriving during this retry wait for its atomic commit.
 *
 * Use only for callbacks whose externally visible effects are contained in
 * the SQLite transaction and therefore safe to repeat after rollback.
 */
export async function withForegroundYieldingDocsExclusiveTransaction<T>(
  task: (tx: DocsSqliteAsyncTransaction) => Promise<T>,
  assertActive: () => void = () => undefined,
  onProgress?: DocsSqliteObserver,
): Promise<T> {
  let nativeActive = false;
  let statements = 0;
  let completedStatements = 0;
  let attempts = 0;
  let activeOperation: DocsSqliteOperation | null = null;
  let operationStartedAt = 0;
  const operationCounts: DocsSqliteOperationCounters = {
    get_all: 0, get_first: 0, run: 0, prepare: 0, execute: 0, finalize: 0,
  };
  const operationElapsedMs = { ...operationCounts };
  const report = (
    awaiting: DocsSqliteProgress["awaiting"],
    operation: DocsSqliteProgress["operation"] = null,
    source: string | null = null,
  ) => {
    const now = Date.now();
    if (operation) {
      statements += 1;
      operationCounts[operation] += 1;
      activeOperation = operation;
      operationStartedAt = now;
    } else if (activeOperation) {
      operationElapsedMs[activeOperation] += Math.max(0, now - operationStartedAt);
      completedStatements += 1;
      activeOperation = null;
    }
    onProgress?.({
      awaiting, operation, table: source === null ? null : docsStatementTable(source),
      statements, completedStatements, attempts,
      operationCounts: { ...operationCounts }, operationElapsedMs: { ...operationElapsedMs },
      nativeActive, startedAt: now,
    });
  };
  const attempt = async (
    tx: DocsSqliteAsyncTransaction,
    yieldToForeground: boolean,
  ): Promise<T> => {
    const checkActive = () => {
      assertActive();
      if (yieldToForeground) yieldIfForegroundWriteIsWaiting();
    };
    checkActive();
    const result = await task(guardedDocsTransaction(tx, checkActive, (operation, source) =>
      report(operation ? "sql" : "transaction_body", operation, source),
    ));
    // Cancellation must roll back even when it arrives after the final SQL.
    checkActive();
    return result;
  };
  const runAttempt = (foreground: boolean): Promise<T> => {
    report("writer_queue");
    const schedule = foreground ? runForegroundSqliteWrite : runBackgroundSqliteWrite;
    return schedule(async () => {
      // A cancelled queued promotion must not open a transaction in a new scope.
      assertActive();
      attempts += 1;
      nativeActive = true;
      report("transaction_begin");
      try {
        return await executeDocsExclusiveTransaction(async (tx) => {
          report("transaction_body");
          try { return await attempt(tx, !foreground); }
          finally { report("native_settlement"); }
        });
      } finally {
        // Expo settles only after COMMIT/ROLLBACK and connection close.
        nativeActive = false;
        report("settled");
      }
    });
  };
  try {
    return await runAttempt(false);
  } catch (error) {
    if (!(error instanceof ForegroundSqliteWriteRequestedError)) {
      throw error;
    }
    // Native rollback has completed. Join behind already-waiting interactive
    // writes, then finish exactly one atomic retry without voluntary rollback.
    // Real SQLite/callback failures still reject and release the queue.
    return runAttempt(true);
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
