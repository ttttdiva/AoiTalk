import { and, asc, desc, eq, inArray, isNull, sql } from "drizzle-orm";
import { getDb, getEffectiveSqliteJournalMode, schema } from "../db/client";
import {
  isSqliteBusyError,
  isSqliteDiagnosticsEnabled,
  logSqliteErrorDiagnostic,
} from "../db/sqlite-errors";
import { runForegroundSqliteWrite } from "../db/sqlite-write-coordinator";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { isMobileReadOnlyDiagnosticsEnabled } from "../lib/read-only-diagnostics";
import { getConfiguredApiServerFingerprint, isApiConnectionError, isApiHttpError, isApiTimeoutError } from "../lib/api-client";
import { requireCharacterSlug } from "../lib/character-api";
import { chatApi, type ConversationMessagesResponse } from "../lib/chat-api";
import { normalizeChatHistoryStatus } from "../lib/chat-session-view";
import { conversationAgentApi } from "../lib/conversation-agent-api";
import {
  canAttemptAoiTalkServer,
  isServerKnownUnreachable,
  useNetworkStore,
} from "../stores/network";
import type {
  AgentRun,
  ConversationAgent,
  ConversationMessage,
  ConversationSession,
} from "../types/api";
import {
  pendingDispatchPayload,
  replacePendingDispatchMessage,
} from "../features/conversation/pending-dispatch-payload";
import { conversationPerformanceDiagnostics } from "../features/conversation/performance-diagnostics";
import { hasManualConversationTitle } from "../features/conversation/title";
import { enqueueOutbox, getOutboxMutationWakeScope, hasPendingOutbox, randomId } from "./outbox";
import { notifyMutationQueued } from "../sync/task-mutation-wake";
import { interruptConversationForSend } from "./conversation-interrupt";
import {
  CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE,
  conversationMessageUpsertStatementCount,
} from "./conversation-message-batching";

type DbSession = typeof schema.conversationSessions.$inferSelect;
type DbMessage = typeof schema.conversationMessages.$inferSelect;
type ConversationDispatchPayload = Parameters<typeof chatApi.dispatchMessage>[1];
type ConversationChangeListener = () => void;
type ProvisionalReadFence = {
  token: number;
  completionAt: string | null;
};

const conversationChangeListeners = new Set<ConversationChangeListener>();
const provisionalReadFences = new Map<string, ProvisionalReadFence>();
let nextProvisionalReadToken = 0;

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

export class AgentBindingConflictError extends Error {
  readonly expectedAgentId: string | null;
  readonly requestedAgentId: string | null;

  constructor(
    expectedAgentId: string | null,
    requestedAgentId: string | null,
    message = "サーバー側のAgent紐付けが変わりました。現在のAgentを確認してから再試行してください。",
  ) {
    super(message);
    this.name = "AgentBindingConflictError";
    this.expectedAgentId = expectedAgentId;
    this.requestedAgentId = requestedAgentId;
  }
}

/** キャラクター変更が通常セッションに限定される理由を副作用なしで判定する。 */
export function getCharacterUpdateEligibility(
  session: ConversationSession,
): CharacterUpdateEligibility {
  if (session.is_group_chat) {
    return {
      allowed: false,
      reason: "グループチャットではキャラクターを変更できません。",
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
  // Sender projection is part of the ordinary message metadata in SQLite so
  // old databases can retain it without a destructive schema migration.
  for (const key of ["sender_type", "sender_id", "sender_display_name"] as const) {
    const value = message[key];
    if (typeof value === "string" && value.trim()) metadata[key] = value.trim();
    else if (value === null) metadata[key] = null;
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

function senderProjectionMetadata(message: ConversationMessage): Record<string, unknown> {
  const metadata: Record<string, unknown> = {};
  for (const key of ["sender_type", "sender_id", "sender_display_name"] as const) {
    const value = message[key];
    if (typeof value === "string" && value.trim()) metadata[key] = value.trim();
    else if (value === null) metadata[key] = null;
  }
  return metadata;
}

function senderProjectionMatches(row: DbMessage, message: ConversationMessage): boolean {
  const incoming = senderProjectionMetadata(message);
  const stored = publicMessageMetadata(row.messageMetadata);
  return Object.entries(incoming).every(([key, value]) => stored[key] === value);
}

function senderProjectionsEqual(
  left: ConversationMessage,
  right: ConversationMessage,
): boolean {
  for (const key of ["sender_type", "sender_id", "sender_display_name"] as const) {
    if (right[key] !== undefined && left[key] !== right[key]) return false;
  }
  return true;
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
      branchCountProjectionMatches(storedBranchCount(row), message) &&
      senderProjectionMatches(row, message)
    );
  }
  return (
    !remoteUpdatedAt &&
    row.sessionId === message.session_id &&
    row.role === message.role &&
    row.content === message.content &&
    valuesEqual(
      publicMessageMetadata(row.messageMetadata),
      { ...(message.metadata ?? {}), ...senderProjectionMetadata(message) },
    ) &&
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
      senderProjectionsEqual(left, right) &&
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
    senderProjectionsEqual(left, right) &&
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
    ...(metadata.local_only === true ? { is_local_only: true } : {}),
    user_id: row.userId ?? "",
    character_name: row.characterName ?? "",
    title: row.title ?? "",
    project_id: row.projectId ?? null,
    session_start: row.createdAt ?? null,
    last_activity: row.updatedAt ?? null,
    message_count: Number(metadata.message_count ?? 0),
    is_active: Boolean(metadata.is_active ?? true),
    is_pinned: Boolean(row.isPinned),
    pinned_at: row.pinnedAt ?? null,
    pin_updated_at: row.pinUpdatedAt ?? null,
    is_group_chat: Boolean(row.isGroupChat),
    agent_id: row.agentId ?? null,
    agent_display_name:
      typeof metadata.agent_display_name === "string"
        ? metadata.agent_display_name
        : null,
    agent_state:
      typeof metadata.agent_state === "string" ? metadata.agent_state : null,
    latest_revision_id:
      typeof metadata.latest_revision_id === "string"
        ? metadata.latest_revision_id
        : null,
    agent_character_id:
      typeof metadata.agent_character_id === "string"
        ? metadata.agent_character_id
        : null,
    agent_binding_pending: isPendingAgentBindingMetadata(metadata),
    agent_binding_error:
      typeof metadata.agent_binding_error === "string"
        ? metadata.agent_binding_error
        : null,
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
    history_status: normalizeChatHistoryStatus(metadata.history_status),
    history_completed_at:
      typeof metadata.history_completed_at === "string"
        ? metadata.history_completed_at
        : null,
    last_read_at:
      typeof metadata.last_read_at === "string" ? metadata.last_read_at : null,
    is_unread: metadata.is_unread === true,
    context,
    private_agent_root: metadata.private_agent_root === true,
    purpose: typeof metadata.purpose === "string" ? metadata.purpose : null,
    readonly_binding: metadata.readonly_binding === true,
    private_agent_root_metadata:
      metadata.private_agent_root_metadata &&
      typeof metadata.private_agent_root_metadata === "object" &&
      !Array.isArray(metadata.private_agent_root_metadata)
        ? (metadata.private_agent_root_metadata as Record<string, unknown>)
        : null,
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
  const metadata = publicMessageMetadata(row.messageMetadata);
  return {
    id: row.id,
    session_id: row.sessionId,
    role: row.role as ConversationMessage["role"],
    content: row.content,
    metadata,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
    token_count: row.tokenCount ?? null,
    branch_count: storedBranchCount(row),
    parent_message_id: row.parentMessageId ?? null,
    branch_index: row.branchIndex ?? 0,
    is_active_branch: row.isActiveBranch ?? true,
    sender_type:
      typeof metadata.sender_type === "string" ? metadata.sender_type : null,
    sender_id: typeof metadata.sender_id === "string" ? metadata.sender_id : null,
    sender_display_name:
      typeof metadata.sender_display_name === "string"
        ? metadata.sender_display_name
        : null,
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
  userId?: string | null,
  agent?: Pick<ConversationAgent, "id" | "display_name" | "state" | "latest_revision_id" | "character_id"> | null,
): ConversationSession {
  const characterSlug = requireCharacterSlug(characterName);
  const now = new Date().toISOString();
  return {
    id: randomId(),
    is_local_only: true,
    user_id: String(userId ?? "").trim(),
    character_name: characterSlug,
    title: "ローカルチャット",
    project_id: projectId ?? null,
    session_start: now,
    last_activity: now,
    message_count: 0,
    is_active: true,
    is_pinned: false,
    pinned_at: null,
    pin_updated_at: null,
    is_group_chat: false,
    agent_id: agent?.id ?? null,
    agent_display_name: agent?.display_name ?? null,
    agent_state: agent?.state ?? null,
    latest_revision_id: agent?.latest_revision_id ?? null,
    agent_character_id: agent?.character_id ?? null,
  };
}

async function canUseServer(): Promise<boolean> {
  return canAttemptAoiTalkServer() && Boolean(await getToken());
}

async function canAttemptServer(): Promise<boolean> {
  return canAttemptAoiTalkServer() && Boolean(await getToken());
}

async function canRefreshServer(): Promise<boolean> {
  // serverReachable は直近の失敗で stale になりやすいため、読み取り同期は
  // online + token があれば直接試し、成功したら到達可能状態へ戻す。
  return canAttemptServer();
}

function normalizeAgentCatalog(items: ConversationAgent[]): ConversationAgent[] {
  return items.filter((item) => Boolean(item && item.id && item.display_name));
}

export type ConversationAgentCatalogScope = {
  sessionId?: string | null;
  projectId?: string | null;
};

function normalizedCatalogScope(scope: ConversationAgentCatalogScope = {}): {
  sessionId: string | null;
  projectId: string | null;
  scopeKey: string;
} {
  const sessionId = typeof scope.sessionId === "string" && scope.sessionId.trim()
    ? scope.sessionId.trim()
    : null;
  const projectId = typeof scope.projectId === "string" && scope.projectId.trim()
    ? scope.projectId.trim()
    : null;
  // Keep the exact session/project ACL query identity. A project catalog must
  // never be reused for another project, and a session-scoped response must
  // never fall back to the project/global catalog.
  const scopeKey = JSON.stringify({ session_id: sessionId, project_id: projectId });
  return { sessionId, projectId, scopeKey };
}

/** Persist the ACL-filtered Agent catalog under the current account/server key. */
export async function applyRemoteConversationAgents(
  agents: ConversationAgent[],
  authScope: string,
  serverFingerprint: string,
  scope: ConversationAgentCatalogScope = {},
  options: { complete?: boolean } = {},
): Promise<void> {
  const normalized = normalizeAgentCatalog(agents);
  if (!authScope || !serverFingerprint) return;
  const catalogScope = normalizedCatalogScope(scope);
  const db = getDb();
  const cachedAt = new Date().toISOString();
  await runForegroundSqliteWrite(async () => {
    if (options.complete === true) {
      await db.delete(schema.conversationAgents).where(and(
        eq(schema.conversationAgents.authScope, authScope),
        eq(schema.conversationAgents.serverFingerprint, serverFingerprint),
        eq(schema.conversationAgents.scopeKey, catalogScope.scopeKey),
      ));
    }
    if (!normalized.length) return;
    for (const agent of normalized) {
      await db
        .insert(schema.conversationAgents)
        .values({
          authScope,
          serverFingerprint,
          scopeKey: catalogScope.scopeKey,
          sessionId: catalogScope.sessionId,
          projectId: catalogScope.projectId,
          id: agent.id,
          displayName: agent.display_name,
          state: agent.state ?? null,
          latestRevisionId: agent.latest_revision_id ?? null,
          characterId: agent.character_id ?? null,
          characterName: agent.character_name ?? null,
          utilization: agent.utilization ?? null,
          updatedAt: agent.updated_at ?? cachedAt,
          cachedAt,
        })
        .onConflictDoUpdate({
          target: [
            schema.conversationAgents.authScope,
            schema.conversationAgents.serverFingerprint,
            schema.conversationAgents.scopeKey,
            schema.conversationAgents.id,
          ],
          set: {
            displayName: agent.display_name,
            state: agent.state ?? null,
            latestRevisionId: agent.latest_revision_id ?? null,
            characterId: agent.character_id ?? null,
            characterName: agent.character_name ?? null,
            utilization: agent.utilization ?? null,
            updatedAt: agent.updated_at ?? cachedAt,
            cachedAt,
          },
        });
    }
  });
}

async function conversationAgentScope(): Promise<{
  authScope: string;
  serverFingerprint: string;
} | null> {
  const token = await getToken();
  if (!token) return null;
  const serverFingerprint = await getConfiguredApiServerFingerprint();
  if (!serverFingerprint) return null;
  return { authScope: getTokenAuthScope(token), serverFingerprint };
}

export async function listConversationAgentsLocal(
  catalogRequestScope: ConversationAgentCatalogScope = {},
): Promise<ConversationAgent[]> {
  const authScope = await conversationAgentScope();
  if (!authScope) return [];
  const catalogScope = normalizedCatalogScope(catalogRequestScope);
  const rows = await getDb()
    .select()
    .from(schema.conversationAgents)
    .where(and(
      eq(schema.conversationAgents.authScope, authScope.authScope),
      eq(schema.conversationAgents.serverFingerprint, authScope.serverFingerprint),
      eq(schema.conversationAgents.scopeKey, catalogScope.scopeKey),
    ))
    .orderBy(asc(schema.conversationAgents.displayName));
  return rows.map((row) => ({
    id: row.id,
    display_name: row.displayName,
    state: row.state ?? null,
    latest_revision_id: row.latestRevisionId ?? null,
    character_id: row.characterId ?? null,
    character_name: row.characterName ?? null,
    updated_at: row.updatedAt ?? null,
    utilization:
      row.utilization && typeof row.utilization === "object" && !Array.isArray(row.utilization)
        ? row.utilization as ConversationAgent["utilization"]
        : null,
  }));
}

export async function refreshConversationAgents(
  catalogRequestScope: ConversationAgentCatalogScope = {},
): Promise<ConversationAgent[]> {
  const scope = await conversationAgentScope();
  if (!scope || !(await canAttemptServer())) return listConversationAgentsLocal(catalogRequestScope);
  const listWithMeta = conversationAgentApi.listWithMeta;
  const response = typeof listWithMeta === "function"
    ? await listWithMeta(catalogRequestScope)
    : { agents: await conversationAgentApi.list(catalogRequestScope), complete: true };
  const agents = response.agents;
  // Never write a response observed under a different auth/server scope.
  const current = await conversationAgentScope();
  if (!current || current.authScope !== scope.authScope || current.serverFingerprint !== scope.serverFingerprint) {
    return listConversationAgentsLocal(catalogRequestScope);
  }
  await applyRemoteConversationAgents(
    agents,
    scope.authScope,
    scope.serverFingerprint,
    catalogRequestScope,
    { complete: response.complete },
  );
  return agents;
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

// Explicit user deletion is permanent; ACL/scope reconciliation tombstones are not.
function isUserDeletedSession(row: DbSession | undefined): boolean {
  return (row?.sessionMetadata as Record<string, unknown> | null)?.user_deleted === true;
}

function timestampMs(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function newestTimestamp(...values: unknown[]): string | null {
  let newest: { value: string; time: number } | null = null;
  for (const value of values) {
    const time = timestampMs(value);
    if (time === null || typeof value !== "string") continue;
    if (!newest || time > newest.time) newest = { value, time };
  }
  return newest?.value ?? null;
}

/** Filter after a remote response, without excluding sessions not cached on this device. */
export async function excludeUserDeletedSessions<T>(
  items: readonly T[],
  sessionIdOf: (item: T) => string,
): Promise<T[]> {
  if (!items.length) return [];
  const rows = await getDb().select().from(schema.conversationSessions)
    .where(inArray(schema.conversationSessions.id, [...new Set(items.map(sessionIdOf))]));
  const deleted = new Set(rows.filter(isUserDeletedSession).map((row) => row.id));
  return items.filter((item) => !deleted.has(sessionIdOf(item)));
}

const sessionDeleteFlights = new Map<string, Promise<void>>();

async function deleteConversationSessionOnce(sessionId: string): Promise<void> {
  if (uploadInFlight.has(sessionId) || pendingFlushFlights.has(sessionId)) {
    throw new Error("この会話は同期処理中です。完了後に削除を再試行してください。");
  }
  const token = await getToken();
  const authScope = getTokenAuthScope(token);
  const row = await runForegroundSqliteWrite(() => getDb().select()
    .from(schema.conversationSessions).where(eq(schema.conversationSessions.id, sessionId)).get());
  if (isUserDeletedSession(row)) return;
  // Server-backed conversations are local-first too: hide them immediately and
  // replay the durable delete through the normal sync outbox. The old path
  // required a live DELETE response before touching SQLite, so one transient
  // reachability failure made every item in a bulk delete fail at once.
  const requiresServerDelete = !row || isServerBackedSession(row);
  if (requiresServerDelete && !token) {
    throw new Error("サーバー上の会話を削除するにはログインが必要です。");
  }
  let queuedDelete = false;
  await runForegroundSqliteWrite(() => {
    const db = getDb();
    const now = new Date().toISOString();
    db.transaction((tx) => {
      const current = tx.select().from(schema.conversationSessions)
        .where(eq(schema.conversationSessions.id, sessionId)).get();
      if (isUserDeletedSession(current)) return;

      if (requiresServerDelete) {
        // Delete supersedes pending pin/update mutations for the same session.
        // Keep the outbox replacement in the same SQLite transaction as the
        // tombstone so a crash cannot leave only one side durable.
        tx.delete(schema.outbox).where(and(
          eq(schema.outbox.authScope, authScope),
          eq(schema.outbox.tableName, "conversation_sessions"),
          eq(schema.outbox.entityId, sessionId),
        )).run();
        tx.insert(schema.outbox).values({
          opId: randomId(),
          createdAt: Date.now(),
          tableName: "conversation_sessions",
          action: "delete",
          entityId: sessionId,
          payload: "{}",
          authScope,
          baseUpdatedAt: null,
          retryCount: 0,
          lastError: null,
        }).run();
        queuedDelete = true;
      }
      const patch = {
        deletedAt: now,
        updatedAt: now,
        sessionMetadata: {
          ...((current?.sessionMetadata as Record<string, unknown> | null) ?? {}),
          user_deleted: true,
        },
      };
      if (current) {
        tx.update(schema.conversationSessions).set(patch)
          .where(eq(schema.conversationSessions.id, sessionId)).run();
      } else {
        // Persist a tombstone even for an uncached search result.
        tx.insert(schema.conversationSessions).values({ id: sessionId, ...patch }).run();
      }
      tx.update(schema.conversationMessages).set({ deletedAt: now, updatedAt: now })
        .where(eq(schema.conversationMessages.sessionId, sessionId)).run();
      tx.delete(schema.syncState).where(eq(schema.syncState.tableName,
        conversationMessageSyncStateKey(authScope, sessionId))).run();
    });
    notifyConversationChanges();
  });
  if (queuedDelete) notifyMutationQueued(authScope);
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
    if (isUserDeletedSession(existing)) continue;
    const existingMetadata =
      (existing?.sessionMetadata as Record<string, unknown> | null) ?? {};
    const pendingSlug = pendingCharacterSlug(existing);
    const preservePendingCharacter =
      Boolean(pendingSlug) && session.character_name !== pendingSlug;
    const characterName = preservePendingCharacter
      ? pendingSlug!
      : session.character_name;
    const pendingAgent = pendingAgentBinding(existing);
    // Older deployments may omit the additive Agent field entirely. Preserve
    // an already bound session in that case so a hidden/paused Agent is never
    // silently detached while the catalog is unavailable.
    const hasRemoteAgentField = Object.prototype.hasOwnProperty.call(
      session,
      "agent_id",
    );
    const remoteAgentId = hasRemoteAgentField
      ? session.agent_id ?? null
      : existing?.agentId ?? null;
    const preservePendingAgent =
      pendingAgent !== null &&
      (!hasRemoteAgentField || remoteAgentId !== pendingAgent.targetAgentId);
    const agentId = session.is_group_chat
      ? null
      : preservePendingAgent
        ? pendingAgent.targetAgentId
        : remoteAgentId;
    const preservePendingPin = await hasPendingOutbox(
      "conversation_sessions",
      session.id,
    );
    const isPinned = preservePendingPin
      ? Boolean(existing?.isPinned)
      : session.is_pinned === true;
    const pinnedAt = preservePendingPin
      ? existing?.pinnedAt ?? null
      : isPinned
        ? session.pinned_at ?? null
        : null;
    const pinUpdatedAt = preservePendingPin
      ? existing?.pinUpdatedAt ?? null
      : session.pin_updated_at ?? null;
    // A list pull can have started before the user opened a row. Compare the
    // server-owned assistant completion fence rather than last_activity:
    // user messages also advance last_activity and must not reopen a row.
    const localLastReadAt =
      typeof existingMetadata.last_read_at === "string"
        ? existingMetadata.last_read_at
        : null;
    const remoteLastReadAt = session.last_read_at ?? null;
    const provisionalReadFence = provisionalReadFences.get(session.id);
    const localReadCompletionAt = newestTimestamp(
      typeof existingMetadata.history_read_completion_at === "string"
        ? existingMetadata.history_read_completion_at
        : null,
      provisionalReadFence?.completionAt ?? null,
    );
    const remoteCompletionAt =
      typeof session.history_completed_at === "string"
        ? session.history_completed_at
        : null;
    const existingCompletionAt =
      typeof existingMetadata.history_completed_at === "string"
        ? existingMetadata.history_completed_at
        : null;
    const remoteHistoryIsOlder = Boolean(
      existingCompletionAt &&
        (!remoteCompletionAt ||
          (timestampMs(remoteCompletionAt) ?? -1) <
            (timestampMs(existingCompletionAt) ?? -1)),
    );
    const effectiveCompletionAt = newestTimestamp(
      existingCompletionAt,
      remoteCompletionAt,
    );
    const localReadSupersedesRemote = Boolean(
      localReadCompletionAt &&
        remoteCompletionAt &&
        (timestampMs(localReadCompletionAt) ?? -1) >=
          (timestampMs(remoteCompletionAt) ?? Number.MAX_SAFE_INTEGER),
    );
    const lastReadAt = newestTimestamp(localLastReadAt, remoteLastReadAt);
    const isUnread = remoteHistoryIsOlder
      ? existingMetadata.is_unread === true
      : localReadSupersedesRemote
        ? false
        : session.is_unread === true;
    const hasRemoteHistoryStatus = Object.prototype.hasOwnProperty.call(
      session,
      "history_status",
    );
    const remoteHistoryStatus = remoteHistoryIsOlder
      ? normalizeChatHistoryStatus(existingMetadata.history_status)
      : hasRemoteHistoryStatus
        ? normalizeChatHistoryStatus(session.history_status)
        : normalizeChatHistoryStatus(existingMetadata.history_status);
    const historyStatus =
      remoteHistoryIsOlder
        ? remoteHistoryStatus
        : localReadSupersedesRemote && remoteHistoryStatus === "ready"
          ? "idle"
          : remoteHistoryStatus;
    const remoteMetadata = {
      ...(session.is_local_only ? { local_only: true } : {}),
      message_count: session.message_count,
      is_active: session.is_active,
      app_id: session.app_id ?? null,
      app_target_id: session.app_target_id ?? null,
      development_status: session.development_status ?? null,
      history_status: historyStatus,
      history_completed_at: effectiveCompletionAt,
      history_read_completion_at:
        typeof existingMetadata.history_read_completion_at === "string"
          ? existingMetadata.history_read_completion_at
          : null,
      last_read_at: lastReadAt,
      is_unread: isUnread,
      context: session.context ?? null,
      private_agent_root: session.private_agent_root === true,
      purpose: session.purpose ?? null,
      readonly_binding: session.readonly_binding === true,
      private_agent_root_metadata: session.private_agent_root_metadata ?? null,
      parent_session_id: session.parent_session_id ?? null,
      forked_from_message_id: session.forked_from_message_id ?? null,
      group_character_names: session.group_character_names ?? [],
      participants: session.participants ?? [],
      rp_settings: session.rp_settings ?? {},
      agent_display_name: session.is_group_chat
        ? null
        : preservePendingAgent
          ? pendingAgent.displayName
          : Object.prototype.hasOwnProperty.call(session, "agent_display_name")
            ? session.agent_display_name ?? null
            : typeof existingMetadata.agent_display_name === "string"
              ? existingMetadata.agent_display_name
              : null,
      agent_state: session.is_group_chat
        ? null
        : Object.prototype.hasOwnProperty.call(session, "agent_state")
          ? session.agent_state ?? null
          : typeof existingMetadata.agent_state === "string"
            ? existingMetadata.agent_state
            : null,
      latest_revision_id: session.is_group_chat
        ? null
        : Object.prototype.hasOwnProperty.call(session, "latest_revision_id")
          ? session.latest_revision_id ?? null
          : typeof existingMetadata.latest_revision_id === "string"
            ? existingMetadata.latest_revision_id
            : null,
      agent_character_id: session.is_group_chat
        ? null
        : Object.prototype.hasOwnProperty.call(session, "agent_character_id")
          ? session.agent_character_id ?? null
          : typeof existingMetadata.agent_character_id === "string"
            ? existingMetadata.agent_character_id
            : null,
      ...(!session.is_group_chat && (preservePendingAgent || session.agent_binding_pending)
        ? { agent_binding_pending: true }
        : {}),
      ...(preservePendingAgent && !session.is_group_chat
        ? {
            [PENDING_AGENT_BINDING_KEY]: {
              target_agent_id: pendingAgent.targetAgentId,
              expected_agent_id: pendingAgent.expectedAgentId,
              display_name: pendingAgent.displayName,
              auth_scope: pendingAgent.authScope,
              server_fingerprint: pendingAgent.serverFingerprint,
              ...(pendingAgent.error ? { error: pendingAgent.error } : {}),
            },
          }
        : {}),
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
        agentId,
        isPinned,
        pinnedAt,
        pinUpdatedAt,
        sessionMetadata: remoteMetadata,
        createdAt: session.session_start ?? now,
        updatedAt: session.last_activity ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.conversationSessions.id,
        // The earlier read can race with deletion: enforce the invariant in SQLite too.
        setWhere: sql.raw("coalesce(json_extract(conversation_sessions.session_metadata, '$.user_deleted'), 0) != 1"),
        set: {
          userId: session.user_id,
          characterName,
          projectId: session.project_id ?? null,
          title: session.title ?? "",
          isGroupChat: session.is_group_chat ?? false,
          agentId,
          isPinned,
          pinnedAt,
          pinUpdatedAt,
          updatedAt: session.last_activity ?? now,
          deletedAt: null,
          sessionMetadata: remoteMetadata,
        },
      });
  }
  notifyConversationChanges();
}

/**
 * Persist an interactive session creation without changing the priority of
 * sync/reconciliation callers of applyRemoteConversationSessions().
 */
export async function applyForegroundConversationSessions(
  list: ConversationSession[],
): Promise<void> {
  await runForegroundSqliteWrite(() => applyRemoteConversationSessions(list));
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
  const metadata = (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
  return Boolean(session?.userId) && metadata.local_only !== true;
}

const PENDING_CHARACTER_SLUG_KEY = "pending_character_slug";
const PENDING_AGENT_BINDING_KEY = "pending_agent_binding";

type PendingAgentBinding = {
  targetAgentId: string | null;
  expectedAgentId: string | null;
  displayName: string | null;
  authScope: string;
  serverFingerprint: string;
  error?: string | null;
};

function pendingAgentBinding(
  session: DbSession | undefined,
): PendingAgentBinding | null {
  const metadata =
    (session?.sessionMetadata as Record<string, unknown> | null) ?? {};
  const raw = metadata[PENDING_AGENT_BINDING_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  const targetAgentId =
    typeof value.target_agent_id === "string" && value.target_agent_id.trim()
      ? value.target_agent_id.trim()
      : null;
  const expectedAgentId =
    typeof value.expected_agent_id === "string" && value.expected_agent_id.trim()
      ? value.expected_agent_id.trim()
      : null;
  const authScope = typeof value.auth_scope === "string" ? value.auth_scope : "";
  const serverFingerprint =
    typeof value.server_fingerprint === "string" ? value.server_fingerprint : "";
  if (!authScope || !serverFingerprint) return null;
  return {
    targetAgentId,
    expectedAgentId,
    displayName:
      typeof value.display_name === "string" ? value.display_name : null,
    authScope,
    serverFingerprint,
    ...(typeof value.error === "string" ? { error: value.error } : {}),
  };
}

function isPendingAgentBindingMetadata(
  metadata: Record<string, unknown>,
): boolean {
  const value = metadata[PENDING_AGENT_BINDING_KEY];
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function withPendingAgentBinding(
  metadata: Record<string, unknown>,
  binding: PendingAgentBinding | null,
): Record<string, unknown> {
  const next = { ...metadata };
  if (binding) {
    next[PENDING_AGENT_BINDING_KEY] = {
      target_agent_id: binding.targetAgentId,
      expected_agent_id: binding.expectedAgentId,
      display_name: binding.displayName,
      auth_scope: binding.authScope,
      server_fingerprint: binding.serverFingerprint,
      ...(binding.error ? { error: binding.error } : {}),
    };
  }
  else delete next[PENDING_AGENT_BINDING_KEY];
  return next;
}

function normalizedAgentId(value: string | null | undefined): string | null {
  const normalized = typeof value === "string" ? value.trim() : "";
  return normalized || null;
}

function agentBindingConflictFrom(error: unknown): AgentBindingConflictError | null {
  if (!isApiHttpError(error) || error.status !== 409) return null;
  return new AgentBindingConflictError(null, null);
}

async function queuePendingAgentBinding(
  row: DbSession,
  targetAgentId: string | null,
  expectedAgentId: string | null,
  agent: ConversationAgent | null,
  scope: { authScope: string; serverFingerprint: string },
  error?: string | null,
): Promise<ConversationSession> {
  const now = new Date().toISOString();
  const existingMetadata =
    (row.sessionMetadata as Record<string, unknown> | null) ?? {};
  const metadata = withPendingAgentBinding(existingMetadata, {
    targetAgentId,
    expectedAgentId,
    displayName: agent?.display_name ?? null,
    authScope: scope.authScope,
    serverFingerprint: scope.serverFingerprint,
    ...(error ? { error } : {}),
  });
  await runForegroundSqliteWrite(() => getDb()
    .update(schema.conversationSessions)
    .set({
      agentId: targetAgentId,
      sessionMetadata: metadata,
      updatedAt: now,
    })
    .where(eq(schema.conversationSessions.id, row.id)));
  notifyConversationChanges();
  return toSession({
    ...row,
    agentId: targetAgentId,
    sessionMetadata: metadata,
    updatedAt: now,
  });
}

async function preservePendingAgentBindingError(
  sessionId: string,
  pending: PendingAgentBinding,
  errorMessage: string,
): Promise<void> {
  const row = getDb().select().from(schema.conversationSessions)
    .where(eq(schema.conversationSessions.id, sessionId)).get();
  if (!row) return;
  const metadata = withPendingAgentBinding(
    (row.sessionMetadata as Record<string, unknown> | null) ?? {},
    { ...pending, error: errorMessage },
  );
  await runForegroundSqliteWrite(() => getDb()
    .update(schema.conversationSessions)
    .set({ agentId: pending.targetAgentId, sessionMetadata: metadata })
    .where(eq(schema.conversationSessions.id, sessionId)));
  notifyConversationChanges();
}

/** Refresh the canonical binding for display while retaining a pending local
 * desired target. A 409 must never silently retarget queued payloads. */
async function refreshCanonicalAgentSessionAfterConflict(
  row: DbSession,
  pending: PendingAgentBinding | null,
  reason: string,
): Promise<ConversationSession | null> {
  try {
    const remote = await chatApi.resumeSession(row.id, { includeMessages: false });
    if (!Object.prototype.hasOwnProperty.call(remote.session, "agent_id")) {
      if (pending) await preservePendingAgentBindingError(row.id, pending, reason);
      return null;
    }
    await applyForegroundConversationSessions([remote.session]);
    if (pending) await preservePendingAgentBindingError(row.id, pending, reason);
    return remote.session;
  } catch {
    if (pending) await preservePendingAgentBindingError(row.id, pending, reason);
    return null;
  }
}

async function adoptCanonicalAgentBinding(
  sessionId: string,
): Promise<ConversationSession> {
  const remote = await chatApi.resumeSession(sessionId, { includeMessages: false });
  if (!Object.prototype.hasOwnProperty.call(remote.session, "agent_id")) {
    throw new Error("ServerのAgent bindingを取得できませんでした。接続先を確認して再試行してください。");
  }
  await applyForegroundConversationSessions([remote.session]);
  const row = getDb().select().from(schema.conversationSessions)
    .where(eq(schema.conversationSessions.id, sessionId)).get();
  if (!row) return remote.session;
  const metadata = withPendingAgentBinding(
    (row.sessionMetadata as Record<string, unknown> | null) ?? {},
    null,
  );
  delete metadata.agent_binding_pending;
  delete metadata.agent_binding_error;
  const updatedAt = new Date().toISOString();
  await runForegroundSqliteWrite(() => getDb()
    .update(schema.conversationSessions)
    .set({
      agentId: remote.session.agent_id ?? null,
      sessionMetadata: metadata,
      updatedAt,
    })
    .where(eq(schema.conversationSessions.id, sessionId)));
  notifyConversationChanges();
  return toSession({ ...row, agentId: remote.session.agent_id ?? null, sessionMetadata: metadata, updatedAt });
}

async function flushPendingAgentBinding(row: DbSession): Promise<ConversationSession | null> {
  const pending = pendingAgentBinding(row);
  if (!pending || !isServerBackedSession(row)) return null;
  const token = await getToken();
  const currentScope = {
    authScope: getTokenAuthScope(token),
    serverFingerprint: await getConfiguredApiServerFingerprint(),
  };
  if (
    currentScope.authScope !== pending.authScope ||
    currentScope.serverFingerprint !== pending.serverFingerprint
  ) {
    throw new Error("Agent変更を受け付けたアカウントまたは接続先と一致しません。元の接続先で再試行してください。");
  }
  if (!(await canAttemptServer())) return null;
  try {
    const updated = await conversationAgentApi.bindSession(
      row.id,
      pending.targetAgentId,
      pending.expectedAgentId,
    );
    if (normalizedAgentId(updated.agent_id) !== pending.targetAgentId) {
      throw new AgentBindingConflictError(
        pending.expectedAgentId,
        pending.targetAgentId,
        "ServerのAgent bindingを確認できませんでした。送信を保留しています。",
      );
    }
    await applyForegroundConversationSessions([updated]);
    return updated;
  } catch (error) {
    const conflict = agentBindingConflictFrom(error);
    if (conflict) {
      await refreshCanonicalAgentSessionAfterConflict(
        row,
        pending,
        "Server側のAgent bindingが変わりました。保留中の変更と未送信メッセージを確認してください。",
      );
      throw new AgentBindingConflictError(
        pending.expectedAgentId,
        pending.targetAgentId,
      );
    }
    if (isApiConnectionError(error)) {
      useNetworkStore.getState().setServerReachable(false);
    }
    throw error;
  }
}

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
  await runForegroundSqliteWrite(() => db
    .update(schema.conversationSessions)
    .set({
      characterName: characterSlug,
      sessionMetadata: withPendingCharacterSlug(session, characterSlug, pending),
      updatedAt: now,
    })
    .where(eq(schema.conversationSessions.id, session.id)));
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
      message.metadata?.delivery_route !== "direct" &&
      !message.metadata?.direct_cloud &&
      Boolean(message.metadata?.pending),
  );
}

const sessionDispatchTails = new Map<string, Promise<void>>();

const pendingMessageDispatchFlights = new Map<
  string,
  Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null>
>();
const pendingMessageEditFlights = new Map<
  string,
  Promise<ConversationMessage>
>();

async function dispatchPendingConversationMessageOnce(
  sessionId: string,
  message: ConversationMessage,
  payload: ConversationDispatchPayload,
  checkRemoteDuplicate: boolean,
  onHandoff?: () => void,
): Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null> {
  const key = `${sessionId}:${message.id}`;
  const editFlight = pendingMessageEditFlights.get(key);
  if (editFlight) await editFlight;
  const current = (
    await conversationsRepo.listMessagesLocal(sessionId)
  ).find((candidate) => candidate.id === message.id);
  if (!current || !Boolean(current.metadata?.pending) || current.metadata?.delivery_route === "direct" || current.metadata?.direct_cloud) return null;

  // A queued input carries the exact Agent binding observed at enqueue time.
  // If the local session is still unpromoted or has a pending CAS binding,
  // leave the input queued so reconnect can settle the binding first.
  const sessionQuery = getDb().select().from(schema.conversationSessions)
    .where(eq(schema.conversationSessions.id, sessionId)) as unknown as {
      get?: () => DbSession | undefined;
    };
  const sessionRow = sessionQuery.get?.();
  // A few legacy test/adaptor shims do not expose the additive column yet;
  // their session rows must retain the pre-Agent dispatch behavior. Real
  // migrated rows always expose `agentId` (nullable) on the selected record.
  const hasAgentColumn = Boolean(
    sessionRow && (
      Object.prototype.hasOwnProperty.call(sessionRow, "agentId") ||
      Object.prototype.hasOwnProperty.call(schema.conversationSessions, "agentId")
    ),
  );
  if (sessionRow && hasAgentColumn) {
    const pendingAgent = pendingAgentBinding(sessionRow);
    if (!isServerBackedSession(sessionRow) || pendingAgent) {
      throw new Error("Agentの紐付けをServerへ同期してから送信します。入力は未送信キューに保持されています。");
    }
    const expectedAgentId = normalizedAgentId(payload.expected_agent_id);
    if (normalizedAgentId(sessionRow.agentId) !== expectedAgentId) {
      throw new AgentBindingConflictError(
        expectedAgentId,
        normalizedAgentId(sessionRow.agentId),
      );
    }
  }

  const expectedScope = current.metadata?.dispatch_auth_scope;
  const expectedServer = current.metadata?.dispatch_server_fingerprint;
  if ((typeof expectedScope === "string" && getTokenAuthScope(await getToken()) !== expectedScope) ||
      (typeof expectedServer === "string" && await getConfiguredApiServerFingerprint() !== expectedServer)) {
    throw new Error("この入力を受け付けた接続先またはアカウントと一致しません。元の接続先で再試行してください。");
  }
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

  // Mobile send always interrupts. Duplicate detection above deliberately runs
  // first: a retry of an accepted message must not stop its own response.
  const stoppedMessages = await interruptConversationForSend(sessionId);
  if (stoppedMessages.length) {
    await runForegroundSqliteWrite(() => applyRemoteConversationMessages(stoppedMessages));
  }
  onHandoff?.();
  const currentPayload = pendingDispatchPayload(current, {
    projectId: payload.project_id,
    contextProjectHintId: payload.context_project_hint_id,
    appId: payload.app_id,
    appTargetId: payload.app_target_id,
    includeProjectContext: payload.include_project_context,
    agentMode: payload.agent_mode,
  });
  const result = await chatApi.dispatchMessage(sessionId, {
    ...currentPayload,
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
  options?: { checkRemoteDuplicate?: boolean; onHandoff?: () => void },
): Promise<Awaited<ReturnType<typeof chatApi.dispatchMessage>> | null> {
  const key = `${sessionId}:${message.id}`;
  const existing = pendingMessageDispatchFlights.get(key);
  if (existing) return existing;
  const previous = sessionDispatchTails.get(sessionId) ?? Promise.resolve();
  const flight = previous.then(() => dispatchPendingConversationMessageOnce(
    sessionId,
    message,
    payload,
    options?.checkRemoteDuplicate !== false,
    options?.onHandoff,
  )).finally(() => {
    if (pendingMessageDispatchFlights.get(key) === flight) {
      pendingMessageDispatchFlights.delete(key);
    }
  });
  pendingMessageDispatchFlights.set(key, flight);
  const tail = flight.then(() => undefined, () => undefined);
  sessionDispatchTails.set(sessionId, tail);
  void tail.then(() => {
    if (sessionDispatchTails.get(sessionId) === tail) sessionDispatchTails.delete(sessionId);
  });
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
    await applyForegroundConversationSessions([updated]);
  } catch (error) {
    // A 5xx is an HTTP response: the API is reachable even though this
    // operation failed.  Only a transport failure should mark the endpoint
    // temporarily unreachable.
    if (isApiConnectionError(error)) {
      useNetworkStore.getState().setServerReachable(false);
    }
    throw error;
  }
}

async function promoteLocalSession(
  session: DbSession,
): Promise<ConversationSession> {
  const db = getDb();
  const pending = pendingAgentBinding(session);
  if (pending) {
    const token = await getToken();
    const currentScope = {
      authScope: getTokenAuthScope(token),
      serverFingerprint: await getConfiguredApiServerFingerprint(),
    };
    if (
      currentScope.authScope !== pending.authScope ||
      currentScope.serverFingerprint !== pending.serverFingerprint
    ) {
      throw new Error("Agent変更を受け付けたアカウントまたは接続先と一致しません。元の接続先で再試行してください。");
    }
  }
  const remote = await chatApi.createSession(
    requireCharacterSlug(session.characterName),
    session.projectId ?? undefined,
    {
      clientSessionId: session.id,
      ...(session.agentId ? { agentId: session.agentId } : {}),
    },
  );
  if (normalizedAgentId(remote.agent_id) !== normalizedAgentId(session.agentId)) {
    throw new AgentBindingConflictError(
      normalizedAgentId(session.agentId),
      normalizedAgentId(session.agentId),
      "Serverが指定したAgent bindingを確認できませんでした。送信を保留しています。",
    );
  }

  // appendLocalMessage() has already released its foreground slot before
  // promotion starts. Promotion therefore takes a separate foreground slot
  // rather than nesting coordinator callbacks. Keep all promotion writes in
  // that one slot so a Docs background transaction cannot start between the
  // remote-session upsert and the local-session reassignment/tombstone.
  await runForegroundSqliteWrite(async () => {
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
  });

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
export async function uploadLocalSession(
  sessionId: string,
  options?: { deferPendingDispatch?: boolean },
): Promise<string> {
  if (sessionDeleteFlights.has(sessionId)) {
    throw new Error("このセッションは削除処理中です。");
  }
  if (uploadInFlight.has(sessionId)) {
    throw new Error("このセッションは既に同期処理中です。");
  }
  uploadInFlight.add(sessionId);
  try {
    return await uploadLocalSessionOnce(sessionId, options);
  } finally {
    uploadInFlight.delete(sessionId);
  }
}

async function uploadLocalSessionOnce(
  sessionId: string,
  options?: { deferPendingDispatch?: boolean },
): Promise<string> {
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
  if (!row || row.deletedAt) {
    throw new Error("同期対象のセッションが見つかりません。");
  }
  const pendingAgent = pendingAgentBinding(row);
  if (pendingAgent) {
    const currentScope = {
      authScope: getTokenAuthScope(await getToken()),
      serverFingerprint: await getConfiguredApiServerFingerprint(),
    };
    if (
      currentScope.authScope !== pendingAgent.authScope ||
      currentScope.serverFingerprint !== pendingAgent.serverFingerprint
    ) {
      throw new Error("Agent変更を受け付けたアカウントまたは接続先と一致しません。元の接続先で再試行してください。");
    }
  }
  if (isServerBackedSession(row)) {
    // 既にサーバー化済み。既存動作を変えないため、そのまま id を返す。
    if (pendingAgent) await flushPendingAgentBinding(row);
    return sessionId;
  }

  const localMessages = await conversationsRepo.listMessagesLocal(row.id);
  // A fork continued via Server must import its copied history before the
  // next input dispatch. Importing that pending input too would make duplicate
  // detection mark it accepted without ever starting an assistant response.
  const importMessages = options?.deferPendingDispatch
    ? localMessages.filter((message) => !message.metadata?.pending)
    : localMessages;
  if (localMessages.some((message) => Boolean(message.metadata?.fork_source_message_id))
    && importMessages.some((message) => message.role === "system" || Boolean(pendingDispatchPayload(message).attachments?.length))) {
    throw new Error("このフォークには現在サーバー同期できないシステムメッセージまたは添付があります。履歴を省略せず端末内に保持しています。");
  }
  const uploadRows = orderLocalMessageRowsForUpload(importMessages);
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
      {
        clientSessionId: row.id,
        ...(row.agentId ? { agentId: row.agentId } : {}),
      },
    );
    if (normalizedAgentId(remote.agent_id) !== normalizedAgentId(row.agentId)) {
      throw new AgentBindingConflictError(
        normalizedAgentId(row.agentId),
        normalizedAgentId(row.agentId),
        "Serverが指定したAgent bindingを確認できませんでした。同期を保留しています。",
      );
    }
    remoteId = remote.id;
    // 付替え・tombstone 前の中間状態マーカーを保存（再開の起点）。
    await runForegroundSqliteWrite(async () => {
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
    });
  }

  for (let index = 0; index < uploads.length; index += 1) {
    if (uploadedClientMessageIds.has(uploadRows[index].id)) continue;
    const upload = uploads[index];
    const clientMessageId = uploadRows[index].id;
    if (upload.role === "assistant") {
      await chatApi.importLocalAssistantMessage(remoteId, {
        role: "assistant",
        content: upload.content,
        client_message_id: clientMessageId,
      });
    } else {
      await chatApi.addMessage(remoteId, {
        role: "user",
        content: upload.content,
        client_message_id: clientMessageId,
      });
    }
  }

  // Keep an edited/fork title through explicit upload and later promotion.
  // A failed title write leaves the existing resumable upload marker intact.
  if (row.title?.trim()) {
    await chatApi.updateTitle(remoteId, row.title.trim());
    await conversationsRepo.updateTitle(remoteId, row.title.trim(), { syncServer: false });
  }

  await runForegroundSqliteWrite(async () => {
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

  });
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

  // Agent binding is a dedicated CAS operation.  It must settle before any
  // queued dispatch is sent, and never goes through general sync UUID writes.
  await flushPendingAgentBinding(row);
  await flushPendingCharacterUpdate(row);

  const pendingBeforePromotion = await listPendingMessagesForSessionId(row.id);
  if (!pendingBeforePromotion.length) return sessionId;

  let remoteSessionId = row.id;
  let projectId = row.projectId ?? undefined;
  if (!isServerBackedSession(row)) {
    const localHistory = await conversationsRepo.listMessagesLocal(row.id);
    const hasForkHistory = localHistory.some((message) => Boolean(message.metadata?.fork_source_message_id));
    if (hasForkHistory) {
      // A local fork is already a transcript. The ordinary empty-session
      // promotion path would lose that transcript on the server.
      remoteSessionId = await uploadLocalSession(row.id, { deferPendingDispatch: true });
    } else {
      const promoted = await promoteLocalSession(row);
      remoteSessionId = promoted.id;
      projectId = promoted.project_id ?? row.projectId ?? undefined;
    }
  }

  const pending = await listPendingMessagesForSessionId(remoteSessionId);
  for (const message of pending) {
    const dispatchPayload = pendingDispatchPayload(message, {
      projectId,
      contextProjectHintId: message.metadata?.context_project_hint_id as string | null | undefined,
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
  if (isMobileReadOnlyDiagnosticsEnabled()) return Promise.resolve(sessionId);
  if (sessionDeleteFlights.has(sessionId)) return Promise.resolve(sessionId);
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
  if (metadata?.user_deleted === true) return null;
  const promotedTo = metadata?.promoted_to_session_id;
  return typeof promotedTo === "string" ? promotedTo : null;
}

export async function flushPendingConversations(): Promise<void> {
  if (isMobileReadOnlyDiagnosticsEnabled()) return;
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
    if (!isServerBackedSession(row)) continue;
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
  const reconciledLocalIds = options.reconcileSessionId
    ? await loadReconciledLocalMessageIds(options.reconcileSessionId, list)
    : [];
  // Synchronous reads after the final await: no deletion can interleave before the transaction.
  const parentIds = [...new Set(list.map((message) => message.session_id))];
  const deletedParents = new Set<string>();
  let parentReadStatements = 0;
  for (let offset = 0; offset < parentIds.length; offset += 500) {
    const parents = db.select().from(schema.conversationSessions)
      .where(inArray(schema.conversationSessions.id, parentIds.slice(offset, offset + 500))).all();
    parentReadStatements += 1;
    for (const parent of parents) {
      if (isUserDeletedSession(parent)) deletedParents.add(parent.id);
    }
  }
  const changed = list.filter((message) => {
    if (deletedParents.has(message.session_id)) return false;
    const existing = existingById.get(message.id);
    return !existing || !remoteRevisionMatches(existing, message);
  });
  const insertedCount = changed.filter(
    (message) => !existingById.has(message.id),
  ).length;
  const updatedCount = changed.length - insertedCount;
  const upsertStatementCount = conversationMessageUpsertStatementCount(
    changed.length,
  );
  const hasTransactionWrites =
    changed.length > 0 || reconciledLocalIds.length > 0;
  const bridgeStatementCount =
    1 + parentReadStatements +
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
  async listAgents(options?: {
    forceRefresh?: boolean;
    sessionId?: string | null;
    projectId?: string | null;
  }): Promise<ConversationAgent[]> {
    const catalogScope = {
      sessionId: options?.sessionId ?? null,
      projectId: options?.projectId ?? null,
    };
    const local = await listConversationAgentsLocal(catalogScope);
    if (!options?.forceRefresh && local.length > 0) return local;
    if (!(await canAttemptServer())) return local;
    try {
      const refreshed = await refreshConversationAgents(catalogScope);
      useNetworkStore.getState().setServerReachable(true);
      // A successful empty projection is authoritative for this exact ACL
      // scope. The repository clears it in applyRemoteConversationAgents;
      // never resurrect a paused/revoked candidate from an older cache.
      return refreshed;
    } catch (error) {
      if (isApiConnectionError(error) || isApiTimeoutError(error)) {
        useNetworkStore.getState().setServerReachable(false);
        return local;
      }
      // HTTP/ACL responses are authoritative failures for this scope. Keep
      // bound session display metadata, but do not offer stale candidates.
      return [];
    }
  },

  async listAgentsLocal(options?: ConversationAgentCatalogScope): Promise<ConversationAgent[]> {
    return listConversationAgentsLocal(options);
  },

  async listAgentConversations(agentId: string): Promise<ConversationSession[]> {
    return conversationAgentApi.listConversations(agentId);
  },

  async listAgentRuns(agentId: string): Promise<AgentRun[]> {
    return conversationAgentApi.listRuns(agentId);
  },

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
      .orderBy(
        desc(schema.conversationSessions.isPinned),
        desc(schema.conversationSessions.updatedAt),
        desc(schema.conversationSessions.createdAt),
      );
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
      } catch (error) {
        if (isApiConnectionError(error)) {
          useNetworkStore.getState().setServerReachable(false);
        }
        // Deletion may have completed while the request was failing.
        return this.listSessionsLocal(projectId);
      }
    }
    return local;
  },

  async refreshSessions(
    projectId?: string | null,
  ): Promise<ConversationSession[]> {
    const sessions = await chatApi.listSessions(projectId ?? undefined);
    await applyForegroundConversationSessions(sessions);
    await excludeUserDeletedSessions(sessions, (session) => session.id);
    return this.listSessionsLocal(projectId);
  },

  async setSessionPinned(
    sessionId: string,
    isPinned: boolean,
  ): Promise<ConversationSession> {
    const token = await getToken();
    if (!token) {
      throw new Error("ピン留めを同期するにはログインが必要です");
    }
    const authScope = getTokenAuthScope(token);
    let queuedPinScope: string | null = null;
    const result = await runForegroundSqliteWrite(async () => {
      const db = getDb();
      const before = (
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
      if (!before) throw new Error("会話が見つかりません");
      if (!isServerBackedSession(before)) {
        throw new Error("ローカル専用チャットはサーバー同期後にピン留めできます");
      }
      if (Boolean(before.isPinned) === isPinned) return toSession(before);

      const basePinUpdatedAt = before.pinUpdatedAt ?? null;
      const changedAt = new Date().toISOString();
      await db
        .update(schema.conversationSessions)
        .set({
          isPinned,
          pinnedAt: isPinned ? changedAt : null,
          pinUpdatedAt: changedAt,
        })
        .where(eq(schema.conversationSessions.id, sessionId));
      const opId = await enqueueOutbox({
        table: "conversation_sessions",
        action: "update",
        entityId: sessionId,
        authScope,
        payload: { is_pinned: isPinned },
        baseUpdatedAt: basePinUpdatedAt,
      });
      queuedPinScope = await getOutboxMutationWakeScope(opId);
      const after = (
        await db
          .select()
          .from(schema.conversationSessions)
          .where(eq(schema.conversationSessions.id, sessionId))
      )[0];
      if (!after) throw new Error("ピン状態を保存できませんでした");
      notifyConversationChanges();
      return toSession(after);
    });
    if (queuedPinScope) notifyMutationQueued(queuedPinScope);
    return result;
  },

  async markSessionRead(sessionId: string): Promise<void> {
    const readAt = new Date().toISOString();
    const provisionalToken = ++nextProvisionalReadToken;
    provisionalReadFences.set(sessionId, {
      token: provisionalToken,
      completionAt: null,
    });
    let readCompletionFenceCaptured = false;
    let readCompletionFenceAt: string | null = null;
    const applyReadAt = (
      timestamp: string,
      options: { commitCompletionFence?: boolean } = {},
    ) => runForegroundSqliteWrite(async () => {
      const db = getDb();
      const rows = await db.select().from(schema.conversationSessions)
        .where(eq(schema.conversationSessions.id, sessionId));
      if (!rows[0]) return;
      const metadata = (rows[0].sessionMetadata as Record<string, unknown> | null) ?? {};
      const monotonicReadAt = newestTimestamp(metadata.last_read_at, timestamp) ?? timestamp;
      const currentCompletionAt =
        typeof metadata.history_completed_at === "string"
          ? metadata.history_completed_at
          : null;
      const observedCompletionAt = currentCompletionAt
        ?? (typeof metadata.history_read_completion_at === "string"
          ? metadata.history_read_completion_at
          : null);
      if (!readCompletionFenceCaptured) {
        readCompletionFenceCaptured = true;
        readCompletionFenceAt = observedCompletionAt;
        const provisional = provisionalReadFences.get(sessionId);
        if (provisional?.token === provisionalToken) {
          provisional.completionAt = observedCompletionAt;
        }
      }
      // The HTTP read response may be slower than a pull that observes a new
      // assistant completion. In that case the response only acknowledges the
      // earlier read and must not clear the newer ready projection.
      const completionAdvancedSinceRead = Boolean(
        readCompletionFenceCaptured &&
          currentCompletionAt &&
          (readCompletionFenceAt === null ||
            (timestampMs(currentCompletionAt) ?? -1) >
              (timestampMs(readCompletionFenceAt) ?? -1)),
      );
      const confirmedCompletionAt =
        typeof metadata.history_read_completion_at === "string"
          ? metadata.history_read_completion_at
          : null;
      const canConfirmCompletionFence =
        options.commitCompletionFence === true &&
        provisionalReadFences.get(sessionId)?.token === provisionalToken;
      const nextMetadata = {
        ...metadata,
        last_read_at: monotonicReadAt,
        is_unread: completionAdvancedSinceRead
          ? metadata.is_unread === true
          : false,
        // Only a successful server read confirms this local fence. The
        // in-memory provisional fence covers the HTTP flight without making
        // offline/catch paths permanently hide a later server-ready result.
        history_read_completion_at: canConfirmCompletionFence
          ? newestTimestamp(confirmedCompletionAt, readCompletionFenceAt)
          : confirmedCompletionAt,
        // `ready` is defined as an unread completed assistant response. The
        // durable read mutation therefore clears that one derived state
        // optimistically; attention/working remain server-owned states.
        ...(!completionAdvancedSinceRead && metadata.history_status === "ready"
          ? { history_status: "idle" }
          : {}),
      };
      await db.update(schema.conversationSessions).set({
        sessionMetadata: nextMetadata,
      }).where(eq(schema.conversationSessions.id, sessionId));
      notifyConversationChanges();
    });
    try {
      await applyReadAt(readAt);
    } catch (error) {
      if (provisionalReadFences.get(sessionId)?.token === provisionalToken) {
        provisionalReadFences.delete(sessionId);
      }
      throw error;
    }

    if (!(await canAttemptServer())) {
      if (provisionalReadFences.get(sessionId)?.token === provisionalToken) {
        provisionalReadFences.delete(sessionId);
      }
      return;
    }
    try {
      // HTTP must not occupy the SQLite slot. Re-read current metadata after
      // the response, once the next foreground write can safely begin.
      const response = await chatApi.markSessionRead(sessionId);
      await applyReadAt(response.last_read_at ?? readAt, {
        commitCompletionFence: true,
      });
      if (provisionalReadFences.get(sessionId)?.token === provisionalToken) {
        provisionalReadFences.delete(sessionId);
      }
    } catch {
      if (provisionalReadFences.get(sessionId)?.token === provisionalToken) {
        provisionalReadFences.delete(sessionId);
      }
      // Keep the local read state; the next sync reconciles it after recovery.
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
      } catch (error) {
        if (isApiConnectionError(error)) {
          useNetworkStore.getState().setServerReachable(false);
        }
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

    // Keep message writes, reconciliation, stats and the cursor in one queued
    // unit so they cannot overlap a native Docs promotion transaction.
    return runForegroundSqliteWrite(async () => {
      const session = getDb().select().from(schema.conversationSessions)
        .where(eq(schema.conversationSessions.id, sessionId)).get();
      if (isUserDeletedSession(session)) {
        return {
          messages: [], mode, receivedCount: response.messages.length,
          upsertedCount: 0, inactiveCount: 0, cursor: response.server_time,
        };
      }
      const local = await this.listMessagesLocal(sessionId);
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
    });
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

  /** 旧ローカル会話の所有者を、現在の明示ユーザーからだけ補完する。別accountの履歴は推測しない。 */
  async backfillLocalSessionUserId(
    sessionId: string,
    preferredUserId?: string | null,
  ): Promise<ConversationSession | null> {
    const userId = String(preferredUserId ?? "").trim();
    return runForegroundSqliteWrite(async () => {
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
      if (!row) return null;
      if (String(row.userId ?? "").trim() || !userId) return toSession(row);

      // 所有者補完はサーバー登録ではない。既存metadataも同じ書込枠で保持する。
      const sessionMetadata = {
        ...((row.sessionMetadata as Record<string, unknown> | null) ?? {}),
        local_only: true,
      };
      await db
        .update(schema.conversationSessions)
        .set({ userId, sessionMetadata })
        .where(
          and(
            eq(schema.conversationSessions.id, sessionId),
            isNull(schema.conversationSessions.deletedAt),
            sql`COALESCE(${schema.conversationSessions.userId}, '') = ''`,
          ),
        );
      return toSession({ ...row, userId, sessionMetadata });
    });
  },

  /** 新規チャット画面を開くための、通信を伴わないセッション作成。 */
  async createLocalSession(
    characterName: string,
    projectId?: string | null,
    userId?: string | null,
    agent?: ConversationAgent | null,
  ): Promise<ConversationSession> {
    const scope = agent ? await conversationAgentScope() : null;
    if (agent && !scope) {
      throw new Error("Agent付きのオフラインChatにはログインが必要です。");
    }
    const session = buildLocalSession(characterName, projectId, userId, agent);
    await applyForegroundConversationSessions([session]);
    if (agent && scope) {
      const row = getDb().select().from(schema.conversationSessions)
        .where(eq(schema.conversationSessions.id, session.id)).get();
      if (row) {
        return queuePendingAgentBinding(
          row,
          agent.id,
          null,
          agent,
          scope,
        );
      }
    }
    return session;
  },

  async createSession(
    characterName: string,
    projectId?: string | null,
    userId?: string | null,
    agent?: ConversationAgent | null,
  ): Promise<ConversationSession> {
    const characterSlug = requireCharacterSlug(characterName);
    if ((await canAttemptServer()) && !isServerKnownUnreachable()) {
      try {
        const session = await chatApi.createSession(
          characterSlug,
          projectId ?? undefined,
          agent ? { agentId: agent.id } : undefined,
        );
        if (agent && normalizedAgentId(session.agent_id) !== agent.id) {
          throw new AgentBindingConflictError(
            null,
            agent.id,
            "Serverが指定したAgent bindingを確認できませんでした。Chat作成を保留しています。",
          );
        }
        await applyForegroundConversationSessions([session]);
        return session;
      } catch (error) {
        if (!isApiConnectionError(error)) throw error;
        useNetworkStore.getState().setServerReachable(false);
      }
    }
    const session = buildLocalSession(characterSlug, projectId, userId, agent);
    await applyForegroundConversationSessions([session]);
    return session;
  },

  /**
   * Change the Agent主体 for a session.  Online writes use the dedicated
   * server CAS endpoint; offline writes remain local with an auth/server
   * scoped pending marker and are flushed before any queued dispatch.
   */
  async bindAgent(
    sessionId: string,
    agentId: string | null,
  ): Promise<ConversationSession> {
    const targetAgentId = normalizedAgentId(agentId);
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
    if (!row) throw new Error("Agent変更対象のセッションが見つかりません。");
    if (row.isGroupChat) {
      throw new Error("グループチャットにはprimary Agentを紐付けできません。参加Agentは既存のgroup設定を使用します。");
    }
    const pendingBinding = pendingAgentBinding(row);
    // Coalesce multiple offline changes into one desired target while keeping
    // the first server observation as the CAS base (B -> offline A -> C must
    // flush with expected=B, desired=C).
    const expectedAgentId = pendingBinding
      ? pendingBinding.expectedAgentId
      : normalizedAgentId(row.agentId);
    if (!pendingBinding && expectedAgentId === targetAgentId) return toSession(row);

    const scope = await conversationAgentScope();
    if (!scope) throw new Error("Agentを選択するにはログインが必要です。");
    if (
      pendingBinding &&
      (pendingBinding.authScope !== scope.authScope ||
        pendingBinding.serverFingerprint !== scope.serverFingerprint)
    ) {
      throw new Error("保留中のAgent変更を受け付けたアカウントまたは接続先に戻ってから変更してください。");
    }
    const catalog = await listConversationAgentsLocal({
      sessionId: isServerBackedSession(row) ? row.id : null,
      projectId: row.projectId ?? null,
    });
    const selected = targetAgentId
      ? catalog.find((agent) => agent.id === targetAgentId) ?? null
      : null;
    if (targetAgentId && !selected) {
      throw new Error("このAgentは現在の接続先で選択できません。オンラインで一覧を更新してください。");
    }

    if (isServerBackedSession(row) && (await canAttemptServer())) {
      try {
        const updated = await conversationAgentApi.bindSession(
          sessionId,
          targetAgentId,
          expectedAgentId,
        );
        await applyForegroundConversationSessions([updated]);
        return updated;
      } catch (error) {
        const conflict = agentBindingConflictFrom(error);
        if (conflict) {
          await refreshCanonicalAgentSessionAfterConflict(
            row,
            pendingBinding,
            "Server側のAgent bindingが変わりました。保留中の変更と未送信メッセージを確認してください。",
          );
          throw new AgentBindingConflictError(expectedAgentId, targetAgentId);
        }
        if (!isApiConnectionError(error)) throw error;
        useNetworkStore.getState().setServerReachable(false);
      }
    }

    return queuePendingAgentBinding(
      row,
      targetAgentId,
      expectedAgentId,
      selected,
      scope,
    );
  },

  /** Explicit user action to adopt the current server binding and drop a
   * pending desired Agent. Queued messages retain their own expected snapshot. */
  async adoptServerAgentBinding(sessionId: string): Promise<ConversationSession> {
    return adoptCanonicalAgentBinding(sessionId);
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
          await applyForegroundConversationSessions([updated]);
          return updated;
        } catch (error) {
          if (!isRecoverableCharacterUpdateError(error)) throw error;
          if (isApiConnectionError(error)) {
            useNetworkStore.getState().setServerReachable(false);
          }
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
      await applyForegroundConversationSessions([updated]);
      return updated;
    }

    const now = new Date().toISOString();
    await runForegroundSqliteWrite(() => db
      .update(schema.conversationSessions)
      .set({ projectId, updatedAt: now })
      .where(eq(schema.conversationSessions.id, sessionId)));
    notifyConversationChanges();
    return {
      ...toSession(row),
      project_id: projectId,
      last_activity: now,
    };
  },

  deleteSession(sessionId: string): Promise<void> {
    const existing = sessionDeleteFlights.get(sessionId);
    if (existing) return existing;
    const flight = deleteConversationSessionOnce(sessionId).finally(() => {
      if (sessionDeleteFlights.get(sessionId) === flight) sessionDeleteFlights.delete(sessionId);
    });
    sessionDeleteFlights.set(sessionId, flight);
    return flight;
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
        await chatApi.updateTitle(sessionId, title, options?.source);
      }
    } else if (options?.syncServer !== false && (await canUseServer())) {
      try {
        await chatApi.updateTitle(sessionId, title, options?.source);
      } catch (error) {
        // A server-side manual rename must not become a successful local
        // generated title merely because this call uses local-first writes.
        if (options?.source && isApiHttpError(error) && error.status === 409) {
          throw error;
        }
        // Fall back to local-first below.
      }
    }
    await runForegroundSqliteWrite(() => updateSessionStats(sessionId, (session) => {
      const metadata = {
        ...((session?.sessionMetadata as Record<string, unknown> | null) ?? {}),
      };
      if (options?.source && hasManualConversationTitle(metadata.context)) {
        throw new Error("手動で設定したタイトルは自動更新できません");
      }
      const context = metadata.context && typeof metadata.context === "object" && !Array.isArray(metadata.context)
        ? metadata.context as Record<string, unknown>
        : {};
      // Use the same persisted context contract as server-backed sessions.
      delete metadata.title_generation;
      return {
        title,
        sessionMetadata: {
          ...metadata,
          context: {
            ...context,
            title_generation: { source: options?.source ?? "manual" },
          },
        },
      };
    }));
  },

  async saveLocalMessages(
    sessionId: string,
    messages: ConversationMessage[],
  ): Promise<void> {
    await runForegroundSqliteWrite(async () => {
      await applyRemoteConversationMessages(messages);
      await updateSessionMessageStatsIfChanged(sessionId, messages.length);
    });
  },

  async appendLocalMessage(
    sessionId: string,
    role: ConversationMessage["role"],
    content: string,
    metadata: Record<string, unknown> = {},
    identity?: { id: string; created_at: string },
  ): Promise<ConversationMessage> {
    const now = identity?.created_at ?? new Date().toISOString();
    let message: ConversationMessage = {
      id: identity?.id ?? randomId(), session_id: sessionId, role, content, metadata,
      created_at: now, updated_at: now, parent_message_id: null,
      branch_index: 0, is_active_branch: true,
    };
    await runForegroundSqliteWrite(async () => {
      const db = getDb();
      // Retrying only this atomic unit is safe: a lock/error cannot leave an
      // inserted message without its session count, or increment that count twice.
      let diagnosticLogged = false;
      for (let attempt = 0; ; attempt += 1) {
        try {
          db.transaction((tx) => {
            const existing = tx.select().from(schema.conversationMessages)
              .where(eq(schema.conversationMessages.id, message.id)).get();
            if (existing) {
              if (existing.sessionId !== sessionId || existing.role !== role || existing.content !== content) {
                throw new Error("送信IDが別のメッセージに使用されています。");
              }
              message = toMessage(existing);
              return;
            }
            tx.insert(schema.conversationMessages).values({
              id: message.id, sessionId, role, content, messageMetadata: metadata,
              tokenCount: null, parentMessageId: null, branchIndex: 0,
              isActiveBranch: true, createdAt: now, updatedAt: now, deletedAt: null,
            }).run();
            const session = tx.select().from(schema.conversationSessions)
              .where(eq(schema.conversationSessions.id, sessionId)).get();
            const current = session?.sessionMetadata as Record<string, unknown> ?? {};
            tx.update(schema.conversationSessions).set({ updatedAt: now,
              sessionMetadata: { ...current, last_activity: now, message_count: Number(current.message_count ?? 0) + 1 },
            }).where(eq(schema.conversationSessions.id, sessionId)).run();
          });
          break;
        } catch (error) {
          // Explicitly opt-in logcat capture for one device reproduction. The
          // formatter records only error classes/codes/stack frames and the
          // effective journal mode; it never serializes error.message or SQL.
          if (!diagnosticLogged && isSqliteDiagnosticsEnabled()) {
            logSqliteErrorDiagnostic(error, {
              operation: "append_local_message",
              journalMode: getEffectiveSqliteJournalMode(),
            });
            diagnosticLogged = true;
          }
          if (!isSqliteBusyError(error) || attempt >= 5) throw error;
          // Yield the JS thread so another native async connection can finish.
          await new Promise<void>((resolve) => setTimeout(resolve, 20 * 2 ** attempt));
        }
      }
    });
    notifyConversationChanges();
    return message;
  },

  /** 自動再送のflightと競合させず、LLM要求前にDirectの送信元を確保する。 */
  async beginDirectReply(userMessage: ConversationMessage): Promise<void> {
    return runForegroundSqliteWrite(async () => {
      const db = getDb();
      const source = db.select().from(schema.conversationMessages).where(eq(schema.conversationMessages.id, userMessage.id)).get();
      if (!source || source.deletedAt) throw new Error("送信元の履歴が見つかりません。");
      const sourceMetadata = source.messageMetadata as Record<string, unknown> ?? {};
      if (sourceMetadata.direct_cloud || sourceMetadata.message_state === "persisted") {
        throw new Error("このメッセージは完了済みです。履歴を再読み込みしてください。");
      }
      if (sourceMetadata.server_message_id || (
        sourceMetadata.delivery_route === "server" &&
        (sourceMetadata.delivery_error || sourceMetadata.message_state === "dispatched")
      )) {
        throw new Error("サーバーの受理状況が未確認のためDirectへ切り替えられません。サーバーへ再接続して送信結果を確認してください。");
      }
      if (pendingMessageDispatchFlights.has(`${source.sessionId}:${source.id}`)) {
        throw new Error("この入力のサーバー送信結果を確認中です。未送信一覧で結果を確認してから再試行してください。");
      }
      // flight確認からこの更新までawaitを挟まない。
      db.update(schema.conversationMessages).set({
        messageMetadata: { ...(source.messageMetadata as Record<string, unknown> ?? {}), pending: false, delivery_route: "direct", message_state: "direct-running", direct_error: null },
        updatedAt: new Date().toISOString(),
      }).where(eq(schema.conversationMessages.id, userMessage.id)).run();
    });
  },

  /** 応答保存と送信元の完了を同一transactionにし、再接続・再試行で再生成しない。 */
  async completeDirectReply(
    userMessage: ConversationMessage,
    content: string,
    metadata: Record<string, unknown>,
  ): Promise<ConversationMessage> {
    return runForegroundSqliteWrite(async () => {
      const db = getDb();
      const now = new Date().toISOString();
      const id = `direct-${userMessage.id}`;
      const completed = { ...metadata, pending: false, direct_cloud: true, delivery_route: "direct", message_state: "persisted", direct_error: null };
      const message: ConversationMessage = {
        id, session_id: userMessage.session_id, role: "assistant", content,
        metadata: { ...completed, local_only: true, reply_to_local_message_id: userMessage.id },
        created_at: now, updated_at: now, parent_message_id: null, branch_index: 0, is_active_branch: true,
      };
      db.transaction((tx) => {
        const source = tx.select().from(schema.conversationMessages).where(eq(schema.conversationMessages.id, userMessage.id)).get();
        if (!source || source.deletedAt) throw new Error("送信元の履歴が見つからないためDirect応答を保存できません。");
        // 生成中に別の保留入力が同期され、会話IDが昇格していても同じ履歴へ保存する。
        message.session_id = source.sessionId;
        const existing = tx.select().from(schema.conversationMessages).where(eq(schema.conversationMessages.id, id)).get();
        if (existing) { message.content = existing.content; return; }
        tx.insert(schema.conversationMessages).values({
          id, sessionId: source.sessionId, role: "assistant", content,
          messageMetadata: message.metadata, createdAt: now, updatedAt: now,
          parentMessageId: null, branchIndex: 0, isActiveBranch: true, deletedAt: null,
        }).run();
        tx.update(schema.conversationMessages).set({
          messageMetadata: { ...(source.messageMetadata as Record<string, unknown> ?? {}), ...completed }, updatedAt: now,
        }).where(eq(schema.conversationMessages.id, userMessage.id)).run();
        const session = tx.select().from(schema.conversationSessions).where(eq(schema.conversationSessions.id, source.sessionId)).get();
        const sessionMetadata = session?.sessionMetadata as Record<string, unknown> ?? {};
        tx.update(schema.conversationSessions).set({
          updatedAt: now,
          sessionMetadata: { ...sessionMetadata, last_activity: now, message_count: Number(sessionMetadata.message_count ?? 0) + 1 },
        }).where(eq(schema.conversationSessions.id, source.sessionId)).run();
      });
      notifyConversationChanges();
      return message;
    });
  },

  async listPendingMessages(sessionId: string): Promise<ConversationMessage[]> {
    return listPendingMessagesForSessionId(sessionId);
  },

  async updatePendingLocalMessage(
    sessionId: string,
    messageId: string,
    content: string,
  ): Promise<ConversationMessage> {
    const normalized = content.trim();
    if (!normalized) throw new Error("メッセージ本文を入力してください。");
    const key = `${sessionId}:${messageId}`;
    if (pendingMessageDispatchFlights.has(key)) {
      throw new Error("このメッセージは送信処理中です。完了後にもう一度編集してください。");
    }
    const existingEdit = pendingMessageEditFlights.get(key);
    if (existingEdit) return existingEdit;

    const editFlight = runForegroundSqliteWrite(async () => {
      const db = getDb();
      const row = (
        await db
          .select()
          .from(schema.conversationMessages)
          .where(
            and(
              eq(schema.conversationMessages.id, messageId),
              eq(schema.conversationMessages.sessionId, sessionId),
            ),
          )
      )[0];
      if (!row || row.deletedAt) {
        throw new Error("編集対象の未送信メッセージが見つかりません。");
      }
      const current = toMessage(row);
      const state = String(current.metadata?.message_state ?? "");
      if (
        current.role !== "user" ||
        (!current.metadata?.pending &&
          !current.metadata?.persistence_error &&
          state !== "direct-failed" &&
          state !== "local-draft")
      ) {
        throw new Error("このメッセージは未送信状態ではないため編集できません。");
      }
      const now = new Date().toISOString();
      const metadata = replacePendingDispatchMessage(current, normalized);
      await db
        .update(schema.conversationMessages)
        .set({ content: normalized, messageMetadata: metadata, updatedAt: now })
        .where(eq(schema.conversationMessages.id, messageId));
      return {
        ...current,
        content: normalized,
        metadata,
        updated_at: now,
      };
    });
    pendingMessageEditFlights.set(key, editFlight);
    try {
      const updated = await editFlight;
      notifyConversationChanges();
      return updated;
    } finally {
      if (pendingMessageEditFlights.get(key) === editFlight) {
        pendingMessageEditFlights.delete(key);
      }
    }
  },

  /**
   * Remove one input from the server retry queue without deleting the
   * conversation or its text. Accepted/uncertain server deliveries are kept
   * protected from this local queue action.
   */
  async cancelPendingMessage(
    sessionId: string,
    messageId: string,
    mode: "cancel" | "draft" = "cancel",
  ): Promise<ConversationMessage> {
    const key = `${sessionId}:${messageId}`;
    if (pendingMessageDispatchFlights.has(key) || pendingMessageEditFlights.has(key)) {
      throw new Error("この入力は送信結果を確認中です。受理状況を確認してから操作してください。");
    }
    return runForegroundSqliteWrite(async () => {
      const db = getDb();
      const row = db.select().from(schema.conversationMessages)
        .where(and(
          eq(schema.conversationMessages.id, messageId),
          eq(schema.conversationMessages.sessionId, sessionId),
        )).get();
      if (!row || row.deletedAt) throw new Error("未送信メッセージが見つかりません。");
      const current = toMessage(row);
      const metadata = current.metadata ?? {};
      const state = String(metadata.message_state ?? "");
      const legacyServerResultUncertain =
        metadata.server_result_uncertain === undefined &&
        metadata.delivery_route === "server" &&
        Boolean(metadata.delivery_error) &&
        (Boolean(metadata.pending) || state === "queued");
      const acceptedOrUncertain = Boolean(metadata.server_message_id)
        || state === "dispatched"
        || metadata.server_result_uncertain === true
        || legacyServerResultUncertain;
      if (acceptedOrUncertain) {
        throw new Error("Serverの受理状況が不明な入力は、未送信キューの取消対象ではありません。");
      }
      const queueState = Boolean(metadata.pending)
        || Boolean(metadata.persistence_error)
        || state === "direct-failed"
        || state === "local-draft";
      if (current.role !== "user" || !queueState) {
        throw new Error("この入力は未送信キューにありません。");
      }
      const nextMetadata: Record<string, unknown> = {
        ...metadata,
        pending: false,
        persistence_error: false,
        delivery_error: null,
        direct_error: null,
        message_state: mode === "draft" ? "local-draft" : "cancelled",
        ...(mode === "cancel" ? { queue_cancelled: true } : { draft_saved: true }),
      };
      const now = new Date().toISOString();
      db.update(schema.conversationMessages).set({
        messageMetadata: nextMetadata,
        updatedAt: now,
      }).where(eq(schema.conversationMessages.id, messageId)).run();
      notifyConversationChanges();
      return { ...current, metadata: nextMetadata, updated_at: now };
    });
  },

  async markPendingMessageQueued(
    messageId: string,
    serverMessageId?: string,
  ): Promise<void> {
    return runForegroundSqliteWrite(async () => {
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
    });
  },

  async mergeMessageMetadata(
    messageId: string,
    patch: Record<string, unknown>,
  ): Promise<void> {
    return runForegroundSqliteWrite(async () => {
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
    });
  },

  async pruneSentLocalMessages(
    sessionId: string,
    remoteMessages: ConversationMessage[] = [],
  ): Promise<void> {
    return runForegroundSqliteWrite(async () => {
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
    });
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
    await runForegroundSqliteWrite(() => applyRemoteConversationMessages(branches));
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
