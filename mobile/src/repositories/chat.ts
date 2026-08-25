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
import { conversationsRepo, uploadLocalSession } from "./conversations";
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
  };
}

/** Local-first context binding. A local session is promoted before App attach. */
export async function bindChatAppContext(
  sessionId: string,
  context: ChatAppContext | null,
): Promise<ConversationSession> {
  const local = await conversationsRepo.getSessionLocal(sessionId);
  if (!local) throw new Error("会話セッションが見つかりません。");
  let remoteId = sessionId;
  if (!local.user_id) {
    if (!context) return local;
    remoteId = await uploadLocalSession(sessionId);
  }
  const updated = await chatApi.bindAppContext(remoteId, context);
  // uploadLocalSession already applies the new remote row, but applying again
  // makes this helper safe for a server-backed session and test doubles.
  await import("./conversations").then(({ applyRemoteConversationSessions }) =>
    applyRemoteConversationSessions([updated]),
  );
  return updated;
}

export async function forkChatSession(
  sessionId: string,
  fromMessageId: string,
  title?: string | null,
): Promise<ConversationSession> {
  const result = await chatApi.forkSession(sessionId, fromMessageId, title);
  await import("./conversations").then(({ applyRemoteConversationSessions }) =>
    applyRemoteConversationSessions([result.session]),
  );
  return result.session;
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
      session.title.toLocaleLowerCase().includes(normalized) ||
      session.character_name.toLocaleLowerCase().includes(normalized)
    ) {
      results.push(sessionResult(session, normalized));
    }
  }
  return results.slice(0, max);
}

/** Online search with an offline/local fallback. */
export async function searchChat(
  query: string,
  projectId?: string | null,
  limit = 50,
): Promise<ConversationSearchResult[]> {
  const normalized = query.trim();
  if (!normalized) return [];
  try {
    const result = await chatApi.searchConversations(normalized, projectId, limit);
    return result.results.slice(0, 50);
  } catch {
    return searchChatLocal(normalized, projectId, limit);
  }
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
    await db
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
      });
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
  searchLocal: searchChatLocal,
  search: searchChat,
  getContextSnapshot: getChatContextSnapshot,
};
