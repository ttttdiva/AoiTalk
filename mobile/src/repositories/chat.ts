/** Chat parity helpers kept outside the legacy conversation repository. */

import { getToken, getTokenAuthScope } from "../lib/auth";
import { and, eq } from "drizzle-orm";
import {
  chatApi,
  resolveMainContextSnapshot,
  type ChatAppContext,
  type ContextSnapshot,
  type ConversationSearchResult,
  type ContextRequestSnapshot,
} from "../lib/chat-api";
import { applyForegroundConversationSessions, conversationsRepo, excludeUserDeletedSessions, getPromotedConversationSessionId, uploadLocalSession } from "./conversations";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { getDb, schema } from "../db/client";
import type { ConversationMessage, ConversationSession } from "../types/api";

function contextSnapshotOf(value: unknown): ContextSnapshot | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as ContextSnapshot;
}

function snippet(content: string, query: string, maxLength = 160): string {
  const normalized = content.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  const index = normalized.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  const center = index >= 0 ? index : 0;
  const start = Math.max(0, center - Math.floor(maxLength / 3));
  const end = Math.min(normalized.length, start + maxLength);
  return `${start > 0 ? "..." : ""}${normalized.slice(start, end)}${
    end < normalized.length ? "..." : ""
  }`;
}

function sessionResult(session: ConversationSession, query: string): ConversationSearchResult {
  const title = session.title || "無題の会話";
  const character = session.character_name || "";
  return {
    id: `session:${session.id}`,
    match_type: "session",
    session_id: session.id,
    message_id: null,
    title,
    character_name: character,
    snippet: snippet(`タイトル: ${title} / 相手: ${character}`, query),
    created_at: session.session_start ?? null,
    last_activity: session.last_activity ?? session.session_start ?? null,
    project_id: session.project_id ?? null,
    history_status: session.history_status ?? null,
    history_completed_at: session.history_completed_at ?? null,
  };
}

function messageResult(
  session: ConversationSession,
  message: ConversationMessage,
  query: string,
): ConversationSearchResult {
  return {
    id: `message:${message.id}`,
    match_type: "message",
    session_id: session.id,
    message_id: message.id,
    title: session.title || "無題の会話",
    character_name: session.character_name || "",
    role: message.role,
    snippet: snippet(message.content, query),
    created_at: message.created_at ?? null,
    last_activity: session.last_activity ?? session.session_start ?? null,
    project_id: session.project_id ?? null,
    history_status: session.history_status ?? null,
    history_completed_at: session.history_completed_at ?? null,
  };
}

/** Local-first context binding. A local session is promoted before App attach. */
export async function bindChatAppContext(
  sessionId: string,
  context: ChatAppContext | null,
): Promise<ConversationSession> {
  let local = await conversationsRepo.getSessionLocal(sessionId);
  if (!local) {
    // Upload may have committed locally before the App PUT failed. The screen
    // still has the old route ID until binding succeeds; resume its exact
    // durable promotion target instead of creating or reviving a session.
    const promotedId = await getPromotedConversationSessionId(sessionId);
    if (promotedId) local = await conversationsRepo.getSessionLocal(promotedId);
  }
  if (!local) throw new Error("会話セッションが見つかりません。");
  let remoteId = local.id;
  if (!local.user_id || local.is_local_only) {
    if (!context) return local;
    remoteId = await uploadLocalSession(sessionId);
  }
  const updated = await chatApi.bindAppContext(remoteId, context);
  // uploadLocalSession already applies the new remote row, but applying again
  // makes this helper safe for a server-backed session and test doubles.
  await applyForegroundConversationSessions([updated]);
  return updated;
}

export async function forkChatSession(
  sessionId: string,
  fromMessageId: string,
  title?: string | null,
): Promise<ConversationSession> {
  const result = await chatApi.forkSession(sessionId, fromMessageId, title);
  // Foreground application also notifies every conversation list/sidebar.
  await applyForegroundConversationSessions([result.session]);
  return result.session;
}

function completeActiveBranch(messages: ConversationMessage[], sessionId: string): ConversationMessage[] {
  if (!messages.length) return [];
  const byId = new Map(messages.map((message) => [message.id, message]));
  const parents = new Set(messages.map((message) => message.parent_message_id).filter(Boolean));
  const leaves = messages.filter((message) => !parents.has(message.id));
  const invalid = () => new Error("会話の完全な分岐を確認できませんでした。同期してから再試行してください。");
  if (byId.size !== messages.length || leaves.length !== 1) throw invalid();
  const path: ConversationMessage[] = [];
  const visited = new Set<string>();
  let cursor: ConversationMessage | undefined = leaves[0];
  while (cursor) {
    if (cursor.session_id !== sessionId || visited.has(cursor.id)) throw invalid();
    visited.add(cursor.id);
    path.push(cursor);
    if (!cursor.parent_message_id) break;
    const parent = byId.get(cursor.parent_message_id);
    if (!parent) throw invalid();
    cursor = parent;
  }
  if (path.length !== messages.length) throw invalid();
  return path.reverse();
}

/**
 * Fork the entire currently active branch. Cached server messages may be only
 * a delta or a recent window, so they must never supply the server fork cutoff.
 */
export async function forkChatSessionToEnd(
  sessionId: string,
  title?: string | null,
): Promise<ConversationSession> {
  const source = await conversationsRepo.getSessionLocal(sessionId);
  if (!source) throw new Error("フォーク元の会話が見つかりません。");
  const local = await conversationsRepo.listMessagesLocal(sessionId);
  const localOnly = !source.user_id || source.is_local_only === true;
  let messages = local;
  if (!localOnly) {
    if (!(await getToken())) throw new Error("サーバー上の会話を最後までフォークするにはログインが必要です。");
    let remote: ConversationMessage[];
    try {
      // getMessages has no since/limit and no cache fallback: this is the
      // authoritative full active history, including old uncached messages.
      remote = completeActiveBranch(
        (await chatApi.getMessages(sessionId)).filter((message) => message.is_active_branch !== false),
        sessionId,
      );
    } catch (failure) {
      throw new Error(`会話の完全な履歴を取得できませんでした。接続を確認して再試行してください。${failure instanceof Error ? `（${failure.message}）` : ""}`);
    }
    const remoteIds = new Set(remote.map((message) => message.id));
    const remoteClientIds = new Set(remote.map((message) => message.metadata?.client_message_id).filter(Boolean));
    const unsynced = local.filter((message) => {
      if (!message.metadata?.local_only && !message.metadata?.pending && !message.metadata?.persistence_error && !message.id.startsWith("temp-")) return false;
      const clientId = message.metadata?.client_message_id ?? message.id;
      const serverId = message.metadata?.server_message_id;
      // A confirmed server ID missing from the active snapshot belongs to an
      // inactive/deleted branch, not a new offline input. Never resurrect it.
      if (typeof serverId === "string" && serverId) return false;
      return !remoteIds.has(message.id) && !remoteClientIds.has(clientId);
    });
    if (unsynced.length === 0) {
      const last = remote[remote.length - 1];
      if (!last) throw new Error("メッセージがない会話はフォークできません。");
      return forkChatSession(sessionId, last.id, title);
    }
    // Preserve pending/local inputs without dispatching them. Only the server
    // snapshot plus genuine unsynced rows belong to this branch; cached rows
    // from an old branch must never be resurrected here.
    messages = [...remote, ...unsynced].sort((left, right) =>
      (left.created_at ?? "").localeCompare(right.created_at ?? ""),
    );
  }
  if (messages.length === 0) throw new Error("メッセージがない会話はフォークできません。");
  if (source.is_group_chat || source.agent_id || source.app_id || ("private_agent_root" in source && source.private_agent_root === true)) {
    throw new Error("この種類の会話はローカルでフォークできません。未同期の入力と会話をサーバーへ同期してから再試行してください。");
  }
  const forked = await conversationsRepo.createLocalSession(source.character_name, source.project_id ?? null, source.user_id);
  const forkTitle = (title?.trim() || `${source.title || "新しい会話"}（フォーク）`).slice(0, 200);
  try {
    await conversationsRepo.updateTitle(forked.id, forkTitle, { syncServer: false });
    for (const message of messages) {
      const metadata: Record<string, unknown> = {
        ...message.metadata,
        local_only: true,
        pending: false,
        message_state: "persisted",
        fork_source_message_id: message.id,
      };
      for (const key of ["persistence_error", "upload_target_session_id", "server_message_id", "client_message_id", "server_result_uncertain", "delivery_error", "direct_error", "queue_cancelled", "draft_saved", "agent_run_id", "context_snapshot", "context_manifest", "memory_selection_observation"]) delete metadata[key];
      await conversationsRepo.appendLocalMessage(forked.id, message.role, message.content, metadata);
    }
    return { ...forked, title: forkTitle, message_count: messages.length };
  } catch (failure) {
    // A failed local copy must not leave an apparently completed partial fork.
    try { await conversationsRepo.deleteSession(forked.id); }
    catch { throw new Error("フォークを完了できませんでした。作成途中のローカル会話が残っています。削除してから再試行してください。"); }
    throw failure;
  }
}

/** Search cached sessions/messages without a network round trip. Result max is 50. */
export async function searchChatLocal(
  query: string,
  projectId?: string | null,
  limit = 50,
): Promise<ConversationSearchResult[]> {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return [];
  const max = Math.min(50, Math.max(1, Math.floor(limit)));
  const sessions = await conversationsRepo.listSessionsLocal(projectId ?? undefined);
  const results: ConversationSearchResult[] = [];
  for (const session of sessions) {
    if (results.length >= max) break;
    const messages = await conversationsRepo.listMessagesLocal(session.id);
    const matchingMessage = messages.find((message) =>
      message.content.toLocaleLowerCase().includes(normalized),
    );
    if (matchingMessage) {
      results.push(messageResult(session, matchingMessage, normalized));
      continue;
    }
    if (
      (session.title || "").toLocaleLowerCase().includes(normalized) ||
      (session.character_name || "").toLocaleLowerCase().includes(normalized)
    ) {
      results.push(sessionResult(session, normalized));
    }
  }
  return excludeUserDeletedSessions(results.slice(0, max), (item) => item.session_id);
}

/** Online search with an offline/local fallback. */
export async function searchChat(
  query: string,
  projectId?: string | null,
  limit = 50,
): Promise<ConversationSearchResult[]> {
  const normalized = query.trim();
  if (!normalized) return [];
  const max = Math.min(50, Math.max(1, Math.floor(limit)));
  const [remote, local] = await Promise.allSettled([
    chatApi.searchConversations(normalized, projectId, max),
    searchChatLocal(normalized, projectId, max),
  ]);
  if (remote.status === "rejected" && local.status === "rejected") throw local.reason;
  // Successful online search must not hide local-only or unsent body matches.
  // Deduplicate identical hits while retaining different matching messages.
  const byId = new Map<string, ConversationSearchResult>();
  if (local.status === "fulfilled") for (const result of local.value) byId.set(result.id, result);
  if (remote.status === "fulfilled") for (const result of remote.value.results) byId.set(result.id, result);
  const merged = [...byId.values()].sort((left, right) =>
    (right.last_activity ?? right.created_at ?? "").localeCompare(left.last_activity ?? left.created_at ?? ""),
  );
  return (await excludeUserDeletedSessions(merged, (item) => item.session_id)).slice(0, max);
}

export async function getChatContextSnapshot(
  sessionId: string,
): Promise<{
  snapshot: ContextSnapshot | null;
  main: ContextRequestSnapshot | null;
}> {
  const authScope = getTokenAuthScope(await getToken());
  const db = getDb();
  const session = await Promise.resolve(
    conversationsRepo.getSessionLocal(sessionId),
  ).catch(() => null);
  const readCached = async () => {
    try {
      const rows = await db
        .select()
        .from(schema.conversationContextSnapshots)
        .where(
          and(
            eq(schema.conversationContextSnapshots.authScope, authScope),
            eq(schema.conversationContextSnapshots.sessionId, sessionId),
          ),
        )
        .limit(1);
      const row = rows[0];
      const snapshot = contextSnapshotOf(row?.payloadJson);
      return { snapshot, main: resolveMainContextSnapshot(snapshot) };
    } catch {
      return { snapshot: null, main: null };
    }
  };
  try {
    const result = await chatApi.getContextSnapshot(sessionId);
    const snapshot = result.snapshot ?? null;
    const now = new Date().toISOString();
    await runForegroundSqliteWrite(() => db
      .insert(schema.conversationContextSnapshots)
      .values({
        authScope,
        sessionId,
        projectId: session?.project_id ?? null,
        appId: session?.app_id ?? null,
        appTargetId: session?.app_target_id ?? null,
        status: result.status || (snapshot ? "available" : "unavailable"),
        payloadJson: snapshot,
        messageId: snapshot?.message_id ?? null,
        snapshotVersion:
          typeof snapshot?.request_count === "number"
            ? snapshot.request_count
            : null,
        cachedAt: now,
        updatedAt: now,
      })
      .onConflictDoUpdate({
        target: [
          schema.conversationContextSnapshots.authScope,
          schema.conversationContextSnapshots.sessionId,
        ],
        set: {
          status: result.status || (snapshot ? "available" : "unavailable"),
          projectId: session?.project_id ?? null,
          appId: session?.app_id ?? null,
          appTargetId: session?.app_target_id ?? null,
          payloadJson: snapshot,
          messageId: snapshot?.message_id ?? null,
          snapshotVersion:
            typeof snapshot?.request_count === "number"
              ? snapshot.request_count
              : null,
          cachedAt: now,
          updatedAt: now,
        },
      }));

    return {
      snapshot,
      main: resolveMainContextSnapshot(snapshot),
    };
  } catch {
    return readCached();
  }
}

export const chatRepo = {
  bindAppContext: bindChatAppContext,
  forkSession: forkChatSession,
  forkSessionToEnd: forkChatSessionToEnd,
  searchLocal: searchChatLocal,
  search: searchChat,
  getContextSnapshot: getChatContextSnapshot,
};
