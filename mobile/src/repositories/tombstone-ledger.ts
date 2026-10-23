import { eq } from "drizzle-orm";
import { schema } from "../db/client";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../db/sqlite-writer";
import { getToken, getTokenAuthScope } from "../lib/auth";

export type TombstoneLedger = { storageKey: string; entries: Map<string, string> };

function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

export function compareTombstoneTimestamps(
  left: string | null | undefined,
  right: string | null | undefined,
): number {
  const leftMs = parseTimestamp(left);
  const rightMs = parseTimestamp(right);
  if (leftMs !== null && rightMs !== null) return leftMs === rightMs ? 0 : leftMs < rightMs ? -1 : 1;
  if (left == null && right == null) return 0;
  if (left == null) return -1;
  if (right == null) return 1;
  return left === right ? 0 : left < right ? -1 : 1;
}

async function authScope(): Promise<string> {
  try {
    const token = await getToken();
    return getTokenAuthScope(token) ?? "anonymous";
  } catch {
    return "anonymous";
  }
}

/** Async preparation (token read) for the InWriter ledger functions. */
export async function prepareTombstoneLedgerKey(prefix: string): Promise<string> {
  return `${prefix}:${await authScope()}`;
}

/**
 * Synchronous ledger load for a write unit. The ledger lives only in
 * sync_state; write failures propagate and roll back the caller's transaction
 * together with the ledger. A corrupt cursor JSON is treated as empty.
 */
export function loadTombstoneLedgerInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  storageKey: string,
): TombstoneLedger {
  assertActiveWriter(writer);
  const entries = new Map<string, string>();
  const rows = tx
    .select({ cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, storageKey))
    .all();
  const raw = rows[0]?.cursor;
  if (typeof raw === "string") {
    let parsed: Record<string, unknown> | null = null;
    try {
      parsed = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      parsed = null;
    }
    for (const [id, value] of Object.entries(parsed ?? {})) {
      if (typeof value === "string") entries.set(id, value);
    }
  }
  return { storageKey, entries };
}

export function persistTombstoneLedgerInWriter(
  writer: SqliteWriter,
  tx: SqliteHandle,
  ledger: TombstoneLedger,
): void {
  assertActiveWriter(writer);
  const cursor = JSON.stringify(Object.fromEntries(ledger.entries));
  tx.insert(schema.syncState)
    .values({
      tableName: ledger.storageKey,
      lastPulledAt: null,
      lastPushedAt: null,
      cursor,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor },
    })
    .run();
}
