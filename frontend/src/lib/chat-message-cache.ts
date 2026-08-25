"use client";

import type { ConversationMessage } from "@/lib/chat-api";
import {
  CHAT_MESSAGES_CACHE_PREFIX,
  readCachedSnapshot,
  writeCachedSnapshot,
} from "@/lib/persistent-cache";

// セッションごとのメッセージ・server_time を IndexedDB に永続化し、
// 再訪時の即描画と since 差分取得を支える。

type CachedMessages = {
  messages: ConversationMessage[];
  serverTime: string | null;
};

// セッションごとの最終 server_time（since 差分取得の基準）。
const lastServerTimeBySession = new Map<string, string | null>();

export function resetChatMessageCacheMemory(): void {
  lastServerTimeBySession.clear();
}

export function getLastServerTime(sessionId: string): string | null {
  return lastServerTimeBySession.get(sessionId) ?? null;
}

export function setLastServerTime(
  sessionId: string,
  serverTime: string | null,
): void {
  lastServerTimeBySession.set(sessionId, serverTime);
}

function cacheKey(sessionId: string): string {
  return `${CHAT_MESSAGES_CACHE_PREFIX}${sessionId}`;
}

export async function readCachedMessages(
  sessionId: string,
): Promise<CachedMessages | undefined> {
  const cached = await readCachedSnapshot<CachedMessages>(cacheKey(sessionId));
  if (!cached || !Array.isArray(cached.messages)) return undefined;
  return {
    messages: cached.messages,
    serverTime: cached.serverTime ?? null,
  };
}

export async function writeCachedMessages(
  sessionId: string,
  messages: ConversationMessage[],
  serverTime: string | null,
): Promise<void> {
  await writeCachedSnapshot(cacheKey(sessionId), { messages, serverTime });
}

function timestampMs(value: string | null | undefined): number {
  if (!value) return 0;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : 0;
}

function messageUpdatedMs(message: ConversationMessage): number {
  const updatedAt = (message as { updated_at?: string | null }).updated_at;
  return Math.max(timestampMs(updatedAt), timestampMs(message.created_at));
}

/**
 * 既存の永続メッセージ列に since 差分をマージする。
 * - message id で重複排除。
 * - 衝突時は updated_at（無ければ created_at）が新しい方を採用。差分側が同値/新しければ差分を採用。
 * - created_at 昇順で安定ソートし、表示順（サーバの asc created_at）を保つ。
 */
export function mergePersistedById(
  prev: ConversationMessage[],
  incoming: ConversationMessage[],
): ConversationMessage[] {
  const byId = new Map<string, ConversationMessage>();
  for (const message of prev) byId.set(message.id, message);
  for (const message of incoming) {
    const existing = byId.get(message.id);
    if (!existing) {
      byId.set(message.id, message);
      continue;
    }
    // 差分（incoming）はより新鮮な取得なので、同値以上なら差分を採用。
    byId.set(
      message.id,
      messageUpdatedMs(message) >= messageUpdatedMs(existing)
        ? message
        : existing,
    );
  }
  return Array.from(byId.values())
    // 差分APIはbranch切替でinactive化された行をtombstoneとして返す。
    // 表示・永続キャッシュには現在のactive branchだけを残す。
    .filter((message) => message.is_active_branch !== false)
    .sort(
      (a, b) => timestampMs(a.created_at) - timestampMs(b.created_at),
    );
}

/** メッセージ列から最新の server_time 相当（max updated_at/created_at）を導出する。 */
export function deriveServerTime(
  messages: ConversationMessage[],
): string | null {
  let latest = 0;
  let latestIso: string | null = null;
  for (const message of messages) {
    const updatedAt = (message as { updated_at?: string | null }).updated_at;
    const candidates: Array<string | null | undefined> = [
      updatedAt,
      message.created_at,
    ];
    for (const value of candidates) {
      const ms = timestampMs(value);
      if (ms > latest) {
        latest = ms;
        latestIso = value ?? null;
      }
    }
  }
  return latestIso;
}
