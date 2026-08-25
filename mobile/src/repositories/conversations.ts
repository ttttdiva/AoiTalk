import { and, asc, desc, eq, inArray, isNull, sql } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { isApiConnectionError, isApiHttpError } from "../lib/api-client";
import { requireCharacterSlug } from "../lib/character-api";
import { chatApi, type ConversationMessagesResponse } from "../lib/chat-api";
import { isServerKnownUnreachable, useNetworkStore } from "../stores/network";
import type { ConversationMessage, ConversationSession } from "../types/api";
import { pendingDispatchPayload } from "../features/conversation/pending-dispatch-payload";
import { conversationPerformanceDiagnostics } from "../features/conversation/performance-diagnostics";
import { randomId } from "./outbox";
import {
  CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE,
  conversationMessageUpsertStatementCount,
} from "./conversation-message-batching";

type DbSession = typeof schema.conversationSessions.$inferSelect;
type DbMessage = typeof schema.conversationMessages.$inferSelect;
type ConversationDispatchPayload = Parameters<typeof chatApi.dispatchMessage>[1];
type ConversationChangeListener = () => void;

const conversationChangeListeners = new Set<ConversationChangeListener>();

export function subscribeToConversationChanges(
  listener: ConversationChangeListener,
): () => void {
  conversationChangeListeners.add(listener);
  return () => conversationChangeListeners.delete(listener);
}

function notifyConversationChanges(): void {
  for (const listener of conversationChangeListeners) {
    try {
      listener();
    } catch {
      // A stale UI listener must not roll back a completed repository write.
    }
  }
}

export type ApplyRemoteConversationMessagesResult = {
  receivedCount: number;
  insertedCount: number;
  updatedCount: number;
  unchangedCount: number;
  upsertedCount: number;
  reconciledLocalCount: number;
  upsertStatementCount: number;
  bridgeStatementCount: number;
  transactionDurationMs: number;
};

export type ApplyRemoteConversationMessagesOptions = {
  reconcileSessionId?: string;
};

export type ConversationMessageRefreshMode =
  | "full"
  | "delta"
  | "full-reconcile";

export type RefreshConversationMessagesResult = {
  messages: ConversationMessage[];
  mode: ConversationMessageRefreshMode;
  receivedCount: number;
  upsertedCount: number;
  inactiveCount: number;
  cursor: string;
  fallbackReason?: "invalid-local-cursor" | "rejected-cursor" | "invalid-server-cursor";
};

export type CharacterUpdateEligibility =
  | { allowed: true }
  | { allowed: false; reason: string };

export class CharacterUpdateNotAllowedError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    super(reason);
    this.name = "CharacterUpdateNotAllowedError";
    this.reason = reason;
  }
}

/** キャラクター変更が通常セッションに限定される理由を副作用なしで判定する。 */
export function getCharacterUpdateEligibility(
  session: ConversationSession,
): CharacterUpdateEligibility {
  const characterName = session.character_name || "";
  const title = session.title || "";
  if (session.is_group_chat) {
    return {
      allowed: false,
      reason: "グループチャットではキャラクターを変更できません。",
    };
  }
  if (characterName.startsWith("trpg_room_") || title.startsWith("[TRPG]")) {
    return {
      allowed: false,
      reason: "TRPG連携セッションではキャラクターを変更できません。",
    };
  }
  if (/^scenario_roleplay:[^:]+:[^:]+$/.test(characterName)) {
    return {
      allowed: false,
      reason: "シナリオRPセッションではキャラクターを変更できません。",
    };
  }
  if (
    characterName.startsWith("scenario_") ||
    title.startsWith("[シナリオ]") ||
    title.startsWith("[執筆]")
  ) {
    return {
      allowed: false,
      reason: "シナリオ執筆セッションではキャラクターを変更できません。",
    };
  }
  return { allowed: true };
}

function remoteActiveBranch(message: ConversationMessage): boolean {
  return message.is_active_branch ?? true;
}

const BRANCH_COUNT_METADATA_KEY = "__aoitalk_branch_count";

function normalizedBranchCount(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : null;
}

function storedBranchCount(row: DbMessage): number | null {
  const metadata =
    (row.messageMetadata as Record<string, unknown> | null) ?? {};
  return normalizedBranchCount(metadata[BRANCH_COUNT_METADATA_KEY]);
}

function publicMessageMetadata(
  value: unknown,
): Record<string, unknown> {
  const metadata =
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? { ...(value as Record<string, unknown>) }
      : {};
  delete metadata[BRANCH_COUNT_METADATA_KEY];
  return metadata;
}

function storedMessageMetadata(
  message: ConversationMessage,
  existing?: DbMessage,
): Record<string, unknown> {
  const metadata = publicMessageMetadata(message.metadata);
  const branchCount =
    normalizedBranchCount(message.branch_count) ??
    (existing ? storedBranchCount(existing) : null);
  if (branchCount !== null) {
    metadata[BRANCH_COUNT_METADATA_KEY] = branchCount;
  }
  return metadata;
}

function branchCountProjectionMatches(
  stored: number | null,
  message: ConversationMessage,
): boolean {
  const projected = normalizedBranchCount(message.branch_count);
  return projected === null || projected === stored;
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((value, index) => valuesEqual(value, right[index]));
  }
  if (
    left === null ||
    right === null ||
    typeof left !== "object" ||
    typeof right !== "object"
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(rightRecord, key) &&
        valuesEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

function remoteRevisionMatches(row: DbMessage, message: ConversationMessage): boolean {
  const remoteUpdatedAt = message.updated_at ?? null;
  if (remoteUpdatedAt && row.updatedAt === remoteUpdatedAt) {
    return (
      row.isActiveBranch === remoteActiveBranch(message) &&
      (row.branchIndex ?? 0) === (message.branch_index ?? 0) &&
      branchCountProjectionMatches(storedBranchCount(row), message)
    );
  }
  return (
    !remoteUpdatedAt &&
    row.sessionId === message.session_id &&
    row.role === message.role &&
    row.content === message.content &&
    valuesEqual(publicMessageMetadata(row.messageMetadata), message.metadata ?? {}) &&
    row.tokenCount === (message.token_count ?? null) &&
    row.parentMessageId === (message.parent_message_id ?? null) &&
    (row.branchIndex ?? 0) === (message.branch_index ?? 0) &&
    branchCountProjectionMatches(storedBranchCount(row), message) &&
    row.isActiveBranch === remoteActiveBranch(message) &&
    row.deletedAt === null
  );
}

function conversationMessagesEqual(
  left: ConversationMessage,
  right: ConversationMessage,
): boolean {
  if (
    left.updated_at &&
    right.updated_at &&
    left.updated_at === right.updated_at
  ) {
    const rightBranchCount = normalizedBranchCount(right.branch_count);
    return (
      remoteActiveBranch(left) === remoteActiveBranch(right) &&
      (left.branch_index ?? 0) === (right.branch_index ?? 0) &&
      (rightBranchCount === null ||
        normalizedBranchCount(left.branch_count) === rightBranchCount)
    );
  }
  return (
    left.id === right.id &&
    left.session_id === right.session_id &&
    left.role === right.role &&
    left.content === right.content &&
    valuesEqual(left.metadata ?? {}, right.metadata ?? {}) &&
    (left.token_count ?? null) === (right.token_count ?? null) &&
    (left.parent_message_id ?? null) === (right.parent_message_id ?? null) &&
    (left.branch_index ?? 0) === (right.branch_index ?? 0) &&
    (normalizedBranchCount(right.branch_count) === null ||
      normalizedBranchCount(left.branch_count) ===
        normalizedBranchCount(right.branch_count)) &&
    remoteActiveBranch(left) === remoteActiveBranch(right) &&
    (left.created_at ?? null) === (right.created_at ?? null) &&
    (left.updated_at ?? null) === (right.updated_at ?? null)
  );
}

function toSession(row: DbSession): ConversationSession {
  const metadata =
    (row.sessionMetadata as Record<string, unknown> | null) ?? {};
  const context =
    metadata.context && typeof metadata.context === "object" && !Array.isArray(metadata.context)
      ? (metadata.context as Record<string, unknown>)
      : null;
  const groupCharacterNames = Array.isArray(metadata.group_character_names)
    ? metadata.group_character_names.filter(
        (value): value is string => typeof value === "string",
      )
    : undefined;
  const participants = Array.isArray(metadata.participants)
    ? metadata.participants.filter(
        (value): value is Record<string, unknown> =>
          Boolean(value) && typeof value === "object" && !Array.isArray(value),
      )
    : undefined;
  const rpSettings =
    metadata.rp_settings &&
    typeof metadata.rp_settings === "object" &&
    !Array.isArray(metadata.rp_settings)
      ? Object.fromEntries(
          Object.entries(metadata.rp_settings as Record<string, unknown>).flatMap(
            ([key, value]) =>
              typeof value === "number" && Number.isFinite(value)
                ? [[key, value]]
                : [],
          ),
        )
      : undefined;
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
    is_group_chat: Boolean(row.isGroupChat),
    app_id: typeof metadata.app_id === "string" ? metadata.app_id : null,
    app_target_id:
      typeof metadata.app_target_id === "string"
        ? metadata.app_target_id
        : null,
    development_status:
      metadata.development_status === "working" ||
      metadata.development_status === "waiting_for_user" ||
      metadata.development_status === "completed"
        ? metadata.development_status
        : null,
    last_read_at:
      typeof metadata.last_read_at === "string" ? metadata.last_read_at : null,
    is_unread: metadata.is_unread === true,
    context,
    parent_session_id:
      typeof metadata.parent_session_id === "string"
        ? metadata.parent_session_id
        : null,
    forked_from_message_id:
      typeof metadata.forked_from_message_id === "string"
        ? metadata.forked_from_message_id
        : null,
    ...(groupCharacterNames ? { group_character_names: groupCharacterNames } : {}),
    ...(participants ? { participants } : {}),
    ...(rpSettings ? { rp_settings: rpSettings } : {}),
  };
}

function toMessage(row: DbMessage): ConversationMessage {
  return {
    id: row.id,
    session_id: row.sessionId,
    role: row.role as ConversationMessage["role"],
    content: row.content,
    metadata: publicMessageMetadata(row.messageMetadata),
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    token_count: row.tokenCount ?? null,
    branch_count: storedBranchCount(row),
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
  characterName: string,
  projectId?: string | null,
): ConversationSession {
  const characterSlug = requireCharacterSlug(characterName);
  const now = new Date().toISOString();
  return {
    id: randomId(),
    user_id: "",
    character_name: characterSlug,
    title: "ローカルチャット",
    project_id: projectId ?? null,
    session_start: now,
    last_activity: now,
    message_count: 0,
    is_active: true,
    is_group_chat: false,
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
  if (current) notifyConversationChanges();
}

export async function applyRemoteConversationSessions(
  list: ConversationSession[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const session of list) {
    const existingRows = await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, session.id));
    const existing = existingRows.find((row) => row.id === session.id);
    const pendingSlug = pendingCharacterSlug(existing);
    const preservePendingCharacter =
      Boolean(pendingSlug) && session.character_name !== pendingSlug;
    const characterName = preservePendingCharacter
      ? pendingSlug!
      : session.character_name;
    const remoteMetadata = {
      message_count: session.message_count,
      is_active: session.is_active,
      app_id: session.app_id ?? null,
      app_target_id: session.app_target_id ?? null,
      development_status: session.development_status ?? null,
      last_read_at: session.last_read_at ?? null,
      is_unread: session.is_unread === true,
      context: session.context ?? null,
      parent_session_id: session.parent_session_id ?? null,
      forked_from_message_id: session.forked_from_message_id ?? null,
      group_character_names: session.group_character_names ?? [],
      participants: session.participants ?? [],
      rp_settings: session.rp_settings ?? {},
      ...(preservePendingCharacter
        ? { [PENDING_CHARACTER_SLUG_KEY]: pendingSlug }
        : {}),
    };
    await db
      .insert(schema.conversationSessions)
      .values({
        id: session.id,
        userId: session.user_id,
        characterName,
        projectId: session.project_id ?? null,
        title: session.title ?? "",
        isGroupChat: session.is_group_chat ?? false,
        sessionMetadata: remoteMetadata,
        createdAt: session.session_start ?? now,
        updatedAt: session.last_activity ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.conversationSessions.id,
        set: {
          userId: session.user_id,
          characterName,
          projectId: session.project_id ?? null,
          title: session.title ?? "",
          isGroupChat: session.is_group_chat ?? false,
          updatedAt: session.last_activity ?? now,
          deletedAt: null,
          sessionMetadata: remoteMetadata,
        },
      });
  }
  notifyConversationChanges();
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
  notifyConversationChanges();
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

const PENDING_CHARACTER_SLUG_KEY = "pending_character_slug";

function pendingCharacterSlug(session: DbSession | undefined): string | null {
  const metadata =
    (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
  const value = metadata[PENDING_CHARACTER_SLUG_KEY];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function isRecoverableCharacterUpdateError(error: unknown): boolean {
  if (isApiConnectionError(error)) return true;
  return isApiHttpError(error) && error.status >= 500 && error.status < 600;
}

function withPendingCharacterSlug(
  session: DbSession,
  characterSlug: string,
  pending: boolean,
): Record<string, unknown> {
  const metadata =
    (session.sessionMetadata as Record<string, unknown> | null) ?? {};
  const next = { ...metadata };
  if (pending) {
    next[PENDING_CHARACTER_SLUG_KEY] = characterSlug;
  } else {
    delete next[PENDING_CHARACTER_SLUG_KEY];
  }
  return next;
}

async function updateLocalCharacter(
  session: DbSession,
  characterSlug: string,
  pending: boolean,
): Promise<ConversationSession> {
  const now = new Date().toISOString();
  const db = getDb();
  await db
    .update(schema.conversationSessions)
    .set({
      characterName: characterSlug,
      sessionMetadata: withPendingCharacterSlug(session, characterSlug, pending),
      updatedAt: now,
    })
    .where(eq(schema.conversationSessions.id, session.id));
  notifyConversationChanges();
  return {
    ...toSession(session),
    character_name: characterSlug,
    last_activity: now,
  };
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

const pendingMessageDispatchFlights = new Map<
  string,
  Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null>
>();

async function dispatchPendingConversationMessageOnce(
  sessionId: string,
  message: ConversationMessage,
  payload: ConversationDispatchPayload,
  checkRemoteDuplicate: boolean,
): Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null> {
  const current = (
    await conversationsRepo.listMessagesLocal(sessionId)
  ).find((candidate) => candidate.id === message.id);
  if (!current || !Boolean(current.metadata?.pending)) return null;

  if (checkRemoteDuplicate) {
    // 履歴確認が失敗した状態でPOSTすると、Serverが旧版の場合に重複生成する。
    // pendingを維持して次回retryへ委ねるため、GET失敗はそのまま伝播する。
    const remoteMessages = await chatApi.getMessages(sessionId);
    const existing = remoteMessages.find(
      (candidate) =>
        candidate.role === "user" &&
        candidate.metadata?.client_message_id === message.id,
    );
    if (existing) {
      await conversationsRepo.markPendingMessageQueued(
        message.id,
        existing.id,
      );
      return null;
    }
  }

  const result = await chatApi.dispatchMessage(sessionId, {
    ...payload,
    client_message_id: message.id,
  });
  await conversationsRepo.markPendingMessageQueued(
    message.id,
    result.user_message_id,
  );
  return result;
}

/**
 * 通常送信・手動Retry・自動flushをメッセージID単位で一本化する。
 *
 * 同一JS runtime内の競合は共有flightで抑止し、再起動後のRetryはServerに
 * 保存されたclient_message_idを照合して、受理済みメッセージを再dispatchしない。
 */
export function dispatchPendingConversationMessage(
  sessionId: string,
  message: ConversationMessage,
  payload: ConversationDispatchPayload,
  options?: { checkRemoteDuplicate?: boolean },
): Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null> {
  const key = `${sessionId}:${message.id}`;
  const existing = pendingMessageDispatchFlights.get(key);
  if (existing) return existing;
  const flight = dispatchPendingConversationMessageOnce(
    sessionId,
    message,
    payload,
    options?.checkRemoteDuplicate !== false,
  ).finally(() => {
    if (pendingMessageDispatchFlights.get(key) === flight) {
      pendingMessageDispatchFlights.delete(key);
    }
  });
  pendingMessageDispatchFlights.set(key, flight);
  return flight;
}

async function flushPendingCharacterUpdate(
  session: DbSession,
): Promise<void> {
  const characterSlug = pendingCharacterSlug(session);
  if (!characterSlug || !isServerBackedSession(session)) return;
  if (!(await canAttemptServer())) return;

  try {
    const updated = await chatApi.updateCharacter(session.id, characterSlug);
    await applyRemoteConversationSessions([updated]);
  } catch (error) {
    if (isRecoverableCharacterUpdateError(error)) {
      useNetworkStore.getState().setServerReachable(false);
    }
    throw error;
  }
}

async function promoteLocalSession(
  session: DbSession,
): Promise<ConversationSession> {
  const db = getDb();
  const remote = await chatApi.createSession(
    requireCharacterSlug(session.characterName),
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

  notifyConversationChanges();
  return remote;
}

/** アップロード対象メッセージを created_at 昇順に整え、role を user/assistant に限定する。 */
export function orderLocalMessagesForUpload(
  messages: ConversationMessage[],
): Array<{ role: "user" | "assistant"; content: string }> {
  return [...messages]
    .filter(
      (message) => message.role === "user" || message.role === "assistant",
    )
    .sort((left, right) =>
      String(left.created_at ?? "").localeCompare(String(right.created_at ?? "")),
    )
    .map((message) => ({
      role: message.role as "user" | "assistant",
      content: message.content,
    }));
}

type LocalUploadMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

function orderLocalMessageRowsForUpload(
  messages: ConversationMessage[],
): LocalUploadMessage[] {
  return [...messages]
    .filter(
      (message) => message.role === "user" || message.role === "assistant",
    )
    .sort((left, right) =>
      String(left.created_at ?? "").localeCompare(String(right.created_at ?? "")),
    )
    .map((message) => ({
      id: message.id,
      role: message.role as "user" | "assistant",
      content: message.content,
    }));
}

function legacyUploadedLocalMessageIds(
  uploads: LocalUploadMessage[],
  serverMessages: ConversationMessage[],
): Set<string> {
  const unmatchedRemote = [...serverMessages];
  const uploadedIds = new Set<string>();
  for (const upload of uploads) {
    const matchIndex = unmatchedRemote.findIndex(
      (message) =>
        message.role === upload.role && message.content === upload.content,
    );
    if (matchIndex < 0) continue;
    uploadedIds.add(upload.id);
    unmatchedRemote.splice(matchIndex, 1);
  }
  return uploadedIds;
}

/** 昇格元セッションへ書き込む tombstone メタデータ（promoteLocalSession と同一規約）。 */
export function buildPromotedSessionMetadata(
  remoteId: string,
): { local_only: true; promoted_to_session_id: string } {
  return { local_only: true, promoted_to_session_id: remoteId };
}

// 同一セッションの並行アップロードを防ぐ in-flight ガード。
const uploadInFlight = new Set<string>();

/**
 * ローカル専用セッションを明示操作でサーバーへ同期する。
 *
 * 自動昇格 (promoteLocalSession / flushPendingConversation) と異なり、
 * pending メッセージが無い Direct 完結セッションでも呼べる別経路。
 * AI 再生成を避けるため dispatch は使わず、role+content を addMessage で
 * 順次投入してからローカルを付け替え + 旧セッションを tombstone する。
 *
 * 冪等性: createSession 成功直後に中間マーカー `upload_target_session_id` を
 * ローカルへ保存し、途中失敗からの再実行では createSession を再走させず、
 * サーバー側の既存メッセージ数の続き（createdAt 昇順で決定的）から投入を
 * 再開してオーファンセッションの増殖と重複投入を防ぐ。
 */
export async function uploadLocalSession(sessionId: string): Promise<string> {
  if (uploadInFlight.has(sessionId)) {
    throw new Error("このセッションは既に同期処理中です。");
  }
  uploadInFlight.add(sessionId);
  try {
    return await uploadLocalSessionOnce(sessionId);
  } finally {
    uploadInFlight.delete(sessionId);
  }
}

async function uploadLocalSessionOnce(sessionId: string): Promise<string> {
  if (!(await canAttemptServer())) {
    throw new Error(
      "サーバー同期にはログインとネットワーク接続が必要です。",
    );
  }

  const db = getDb();
  const row = (
    await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId))
  )[0];
  if (!row) {
    throw new Error("同期対象のセッションが見つかりません。");
  }
  if (isServerBackedSession(row)) {
    // 既にサーバー化済み。既存動作を変えないため、そのまま id を返す。
    return sessionId;
  }

  const localMessages = await conversationsRepo.listMessagesLocal(row.id);
  const uploadRows = orderLocalMessageRowsForUpload(localMessages);
  const uploads = uploadRows.map(({ role, content }) => ({ role, content }));

  const metadata = (row.sessionMetadata as Record<string, unknown> | null) ?? {};
  const resumeTargetId =
    typeof metadata.upload_target_session_id === "string"
      ? metadata.upload_target_session_id
      : null;

  let remoteId: string;
  let uploadedClientMessageIds = new Set<string>();
  if (resumeTargetId) {
    // 前回の途中失敗を再開する。createSession は再走させない。
    remoteId = resumeTargetId;
    // 既に投入済みの件数を取得し、続きから再開する（AI を発火しない読み取り）。
    const serverMessages = await chatApi.getMessages(resumeTargetId);
    if (metadata.upload_resume_version === 2) {
      uploadedClientMessageIds = new Set(
        serverMessages
          .map((message) => message.metadata?.client_message_id)
          .filter((id): id is string => typeof id === "string" && id.length > 0),
      );
    } else {
      uploadedClientMessageIds = legacyUploadedLocalMessageIds(
        uploadRows,
        serverMessages,
      );
    }
  } else {
    const remote = await chatApi.createSession(
      requireCharacterSlug(row.characterName),
      row.projectId ?? undefined,
    );
    remoteId = remote.id;
    // 付替え・tombstone 前の中間状態マーカーを保存（再開の起点）。
    await db
      .update(schema.conversationSessions)
      .set({
        sessionMetadata: {
          ...metadata,
          upload_target_session_id: remote.id,
          upload_resume_version: 2,
        },
        updatedAt: new Date().toISOString(),
      })
      .where(eq(schema.conversationSessions.id, row.id));
    await applyRemoteConversationSessions([remote]);
  }

  for (let index = 0; index < uploads.length; index += 1) {
    if (uploadedClientMessageIds.has(uploadRows[index].id)) continue;
    await chatApi.addMessage(remoteId, {
      ...uploads[index],
      client_message_id: uploadRows[index].id,
    });
  }

  const now = new Date().toISOString();
  await db
    .update(schema.conversationMessages)
    .set({ sessionId: remoteId, updatedAt: now })
    .where(eq(schema.conversationMessages.sessionId, row.id));
  // 投入完了。中間マーカーは残さず tombstone 規約のメタデータで確定する。
  await db
    .update(schema.conversationSessions)
    .set({
      deletedAt: now,
      updatedAt: now,
      sessionMetadata: buildPromotedSessionMetadata(remoteId),
    })
    .where(eq(schema.conversationSessions.id, row.id));

  notifyConversationChanges();
  return remoteId;
}

const pendingFlushFlights = new Map<string, Promise<string>>();

async function flushPendingConversationOnce(
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
  if (!row) return sessionId;
  if (row.deletedAt) {
    const metadata = row.sessionMetadata as Record<string, unknown> | null;
    const promotedTo = metadata?.promoted_to_session_id;
    return typeof promotedTo === "string" ? promotedTo : sessionId;
  }

  await flushPendingCharacterUpdate(row);

  const pendingBeforePromotion = await listPendingMessagesForSessionId(row.id);
  if (!pendingBeforePromotion.length) return sessionId;

  let remoteSessionId = row.id;
  let projectId = row.projectId ?? undefined;
  if (!isServerBackedSession(row)) {
    const promoted = await promoteLocalSession(row);
    remoteSessionId = promoted.id;
    projectId = promoted.project_id ?? row.projectId ?? undefined;
  }

  const pending = await listPendingMessagesForSessionId(remoteSessionId);
  for (const message of pending) {
    const dispatchPayload = pendingDispatchPayload(message, {
      projectId,
      includeProjectContext: Boolean(projectId),
      agentMode: "confirm",
    });
    await dispatchPendingConversationMessage(
      remoteSessionId,
      message,
      dispatchPayload,
      { checkRemoteDuplicate: true },
    );
  }

  return remoteSessionId;
}

export function flushPendingConversation(sessionId: string): Promise<string> {
  const existing = pendingFlushFlights.get(sessionId);
  if (existing) return existing;
  const flight = flushPendingConversationOnce(sessionId).finally(() => {
    if (pendingFlushFlights.get(sessionId) === flight) {
      pendingFlushFlights.delete(sessionId);
    }
  });
  pendingFlushFlights.set(sessionId, flight);
  return flight;
}

export async function getPromotedConversationSessionId(
  sessionId: string,
): Promise<string | null> {
  const db = getDb();
  const row = (
    await db
      .select({ metadata: schema.conversationSessions.sessionMetadata })
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId))
  )[0];
  const metadata = row?.metadata as Record<string, unknown> | null | undefined;
  const promotedTo = metadata?.promoted_to_session_id;
  return typeof promotedTo === "string" ? promotedTo : null;
}

export async function flushPendingConversations(): Promise<void> {
  if (!(await canAttemptServer())) return;

  const db = getDb();
  const sessions = await db
    .select()
    .from(schema.conversationSessions)
    .where(isNull(schema.conversationSessions.deletedAt));
  for (const session of sessions) {
    if (!isServerBackedSession(session)) continue;
    const pending = await listPendingMessagesForSessionId(session.id);
    if (!pending.length && !pendingCharacterSlug(session)) continue;
    try {
      await flushPendingConversation(session.id);
    } catch (error) {
      // Leave pending metadata intact for the next sync attempt.
      if (isApiConnectionError(error)) throw error;
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
  let changed = false;
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
    changed = true;
  }
  if (changed) notifyConversationChanges();
}

function reconciledLocalMessageIds(
  sessionId: string,
  rows: DbMessage[],
  remoteMessages: ConversationMessage[],
): string[] {
  const candidates = rows.filter((row) => {
    const metadata =
      (row.messageMetadata as Record<string, unknown> | null) ?? {};
    return Boolean(metadata.local_only);
  });
  const signatureOf = (role: string, content: string) =>
    `${role}\u0000${content}`;
  const localLegacyCounts = new Map<string, number>();
  for (const row of candidates) {
    const metadata =
      (row.messageMetadata as Record<string, unknown> | null) ?? {};
    if (
      Boolean(metadata.pending) ||
      typeof metadata.server_message_id === "string"
    ) {
      continue;
    }
    const key = signatureOf(row.role, row.content);
    localLegacyCounts.set(key, (localLegacyCounts.get(key) ?? 0) + 1);
  }
  const remoteLegacyCounts = new Map<string, number>();
  for (const message of remoteMessages) {
    if (
      message.session_id !== sessionId ||
      typeof message.metadata?.client_message_id === "string"
    ) {
      continue;
    }
    const key = signatureOf(message.role, message.content);
    remoteLegacyCounts.set(key, (remoteLegacyCounts.get(key) ?? 0) + 1);
  }
  const consumedRemoteIndexes = new Set<number>();
  const matchedIds: string[] = [];

  for (const row of candidates) {
    const metadata =
      (row.messageMetadata as Record<string, unknown> | null) ?? {};
    const serverMessageId =
      typeof metadata.server_message_id === "string"
        ? metadata.server_message_id
        : null;
    const pending = Boolean(metadata.pending);
    const clientMessageId =
      typeof metadata.client_message_id === "string"
        ? metadata.client_message_id
        : row.id;
    let remoteIndex = remoteMessages.findIndex(
      (message, index) =>
        !consumedRemoteIndexes.has(index) &&
        message.session_id === sessionId &&
        serverMessageId !== null &&
        message.id === serverMessageId,
    );

    if (remoteIndex < 0) {
      remoteIndex = remoteMessages.findIndex(
        (message, index) =>
          !consumedRemoteIndexes.has(index) &&
          message.session_id === sessionId &&
          message.metadata?.client_message_id === clientMessageId,
      );
    }

    if (remoteIndex < 0 && !pending && serverMessageId === null) {
      const key = signatureOf(row.role, row.content);
      if (
        localLegacyCounts.get(key) === 1 &&
        remoteLegacyCounts.get(key) === 1
      ) {
        remoteIndex = remoteMessages.findIndex(
          (message, index) =>
            !consumedRemoteIndexes.has(index) &&
            message.session_id === sessionId &&
            typeof message.metadata?.client_message_id !== "string" &&
            message.role === row.role &&
            message.content === row.content,
        );
      }
    }

    if (remoteIndex < 0) continue;
    consumedRemoteIndexes.add(remoteIndex);
    matchedIds.push(row.id);
  }
  return matchedIds;
}

async function loadReconciledLocalMessageIds(
  sessionId: string,
  remoteMessages: ConversationMessage[],
): Promise<string[]> {
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
  return reconciledLocalMessageIds(sessionId, rows, remoteMessages);
}

export async function applyRemoteConversationMessages(
  list: ConversationMessage[],
  options: ApplyRemoteConversationMessagesOptions = {},
): Promise<ApplyRemoteConversationMessagesResult> {
  if (!list.length) {
    return {
      receivedCount: 0,
      insertedCount: 0,
      updatedCount: 0,
      unchangedCount: 0,
      upsertedCount: 0,
      reconciledLocalCount: 0,
      upsertStatementCount: 0,
      bridgeStatementCount: 0,
      transactionDurationMs: 0,
    };
  }
  const db = getDb();
  const now = new Date().toISOString();
  const existingRows = await db
    .select()
    .from(schema.conversationMessages)
    .where(
      inArray(
        schema.conversationMessages.id,
        [...new Set(list.map((message) => message.id))],
      ),
    );
  const existingById = new Map(existingRows.map((row) => [row.id, row]));
  const changed = list.filter((message) => {
    const existing = existingById.get(message.id);
    return !existing || !remoteRevisionMatches(existing, message);
  });
  const insertedCount = changed.filter(
    (message) => !existingById.has(message.id),
  ).length;
  const updatedCount = changed.length - insertedCount;
  const reconciledLocalIds = options.reconcileSessionId
    ? await loadReconciledLocalMessageIds(options.reconcileSessionId, list)
    : [];
  const upsertStatementCount = conversationMessageUpsertStatementCount(
    changed.length,
  );
  const hasTransactionWrites =
    changed.length > 0 || reconciledLocalIds.length > 0;
  const bridgeStatementCount =
    1 +
    (options.reconcileSessionId ? 1 : 0) +
    (hasTransactionWrites
      ? 2 + upsertStatementCount + (reconciledLocalIds.length ? 1 : 0)
      : 0);

  let transactionDurationMs = 0;
  if (hasTransactionWrites) {
    const values = changed.map((message) => ({
      id: message.id,
      sessionId: message.session_id,
      role: message.role,
      content: message.content,
      messageMetadata: storedMessageMetadata(
        message,
        existingById.get(message.id),
      ),
      tokenCount: message.token_count ?? null,
      parentMessageId: message.parent_message_id ?? null,
      branchIndex: message.branch_index ?? 0,
      isActiveBranch: remoteActiveBranch(message),
      createdAt: message.created_at ?? now,
      updatedAt: message.updated_at ?? now,
      deletedAt: null,
    }));
    const stop = conversationPerformanceDiagnostics.startTimer(
      "sqlite",
      "conversation-message-apply",
    );
    try {
      db.transaction((tx) => {
        if (reconciledLocalIds.length) {
          tx
            .delete(schema.conversationMessages)
            .where(
              inArray(schema.conversationMessages.id, reconciledLocalIds),
            )
            .run();
        }
        for (
          let index = 0;
          index < values.length;
          index += CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE
        ) {
          const chunk = values.slice(
            index,
            index + CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE,
          );
          tx
            .insert(schema.conversationMessages)
            .values(chunk)
            .onConflictDoUpdate({
              target: schema.conversationMessages.id,
              set: {
                sessionId: sql.raw("excluded.session_id"),
                role: sql.raw("excluded.role"),
                content: sql.raw("excluded.content"),
                messageMetadata: sql.raw("excluded.message_metadata"),
                tokenCount: sql.raw("excluded.token_count"),
                parentMessageId: sql.raw("excluded.parent_message_id"),
                branchIndex: sql.raw("excluded.branch_index"),
                isActiveBranch: sql.raw("excluded.is_active_branch"),
                updatedAt: sql.raw("excluded.updated_at"),
                deletedAt: null,
              },
            })
            .run();
        }
      });
    } finally {
      transactionDurationMs = stop();
    }
  }
  conversationPerformanceDiagnostics.increment(
    "sqlite",
    "conversation-message-upserts",
    changed.length,
  );
  conversationPerformanceDiagnostics.increment(
    "sqlite",
    "conversation-message-upsert-statements",
    upsertStatementCount,
  );
  conversationPerformanceDiagnostics.increment(
    "sqlite",
    "conversation-message-bridge-statements",
    bridgeStatementCount,
  );
  return {
    receivedCount: list.length,
    insertedCount,
    updatedCount,
    unchangedCount: list.length - changed.length,
    upsertedCount: changed.length,
    reconciledLocalCount: reconciledLocalIds.length,
    upsertStatementCount,
    bridgeStatementCount,
    transactionDurationMs,
  };
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

const CONVERSATION_MESSAGE_SYNC_STATE_PREFIX = "conversation_messages";

export function conversationMessageSyncStateKey(
  authScope: string,
  sessionId: string,
): string {
  return `${CONVERSATION_MESSAGE_SYNC_STATE_PREFIX}:${authScope}:${sessionId}`;
}

function isValidMessageCursor(cursor: string | null | undefined): cursor is string {
  return Boolean(cursor && Number.isFinite(Date.parse(cursor)));
}

async function readConversationMessageCursor(
  authScope: string,
  sessionId: string,
): Promise<string | null> {
  const db = getDb();
  const rows = await db
    .select({ cursor: schema.syncState.cursor })
    .from(schema.syncState)
    .where(
      eq(
        schema.syncState.tableName,
        conversationMessageSyncStateKey(authScope, sessionId),
      ),
    );
  return rows[0]?.cursor ?? null;
}

function writeConversationMessageCursor(
  authScope: string,
  sessionId: string,
  cursor: string,
): void {
  const db = getDb();
  const tableName = conversationMessageSyncStateKey(authScope, sessionId);
  db
    .insert(schema.syncState)
    .values({ tableName, cursor, lastPulledAt: cursor })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { cursor, lastPulledAt: cursor },
    })
    .run();
}

function mergeConversationMessageDelta(
  local: ConversationMessage[],
  remote: ConversationMessage[],
  full: boolean,
): ConversationMessage[] {
  const stop = conversationPerformanceDiagnostics.startTimer(
    "merge",
    "conversation-message-delta",
  );
  try {
    const remoteClientMessageIds = new Set(
      remote
        .map((message) => message.metadata?.client_message_id)
        .filter((value): value is string => typeof value === "string"),
    );
    const localById = new Map(local.map((message) => [message.id, message]));
    const merged = new Map<string, ConversationMessage>();

    if (!full) {
      for (const message of local) {
        const clientMessageId =
          typeof message.metadata?.client_message_id === "string"
            ? message.metadata.client_message_id
            : message.id;
        if (
          isLocalOnlyMessage(message) &&
          remoteClientMessageIds.has(clientMessageId)
        ) {
          continue;
        }
        merged.set(message.id, message);
      }
    } else {
      for (const message of local) {
        if (!isLocalOnlyMessage(message)) continue;
        const clientMessageId =
          typeof message.metadata?.client_message_id === "string"
            ? message.metadata.client_message_id
            : message.id;
        if (!remoteClientMessageIds.has(clientMessageId)) {
          merged.set(message.id, message);
        }
      }
    }

    for (const message of remote) {
      if (!remoteActiveBranch(message)) {
        merged.delete(message.id);
        continue;
      }
      const existing = localById.get(message.id);
      const projectedMessage =
        normalizedBranchCount(message.branch_count) === null &&
        normalizedBranchCount(existing?.branch_count) !== null
          ? { ...message, branch_count: existing?.branch_count }
          : message;
      merged.set(
        message.id,
        existing && conversationMessagesEqual(existing, projectedMessage)
          ? existing
          : projectedMessage,
      );
    }

    return [...merged.values()].sort((left, right) =>
      (left.created_at ?? "").localeCompare(right.created_at ?? ""),
    );
  } finally {
    stop();
  }
}

function reconcileFullActiveMessages(
  local: ConversationMessage[],
  remote: ConversationMessage[],
): number {
  const activeRemoteIds = new Set(
    remote
      .filter(remoteActiveBranch)
      .map((message) => message.id),
  );
  const staleIds = local
    .filter(
      (message) =>
        !isLocalOnlyMessage(message) && !activeRemoteIds.has(message.id),
    )
    .map((message) => message.id);
  if (!staleIds.length) return 0;

  const db = getDb();
  db.transaction((tx) => {
    for (const id of staleIds) {
      tx
        .update(schema.conversationMessages)
        .set({ isActiveBranch: false })
        .where(eq(schema.conversationMessages.id, id))
        .run();
    }
  });
  return staleIds.length;
}

async function updateSessionMessageStatsIfChanged(
  sessionId: string,
  messageCount: number,
): Promise<boolean> {
  const db = getDb();
  const session = (
    await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId))
  )[0];
  if (!session) return false;
  const metadata =
    (session.sessionMetadata as Record<string, unknown> | null) ?? {};
  const currentCount = Number(metadata.message_count ?? 0);
  const currentActive = Boolean(metadata.is_active ?? true);
  if (currentCount === messageCount && currentActive) return false;
  await db
    .update(schema.conversationSessions)
    .set({
      sessionMetadata: {
        ...metadata,
        message_count: messageCount,
        is_active: true,
      },
      updatedAt: new Date().toISOString(),
    })
    .where(eq(schema.conversationSessions.id, sessionId));
  notifyConversationChanges();
  return true;
}

async function fetchConversationMessageDelta(
  sessionId: string,
  cursor: string | null,
): Promise<{
  response: ConversationMessagesResponse;
  mode: ConversationMessageRefreshMode;
  fallbackReason?: RefreshConversationMessagesResult["fallbackReason"];
}> {
  let fallbackReason: RefreshConversationMessagesResult["fallbackReason"];
  if (cursor && !isValidMessageCursor(cursor)) {
    fallbackReason = "invalid-local-cursor";
    const response = await chatApi.getMessagesDelta(sessionId);
    return { response, mode: "full-reconcile", fallbackReason };
  }

  if (!cursor) {
    return {
      response: await chatApi.getMessagesDelta(sessionId),
      mode: "full",
    };
  }

  try {
    const response = await chatApi.getMessagesDelta(sessionId, cursor);
    if (
      !isValidMessageCursor(response.server_time) ||
      Date.parse(response.server_time) < Date.parse(cursor)
    ) {
      fallbackReason = "invalid-server-cursor";
      return {
        response: await chatApi.getMessagesDelta(sessionId),
        mode: "full-reconcile",
        fallbackReason,
      };
    }
    return { response, mode: "delta" };
  } catch (error) {
    if (!isApiHttpError(error) || error.status !== 400) throw error;
    fallbackReason = "rejected-cursor";
    return {
      response: await chatApi.getMessagesDelta(sessionId),
      mode: "full-reconcile",
      fallbackReason,
    };
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
    options?: { forceRefresh?: boolean },
  ): Promise<ConversationSession[]> {
    // ローカルファースト: SQLite（runSync のデルタ同期で更新済み）を正とし、
    // chatApi.listSessions のフル取得は (a) ローカルが空の初回 と
    // (b) 明示的な pull-to-refresh の時だけに限定して二重フェッチを避ける。
    const local = await this.listSessionsLocal(projectId);
    const shouldFullFetch = Boolean(options?.forceRefresh) || local.length === 0;
    if (shouldFullFetch && (await canRefreshServer())) {
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

  async markSessionRead(sessionId: string): Promise<void> {
    const readAt = new Date().toISOString();
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.conversationSessions)
      .where(eq(schema.conversationSessions.id, sessionId));
    const current = rows[0];
    if (current) {
      const metadata =
        (current.sessionMetadata as Record<string, unknown> | null) ?? {};
      await db
        .update(schema.conversationSessions)
        .set({
          sessionMetadata: {
            ...metadata,
            last_read_at: readAt,
            is_unread: false,
          },
        })
        .where(eq(schema.conversationSessions.id, sessionId));
      notifyConversationChanges();
    }

    if (!(await canAttemptServer())) return;
    try {
      const response = await chatApi.markSessionRead(sessionId);
      const serverReadAt = response.last_read_at ?? readAt;
      if (!current) return;
      const latestRows = await db
        .select()
        .from(schema.conversationSessions)
        .where(eq(schema.conversationSessions.id, sessionId));
      const latest = latestRows[0];
      if (!latest) return;
      const latestMetadata =
        (latest.sessionMetadata as Record<string, unknown> | null) ?? {};
      await db
        .update(schema.conversationSessions)
        .set({
          sessionMetadata: {
            ...latestMetadata,
            last_read_at: serverReadAt,
            is_unread: false,
          },
        })
        .where(eq(schema.conversationSessions.id, sessionId));
    } catch {
      // ローカルの既読状態は維持し、オフラインからの復帰時に同期で再取得する。
    }
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
    return rows
      .filter((row) => row.isActiveBranch !== false)
      .map(toMessage);
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
    return (await this.refreshMessagesDetailed(sessionId)).messages;
  },

  async refreshMessagesDetailed(
    sessionId: string,
  ): Promise<RefreshConversationMessagesResult> {
    const local = await this.listMessagesLocal(sessionId);
    const authScope = getTokenAuthScope(await getToken());
    const previousCursor = await readConversationMessageCursor(
      authScope,
      sessionId,
    );
    const { response, mode, fallbackReason } =
      await fetchConversationMessageDelta(sessionId, previousCursor);
    if (!isValidMessageCursor(response.server_time)) {
      throw new Error("メッセージ同期のserver_timeが不正です");
    }

    const applyResult = await applyRemoteConversationMessages(
      response.messages,
      { reconcileSessionId: sessionId },
    );
    const isFull = mode !== "delta";
    const reconciledInactiveCount = isFull
      ? reconcileFullActiveMessages(local, response.messages)
      : 0;
    const messages = mergeConversationMessageDelta(
      local,
      response.messages,
      isFull,
    );
    await updateSessionMessageStatsIfChanged(sessionId, messages.length);

    // cursorはremote適用・reconcile・session statsがすべて成功した後だけ進める。
    // overlapで同じcursorが返った場合はsync_state自体も書き換えない。
    if (response.server_time !== previousCursor) {
      writeConversationMessageCursor(
        authScope,
        sessionId,
        response.server_time,
      );
    }

    const inactiveCount =
      response.messages.filter((message) => !remoteActiveBranch(message)).length +
      reconciledInactiveCount;
    conversationPerformanceDiagnostics.increment(
      "merge",
      "conversation-message-inactive",
      inactiveCount,
    );
    return {
      messages,
      mode,
      receivedCount: applyResult.receivedCount,
      upsertedCount: applyResult.upsertedCount,
      inactiveCount,
      cursor: response.server_time,
      ...(fallbackReason ? { fallbackReason } : {}),
    };
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

  /** 新規チャット画面を開くための、通信を伴わないセッション作成。 */
  async createLocalSession(
    characterName: string,
    projectId?: string | null,
  ): Promise<ConversationSession> {
    const session = buildLocalSession(characterName, projectId);
    await applyRemoteConversationSessions([session]);
    return session;
  },

  async createSession(
    characterName: string,
    projectId?: string | null,
  ): Promise<ConversationSession> {
    const characterSlug = requireCharacterSlug(characterName);
    if ((await canAttemptServer()) && !isServerKnownUnreachable()) {
      try {
        const session = await chatApi.createSession(
          characterSlug,
          projectId ?? undefined,
        );
        await applyRemoteConversationSessions([session]);
        return session;
      } catch (error) {
        if (!isApiConnectionError(error)) throw error;
        useNetworkStore.getState().setServerReachable(false);
      }
    }
    const session = buildLocalSession(characterSlug, projectId);
    await applyRemoteConversationSessions([
      {
        ...session,
        user_id: "",
      },
    ]);
    return session;
  },

  async updateCharacter(
    sessionId: string,
    characterSlug: string,
  ): Promise<ConversationSession> {
    const normalizedSlug = requireCharacterSlug(characterSlug);
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
    if (!row) {
      throw new Error("キャラクター変更対象のセッションが見つかりません。");
    }

    const current = toSession(row);
    const eligibility = getCharacterUpdateEligibility(current);
    if (!eligibility.allowed) {
      throw new CharacterUpdateNotAllowedError(eligibility.reason);
    }

    if (isServerBackedSession(row)) {
      if (await canAttemptServer()) {
        try {
          const updated = await chatApi.updateCharacter(
            sessionId,
            normalizedSlug,
          );
          await applyRemoteConversationSessions([updated]);
          return updated;
        } catch (error) {
          if (!isRecoverableCharacterUpdateError(error)) throw error;
          useNetworkStore.getState().setServerReachable(false);
          // サーバー停止中も選択操作は端末側で完了させ、復旧後にsyncする。
          return updateLocalCharacter(row, normalizedSlug, true);
        }
      }

      return updateLocalCharacter(row, normalizedSlug, true);
    }

    return updateLocalCharacter(row, normalizedSlug, false);
  },

  async updateProject(
    sessionId: string,
    projectId: string | null,
  ): Promise<ConversationSession> {
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
    if (!row) {
      throw new Error("プロジェクト変更対象のセッションが見つかりません。");
    }

    if (isServerBackedSession(row)) {
      const updated = await chatApi.updateProject(sessionId, projectId);
      await applyRemoteConversationSessions([updated]);
      return updated;
    }

    const now = new Date().toISOString();
    await db
      .update(schema.conversationSessions)
      .set({ projectId, updatedAt: now })
      .where(eq(schema.conversationSessions.id, sessionId));
    notifyConversationChanges();
    return {
      ...toSession(row),
      project_id: projectId,
      last_activity: now,
    };
  },

  async deleteSession(sessionId: string): Promise<void> {
    const authScope = getTokenAuthScope(await getToken());
    if (await canUseServer()) {
      try {
        await chatApi.deleteSession(sessionId);
      } catch {
        // Fall back to local-first below.
      }
    }
    const db = getDb();
    const now = new Date().toISOString();
    db.transaction((tx) => {
      tx
        .update(schema.conversationSessions)
        .set({ deletedAt: now, updatedAt: now })
        .where(eq(schema.conversationSessions.id, sessionId))
        .run();
      tx
        .delete(schema.syncState)
        .where(
          eq(
            schema.syncState.tableName,
            conversationMessageSyncStateKey(authScope, sessionId),
          ),
        )
        .run();
    });
    notifyConversationChanges();
  },

  async updateTitle(
    sessionId: string,
    title: string,
    options?: {
      syncServer?: boolean;
      source?: "llm" | "fallback";
      requireServerSuccess?: boolean;
    },
  ): Promise<void> {
    if (options?.requireServerSuccess) {
      const row = (
        await getDb()
          .select()
          .from(schema.conversationSessions)
          .where(
            and(
              eq(schema.conversationSessions.id, sessionId),
              isNull(schema.conversationSessions.deletedAt),
            ),
          )
      )[0];
      if (!row) {
        throw new Error("タイトル変更対象のセッションが見つかりません。");
      }
      if (isServerBackedSession(row)) {
        await chatApi.updateTitle(sessionId, title);
      }
    } else if (options?.syncServer !== false && (await canUseServer())) {
      try {
        await chatApi.updateTitle(sessionId, title);
      } catch {
        // Fall back to local-first below.
      }
    }
    await updateSessionStats(sessionId, (session) => ({
      title,
      sessionMetadata: {
        ...((session?.sessionMetadata as Record<string, unknown> | null) ?? {}),
        ...(options?.source
          ? { title_generation: { source: options.source } }
          : {}),
      },
    }));
  },

  async saveLocalMessages(
    sessionId: string,
    messages: ConversationMessage[],
  ): Promise<void> {
    await applyRemoteConversationMessages(messages);
    await updateSessionMessageStatsIfChanged(sessionId, messages.length);
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

  async markPendingMessageQueued(
    messageId: string,
    serverMessageId?: string,
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
          pending: false,
          queued_at: new Date().toISOString(),
          ...(serverMessageId ? { server_message_id: serverMessageId } : {}),
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
    const matchedIds = await loadReconciledLocalMessageIds(
      sessionId,
      remoteMessages,
    );
    for (const id of matchedIds) {
      await db
        .delete(schema.conversationMessages)
        .where(eq(schema.conversationMessages.id, id));
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
