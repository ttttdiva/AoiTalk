import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken, getTokenAuthScope } from "../lib/auth";

export type TombstoneLedger = { storageKey: string; entries: Map<string, string> };
const memoryLedgers = new Map<string, Map<string, string>>();

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

export async function loadTombstoneLedger(prefix: string): Promise<TombstoneLedger> {
  const storageKey = `${prefix}:${await authScope()}`;
  const entries = new Map(memoryLedgers.get(storageKey) ?? []);
  try {
    const rows = await getDb()
      .select({ cursor: schema.syncState.cursor })
      .from(schema.syncState)
      .where(eq(schema.syncState.tableName, storageKey));
    const raw = rows[0]?.cursor;
    if (typeof raw === "string") {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      for (const [id, value] of Object.entries(parsed ?? {})) {
        if (typeof value === "string") entries.set(id, value);
      }
    }
  } catch {
    // Legacy test/DB doubles may not have sync_state; the current process
    // still guards rows through their local deleted_at column.
  }
  memoryLedgers.set(storageKey, entries);
  return { storageKey, entries };
}

export async function persistTombstoneLedger(ledger: TombstoneLedger): Promise<void> {
  memoryLedgers.set(ledger.storageKey, new Map(ledger.entries));
  try {
    await getDb()
      .insert(schema.syncState)
      .values({
        tableName: ledger.storageKey,
        lastPulledAt: null,
        lastPushedAt: null,
        cursor: JSON.stringify(Object.fromEntries(ledger.entries)),
      })
      .onConflictDoUpdate({
        target: schema.syncState.tableName,
        set: { cursor: JSON.stringify(Object.fromEntries(ledger.entries)) },
      });
  } catch {
    // Keep the row-level tombstone when a legacy DB cannot persist the ledger.
  }
}
