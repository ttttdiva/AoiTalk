import { and, asc, desc, eq, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken } from "../lib/auth";
import { chatApi } from "../lib/chat-api";
import { useNetworkStore } from "../stores/network";
import type { ConversationMessage, ConversationSession } from "../types/api";
import { randomId } from "./outbox";

type DbSession = typeof schema.conversationSessions.$inferSelect;
type DbMessage = typeof schema.conversationMessages.$inferSelect;

function remoteActiveBranch(message: ConversationMessage): boolean {
  return message.is_active_branch ?? true;
}

function toSession(row: DbSession): ConversationSession {
  const metadata =
    (row.sessionMetadata as Record<string, unknown> | null) ?? {};
  return {
    id: row.id,
    user_id: row.userId ?? "",
    character_name: row.characterName ?? "",
    title: row.title ?? "",
    project_id: row.projectId ?? null,
    session_start: row.createdAt ?? null,
    last_activity: row.updatedAt ?? null,
    message_count: Number(metadata.message_count ?? 0),
    is_active: Boolean(metadata.is_active ?? true),
  };
}

function toMessage(row: DbMessage): ConversationMessage {
  return {
    id: row.id,
    session_id: row.sessionId,
    role: row.role as ConversationMessage["role"],
    content: row.content,
    metadata: (row.messageMetadata as Record<string, unknown> | null) ?? {},
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    token_count: row.tokenCount ?? null,
    parent_message_id: row.parentMessageId ?? null,
    branch_index: row.branchIndex ?? 0,
    is_active_branch: row.isActiveBranch ?? true,
  };
}

function isLocalOnlyMessage(message: ConversationMessage): boolean {
  return Boolean(message.metadata?.local_only);
}

function branchGroupKey(message: ConversationMessage): string {
  return message.parent_message_id ?? "__root__";
}

function buildLocalSession(
  characterName = "default",
  projectId?: string | null,
): ConversationSession {
  const now = new Date().toISOString();
  return {
    id: randomId(),
    user_id: "",
    character_name: characterName,
    title: "ローカルチャット",
    project_id: projectId ?? null,
    session_start: now,
    last_activity: now,
    message_count: 0,
    is_active: true,
  };
}

async function canUseServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && network.serverReachable && Boolean(await getToken());
}

async function canAttemptServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && Boolean(await getToken());
}

async function canRefreshServer(): Promise<boolean> {
  // serverReachable は直近の失敗で stale になりやすいため、読み取り同期は
  // online + token があれば直接試し、成功したら到達可能状態へ戻す。
  return canAttemptServer();
}

async function updateSessionStats(
  sessionId: string,
  updater: (
    session: DbSession | undefined,
  ) => Partial<typeof schema.conversationSessions.$inferInsert>,
): Promise<void> {
  const db = getDb();
  const current = (
    await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId))
  )[0];
  const patch = updater(current);
  await db
    .update(schema.conversationSessions)
    .set({
      ...patch,
      updatedAt: patch.updatedAt ?? new Date().toISOString(),
    })
    .where(eq(schema.conversationSessions.id, sessionId));
}

export async function applyRemoteConversationSessions(
  list: ConversationSession[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const session of list) {
    await db
      .insert(schema.conversationSessions)
      .values({
        id: session.id,
        userId: session.user_id,
        characterName: session.character_name,
        projectId: session.project_id ?? null,
        title: session.title ?? "",
        isGroupChat: false,
        sessionMetadata: {
          message_count: session.message_count,
          is_active: session.is_active,
        },
        createdAt: session.session_start ?? now,
        updatedAt: session.last_activity ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.conversationSessions.id,
        set: {
          userId: session.user_id,
          characterName: session.character_name,
          projectId: session.project_id ?? null,
          title: session.title ?? "",
          updatedAt: session.last_activity ?? now,
          deletedAt: null,
          sessionMetadata: {
            message_count: session.message_count,
            is_active: session.is_active,
          },
        },
      });
  }
}

export async function applyConversationSessionTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.conversationSessions)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.conversationSessions.id, item.id));
    await db
      .update(schema.conversationMessages)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(
        and(
          eq(schema.conversationMessages.sessionId, item.id),
          isNull(schema.conversationMessages.deletedAt),
        ),
      );
  }
}

async function hasLocalOnlyMessages(sessionId: string): Promise<boolean> {
  const db = getDb();
  const rows = await db
    .select({ metadata: schema.conversationMessages.messageMetadata })
    .from(schema.conversationMessages)
    .where(
      and(
        eq(schema.conversationMessages.sessionId, sessionId),
        isNull(schema.conversationMessages.deletedAt),
      ),
    );
  return rows.some((row) =>
    Boolean((row.metadata as Record<string, unknown> | null)?.local_only),
  );
}

function isServerBackedSession(session: DbSession | undefined): boolean {
  return Boolean(session?.userId);
}

async function listPendingMessagesForSessionId(
  sessionId: string,
): Promise<ConversationMessage[]> {
  const messages = await conversationsRepo.listMessagesLocal(sessionId);
  return messages.filter(
    (message) =>
      message.role === "user" &&
      isLocalOnlyMessage(message) &&
      Boolean(message.metadata?.pending),
  );
}

async function promoteLocalSession(
  session: DbSession,
): Promise<ConversationSession> {
  const db = getDb();
  const remote = await chatApi.createSession(
    session.characterName ?? "default",
    session.projectId ?? undefined,
  );
  await applyRemoteConversationSessions([remote]);

  const now = new Date().toISOString();
  await db
    .update(schema.conversationMessages)
    .set({ sessionId: remote.id, updatedAt: now })
    .where(eq(schema.conversationMessages.sessionId, session.id));
  await db
    .update(schema.conversationSessions)
    .set({
      deletedAt: now,
      updatedAt: now,
      sessionMetadata: {
        local_only: true,
        promoted_to_session_id: remote.id,
      },
    })
    .where(eq(schema.conversationSessions.id, session.id));

  return remote;
}

export async function flushPendingConversation(
  sessionId: string,
): Promise<string> {
  if (!(await canAttemptServer())) return sessionId;

  const db = getDb();
  const row = (
    await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId))
  )[0];
  if (!row || row.deletedAt) return sessionId;

  let remoteSessionId = row.id;
  let projectId = row.projectId ?? undefined;
  if (!isServerBackedSession(row)) {
    const promoted = await promoteLocalSession(row);
    remoteSessionId = promoted.id;
    projectId = promoted.project_id ?? row.projectId ?? undefined;
  }

  const pending = await listPendingMessagesForSessionId(remoteSessionId);
  for (const message of pending) {
    await chatApi.dispatchMessage(remoteSessionId, {
      message: message.content,
      project_id: projectId,
      include_project_context: Boolean(projectId),
      agent_mode: "confirm",
    });
    await conversationsRepo.markPendingMessageQueued(message.id);
  }

  return remoteSessionId;
}

export async function flushPendingConversations(): Promise<void> {
  if (!(await canAttemptServer())) return;

  const db = getDb();
  const sessions = await db
    .select()
    .from(schema.conversationSessions)
    .where(isNull(schema.conversationSessions.deletedAt));
  for (const session of sessions) {
    const pending = await listPendingMessagesForSessionId(session.id);
    if (!pending.length && isServerBackedSession(session)) continue;
    try {
      await flushPendingConversation(session.id);
    } catch {
      // Leave pending metadata intact for the next sync attempt.
    }
  }
}

export async function reconcileConversationSessionsWithServer(
  authoritativeIds?: string[],
): Promise<void> {
  if (!authoritativeIds) return;
  const remoteIds = new Set(authoritativeIds);
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.conversationSessions)
    .where(isNull(schema.conversationSessions.deletedAt));
  const deletedAt = new Date().toISOString();
  for (const row of rows) {
    if (remoteIds.has(row.id)) continue;
    if (!row.userId) continue;
    if (await hasLocalOnlyMessages(row.id)) continue;
    await db
      .update(schema.conversationSessions)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.conversationSessions.id, row.id));
    await db
      .update(schema.conversationMessages)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(
        and(
          eq(schema.conversationMessages.sessionId, row.id),
          isNull(schema.conversationMessages.deletedAt),
        ),
      );
  }
}

export async function applyRemoteConversationMessages(
  list: ConversationMessage[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const message of list) {
    await db
      .insert(schema.conversationMessages)
      .values({
        id: message.id,
        sessionId: message.session_id,
        role: message.role,
        content: message.content,
        messageMetadata: message.metadata ?? {},
        tokenCount: message.token_count ?? null,
        parentMessageId: message.parent_message_id ?? null,
        branchIndex: message.branch_index ?? 0,
        isActiveBranch: remoteActiveBranch(message),
        createdAt: message.created_at ?? now,
        updatedAt:
          (message as { updated_at?: string | null }).updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.conversationMessages.id,
        set: {
          sessionId: message.session_id,
          role: message.role,
          content: message.content,
          messageMetadata: message.metadata ?? {},
          tokenCount: message.token_count ?? null,
          parentMessageId: message.parent_message_id ?? null,
          branchIndex: message.branch_index ?? 0,
          isActiveBranch: remoteActiveBranch(message),
          updatedAt:
            (message as { updated_at?: string | null }).updated_at ?? now,
          deletedAt: null,
        },
      });
  }
}

export async function applyConversationMessageTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.conversationMessages)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.conversationMessages.id, item.id));
  }
}

export const conversationsRepo = {
  async listSessionsLocal(
    projectId?: string | null,
  ): Promise<ConversationSession[]> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.conversationSessions)
      .where(
        and(
          projectId
            ? eq(schema.conversationSessions.projectId, projectId)
            : undefined,
          isNull(schema.conversationSessions.deletedAt),
        ),
      )
      .orderBy(desc(schema.conversationSessions.updatedAt));
    return rows.map(toSession);
  },

  async listSessions(
    projectId?: string | null,
  ): Promise<ConversationSession[]> {
    const local = await this.listSessionsLocal(projectId);
    if (await canRefreshServer()) {
      try {
        const refreshed = await this.refreshSessions(projectId);
        useNetworkStore.getState().setServerReachable(true);
        return refreshed;
      } catch {
        useNetworkStore.getState().setServerReachable(false);
        return local;
      }
    }
    return local;
  },

  async refreshSessions(
    projectId?: string | null,
  ): Promise<ConversationSession[]> {
    const sessions = await chatApi.listSessions(projectId ?? undefined);
    await applyRemoteConversationSessions(sessions);
    return sessions;
  },

  async listMessagesLocal(sessionId: string): Promise<ConversationMessage[]> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.conversationMessages)
      .where(
        and(
          eq(schema.conversationMessages.sessionId, sessionId),
          isNull(schema.conversationMessages.deletedAt),
        ),
      )
      .orderBy(schema.conversationMessages.createdAt);
    return rows.map(toMessage);
  },

  async listMessages(sessionId: string): Promise<ConversationMessage[]> {
    const local = await this.listMessagesLocal(sessionId);
    if (await canRefreshServer()) {
      try {
        const refreshed = await this.refreshMessages(sessionId);
        useNetworkStore.getState().setServerReachable(true);
        return refreshed;
      } catch {
        useNetworkStore.getState().setServerReachable(false);
        return local;
      }
    }
    return local;
  },

  async refreshMessages(sessionId: string): Promise<ConversationMessage[]> {
    const local = await this.listMessagesLocal(sessionId);
    const messages = await chatApi.getMessages(sessionId);
    if (!messages.length && local.length) {
      return local;
    }
    await this.pruneSentLocalMessages(sessionId, messages);
    await applyRemoteConversationMessages(messages);
    await updateSessionStats(sessionId, (session) => {
      const metadata =
        (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
      return {
        sessionMetadata: {
          ...metadata,
          message_count: messages.length,
          is_active: true,
        },
        updatedAt: new Date().toISOString(),
      };
    });
    return messages;
  },

  async getSessionLocal(
    sessionId: string,
  ): Promise<ConversationSession | null> {
    const db = getDb();
    const row = (
      await db
        .select()
        .from(schema.conversationSessions)
        .where(
          and(
            eq(schema.conversationSessions.id, sessionId),
            isNull(schema.conversationSessions.deletedAt),
          ),
        )
    )[0];
    return row ? toSession(row) : null;
  },

  async createSession(
    characterName = "default",
    projectId?: string | null,
  ): Promise<ConversationSession> {
    if (await canAttemptServer()) {
      try {
        const session = await chatApi.createSession(
          characterName,
          projectId ?? undefined,
        );
        await applyRemoteConversationSessions([session]);
        return session;
      } catch {
        // Fall back to local-first below.
      }
    }
    const session = buildLocalSession(characterName, projectId);
    await applyRemoteConversationSessions([
      {
        ...session,
        user_id: "",
      },
    ]);
    return session;
  },

  async deleteSession(sessionId: string): Promise<void> {
    if (await canUseServer()) {
      try {
        await chatApi.deleteSession(sessionId);
      } catch {
        // Fall back to local-first below.
      }
    }
    const db = getDb();
    const now = new Date().toISOString();
    await db
      .update(schema.conversationSessions)
      .set({ deletedAt: now, updatedAt: now })
      .where(eq(schema.conversationSessions.id, sessionId));
  },

  async updateTitle(sessionId: string, title: string): Promise<void> {
    if (await canUseServer()) {
      try {
        await chatApi.updateTitle(sessionId, title);
      } catch {
        // Fall back to local-first below.
      }
    }
    await updateSessionStats(sessionId, (session) => ({
      title,
      sessionMetadata:
        (session?.sessionMetadata as Record<string, unknown> | null) ?? {},
    }));
  },

  async saveLocalMessages(
    sessionId: string,
    messages: ConversationMessage[],
  ): Promise<void> {
    await applyRemoteConversationMessages(messages);
    await updateSessionStats(sessionId, (session) => {
      const metadata =
        (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
      return {
        sessionMetadata: {
          ...metadata,
          message_count: messages.length,
        },
      };
    });
  },

  async appendLocalMessage(
    sessionId: string,
    role: ConversationMessage["role"],
    content: string,
    metadata: Record<string, unknown> = {},
  ): Promise<ConversationMessage> {
    const now = new Date().toISOString();
    const db = getDb();
    const message: ConversationMessage = {
      id: randomId(),
      session_id: sessionId,
      role,
      content,
      metadata,
      created_at: now,
      updated_at: now,
      parent_message_id: null,
      branch_index: 0,
      is_active_branch: true,
    };
    await db.insert(schema.conversationMessages).values({
      id: message.id,
      sessionId,
      role,
      content,
      messageMetadata: metadata,
      tokenCount: null,
      parentMessageId: null,
      branchIndex: 0,
      isActiveBranch: true,
      createdAt: now,
      updatedAt: now,
      deletedAt: null,
    });
    await updateSessionStats(sessionId, (session) => {
      const currentMetadata =
        (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
      return {
        sessionMetadata: {
          ...currentMetadata,
          message_count: Number(currentMetadata.message_count ?? 0) + 1,
        },
      };
    });
    return message;
  },

  async listPendingMessages(sessionId: string): Promise<ConversationMessage[]> {
    return listPendingMessagesForSessionId(sessionId);
  },

  async markPendingMessageQueued(messageId: string): Promise<void> {
    const db = getDb();
    const row = (
      await db
        .select()
        .from(schema.conversationMessages)
        .where(eq(schema.conversationMessages.id, messageId))
    )[0];
    const metadata =
      (row?.messageMetadata as Record<string, unknown> | null) ?? {};
    await db
      .update(schema.conversationMessages)
      .set({
        messageMetadata: {
          ...metadata,
          pending: false,
          queued_at: new Date().toISOString(),
        },
        updatedAt: new Date().toISOString(),
      })
      .where(eq(schema.conversationMessages.id, messageId));
  },

  async mergeMessageMetadata(
    messageId: string,
    patch: Record<string, unknown>,
  ): Promise<void> {
    const db = getDb();
    const row = (
      await db
        .select()
        .from(schema.conversationMessages)
        .where(eq(schema.conversationMessages.id, messageId))
    )[0];
    const metadata =
      (row?.messageMetadata as Record<string, unknown> | null) ?? {};
    await db
      .update(schema.conversationMessages)
      .set({
        messageMetadata: {
          ...metadata,
          ...patch,
        },
        updatedAt: new Date().toISOString(),
      })
      .where(eq(schema.conversationMessages.id, messageId));
  },

  async pruneSentLocalMessages(
    sessionId: string,
    remoteMessages: ConversationMessage[] = [],
  ): Promise<void> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.conversationMessages)
      .where(
        and(
          eq(schema.conversationMessages.sessionId, sessionId),
          isNull(schema.conversationMessages.deletedAt),
        ),
      )
      .orderBy(asc(schema.conversationMessages.createdAt));
    for (const row of rows) {
      const metadata =
        (row.messageMetadata as Record<string, unknown> | null) ?? {};
      const hasRemoteCopy = remoteMessages.some(
        (message) =>
          message.session_id === sessionId &&
          message.role === row.role &&
          message.content === row.content,
      );
      if (metadata.local_only && !metadata.pending && hasRemoteCopy) {
        await db
          .delete(schema.conversationMessages)
          .where(eq(schema.conversationMessages.id, row.id));
      }
    }
  },

  async getBranchesLocal(messageId: string): Promise<ConversationMessage[]> {
    const db = getDb();
    const row = (
      await db
        .select()
        .from(schema.conversationMessages)
        .where(eq(schema.conversationMessages.id, messageId))
    )[0];
    if (!row) return [];
    const parentKey = row.parentMessageId ?? "__root__";
    const rows = await db
      .select()
      .from(schema.conversationMessages)
      .where(isNull(schema.conversationMessages.deletedAt))
      .orderBy(
        asc(schema.conversationMessages.branchIndex),
        asc(schema.conversationMessages.createdAt),
      );
    return rows
      .map(toMessage)
      .filter((message) => branchGroupKey(message) === parentKey);
  },

  async fetchBranches(
    sessionId: string,
    messageId: string,
  ): Promise<ConversationMessage[]> {
    const branches = await chatApi.getMessageBranches(sessionId, messageId);
    await applyRemoteConversationMessages(branches);
    return branches;
  },

  async switchBranch(
    sessionId: string,
    messageId: string,
    branchIndex: number,
  ): Promise<ConversationMessage[]> {
    if (!(await canUseServer())) {
      throw new Error("分岐切替はサーバーログイン中のみ利用できます");
    }
    await chatApi.switchBranch(sessionId, messageId, branchIndex);
    return this.refreshMessages(sessionId);
  },

  async editMessage(
    sessionId: string,
    messageId: string,
    content: string,
  ): Promise<ConversationMessage[]> {
    if (!(await canUseServer())) {
      throw new Error("メッセージ編集はサーバーログイン中のみ利用できます");
    }
    await chatApi.editMessage(sessionId, messageId, content);
    return this.refreshMessages(sessionId);
  },
};
