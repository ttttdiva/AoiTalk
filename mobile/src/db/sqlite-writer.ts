import type { getDb } from "./client";

declare const sqliteWriterBrand: unique symbol;

/**
 * Proof that the caller is running inside one coordinator write unit.
 *
 * The coordinator hands a fresh writer to every task and invalidates it when
 * the task settles. `xxxInWriter` functions require it so they can never
 * re-enter the coordinator queue (which would wait on the task that is
 * already running them) and can never be used outside a write unit.
 */
export type SqliteWriter = { readonly [sqliteWriterBrand]: true };

type SqliteDb = ReturnType<typeof getDb>;
type SqliteTransaction = Parameters<Parameters<SqliteDb["transaction"]>[0]>[0];

/** What `getDb()` returns, or the `tx` handed to `db.transaction(tx => ...)`. */
export type SqliteHandle = SqliteDb | SqliteTransaction;

const activeWriters = new WeakSet<object>();

/** Coordinator only: open the writer for one write unit. */
export function openSqliteWriter(): SqliteWriter {
  const writer = Object.freeze({}) as unknown as SqliteWriter;
  activeWriters.add(writer as unknown as object);
  return writer;
}

/** Coordinator only: invalidate the writer once its write unit has settled. */
export function closeSqliteWriter(writer: SqliteWriter): void {
  activeWriters.delete(writer as unknown as object);
}

export function assertActiveWriter(writer: SqliteWriter): void {
  if (
    typeof writer !== "object"
    || writer === null
    || !activeWriters.has(writer as unknown as object)
  ) {
    throw new Error(
      "SQLite writer is not active: call this only inside a coordinator write unit",
    );
  }
}
