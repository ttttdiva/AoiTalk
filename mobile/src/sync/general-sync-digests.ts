import type { SyncPullResponse, SyncTable, SyncTablePayload } from "./api";
import * as Crypto from "expo-crypto";

export const GENERAL_DIGEST_VERSION = 1;
// Must match src/api/general_sync_source_digest.py. Bump both when a server
// projection or its source dependency map changes.
export const GENERAL_SOURCE_TOKEN_VERSION = 1;
export const GENERAL_SOURCE_TERMINAL_RECEIPTS_VERSION = 1;
export const GENERAL_FULL_REPLAY_INTERVAL_MS = 24 * 60 * 60 * 1000;
export const GENERAL_MESSAGE_SYNC_VERSION = 1;

export function validGeneralDigests(value: unknown, tables: readonly string[]): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([table, digest]) =>
    tables.includes(table) && typeof digest === "string" && /^[a-f0-9]{64}$/.test(digest)));
}

export function validGeneralSourceDigests(
  value: unknown,
  tables: readonly string[],
): Record<string, string> {
  return validGeneralDigests(value, tables);
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]),
  );
}

/** Local-only session edits must invalidate the server shortcut once. */
export async function localConversationSessionFingerprint(
  rows: readonly Record<string, unknown>[],
): Promise<string> {
  const canonical = [...rows]
    .sort((left, right) => String(left.id ?? "").localeCompare(String(right.id ?? "")))
    .map(stableValue);
  return Crypto.digestStringAsync(
    Crypto.CryptoDigestAlgorithm.SHA256,
    JSON.stringify(canonical),
  );
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

export function reusableGeneralSourceDigests(
  digests: unknown,
  tables: readonly string[],
  fullReplayedAt: number | undefined,
  now: number,
): Record<string, string> {
  if (!fullReplayedAt || !Number.isFinite(fullReplayedAt)
    || now < fullReplayedAt || now - fullReplayedAt >= GENERAL_FULL_REPLAY_INTERVAL_MS) return {};
  return validGeneralSourceDigests(digests, tables);
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

export type GeneralSourcePageReceipts = {
  receipts: Record<string, string>;
  starts: Record<string, string>;
  notModified: boolean;
};

export type GeneralMessageSyncPage = {
  supported: boolean;
  digest?: string;
  serverTime?: string;
  notModified: boolean;
};

/** Validate the additive conversation-message receipt before it can advance. */
export function validateGeneralMessageSyncPage(
  response: SyncPullResponse,
  pendingTables: readonly SyncTable[],
  requestedDigest: string | null,
): GeneralMessageSyncPage {
  if (!pendingTables.includes("conversation_messages")) {
    return { supported: response.general_message_sync_version === GENERAL_MESSAGE_SYNC_VERSION, notModified: false };
  }
  const payload = response.tables.conversation_messages;
  if (!payload) throw new Error("一般チャット同期ページが欠落しています");
  if (response.general_message_sync_version == null) {
    if (
      payload.general_message_sync_digest
      || payload.general_message_sync_not_modified
      || payload._sync_fingerprint
      || payload.changes.some((row) => typeof row._sync_fingerprint === "string")
    ) {
      throw new Error("一般チャット同期の版がありません");
    }
    return { supported: false, notModified: false };
  }
  if (response.general_message_sync_version !== GENERAL_MESSAGE_SYNC_VERSION) {
    throw new Error("一般チャット同期の版が不正です");
  }
  const digest = payload.general_message_sync_digest;
  const serverTime = payload.general_message_sync_server_time;
  if (payload.general_message_sync_not_modified) {
    if (
      !requestedDigest
      || digest !== requestedDigest
      || !/^[a-f0-9]{32}$/.test(digest ?? "")
      || payload.changes.length
      || payload.tombstones.length
      || payload.authoritative_ids != null
      || payload.cursor
      || typeof serverTime !== "string"
      || !Number.isFinite(Date.parse(serverTime))
    ) {
      throw new Error("一般チャット同期の変更なし応答を確認できません");
    }
    return { supported: true, digest, serverTime, notModified: true };
  }
  if (digest != null) {
    if (
      !/^[a-f0-9]{32}$/.test(digest)
      || payload.cursor
      || typeof serverTime !== "string"
      || !Number.isFinite(Date.parse(serverTime))
    ) {
      throw new Error("一般チャット同期digestが不正です");
    }
  } else if (serverTime != null) {
    throw new Error("一般チャット同期server timeが不正です");
  } else if (!payload.cursor) {
    throw new Error("一般チャット同期terminal digestがありません");
  }
  for (const row of payload.changes) {
    if (row._sync_fingerprint != null
      && (typeof row._sync_fingerprint !== "string"
        || !/^[a-f0-9]{32}$/.test(row._sync_fingerprint))) {
      throw new Error("一般チャット同期row fingerprintが不正です");
    }
  }
  for (const row of payload.tombstones) {
    if (row._sync_fingerprint != null
      && (typeof row._sync_fingerprint !== "string"
        || !/^[a-f0-9]{32}$/.test(row._sync_fingerprint))) {
      throw new Error("一般チャット同期tombstone fingerprintが不正です");
    }
  }
  if (payload.authoritative_ids != null && (
    !Array.isArray(payload.authoritative_ids)
    || payload.authoritative_ids.some((id) => typeof id !== "string")
  )) {
    throw new Error("一般チャット同期authoritative IDが不正です");
  }
  if (digest && payload.changes.some((row) => !row._sync_fingerprint)) {
    // A terminal full/delta page must include fingerprints for all its rows;
    // tombstones are allowed to be absent from a brand-new local cache.
    throw new Error("一般チャット同期row fingerprintが欠落しています");
  }
  return { supported: true, digest, serverTime, notModified: false };
}

/** Validate the additive source receipt before it can suppress local apply. */
export function validateGeneralSourcePage(
  response: SyncPullResponse,
  pendingTables: readonly SyncTable[],
  requestDigests: Record<string, string>,
  requestStarts: Record<string, string>,
): GeneralSourcePageReceipts {
  if (response.general_source_token_version == null) {
    if (
      response.general_source_not_modified
      || pendingTables.some((table) => response.tables[table]?.general_source_not_modified)
    ) {
      throw new Error("一般テーブル同期source tokenの版がありません");
    }
    return { receipts: {}, starts: {}, notModified: false };
  }
  if (response.general_source_token_version !== GENERAL_SOURCE_TOKEN_VERSION) {
    throw new Error("一般テーブル同期source tokenの版が不正です");
  }
  const supportsTerminalReceipts =
    response.general_source_terminal_receipts_version === GENERAL_SOURCE_TERMINAL_RECEIPTS_VERSION;
  if (
    response.general_source_terminal_receipts_version != null
    && !supportsTerminalReceipts
  ) {
    throw new Error("一般テーブル同期terminal receiptの版が不正です");
  }
  const responseTables = supportsTerminalReceipts
    ? [...new Set([...pendingTables, ...Object.keys(requestStarts)])]
    : pendingTables;
  const receipts = validGeneralSourceDigests(
    response.general_source_receipts,
    responseTables,
  );
  const starts = validGeneralSourceDigests(
    response.general_source_token_starts,
    responseTables,
  );
  if (
    response.general_source_receipts
    && Object.keys(receipts).length !== Object.keys(response.general_source_receipts).length
  ) {
    throw new Error("一般テーブル同期source receiptが不正です");
  }
  if (
    response.general_source_token_starts
    && Object.keys(starts).length !== Object.keys(response.general_source_token_starts).length
  ) {
    throw new Error("一般テーブル同期source token startが不正です");
  }
  for (const [table, start] of Object.entries(starts)) {
    if (!pendingTables.includes(table as SyncTable)
      && requestStarts[table] !== start) {
      throw new Error("一般テーブル同期source token startが不正です");
    }
    if (requestStarts[table] && requestStarts[table] !== start) {
      throw new Error("一般テーブル同期source token startがページ間で変わりました");
    }
  }
  for (const table of Object.keys(receipts)) {
    if (!pendingTables.includes(table as SyncTable)
      && (!supportsTerminalReceipts || !requestStarts[table])) {
      throw new Error("一般テーブル同期source receipt対象が不正です");
    }
  }
  const validatedTables = supportsTerminalReceipts
    ? [...new Set([...pendingTables, ...Object.keys(starts), ...Object.keys(receipts)])]
    : pendingTables;
  for (const table of validatedTables) {
    const payload = response.tables[table as SyncTable];
    if (!payload && pendingTables.includes(table as SyncTable)) {
      throw new Error(`一般テーブル同期ページが欠落しています: ${table}`);
    }
    if (payload?.general_source_not_modified) {
      if (
        receipts[table] !== requestDigests[table]
        || !requestDigests[table]
        || payload.changes.length
        || payload.tombstones.length
        || payload.authoritative_ids != null
        || payload.cursor
      ) {
        throw new Error("一般テーブル同期source unchanged応答を確認できません");
      }
    }
    const receipt = receipts[table];
    const start = starts[table]
      ?? requestStarts[table]
      ?? (payload?.general_source_not_modified ? requestDigests[table] : undefined);
    if (receipt && (!start || receipt !== start)) {
      throw new Error("一般テーブル同期source receiptの境界が不正です");
    }
  }
  const notModified = response.general_source_not_modified === true;
  if (notModified && (
    pendingTables.some((table) => !response.tables[table]?.general_source_not_modified)
    || Object.keys(starts).length > 0
  )) {
    throw new Error("一般テーブル同期source unchanged対象が一致しません");
  }
  return { receipts, starts, notModified };
}

export function generalTableNeedsApply(payload: SyncTablePayload | undefined): boolean {
  return Boolean(payload && !payload.general_not_modified && (
    payload.changes.length || payload.tombstones.length || payload.authoritative_ids != null
  ));
}
