import type { SyncPullResponse, SyncTable, SyncTablePayload } from "./api";

export const GENERAL_DIGEST_VERSION = 1;
export const GENERAL_FULL_REPLAY_INTERVAL_MS = 24 * 60 * 60 * 1000;

export function validGeneralDigests(value: unknown, tables: readonly string[]): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([table, digest]) =>
    tables.includes(table) && typeof digest === "string" && /^[a-f0-9]{64}$/.test(digest)));
}

export function generalDigestsForRequest(value: unknown, tables: readonly string[]): Record<string, string> {
  // Session read/title writes can remain local after HTTP failure, without an
  // outbox entry. They rely on the next pull to restore the server projection.
  // Never reuse this receipt, including one restored from an interrupted run.
  return validGeneralDigests(value, tables.filter((table) => table !== "conversation_sessions"));
}

/** Receipts may optimize a full replay; they never turn a wall clock into a cursor. */
export function reusableGeneralDigests(
  digests: unknown,
  tables: readonly string[],
  fullReplayedAt: number | undefined,
  now: number,
): Record<string, string> {
  if (!fullReplayedAt || !Number.isFinite(fullReplayedAt)
    || now < fullReplayedAt || now - fullReplayedAt >= GENERAL_FULL_REPLAY_INTERVAL_MS) return {};
  return generalDigestsForRequest(digests, tables);
}

/** Validate before applying any page; an unproven not-modified claim fails closed. */
export function validateGeneralDigestPage(
  response: SyncPullResponse,
  pendingTables: readonly SyncTable[],
  requestDigests: Record<string, string>,
  since: string | null,
): Record<string, string> {
  const received: Record<string, string> = {};
  for (const table of pendingTables) {
    const payload = response.tables[table];
    if (!payload) throw new Error(`一般テーブル同期ページが欠落しています: ${table}`);
    const digest = payload.general_digest;
    if (digest != null) {
      if (response.general_digest_version !== GENERAL_DIGEST_VERSION || since != null
        || payload.cursor || !/^[a-f0-9]{64}$/.test(digest)) {
        throw new Error("一般テーブル同期digestが不正です");
      }
      received[table] = digest;
    }
    if (payload.general_not_modified) {
      if (table === "conversation_sessions" || !digest || digest !== requestDigests[table]
        || payload.changes.length || payload.tombstones.length
        || payload.authoritative_ids != null || payload.cursor) {
        throw new Error("一般テーブル同期の変更なし応答を確認できません");
      }
    }
  }
  return received;
}

export function generalTableNeedsApply(payload: SyncTablePayload | undefined): boolean {
  return Boolean(payload && !payload.general_not_modified && (
    payload.changes.length || payload.tombstones.length || payload.authoritative_ids != null
  ));
}
