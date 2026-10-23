/**
 * Sync v3 durable state in sync_state.cursor (JSON).
 *
 * - `v3:<authScope>`         cursor / scope set / per-scope bootstrap position
 * - `v3-device:<authScope>`  device_id and the next op_seq for /ops
 *
 * Malformed JSON is "no state" (like the tombstone ledger). SQL errors propagate.
 */
import { eq } from "drizzle-orm";
import { getDb, schema } from "../../db/client";
import { assertActiveWriter, type SqliteHandle, type SqliteWriter } from "../../db/sqlite-writer";
import {
  isSyncV3Table,
  type SyncV3DeviceState,
  type SyncV3ScopeState,
  type SyncV3State,
} from "./types";

export function syncV3StateKey(authScope: string): string {
  return `v3:${authScope}`;
}

/** Prefix of the per-account device identity rows; kept across auth cache clears. */
export const SYNC_V3_DEVICE_KEY_PREFIX = "v3-device:";

export function syncV3DeviceKey(authScope: string): string {
  return `${SYNC_V3_DEVICE_KEY_PREFIX}${authScope}`;
}

export function emptySyncV3State(): SyncV3State {
  return { v: 1, cursor: null, scopeSetRevision: null, fullEvictPending: true, lastSuccessAt: null, scopes: {} };
}

function parseJson(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== "string") return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function parseScopeState(value: unknown): SyncV3ScopeState | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (typeof item.fingerprint !== "string") return null;
  if (!Number.isSafeInteger(item.generation) || (item.generation as number) < 1) return null;
  if (!Array.isArray(item.tables) || !item.tables.every(isSyncV3Table)) return null;
  let bootstrap: SyncV3ScopeState["bootstrap"] = null;
  if (item.bootstrap != null) {
    const position = item.bootstrap as Record<string, unknown>;
    if (
      typeof position !== "object"
      || !Number.isSafeInteger(position.tableIndex)
      || (position.tableIndex as number) < 0
      || (position.after != null && typeof position.after !== "string")
      || (position.seen != null && (!Number.isSafeInteger(position.seen) || (position.seen as number) < 0))
      || (position.total != null && position.total !== null
        && (!Number.isSafeInteger(position.total) || (position.total as number) < 0))
      || (position.fingerprint != null && typeof position.fingerprint !== "string")
    ) return null;
    bootstrap = {
      tableIndex: position.tableIndex as number,
      after: (position.after as string | null | undefined) ?? null,
      ...(Number.isSafeInteger(position.seen) ? { seen: position.seen as number } : {}),
      ...(position.total === null || Number.isSafeInteger(position.total)
        ? { total: position.total as number | null } : {}),
      ...(typeof position.fingerprint === "string" ? { fingerprint: position.fingerprint } : {}),
    };
  }
  const kind = item.kind;
  const lastBootstrap = item.lastBootstrap && typeof item.lastBootstrap === "object"
    && Number.isSafeInteger((item.lastBootstrap as Record<string, unknown>).seen)
    && ((item.lastBootstrap as Record<string, unknown>).total === null
      || Number.isSafeInteger((item.lastBootstrap as Record<string, unknown>).total))
    ? {
        seen: (item.lastBootstrap as { seen: number }).seen,
        total: (item.lastBootstrap as { total: number | null }).total,
      }
    : undefined;
  return {
    fingerprint: item.fingerprint,
    generation: item.generation as number,
    tables: [...item.tables],
    bootstrap,
    ...(lastBootstrap ? { lastBootstrap } : {}),
    ...(kind === "user" || kind === "space" || kind === "project" || kind === "docs" ? { kind } : {}),
    ...(typeof item.id === "string" ? { id: item.id } : {}),
    ...(item.projectId === null || typeof item.projectId === "string"
      ? { projectId: item.projectId as string | null }
      : {}),
    ...(item.access === null || typeof item.access === "string"
      ? { access: item.access as string | null }
      : {}),
    ...(item.readOnly === null || typeof item.readOnly === "boolean"
      ? { readOnly: item.readOnly as boolean | null }
      : {}),
  };
}

/** Parse a stored state; anything malformed is treated as "no state". */
export function parseSyncV3State(raw: unknown): SyncV3State | null {
  const parsed = parseJson(raw);
  if (!parsed || parsed.v !== 1) return null;
  if (parsed.cursor != null && typeof parsed.cursor !== "string") return null;
  if (parsed.scopeSetRevision != null && typeof parsed.scopeSetRevision !== "string") return null;
  if (typeof parsed.fullEvictPending !== "boolean") return null;
  if (parsed.lastSuccessAt != null
    && (!Number.isSafeInteger(parsed.lastSuccessAt) || (parsed.lastSuccessAt as number) < 0)) return null;
  if (!parsed.scopes || typeof parsed.scopes !== "object" || Array.isArray(parsed.scopes)) return null;
  const scopes: Record<string, SyncV3ScopeState> = {};
  for (const [key, value] of Object.entries(parsed.scopes as Record<string, unknown>)) {
    const scope = parseScopeState(value);
    if (!scope) return null;
    scopes[key] = scope;
  }
  return {
    v: 1,
    cursor: (parsed.cursor as string | null | undefined) ?? null,
    scopeSetRevision: (parsed.scopeSetRevision as string | null | undefined) ?? null,
    fullEvictPending: parsed.fullEvictPending,
    lastSuccessAt: (parsed.lastSuccessAt as number | null | undefined) ?? null,
    scopes,
  };
}

export function parseSyncV3DeviceState(raw: unknown): SyncV3DeviceState | null {
  const parsed = parseJson(raw);
  if (!parsed) return null;
  if (typeof parsed.device_id !== "string" || !parsed.device_id) return null;
  if (!Number.isSafeInteger(parsed.next_op_seq) || (parsed.next_op_seq as number) < 0) return null;
  return { device_id: parsed.device_id, next_op_seq: parsed.next_op_seq as number };
}

function readCursorInWriter(tx: SqliteHandle, key: string): string | null {
  const rows = tx
    .select({ cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, key))
    .all();
  return rows[0]?.cursor ?? null;
}

function writeCursorInWriter(tx: SqliteHandle, key: string, cursor: string): void {
  tx.insert(schema.syncState)
    .values({ tableName: key, lastPulledAt: null, lastPushedAt: null, cursor })
    .onConflictDoUpdate({ target: schema.syncState.tableName, set: { cursor } })
    .run();
}

export function readSyncV3StateInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
): SyncV3State | null {
  assertActiveWriter(w);
  return parseSyncV3State(readCursorInWriter(tx, syncV3StateKey(authScope)));
}

export function writeSyncV3StateInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  state: SyncV3State,
): void {
  assertActiveWriter(w);
  writeCursorInWriter(tx, syncV3StateKey(authScope), JSON.stringify(state));
}

export function readSyncV3DeviceStateInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
): SyncV3DeviceState | null {
  assertActiveWriter(w);
  return parseSyncV3DeviceState(readCursorInWriter(tx, syncV3DeviceKey(authScope)));
}

export function writeSyncV3DeviceStateInWriter(
  w: SqliteWriter,
  tx: SqliteHandle,
  authScope: string,
  state: SyncV3DeviceState,
): void {
  assertActiveWriter(w);
  writeCursorInWriter(tx, syncV3DeviceKey(authScope), JSON.stringify(state));
}

/** Read-only snapshot outside a write unit (the unit re-reads before writing). */
export async function readSyncV3State(authScope: string): Promise<SyncV3State | null> {
  const rows = await getDb()
    .select({ cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, syncV3StateKey(authScope)));
  return parseSyncV3State(rows[0]?.cursor ?? null);
}
