import type {
  DocsSqliteAsyncStatement,
  DocsSqliteAsyncTransaction,
} from "./docs-sync-async";

/** Bound native resources even if a future caller generates dynamic SQL. */
export const DOCS_PREPARED_WRITE_CACHE_LIMIT = 32;

/**
 * Keep multi-row writes below SQLite's default bind-parameter limit.  The
 * largest Docs row currently has 28 parameters, so 32 rows use 896 binds.
 * Callers may pass a smaller limit for a wider statement shape.
 */
export const DOCS_MULTIROW_BATCH_SIZE = 32;

/**
 * Execute a fixed-shape INSERT/UPSERT as bounded multi-row statements.
 *
 * The SQL fragments are owned by the caller and must be static source strings;
 * only the VALUES placeholder groups are generated here.  Every batch remains
 * on the caller's existing transaction and write coordinator.  This helper
 * deliberately does not open, commit, or split a transaction.
 */
export async function executeDocsMultiRowBatch(
  tx: DocsSqliteAsyncTransaction,
  insertPrefix: string,
  conflictSuffix: string,
  rows: readonly (readonly unknown[])[],
  maxRows: number = DOCS_MULTIROW_BATCH_SIZE,
): Promise<void> {
  if (!rows.length) return;
  if (!Number.isInteger(maxRows) || maxRows <= 0) {
    throw new Error("Docs multi-row batch size must be a positive integer");
  }
  const width = rows[0].length;
  if (!width) throw new Error("Docs multi-row batch requires at least one column");
  if (rows.some((row) => row.length !== width)) {
    throw new Error("Docs multi-row batch rows must have the same width");
  }
  const placeholders = `(${Array.from({ length: width }, () => "?").join(", ")})`;
  // Expo SQLite 16 exposes prepareAsync on the production connection. Keep
  // the single-row fallback for rolling-upgrade clients and small transaction
  // doubles that expose only runAsync; it preserves their existing semantics
  // while the native production path receives the bridge reduction.
  if (!tx.prepareAsync) {
    const sql = `${insertPrefix} VALUES ${placeholders} ${conflictSuffix}`;
    for (const row of rows) await tx.runAsync(sql, ...row);
    return;
  }
  for (let offset = 0; offset < rows.length; offset += maxRows) {
    const batch = rows.slice(offset, offset + maxRows);
    const sql = `${insertPrefix} VALUES ${batch.map(() => placeholders).join(", ")} ${conflictSuffix}`;
    await tx.runAsync(sql, ...batch.flat());
  }
}

/**
 * Reuse non-returning DML statements inside one existing transaction attempt.
 * Callers must await writes sequentially. The supplied transaction must be the
 * foreground-yielding transaction; its prepare/execute checks are preserved.
 * This helper neither opens nor commits a transaction, and must be recreated
 * by the callback on every rollback/retry. No statements escape the callback.
 */
export async function withDocsPreparedWrites<T>(
  tx: DocsSqliteAsyncTransaction,
  operation: (preparedTx: DocsSqliteAsyncTransaction) => Promise<T>,
): Promise<T> {
  if (!tx.prepareAsync) return operation(tx);
  const statements = new Map<string, DocsSqliteAsyncStatement>();
  const preparedTx: DocsSqliteAsyncTransaction = {
    getAllAsync: <R>(source: string, ...params: unknown[]): Promise<R[]> =>
      tx.getAllAsync<R>(source, ...params),
    getFirstAsync: <R>(source: string, ...params: unknown[]): Promise<R | null> =>
      tx.getFirstAsync<R>(source, ...params),
    prepareAsync: tx.prepareAsync.bind(tx),
    runAsync: async (source: string, ...params: unknown[]): Promise<unknown> => {
      // Expo also accepts a single bindings-array/named-map argument. Keep
      // that overload (and single object/blob arguments) on the original API
      // rather than accidentally introducing an extra array nesting level.
      if (params.length === 1 && params[0] !== null && typeof params[0] === "object") {
        return tx.runAsync(source, ...params);
      }
      // RETURNING results retain a statement cursor; leave their ownership and
      // every non-DML operation with the original convenience API.
      if (!/^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b/i.test(source)
          || /\bRETURNING\b/i.test(source)) {
        return tx.runAsync(source, ...params);
      }
      let statement = statements.get(source);
      if (!statement) {
        if (statements.size >= DOCS_PREPARED_WRITE_CACHE_LIMIT) {
          return tx.runAsync(source, ...params);
        }
        statement = await tx.prepareAsync!(source);
        statements.set(source, statement);
      }
      return statement.executeAsync(params);
    },
  };
  let result!: T;
  let failed = false;
  let primaryError: unknown;
  try {
    result = await operation(preparedTx);
  } catch (error) {
    failed = true;
    primaryError = error;
  }
  let cleanupFailed = false;
  let cleanupError: unknown;
  for (const statement of statements.values()) {
    try {
      await statement.finalizeAsync();
    } catch (error) {
      // Always attempt every resource. In particular, do not hide the original
      // foreground-interruption error or the outer queue would stop retrying.
      if (!cleanupFailed) cleanupError = error;
      cleanupFailed = true;
    }
  }
  if (failed) throw primaryError;
  // A cleanup failure after successful writes must make the outer transaction
  // roll back rather than report a successful, incompletely closed attempt.
  if (cleanupFailed) throw cleanupError;
  return result;
}
