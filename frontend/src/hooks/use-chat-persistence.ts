"use client";

import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type {
  ConversationMessage,
  ConversationGenerationStatus,
  ConversationSession,
} from "@/lib/chat-api";
import type { ChatGenerationEvent } from "@/lib/chat-generation-state";
import type { chatTimelineReducer } from "@/lib/chat-state";
import { isTransientChatMessage } from "@/lib/chat-state";
import {
  clearCachedMessages,
  deriveServerTime,
  getLastServerTime,
  mergePersistedById,
  readCachedMessages,
  setLastServerTime,
  writeCachedMessages,
} from "@/lib/chat-message-cache";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import {
  safeLocalStorageGetItem,
  safeLocalStorageRemoveItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";

const LAST_SESSION_KEY = "aoitalk_last_session_id";
const GENERATION_STATUS_RETRY_DELAY_MS = 250;
const GENERATION_STATUS_MAX_ATTEMPTS = 3;
const GENERATION_RECOVERY_POLL_INTERVAL_MS = 2_000;
const SESSION_LOAD_RECOVERY_DELAY_MS = 2_000;
const GENERATION_STATUS_TERMINAL_STATUSES = new Set([
  "completed",
  "cancelled",
  "failed",
]);

const TRANSIENT_HISTORY_ERROR_STATUSES = new Set([
  408,
  425,
  429,
  500,
  502,
  503,
  504,
]);

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

export type RefreshPersistedMessagesOptions = {
  /**
   * 差分取得ではなく、現在のサーバー側 active path 全体を再取得する。
   * ブランチ切替など、直前の server_time より前の履歴も置き換える必要が
   * ある操作で利用する。
   */
  forceFull?: boolean;
  /** Internal initial-load override. `undefined` means use the cache cursor. */
  since?: string | null;
  /** Cached rows to merge when the initial load already rendered the cache. */
  baseMessages?: ConversationMessage[];
  /** Caller intent used to decide whether an in-flight read needs a follow-up. */
  trigger?: "initial" | "poll" | "terminal" | "reconnect" | "websocket" | "force_full";
  /** Stable row id announced by a websocket event, when available. */
  announcedMessageId?: string | null;
  /** Additional announced row IDs coalesced in the same websocket turn. */
  announcedMessageIds?: string[];
  /** Initial owner fetches the body before resume but applies it afterward. */
  deferApply?: boolean;
};

type RefreshIntent = {
  sessionId: string;
  generation: number;
  forceFull: boolean;
  since: string | null;
  baseMessages?: ConversationMessage[];
  trigger?: RefreshPersistedMessagesOptions["trigger"];
  announcedMessageIds: string[];
  unscopedWebSocket: boolean;
  deferApply?: boolean;
};

type QueuedRefresh = RefreshIntent & {
  waiters: Array<(messages: ConversationMessage[] | null) => void>;
};

type InFlightRefresh = RefreshIntent & {
  promise: Promise<ConversationMessage[] | null>;
};

type RefreshState = {
  inFlight: InFlightRefresh | null;
  queued: QueuedRefresh | null;
};

function refreshStateKey(sessionId: string, generation: number): string {
  return `${sessionId}:${generation}`;
}

function getErrorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined;
  const candidate = error as {
    status?: unknown;
    statusCode?: unknown;
    response?: { status?: unknown } | null;
  };
  for (const value of [
    candidate.status,
    candidate.statusCode,
    candidate.response?.status,
  ]) {
    if (typeof value === "number" && Number.isInteger(value)) return value;
  }
  return undefined;
}

function isTransientHistoryError(error: unknown): boolean {
  const status = getErrorStatus(error);
  if (status !== undefined) return TRANSIENT_HISTORY_ERROR_STATUSES.has(status);
  if (error instanceof TypeError) return true;
  if (error instanceof DOMException) {
    return error.name === "AbortError" || error.name === "TimeoutError";
  }
  // The BFF's older fetch wrappers exposed network failures as plain Error.
  // Treat an unclassified error as recoverable during the bounded initial load;
  // authorization and resource errors are still fenced by their status.
  return true;
}

function isPermanentHistoryError(error: unknown): boolean {
  const status = getErrorStatus(error);
  return status === 401 || status === 403 || status === 404;
}

function sessionLoadErrorMessage(error: unknown): string {
  switch (getErrorStatus(error)) {
    case 401:
      return "認証の有効期限が切れています。再度サインインしてください。";
    case 403:
      return "この会話を表示する権限がありません。";
    case 404:
      return "この会話は見つかりません。";
    default:
      return "会話セッションを表示できませんでした。履歴データは残っている可能性があるため、再読み込みしてください。";
  }
}

function refreshCovers(
  inFlight: InFlightRefresh,
  next: RefreshIntent,
): boolean {
  if (inFlight.sessionId !== next.sessionId || inFlight.generation !== next.generation) {
    return false;
  }
  // A later explicit full refresh or websocket announcement may represent a
  // commit that happened after this request started. Reserve one follow-up,
  // even when both reads happen to use the same full/cursor query. Repeated
  // force_full calls can share the already-authoritative full read.
  if (
    next.trigger === "websocket" &&
    (next.unscopedWebSocket || next.announcedMessageIds.length > 0)
  ) {
    return false;
  }
  if (next.forceFull) return false;
  if (inFlight.forceFull || inFlight.since === null) return true;
  return !next.forceFull && inFlight.since === next.since;
}

function compactTitleText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function fallbackTitleFromFirstUserMessage(content: string): string {
  const title = compactTitleText(content);
  return title.length > 40 ? `${title.slice(0, 37)}...` : title;
}

function shouldRequestSessionTitleGeneration(
  session: ConversationSession,
  messages: ConversationMessage[],
): boolean {
  const title = compactTitleText(session.title || "");
  const firstUserMessage = messages.find(
    (message) =>
      message.role === "user" && compactTitleText(message.content).length > 0,
  );
  if (!firstUserMessage) return false;

  if (!title) return true;
  return title === fallbackTitleFromFirstUserMessage(firstUserMessage.content);
}

type UseChatPersistenceArgs = {
  router: ReturnType<typeof useRouter>;
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  sessionLoadAttempt: number;
  messagesRef: RefObject<ConversationMessage[]>;
  currentSession: ConversationSession | null;
  isConnected: boolean;
  responsePollGenerationRef: RefObject<number>;
  updateSidebarTitle: (sessionId: string, title: string) => void;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  upsertSession: (session: ConversationSession) => void;
  /** Bump the sidebar only after an assistant message is durably persisted. */
  bumpSession: (sessionId: string) => void;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setIsLoadingMessages: Dispatch<SetStateAction<boolean>>;
  setSessionLoadError: Dispatch<SetStateAction<string | null>>;
};

/**
 * 会話の永続化取得・生成状態復元・セッションロード・タイトル生成を担うフック。
 * セッション世代とrefresh coordinatorで再接続・WebSocket・回復取得を統合する。
 */
export function useChatPersistence({
  router,
  activeSessionId,
  activeSessionIdRef,
  sessionLoadAttempt,
  messagesRef,
  currentSession,
  isConnected,
  responsePollGenerationRef,
  updateSidebarTitle,
  dispatchChatTimeline,
  dispatchGeneration,
  upsertSession,
  bumpSession,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
}: UseChatPersistenceArgs) {
  const statusRequestGenerationRef = useRef(0);
  const pendingRecoveryTimersRef = useRef(
    new Map<number, (active: boolean) => void>(),
  );
  const mountedRef = useRef(true);
  const currentSessionRef = useRef<ConversationSession | null>(currentSession);
  currentSessionRef.current = currentSession;
  const loadKeyRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  const refreshStatesRef = useRef(new Map<string, RefreshState>());
  const refreshErrorsRef = useRef(new Map<string, unknown>());
  const refreshServerTimesRef = useRef(new Map<string, string | null>());
  const initialLoadStateRef = useRef<{
    sessionId: string | null;
    generation: number;
    inFlight: boolean;
  }>({ sessionId: null, generation: 0, inFlight: false });
  const pendingReconnectRef = useRef<{
    sessionId: string;
    generation: number;
  } | null>(null);
  const initialBodyRecoveryRef = useRef<{
    sessionId: string;
    generation: number;
    promise: Promise<void>;
  } | null>(null);
  const initialSessionRecoveryRef = useRef<{
    sessionId: string;
    generation: number;
    promise: Promise<void>;
  } | null>(null);
  const refreshGenerationStatusRef = useRef<
    (
      sessionId: string,
      knownMessages: ConversationMessage[],
      titleSession?: ConversationSession | null,
      options?: {
        skipBodyRecovery?: boolean;
        expectedLoadGeneration?: number;
      },
    ) => Promise<ConversationGenerationStatus | null>
  >(async () => null);
  const resumeApplyGateRef = useRef<{
    sessionId: string | null;
    generation: number;
    ready: boolean;
  }>({ sessionId: null, generation: 0, ready: true });
  const deferredRefreshResultsRef = useRef(
    new Map<string, { messages: ConversationMessage[]; serverTime: string | null }>(),
  );
  const connectionStateRef = useRef<{
    sessionId: string | null;
    isConnected: boolean;
  } | null>(null);
  const connectedSessionIdsRef = useRef(new Set<string>());
  const skipNextConnectionRecoveryRef = useRef<string | null>(null);
  const cancelRecoveryTimers = useCallback(() => {
    const pendingRecoveryTimers = pendingRecoveryTimersRef.current;
    for (const [timerId, cancel] of pendingRecoveryTimers) {
      window.clearTimeout(timerId);
      cancel(false);
    }
    pendingRecoveryTimers.clear();
  }, []);
  useEffect(() => {
    mountedRef.current = true;
    const refreshStates = refreshStatesRef.current;
    return () => {
      mountedRef.current = false;
      cancelRecoveryTimers();
      for (const state of refreshStates.values()) {
        const queued = state.queued;
        state.queued = null;
        if (queued) {
          for (const resolve of queued.waiters) resolve(null);
        }
      }
    };
  }, [cancelRecoveryTimers]);

  const waitForRecoveryDelay = useCallback((delayMs: number) => {
    return new Promise<boolean>((resolve) => {
      const timerId = window.setTimeout(() => {
        pendingRecoveryTimersRef.current.delete(timerId);
        resolve(true);
      }, delayMs);
      const cancel = (active: boolean) => {
        pendingRecoveryTimersRef.current.delete(timerId);
        resolve(active);
      };
      pendingRecoveryTimersRef.current.set(timerId, cancel);
    });
  }, []);
  // The first passive effect can run after a user submits from a freshly
  // mounted /chat route.  Do not clear the optimistic provisional row that
  // was appended before that effect; later null-session transitions still
  // clear the previous session normally.
  const previousActiveSessionIdRef = useRef<string | null | undefined>(
    undefined,
  );
  // A response can be observed through both the websocket event and the
  // fallback REST poll. Keep one activity bump per durable assistant message
  // so duplicate notifications do not continually rewrite last_activity.
  const assistantActivityKeysRef = useRef<Set<string>>(new Set());
  const bumpSessionForAssistant = useCallback(
    (sessionId: string, messageId: string) => {
      const key = `${sessionId}:${messageId}`;
      const keys = assistantActivityKeysRef.current;
      if (keys.has(key)) return;
      keys.add(key);
      if (keys.size > 512) {
        const oldest = keys.values().next().value;
        if (oldest) keys.delete(oldest);
      }
      bumpSession(sessionId);
    },
    [bumpSession],
  );
  const maybeGenerateLoadedSessionTitle = useCallback(
    async (session: ConversationSession, messages: ConversationMessage[]) => {
      if (!shouldRequestSessionTitleGeneration(session, messages)) {
        return;
      }

      try {
        const titleResult = await chatApi.generateSessionTitle(session.id);
        if (!titleResult.title || activeSessionIdRef.current !== session.id) {
          return;
        }
        updateSidebarTitle(session.id, titleResult.title);
      } catch (err) {
        console.warn("セッションタイトル生成に失敗:", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [updateSidebarTitle],
  );

  const clearPermanentHistoryState = useCallback(
    async (sessionId: string, generation: number, error: unknown) => {
      if (!isPermanentHistoryError(error)) return false;
      try {
        await clearCachedMessages(sessionId);
      } catch (cacheError) {
        console.warn("メッセージキャッシュの無効化に失敗:", cacheError);
      }
      if (
        !mountedRef.current ||
        activeSessionIdRef.current !== sessionId ||
        loadGenerationRef.current !== generation
      ) {
        return true;
      }
      dispatchChatTimeline({ type: "clear" });
      setSessionLoadError(sessionLoadErrorMessage(error));
      return true;
    },
    [activeSessionIdRef, dispatchChatTimeline, setSessionLoadError],
  );

  const isCurrentRefresh = useCallback(
    (intent: RefreshIntent) =>
      mountedRef.current &&
      activeSessionIdRef.current === intent.sessionId &&
      loadGenerationRef.current === intent.generation,
    [activeSessionIdRef],
  );

  const loadPersistedMessages = useCallback(
    async (intent: RefreshIntent): Promise<ConversationMessage[] | null> => {
      const key = refreshStateKey(intent.sessionId, intent.generation);
      try {
        // 通常は前回の server_time を since に渡し、差分のみ取得する。
        // ブランチ切替後は inactive になった旧 active path を確実に除去し、
        // 新しい active path 全体を表示するため、full GET を行う。
        const data = await chatApi.getMessages(
          intent.sessionId,
          intent.since ?? undefined,
        );
        if (!isCurrentRefresh(intent)) return null;

        // since 指定時は既存の永続メッセージへ差分をマージ（id で重複排除）。
        // since なし（初回）は取得結果をそのまま採用する。
        const prevPersisted = intent.since
          ? (intent.baseMessages ?? messagesRef.current).filter(
              (message) =>
                !isTransientChatMessage(message) &&
                message.session_id === intent.sessionId,
            )
          : [];
        const mergedPersisted = intent.since
          ? mergePersistedById(prevPersisted, data.messages)
          : data.messages;

        const nextServerTime =
          data.server_time ?? deriveServerTime(mergedPersisted);
        refreshErrorsRef.current.delete(key);
        refreshServerTimesRef.current.set(key, nextServerTime);
        const resumeGate = resumeApplyGateRef.current;
        const resumePending =
          resumeGate.sessionId === intent.sessionId &&
          resumeGate.generation === intent.generation &&
          !resumeGate.ready;
        if (intent.deferApply || resumePending) {
          deferredRefreshResultsRef.current.set(key, {
            messages: mergedPersisted,
            serverTime: nextServerTime,
          });
        } else {
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: intent.sessionId,
            messages: mergedPersisted,
          });
          setLastServerTime(intent.sessionId, nextServerTime);
          void writeCachedMessages(
            intent.sessionId,
            mergedPersisted,
            nextServerTime,
          );
        }
        return mergedPersisted;
      } catch (err) {
        refreshErrorsRef.current.set(key, err);
        if (isCurrentRefresh(intent) && isPermanentHistoryError(err)) {
          await clearPermanentHistoryState(intent.sessionId, intent.generation, err);
        }
        console.warn("保存済みメッセージの再取得に失敗:", err);
        return null;
      }
    },
    [clearPermanentHistoryState, dispatchChatTimeline, isCurrentRefresh, messagesRef],
  );

  const startRefresh = useCallback(
    (
      stateKey: string,
      state: RefreshState,
      intent: RefreshIntent,
    ): Promise<ConversationMessage[] | null> => {
      let resolvePromise!: (messages: ConversationMessage[] | null) => void;
      const promise = new Promise<ConversationMessage[] | null>((resolve) => {
        resolvePromise = resolve;
      });
      const request: InFlightRefresh = { ...intent, promise };
      state.inFlight = request;

      void (async () => {
        const messages = await loadPersistedMessages(intent);
        resolvePromise(messages);
        if (state.inFlight !== request) return;
        state.inFlight = null;

        const queued = state.queued;
        state.queued = null;
        const queuedAnnouncementCovered = Boolean(
          queued &&
            !queued.forceFull &&
            queued.trigger === "websocket" &&
            !queued.unscopedWebSocket &&
            queued.announcedMessageIds.length > 0 &&
            queued.announcedMessageIds.every((id) =>
              messages?.some((message) => message.id === id),
            ),
        );
        if (queuedAnnouncementCovered) {
          for (const resolve of queued!.waiters) resolve(messages);
        } else if (
          queued &&
          mountedRef.current &&
          activeSessionIdRef.current === queued.sessionId &&
          loadGenerationRef.current === queued.generation
        ) {
          const nextPromise = startRefresh(stateKey, state, queued);
          nextPromise.then((nextMessages) => {
            for (const resolve of queued.waiters) resolve(nextMessages);
          });
        } else if (queued) {
          for (const resolve of queued.waiters) resolve(null);
        }
        if (!state.inFlight && !state.queued) {
          refreshStatesRef.current.delete(stateKey);
        }
      })();
      return promise;
    },
    [activeSessionIdRef, loadPersistedMessages],
  );

  const refreshPersistedMessages = useCallback(
    (
      sessionId: string,
      options?: RefreshPersistedMessagesOptions,
    ): Promise<ConversationMessage[] | null> => {
      const generation = loadGenerationRef.current;
      const forceFull = options?.forceFull === true;
      const since = forceFull
        ? null
        : options && Object.prototype.hasOwnProperty.call(options, "since")
          ? options.since ?? null
          : getLastServerTime(sessionId);
      const intent: RefreshIntent = {
        sessionId,
        generation,
        forceFull,
        since,
        baseMessages: options?.baseMessages,
        trigger: options?.trigger,
        announcedMessageIds: [
          ...(options?.announcedMessageIds ?? []),
          ...(options?.announcedMessageId ? [options.announcedMessageId] : []),
        ].filter((id, index, ids) => ids.indexOf(id) === index),
        unscopedWebSocket:
          options?.trigger === "websocket" &&
          !options?.announcedMessageId &&
          !(options?.announcedMessageIds?.length),
        deferApply: options?.deferApply,
      };
      const stateKey = refreshStateKey(sessionId, generation);
      let state = refreshStatesRef.current.get(stateKey);
      if (!state) {
        state = { inFlight: null, queued: null };
        refreshStatesRef.current.set(stateKey, state);
      }

      if (state.inFlight && refreshCovers(state.inFlight, intent)) {
        return state.inFlight.promise;
      }

      if (state.inFlight) {
        if (state.queued) {
          state.queued.forceFull ||= intent.forceFull;
          if (intent.forceFull) state.queued.since = null;
          else if (!state.queued.forceFull) state.queued.since = intent.since;
          if (intent.baseMessages) state.queued.baseMessages = intent.baseMessages;
          if (intent.trigger === "websocket") {
            state.queued.trigger = "websocket";
            state.queued.unscopedWebSocket ||= intent.unscopedWebSocket;
            state.queued.announcedMessageIds = [
              ...new Set([
                ...state.queued.announcedMessageIds,
                ...intent.announcedMessageIds,
              ]),
            ];
          }
          return new Promise((resolve) => state!.queued!.waiters.push(resolve));
        }
        return new Promise((resolve) => {
          state!.queued = {
            ...intent,
            waiters: [resolve],
          };
        });
      }

      return startRefresh(stateKey, state, intent);
    },
    [startRefresh],
  );

  const getRefreshError = useCallback(
    (sessionId: string, generation = loadGenerationRef.current) =>
      refreshErrorsRef.current.get(refreshStateKey(sessionId, generation)),
    [],
  );

  const getRefreshServerTime = useCallback(
    (sessionId: string, generation = loadGenerationRef.current) =>
      refreshServerTimesRef.current.get(refreshStateKey(sessionId, generation)),
    [],
  );

  const refreshInitialMessagesWithRecovery = useCallback(
    async (
      sessionId: string,
      generation: number,
      cachedMessages: Awaited<ReturnType<typeof readCachedMessages>>,
    ) => {
      const since = cachedMessages?.serverTime ?? null;
      if (
        !mountedRef.current ||
        loadGenerationRef.current !== generation ||
        activeSessionIdRef.current !== sessionId
      ) {
        return null;
      }
      return refreshPersistedMessages(sessionId, {
        since,
        baseMessages: cachedMessages?.messages,
        trigger: "initial",
        deferApply: true,
      });
    },
    [
      activeSessionIdRef,
      refreshPersistedMessages,
    ],
  );

  const requestGenerationStatusWithRetry = useCallback(
    async (
      sessionId: string,
      statusGeneration: number,
      maxAttempts = GENERATION_STATUS_MAX_ATTEMPTS,
    ): Promise<ConversationGenerationStatus | null> => {
      const isCurrentStatusRequest = (
        status?: { session_id?: string | null } | null,
      ) =>
        mountedRef.current &&
        statusRequestGenerationRef.current === statusGeneration &&
        activeSessionIdRef.current === sessionId &&
        (status?.session_id == null || status.session_id === sessionId);

      let lastError: unknown = null;
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        if (!isCurrentStatusRequest()) return null;
        if (attempt > 0) {
          const waited = await waitForRecoveryDelay(
            GENERATION_STATUS_RETRY_DELAY_MS,
          );
          if (!waited || !isCurrentStatusRequest()) return null;
        }

        try {
          const status = await chatApi.getGenerationStatus(sessionId);
          if (!isCurrentStatusRequest(status)) return null;
          if (
            typeof status?.running !== "boolean" ||
            typeof status?.status !== "string"
          ) {
            throw new Error("生成状態レスポンスが不正です");
          }
          return status;
        } catch (err) {
          lastError = err;
          if (!isTransientHistoryError(err)) throw err;
        }
      }

      if (lastError) throw lastError;
      return null;
    },
    [activeSessionIdRef, waitForRecoveryDelay],
  );

  const waitForPersistedAssistantResponse = useCallback(
    async (
      sessionId: string,
      knownAssistantIds: Set<string>,
      _titleSession?: ConversationSession | null,
    ) => {
      if (!mountedRef.current) return;
      void _titleSession;
      const generation = ++responsePollGenerationRef.current;
      const loadGeneration = loadGenerationRef.current;
      const statusGeneration = ++statusRequestGenerationRef.current;
      const timeoutAt = Date.now() + 300_000;
      let skipNextRecoveryDelay = false;
      let terminalImmediateRetryUsed = false;

      while (Date.now() < timeoutAt) {
        if (!skipNextRecoveryDelay) {
          const waited = await waitForRecoveryDelay(
            GENERATION_RECOVERY_POLL_INTERVAL_MS,
          );
          if (!waited || responsePollGenerationRef.current !== generation) {
            return;
          }
        } else {
          skipNextRecoveryDelay = false;
        }
        const currentSessionId = activeSessionIdRef.current;
        if (!mountedRef.current || currentSessionId !== sessionId) return;

        const persistedMessages = await refreshPersistedMessages(sessionId);
        if (
          !mountedRef.current ||
          responsePollGenerationRef.current !== generation ||
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }
        if (persistedMessages === null) {
          const error = getRefreshError(sessionId, loadGeneration);
          if (error && isPermanentHistoryError(error)) return;
        }

        const newAssistantMessage = persistedMessages?.find(
          (message) =>
            message.role === "assistant" && !knownAssistantIds.has(message.id),
        );
        if (newAssistantMessage) {
          knownAssistantIds.add(newAssistantMessage.id);
          bumpSessionForAssistant(sessionId, newAssistantMessage.id);
          const agentRunId =
            typeof newAssistantMessage.metadata?.agent_run_id === "string"
              ? newAssistantMessage.metadata.agent_run_id
              : null;
          dispatchGeneration({
            type: "assistant_persisted",
            sessionId,
            agentRunId,
            assistantMessageId: newAssistantMessage.id,
            eventId: `poll:message:${sessionId}:${newAssistantMessage.id}`,
          });
        }

        if (statusRequestGenerationRef.current !== statusGeneration) return;
        let status: ConversationGenerationStatus | null = null;
        try {
          status = await requestGenerationStatusWithRetry(
            sessionId,
            statusGeneration,
            1,
          );
        } catch (error) {
          // statusが一時的に取得できなくても、次のpoll周期で再照会する。
          if (!isTransientHistoryError(error)) return;
          status = null;
        }
        if (
          !mountedRef.current ||
          responsePollGenerationRef.current !== generation ||
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }
        if (!status) {
          if (statusRequestGenerationRef.current !== statusGeneration) return;
          continue;
        }

        dispatchGeneration({
          type: "status_restored",
          sessionId,
          status,
          eventId: `poll:recovery-status:${sessionId}:${status.status}:${status.updated_at ?? statusGeneration}`,
        });

        const normalizedStatus = status.status.toLowerCase();
        if (
          !status.running &&
          GENERATION_STATUS_TERMINAL_STATUSES.has(normalizedStatus)
        ) {
          const terminalMessages = await refreshPersistedMessages(sessionId);
          if (
            !mountedRef.current ||
            responsePollGenerationRef.current !== generation ||
            activeSessionIdRef.current !== sessionId
          ) {
            return;
          }
          const terminalAssistantMessage = terminalMessages?.find(
            (message) =>
              message.role === "assistant" &&
              !knownAssistantIds.has(message.id),
          );
          if (terminalAssistantMessage) {
            knownAssistantIds.add(terminalAssistantMessage.id);
            bumpSessionForAssistant(sessionId, terminalAssistantMessage.id);
            const agentRunId =
              typeof terminalAssistantMessage.metadata?.agent_run_id ===
                "string"
                ? terminalAssistantMessage.metadata.agent_run_id
                : null;
            dispatchGeneration({
              type: "assistant_persisted",
              sessionId,
              agentRunId,
              assistantMessageId: terminalAssistantMessage.id,
              eventId: `poll:terminal-message:${sessionId}:${terminalAssistantMessage.id}`,
            });
          }
          // A terminal status is authoritative for generation state, but the
          // assistant row can still be committed a little later. Keep the
          // bounded recovery loop alive until a successful body fetch arrives;
          // the final fetch below remains the last chance after the timeout.
          if (terminalMessages !== null) return;
          const terminalError = getRefreshError(sessionId, loadGeneration);
          if (
            terminalError &&
            !isTransientHistoryError(terminalError)
          ) {
            return;
          }
          // The status endpoint has already declared completion. If the
          // terminal body read itself exhausted the API retry budget, make one
          // immediate body retry before returning to the regular two-second
          // cadence; this closes the short persistence race without polling
          // rapidly for the remainder of the bounded recovery window.
          if (!terminalImmediateRetryUsed) {
            terminalImmediateRetryUsed = true;
            skipNextRecoveryDelay = true;
          }
        }
      }

      const finalMessages = await refreshPersistedMessages(sessionId);
      if (
        !mountedRef.current ||
        responsePollGenerationRef.current !== generation ||
        activeSessionIdRef.current !== sessionId
      ) {
        return;
      }
      if (finalMessages === null) {
        const error = getRefreshError(sessionId, loadGeneration);
        if (error && isPermanentHistoryError(error)) return;
      }
      const finalAssistantMessage = finalMessages?.find(
        (message) =>
          message.role === "assistant" && !knownAssistantIds.has(message.id),
      );
      if (finalMessages && finalAssistantMessage) {
        bumpSessionForAssistant(sessionId, finalAssistantMessage.id);
        const agentRunId =
          typeof finalAssistantMessage.metadata?.agent_run_id === "string"
            ? finalAssistantMessage.metadata.agent_run_id
            : null;
        dispatchGeneration({
          type: "assistant_persisted",
          sessionId,
          agentRunId,
          assistantMessageId: finalAssistantMessage.id,
          eventId: `poll:message:${sessionId}:${finalAssistantMessage.id}`,
        });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      dispatchGeneration,
      refreshPersistedMessages,
      bumpSessionForAssistant,
      requestGenerationStatusWithRetry,
      waitForRecoveryDelay,
      getRefreshError,
    ],
  );

  const waitForTerminalPersistedAssistantResponse = useCallback(
    async (sessionId: string, knownAssistantIds: Set<string>) => {
      if (!mountedRef.current) return;
      const generation = ++responsePollGenerationRef.current;
      const loadGeneration = loadGenerationRef.current;
      const timeoutAt = Date.now() + 300_000;
      let firstAttempt = true;

      const applyAssistant = (messages: ConversationMessage[] | null) => {
        const assistant = messages?.find(
          (message) =>
            message.role === "assistant" && !knownAssistantIds.has(message.id),
        );
        if (!assistant) return;
        knownAssistantIds.add(assistant.id);
        bumpSessionForAssistant(sessionId, assistant.id);
        const agentRunId =
          typeof assistant.metadata?.agent_run_id === "string"
            ? assistant.metadata.agent_run_id
            : null;
        dispatchGeneration({
          type: "assistant_persisted",
          sessionId,
          agentRunId,
          assistantMessageId: assistant.id,
          eventId: `poll:terminal-message:${sessionId}:${assistant.id}`,
        });
      };

      while (Date.now() < timeoutAt) {
        if (!firstAttempt) {
          const waited = await waitForRecoveryDelay(
            GENERATION_RECOVERY_POLL_INTERVAL_MS,
          );
          if (
            !waited ||
            responsePollGenerationRef.current !== generation ||
            loadGenerationRef.current !== loadGeneration
          ) {
            return;
          }
        }
        firstAttempt = false;
        if (
          !mountedRef.current ||
          responsePollGenerationRef.current !== generation ||
          loadGenerationRef.current !== loadGeneration ||
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }

        const messages = await refreshPersistedMessages(sessionId);
        if (
          !mountedRef.current ||
          responsePollGenerationRef.current !== generation ||
          loadGenerationRef.current !== loadGeneration ||
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }
        if (messages !== null) {
          applyAssistant(messages);
          return;
        }
        const error = getRefreshError(sessionId);
        if (error && !isTransientHistoryError(error)) return;
      }

      // Keep one final body fetch after the bounded recovery window. A
      // terminal status must never remove this last chance to observe the
      // durable assistant row.
      const finalMessages = await refreshPersistedMessages(sessionId);
      if (
        mountedRef.current &&
        responsePollGenerationRef.current === generation &&
        loadGenerationRef.current === loadGeneration &&
        activeSessionIdRef.current === sessionId
      ) {
        applyAssistant(finalMessages);
      }
    },
    [
      activeSessionIdRef,
      bumpSessionForAssistant,
      dispatchGeneration,
      getRefreshError,
      refreshPersistedMessages,
      responsePollGenerationRef,
      waitForRecoveryDelay,
    ],
  );

  const recoverInitialPersistedMessages = useCallback(
    (
      sessionId: string,
      generation: number,
      knownMessages: ConversationMessage[],
      cachedMessages: Awaited<ReturnType<typeof readCachedMessages>>,
    ): Promise<void> => {
      const existing = initialBodyRecoveryRef.current;
      if (
        existing?.sessionId === sessionId &&
        existing.generation === generation
      ) {
        return existing.promise;
      }

      const ownsRecovery = () =>
        mountedRef.current &&
        loadGenerationRef.current === generation &&
        activeSessionIdRef.current === sessionId;
      const promise = (async () => {
        const applyRecoveredMessages = async (
          messages: ConversationMessage[],
        ) => {
          const knownAssistantIds = new Set(
            knownMessages
              .filter((message) => message.role === "assistant")
              .map((message) => message.id),
          );
          const assistant = messages.find(
            (message) =>
              message.role === "assistant" &&
              !knownAssistantIds.has(message.id),
          );
          if (assistant) {
            bumpSessionForAssistant(sessionId, assistant.id);
            dispatchGeneration({
              type: "assistant_persisted",
              sessionId,
              agentRunId:
                typeof assistant.metadata?.agent_run_id === "string"
                  ? assistant.metadata.agent_run_id
                  : null,
              assistantMessageId: assistant.id,
              eventId: `poll:initial-message:${sessionId}:${assistant.id}`,
            });
          }
          setSessionLoadError(null);
          await refreshGenerationStatusRef.current(
            sessionId,
            messages,
            currentSessionRef.current,
            { expectedLoadGeneration: generation },
          );
        };
        const timeoutAt = Date.now() + 300_000;
        while (Date.now() < timeoutAt) {
          const waited = await waitForRecoveryDelay(
            SESSION_LOAD_RECOVERY_DELAY_MS,
          );
          if (!waited || !ownsRecovery()) return;
          const messages = await refreshPersistedMessages(sessionId, {
            since: cachedMessages?.serverTime ?? null,
            trigger: "poll",
            baseMessages: cachedMessages?.messages,
          });
          if (!ownsRecovery()) return;
          if (messages !== null) {
            await applyRecoveredMessages(messages);
            return;
          }
          const error = getRefreshError(sessionId, generation);
          if (error && !isTransientHistoryError(error)) {
            return;
          }
        }

        // Keep one final body fetch after the bounded recovery window.
        const messages = await refreshPersistedMessages(sessionId, {
          since: cachedMessages?.serverTime ?? null,
          trigger: "poll",
          baseMessages: cachedMessages?.messages,
        });
        if (ownsRecovery() && messages !== null) {
          await applyRecoveredMessages(messages);
        } else if (ownsRecovery()) {
          const error = getRefreshError(sessionId, generation);
          if (error && !isTransientHistoryError(error)) {
            return;
          }
        }
      })();
      initialBodyRecoveryRef.current = { sessionId, generation, promise };
      promise.finally(() => {
        if (initialBodyRecoveryRef.current?.promise === promise) {
          initialBodyRecoveryRef.current = null;
        }
      });
      return promise;
    },
    [
      activeSessionIdRef,
      bumpSessionForAssistant,
      dispatchGeneration,
      getRefreshError,
      refreshPersistedMessages,
      setSessionLoadError,
      waitForRecoveryDelay,
    ],
  );

  const recoverInitialSessionAndMessages = useCallback(
    (
      sessionId: string,
      generation: number,
      cachedMessages: Awaited<ReturnType<typeof readCachedMessages>>,
    ): Promise<void> => {
      const existing = initialSessionRecoveryRef.current;
      if (
        existing?.sessionId === sessionId &&
        existing.generation === generation
      ) {
        return existing.promise;
      }
      const ownsRecovery = () =>
        mountedRef.current &&
        loadGenerationRef.current === generation &&
        activeSessionIdRef.current === sessionId;
      const promise = (async () => {
        const timeoutAt = Date.now() + 300_000;
        let lastError: unknown = null;
        let resumedSession: Awaited<ReturnType<typeof chatApi.resumeSession>> | null =
          null;
        while (Date.now() < timeoutAt) {
          const waited = await waitForRecoveryDelay(
            SESSION_LOAD_RECOVERY_DELAY_MS,
          );
          if (!waited || !ownsRecovery()) return;
          try {
            if (!resumedSession) {
              resumedSession = await chatApi.resumeSession(sessionId, false);
            }
            if (!ownsRecovery()) return;
            resumeApplyGateRef.current = {
              sessionId,
              generation,
              ready: true,
            };
            const messages = await refreshPersistedMessages(sessionId, {
              since: cachedMessages?.serverTime ?? null,
              trigger: "poll",
              baseMessages: cachedMessages?.messages,
            });
            if (!ownsRecovery()) return;
            if (messages === null) {
              lastError = getRefreshError(sessionId, generation);
              if (lastError && !isTransientHistoryError(lastError)) {
                return;
              }
              continue;
            }
            upsertSession(resumedSession.session);
            safeLocalStorageSetItem(LAST_SESSION_KEY, sessionId);
            setSessionLoadError(null);
            await refreshGenerationStatusRef.current(
              sessionId,
              messages,
              resumedSession.session,
              { expectedLoadGeneration: generation },
            );
            return;
          } catch (error) {
            lastError = error;
            if (!isTransientHistoryError(error)) {
              if (ownsRecovery()) {
                if (isPermanentHistoryError(error)) {
                  await clearPermanentHistoryState(sessionId, generation, error);
                } else {
                  dispatchChatTimeline({ type: "clear" });
                  setSessionLoadError(sessionLoadErrorMessage(error));
                }
              }
              return;
            }
          }
        }

        // Keep one final resume/body attempt after the bounded recovery window.
        try {
          if (!resumedSession) {
            resumedSession = await chatApi.resumeSession(sessionId, false);
          }
          if (!ownsRecovery()) return;
          resumeApplyGateRef.current = {
            sessionId,
            generation,
            ready: true,
          };
          const messages = await refreshPersistedMessages(sessionId, {
            since: cachedMessages?.serverTime ?? null,
            trigger: "poll",
            baseMessages: cachedMessages?.messages,
          });
          if (ownsRecovery() && messages !== null) {
            upsertSession(resumedSession.session);
            setSessionLoadError(null);
            await refreshGenerationStatusRef.current(
              sessionId,
              messages,
              resumedSession.session,
              { expectedLoadGeneration: generation },
            );
          } else if (ownsRecovery() && lastError) {
            if (isPermanentHistoryError(lastError)) {
              await clearPermanentHistoryState(sessionId, generation, lastError);
            } else {
              dispatchChatTimeline({ type: "clear" });
              setSessionLoadError(sessionLoadErrorMessage(lastError));
            }
          }
        } catch (error) {
          if (ownsRecovery()) {
            if (isPermanentHistoryError(error)) {
              await clearPermanentHistoryState(sessionId, generation, error);
            } else {
              dispatchChatTimeline({ type: "clear" });
              setSessionLoadError(sessionLoadErrorMessage(error));
            }
          }
        }
      })();
      initialSessionRecoveryRef.current = { sessionId, generation, promise };
      promise.finally(() => {
        if (initialSessionRecoveryRef.current?.promise === promise) {
          initialSessionRecoveryRef.current = null;
        }
      });
      return promise;
    },
    [
      activeSessionIdRef,
      clearPermanentHistoryState,
      dispatchChatTimeline,
      getRefreshError,
      refreshPersistedMessages,
      setSessionLoadError,
      upsertSession,
      waitForRecoveryDelay,
    ],
  );

  const refreshGenerationStatus = useCallback(
    async (
      sessionId: string,
      knownMessages: ConversationMessage[],
      titleSession?: ConversationSession | null,
      options?: {
        skipBodyRecovery?: boolean;
        expectedLoadGeneration?: number;
      },
    ) => {
      const expectedLoadGeneration = options?.expectedLoadGeneration;
      if (
        !mountedRef.current ||
        activeSessionIdRef.current !== sessionId ||
        (expectedLoadGeneration !== undefined &&
          loadGenerationRef.current !== expectedLoadGeneration)
      ) {
        return null;
      }
      const loadGeneration = expectedLoadGeneration ?? loadGenerationRef.current;
      const statusGeneration = ++statusRequestGenerationRef.current;
      try {
        const status = await requestGenerationStatusWithRetry(
          sessionId,
          statusGeneration,
        );
        // A session switch or unmount can invalidate a retry while it is
        // sleeping. Do not interpret the result after that ownership fence.
        if (
          !status ||
          !mountedRef.current ||
          loadGenerationRef.current !== loadGeneration ||
          activeSessionIdRef.current !== sessionId
        ) {
          return null;
        }
        dispatchGeneration({
          type: "status_restored",
          sessionId,
          status,
          eventId: `poll:status:${sessionId}:${status.updated_at ?? statusGeneration}`,
        });
        if (status.running && !options?.skipBodyRecovery) {
          const knownAssistantIds = new Set(
            knownMessages
              .filter((message) => message.role === "assistant")
              .map((message) => message.id),
          );
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            titleSession,
          );
        } else if (status.status !== "idle" && !options?.skipBodyRecovery) {
          const knownAssistantIds = new Set(
            knownMessages
              .filter((message) => message.role === "assistant")
              .map((message) => message.id),
          );
          void waitForTerminalPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
          );
        }
        return status;
      } catch (err) {
        console.warn("生成状態の復元に失敗しました", err);
        if (
          mountedRef.current &&
          loadGenerationRef.current === loadGeneration &&
          statusRequestGenerationRef.current === statusGeneration &&
          activeSessionIdRef.current === sessionId
        ) {
          dispatchGeneration({ type: "hydration_failed", sessionId });
          if (isTransientHistoryError(err)) {
            const knownAssistantIds = new Set(
              knownMessages
                .filter((message) => message.role === "assistant")
                .map((message) => message.id),
            );
            if (!options?.skipBodyRecovery) {
              void waitForPersistedAssistantResponse(
                sessionId,
                knownAssistantIds,
                titleSession,
              );
            }
          }
        }
        return null;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      dispatchGeneration,
      refreshPersistedMessages,
      requestGenerationStatusWithRetry,
      waitForPersistedAssistantResponse,
      waitForTerminalPersistedAssistantResponse,
    ],
  );
  refreshGenerationStatusRef.current = refreshGenerationStatus;

  // ─── セッション選択時にメッセージを取得 ───
  useEffect(() => {
    const loadKey = activeSessionId
      ? `${activeSessionId}:${sessionLoadAttempt}`
      : `__none__:${sessionLoadAttempt}`;
    // React StrictMode replays passive effects. The first setup owns the
    // request; replaying the same key must not start another load (or cancel
    // the first request before its promise continuation runs).
    if (loadKeyRef.current === loadKey) return;
    loadKeyRef.current = loadKey;
    const loadGeneration = ++loadGenerationRef.current;
    // A new load generation owns all recovery delays. StrictMode replay uses
    // the same key and returns above, while A→B→A invalidates old timers here.
    cancelRecoveryTimers();
    pendingReconnectRef.current = null;
    resumeApplyGateRef.current = {
      sessionId: activeSessionId,
      generation: loadGeneration,
      ready: !activeSessionId,
    };
    if (activeSessionId) {
      deferredRefreshResultsRef.current.delete(
        refreshStateKey(activeSessionId, loadGeneration),
      );
    }

    // A→B→Aのような再選択でも、最初のA向け遅延responseを失効させる。
    statusRequestGenerationRef.current += 1;
    const previousActiveSessionId = previousActiveSessionIdRef.current;
    previousActiveSessionIdRef.current = activeSessionId;
    if (!activeSessionId) {
      initialLoadStateRef.current = {
        sessionId: null,
        generation: loadGeneration,
        inFlight: false,
      };
      if (previousActiveSessionId !== undefined) {
        dispatchChatTimeline({ type: "clear" });
      }
      dispatchGeneration({ type: "reset", sessionId: null });
      setIsLoadingMessages(false);
      setSessionLoadError(null);
      setSteeringInstructions([]);
      return;
    }

    const ownsLoad = () =>
      mountedRef.current &&
      loadGenerationRef.current === loadGeneration &&
      activeSessionIdRef.current === activeSessionId;
    initialLoadStateRef.current = {
      sessionId: activeSessionId,
      generation: loadGeneration,
      inFlight: true,
    };
    dispatchGeneration({ type: "reset", sessionId: activeSessionId });
    setIsLoadingMessages(true);
    setSessionLoadError(null);
    // 現在のセッションのtempメッセージのみ保持（別セッションのものはクリア）
    dispatchChatTimeline({
      type: "keep_transient_for_session",
      sessionId: activeSessionId,
    });
    setSteeringInstructions([]);

    (async () => {
      let loadReadyForReconnect = false;
      // キャッシュ済みメッセージがあれば即描画（低帯域配慮）。
      // resume は履歴本文を含めず、続く messages GET で差分だけを取得する。
      let cachedMessages:
        | Awaited<ReturnType<typeof readCachedMessages>>
        | undefined;
      try {
        cachedMessages = await readCachedMessages(activeSessionId);
        if (
          ownsLoad() &&
          cachedMessages &&
          cachedMessages.messages.length > 0
        ) {
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: activeSessionId,
            messages: cachedMessages.messages,
          });
          setLastServerTime(activeSessionId, cachedMessages.serverTime);
          setIsLoadingMessages(false);
        }
      } catch {
        // キャッシュ読み出し失敗は無視して通常ロードへ。
      }

      try {
        if (!ownsLoad()) return;
        // Start the shared body request before resume. This registers the
        // initial owner in the refresh coordinator so a reconnect or a
        // websocket event can join it instead of issuing a second GET.
        const messagesPromise = refreshInitialMessagesWithRecovery(
          activeSessionId,
          loadGeneration,
          cachedMessages,
        );
        const data = await chatApi.resumeSession(activeSessionId, false);
        if (!data || !ownsLoad()) return;

        let messagesForDisplay = await messagesPromise;
        if (!ownsLoad()) return;
        const latestBodyError = getRefreshError(activeSessionId, loadGeneration);
        if (latestBodyError && isPermanentHistoryError(latestBodyError)) {
          throw latestBodyError;
        }
        const deferredResultKey = refreshStateKey(activeSessionId, loadGeneration);
        const deferredResult = deferredRefreshResultsRef.current.get(
          deferredResultKey,
        );
        if (deferredResult) {
          messagesForDisplay = deferredResult.messages;
          deferredRefreshResultsRef.current.delete(deferredResultKey);
        }
        resumeApplyGateRef.current = {
          sessionId: activeSessionId,
          generation: loadGeneration,
          ready: true,
        };
        let bodyRecoveryNeeded = false;
        if (messagesForDisplay === null) {
          const bodyError = getRefreshError(activeSessionId, loadGeneration);
          // Authorization/resource failures must never silently fall back to
          // a cached conversation from an earlier access grant.
          if (bodyError && !isTransientHistoryError(bodyError)) {
            throw bodyError;
          }
          bodyRecoveryNeeded = true;
          messagesForDisplay = cachedMessages?.messages ?? [];
          if (cachedMessages) {
            console.warn(
              "メッセージ差分の取得に失敗したためキャッシュを表示します:",
              bodyError,
            );
          } else {
            dispatchChatTimeline({
              type: "hydrate_persisted",
              sessionId: activeSessionId,
              messages: [],
            });
            setSessionLoadError(
              sessionLoadErrorMessage(
                bodyError ?? new Error("保存済みメッセージの再取得に失敗しました"),
              ),
            );
          }
        }
        if (!ownsLoad()) return;

        upsertSession(data.session);
        loadReadyForReconnect = true;
        safeLocalStorageSetItem(LAST_SESSION_KEY, activeSessionId);
        const serverTime =
          deferredResult?.serverTime ??
          getRefreshServerTime(activeSessionId, loadGeneration);
        const nextServerTime =
          serverTime !== undefined
            ? serverTime
            : cachedMessages?.serverTime ?? deriveServerTime(messagesForDisplay);
        // The initial owner defers application until resume succeeds. This
        // gate prevents a body response from revealing a session whose resume
        // request is still unauthorized or unavailable.
        dispatchChatTimeline({
          type: "hydrate_persisted",
          sessionId: activeSessionId,
          messages: messagesForDisplay,
        });
        setLastServerTime(activeSessionId, nextServerTime);
        void writeCachedMessages(
          activeSessionId,
          messagesForDisplay,
          nextServerTime,
        );
        await refreshGenerationStatus(
          activeSessionId,
          messagesForDisplay,
          data.session,
          {
            ...(bodyRecoveryNeeded ? { skipBodyRecovery: true } : {}),
            expectedLoadGeneration: loadGeneration,
          },
        );
        if (bodyRecoveryNeeded) {
          void recoverInitialPersistedMessages(
            activeSessionId,
            loadGeneration,
            messagesForDisplay,
            cachedMessages,
          );
        }
      } catch (err) {
        console.error("セッション再開失敗:", err);
        if (ownsLoad()) {
          if (safeLocalStorageGetItem(LAST_SESSION_KEY) === activeSessionId) {
            safeLocalStorageRemoveItem(LAST_SESSION_KEY);
          }
          setSessionLoadError(sessionLoadErrorMessage(err));
          if (isTransientHistoryError(err)) {
            if (!cachedMessages) {
              dispatchChatTimeline({ type: "clear" });
            }
            void recoverInitialSessionAndMessages(
              activeSessionId,
              loadGeneration,
              cachedMessages,
            );
          } else if (isPermanentHistoryError(err)) {
            await clearPermanentHistoryState(
              activeSessionId,
              loadGeneration,
              err,
            );
          } else {
            dispatchChatTimeline({ type: "clear" });
          }
        }
      } finally {
        if (ownsLoad()) {
          initialLoadStateRef.current = {
            sessionId: activeSessionId,
            generation: loadGeneration,
            inFlight: false,
          };
          const pendingReconnect = pendingReconnectRef.current;
          if (
            loadReadyForReconnect &&
            pendingReconnect?.sessionId === activeSessionId &&
            pendingReconnect.generation === loadGeneration
          ) {
            pendingReconnectRef.current = null;
            const reconnectGeneration = loadGeneration;
            void (async () => {
              const refreshed = await refreshPersistedMessages(activeSessionId, {
                trigger: "reconnect",
              });
              if (!ownsLoad()) return;
              await refreshGenerationStatus(
                activeSessionId,
                refreshed ?? messagesRef.current,
                currentSessionRef.current,
                { expectedLoadGeneration: reconnectGeneration },
              );
            })();
          }
          setIsLoadingMessages(false);
        }
      }
    })();

    return () => {
      statusRequestGenerationRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
      activeSessionId,
      getRefreshError,
      getRefreshServerTime,
      refreshInitialMessagesWithRecovery,
      refreshGenerationStatus,
      recoverInitialPersistedMessages,
      recoverInitialSessionAndMessages,
      dispatchGeneration,
      router,
      sessionLoadAttempt,
      upsertSession,
  ]);

  useEffect(() => {
    const previousConnection = connectionStateRef.current;
    connectionStateRef.current = { sessionId: activeSessionId, isConnected };
    const sessionId = activeSessionIdRef.current ?? activeSessionId;
    if (previousConnection?.sessionId !== sessionId) {
      skipNextConnectionRecoveryRef.current = sessionId;
    }
    if (!sessionId || !isConnected) return;
    const firstConnectionForSession = !connectedSessionIdsRef.current.has(
      sessionId,
    );
    connectedSessionIdsRef.current.add(sessionId);
    if (skipNextConnectionRecoveryRef.current === sessionId) {
      skipNextConnectionRecoveryRef.current = null;
      return;
    }
    // Initial hydration owns the first body GET. Reconnect recovery is only
    // needed for a real false -> true transition of the same session.
    if (
      firstConnectionForSession ||
      !previousConnection ||
      previousConnection.sessionId !== sessionId ||
      previousConnection.isConnected
    ) {
      return;
    }
    const initialLoad = initialLoadStateRef.current;
    if (
      initialLoad.inFlight &&
      initialLoad.sessionId === sessionId &&
      initialLoad.generation === loadGenerationRef.current
    ) {
      pendingReconnectRef.current = {
        sessionId,
        generation: loadGenerationRef.current,
      };
      return;
    }
    const reconnectGeneration = loadGenerationRef.current;
    void (async () => {
      const refreshed = await refreshPersistedMessages(sessionId, {
        trigger: "reconnect",
      });
      if (
        !mountedRef.current ||
        activeSessionIdRef.current !== sessionId ||
        loadGenerationRef.current !== reconnectGeneration
      ) {
        return;
      }
      await refreshGenerationStatus(
        sessionId,
        refreshed ?? messagesRef.current,
        currentSessionRef.current,
        { expectedLoadGeneration: reconnectGeneration },
      );
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeSessionId,
    activeSessionIdRef,
    isConnected,
    refreshGenerationStatus,
    refreshPersistedMessages,
  ]);

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
    bumpSessionForAssistant,
  };
}
