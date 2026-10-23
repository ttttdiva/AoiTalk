"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { ChatApiError, chatApi } from "@/lib/chat-api";
import type {
  ChatCommandCapability,
  ChatResponseModelSelection,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import {
  attachmentsFromMessageMetadata,
  createLocalMessage,
  createLocalUserMessage,
} from "@/lib/chat-local-messages";
import { getMessageClientId } from "@/lib/chat-state";
import { normalizeRuntimeProfileId } from "@/lib/agent-runtime-selection";
import {
  createChatSendOutboxRecord,
  deleteChatSendOutboxRecord,
  getChatSendOutboxRecord,
  listChatSendOutboxRecords,
  outboxAttachmentMetadata,
  requestChatSendRestore,
  saveChatSendOutboxRecord,
  updateChatSendOutboxRecord,
  type ChatSendDeliveryState,
  type ChatSendOutboxRecord,
} from "@/lib/chat-send-outbox";
import { commandCapabilitiesFromMessageMetadata } from "@/lib/chat-commands";
import { loadStoredPlanningPolicy } from "@/lib/planning-policy";
import {
  CHAT_SESSION_NAVIGATION_EVENT,
  navigateChatSessionInPlace,
} from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";
import type { ChatGenerationEvent } from "@/lib/chat-generation-state";
import type {
  ChatComposerSendResult,
  ChatComposerSendOptions,
  SubmittedSteeringInstruction,
} from "@/components/chat/chat-composer";
import type { MentionItem } from "@/components/chat/mention-menu";
import type { ChatAppContextSelection } from "@/components/chat/app-context-picker";
import { useChatPersistence } from "@/hooks/use-chat-persistence";
import {
  OPTIMISTIC_NEW_CHAT_SESSION_PREFIX,
  useDeepResearchMessage,
} from "@/hooks/use-deep-research-message";
import { useGroupChatCreate } from "@/hooks/use-group-chat-create";
import { resolveMessageProjectId } from "@/lib/project-message-routing";
import {
  applyPendingNewChatLlmSettingsToSession,
  getPendingNewChatLlmSettingsSnapshot,
  hydratePendingNewChatLlmSettings,
} from "@/lib/new-chat-llm-settings-store";
import { PendingLlmHandoffError } from "@/lib/chat-session-route-handoff";
import {
  NewChatSendRouteError,
  resolveNewChatSendRoute,
} from "@/lib/new-chat-send-route";
import { awaitSessionLlmSettingsReady } from "@/lib/session-llm-settings-save-queue";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { toast } from "sonner";
import {
  DEFAULT_GENERATION_PROFILE,
  normalizeGenerationProfile,
  type GenerationProfile,
} from "@/lib/generation-profile";

const DISPATCH_FAILURE_MESSAGE =
  "応答生成を開始できませんでした。送信内容が保存されたかは確認できません。接続を確認し、会話を再読み込みしてからもう一度実行してください。";

const LLM_HANDOFF_FAILURE_MESSAGE =
  "選択した Provider / Model 設定を会話へ適用できなかったため、応答生成を開始しませんでした。Chat settings を確認してからもう一度送信してください。";

/**
 * Surface the server's sanitized 4xx envelope when an explicit runtime was
 * rejected. The caller keeps the original outbox record: no other profile and
 * no legacy fallback is ever dispatched on the user's behalf.
 */
function notifyRuntimeRejection(err: unknown, executionProfileId: string | null | undefined) {
  if (!(err instanceof ChatApiError)) return;
  if (err.status == null || err.status < 400 || err.status >= 500) return;
  const requiresCliSession =
    err.code === "cli_route_not_supported" && err.requiredIngress === "cli_session";
  if (!executionProfileId && !requiresCliSession) return;
  if (requiresCliSession) {
    toast.error(`${err.message}（必要な入口: CLIセッション）`);
    return;
  }
  toast.error(err.message || "選択した実行環境で開始できませんでした");
}

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

type SendFlight = {
  token: symbol;
  draftView: symbol | null;
  userId: string | null;
  sessionId: string | null;
  agentId: string | null;
  fingerprint: string;
};

export type PendingMessage = {
  sessionId: string;
  content: string;
  clientMessageId: string;
  /** null explicitly suppresses the selected Project for Help turns. */
  projectId?: string | null;
  files?: File[];
  mentions?: MentionItem[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  appContext?: ChatAppContextSelection | null;
  commandCapabilities?: ChatCommandCapability[];
  toolsRequired?: boolean;
  /** Runtime chosen when this request was first submitted. */
  executionProfileId?: string | null;
  /** WebSocket 再接続後の最終 dispatch 結果を composer へ通知する。 */
  settle?: (result: ChatComposerSendResult) => void;
  signal?: AbortSignal;
  dispatching?: boolean;
  setWaiting?: (waiting: boolean) => void;
};

const PENDING_DISPATCH_TIMEOUT_MS = 20_000;
const PENDING_UPLOAD_TIMEOUT_MS = 120_000;
const PENDING_ACK_TIMEOUT_MS = 30_000;

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function createSendFingerprint(input: {
  content: string;
  files?: File[];
  mentions?: MentionItem[];
  generationProfile?: string;
  commandCapabilities?: ChatCommandCapability[];
  toolsRequired?: boolean;
  appContext?: ChatAppContextSelection | null;
  effectiveProjectId?: string;
  includeProjectContext: boolean;
  executionProfileId?: string | null;
}): string {
  return JSON.stringify({
    content: input.content,
    files: input.files?.map((file) => ({
      name: file.name,
      size: file.size,
      type: file.type,
      lastModified: file.lastModified,
    })) ?? [],
    mentions: input.mentions ?? [],
    generationProfile: input.generationProfile ?? null,
    commandCapabilities: input.commandCapabilities ?? [],
    toolsRequired: input.toolsRequired ?? null,
    appContext: input.appContext ?? null,
    effectiveProjectId: input.effectiveProjectId ?? null,
    includeProjectContext: input.includeProjectContext,
    executionProfileId: input.executionProfileId ?? null,
  });
}

const UUID_MESSAGE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

function createClientSessionId(clientMessageId: string): string {
  if (UUID_MESSAGE_ID.test(clientMessageId)) return clientMessageId;
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return "00000000-0000-4000-8000-" + Math.random().toString(16).slice(2, 14).padEnd(12, "0");
}

/**
 * Resolve the mode that a branch/rerun should use without promoting it to
 * autonomous work implicitly.
 *
 * The user-message row is the most specific source.  Older rows may not have
 * a persisted profile yet, so accept the session's server-owned context as a
 * compatibility fallback and finally use the bounded chat default.  Every
 * candidate is validated against the known profile enum; arbitrary metadata
 * must never select a more privileged execution mode.
 */
export function resolveBranchGenerationProfile(
  sourceMessage: Pick<ConversationMessage, "metadata">,
  session?: Pick<ConversationSession, "context"> | null,
): GenerationProfile {
  const metadata = sourceMessage?.metadata;
  const metadataRecord =
    metadata && typeof metadata === "object"
      ? (metadata as Record<string, unknown>)
      : {};
  const generationMetrics =
    metadataRecord.generation_metrics &&
    typeof metadataRecord.generation_metrics === "object" &&
    !Array.isArray(metadataRecord.generation_metrics)
      ? (metadataRecord.generation_metrics as Record<string, unknown>)
      : {};
  const generation =
    metadataRecord.generation &&
    typeof metadataRecord.generation === "object" &&
    !Array.isArray(metadataRecord.generation)
      ? (metadataRecord.generation as Record<string, unknown>)
      : {};

  const candidates: unknown[] = [
    metadataRecord.generation_profile,
    metadataRecord.generationProfile,
    generation.generation_profile,
    generation.generationProfile,
    generation.profile,
    generationMetrics.generation_profile,
    generationMetrics.generationProfile,
  ];

  const context = session?.context;
  if (context && typeof context === "object" && !Array.isArray(context)) {
    const contextRecord = context as Record<string, unknown>;
    const chat =
      contextRecord.chat &&
      typeof contextRecord.chat === "object" &&
      !Array.isArray(contextRecord.chat)
        ? (contextRecord.chat as Record<string, unknown>)
        : {};
    const chatLlmSettings =
      contextRecord.chat_llm_settings &&
      typeof contextRecord.chat_llm_settings === "object" &&
      !Array.isArray(contextRecord.chat_llm_settings)
        ? (contextRecord.chat_llm_settings as Record<string, unknown>)
        : {};
    candidates.push(
      contextRecord.generation_profile,
      contextRecord.generationProfile,
      chat.generation_profile,
      chat.generationProfile,
      chatLlmSettings.generation_profile,
      chatLlmSettings.generationProfile,
    );
  }

  for (const candidate of candidates) {
    const normalized = normalizeGenerationProfile(candidate);
    if (normalized) return normalized;
  }
  return DEFAULT_GENERATION_PROFILE;
}

type UseChatMessagingArgs = {
  // ナビゲーション・コンテキスト値
  router: ReturnType<typeof useRouter>;
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  activateSession: (sessionId: string) => void;
  allProjects: Array<{
    id: string;
    name: string;
    slug?: string;
    aliases?: string[];
    metadata?: Record<string, unknown>;
  }>;
  effectiveProjectId: string | undefined;
  isGroupChat: boolean;
  includeProjectContext: boolean;
  deepResearchEnabled: boolean;
  sessionLoadAttempt: number;

  // 観測値
  messages: ConversationMessage[];
  messagesRef: RefObject<ConversationMessage[]>;
  currentSession: ConversationSession | null;
  isSending: boolean;
  isConnected: boolean;
  isStreaming: boolean;
  displayIsWaitingResponse: boolean;

  // refs
  responsePollGenerationRef: RefObject<number>;
  pendingMessageRef: RefObject<PendingMessage | null>;

  // Chat セッション context
  addSession: (session: ConversationSession) => void;
  upsertSession: (session: ConversationSession) => void;
  bumpSession: (sessionId: string) => void;
  updateSidebarTitle: (sessionId: string, title: string) => void;

  // WebSocket
  sendMessage: ReturnType<
    typeof import("@/hooks/use-websocket").useWebSocket
  >["sendMessage"];

  // dispatch / setters
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  markWaitingResponse: (
    sessionId: string | null,
    clientMessageId?: string | null,
  ) => void;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setIsLoadingMessages: Dispatch<SetStateAction<boolean>>;
  setSessionLoadError: Dispatch<SetStateAction<string | null>>;
  /** Pending session-scoped Agent selected on the blank Chat route. */
  newSessionAgentId?: string | null;
  onSessionAgentConsumed?: () => void;
};

/**
 * チャットのメッセージ送信・分岐・DeepResearch・グループ作成・永続化ポーリング・
 * セッションロードのロジックを担うフック。
 * `page.tsx` の該当 useCallback / useEffect を挙動不変で移設したもの。
 * 依存配列は元コードと同一に保つ。state / ref は `page.tsx` が保持し、引数で渡す。
 */
export function useChatMessaging({
  router,
  activeSessionId,
  activeSessionIdRef,
  activateSession,
  allProjects,
  effectiveProjectId,
  isGroupChat,
  includeProjectContext,
  deepResearchEnabled,
  sessionLoadAttempt,
  messages,
  messagesRef,
  currentSession,
  isSending,
  isConnected,
  isStreaming,
  displayIsWaitingResponse,
  responsePollGenerationRef,
  pendingMessageRef,
  addSession,
  upsertSession,
  bumpSession,
  updateSidebarTitle,
  sendMessage,
  dispatchChatTimeline,
  dispatchGeneration,
  markWaitingResponse,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
  newSessionAgentId = null,
  onSessionAgentConsumed,
}: UseChatMessagingArgs) {
  const draftUserId = useCurrentUserId();
  const normalizedNewSessionAgentId = newSessionAgentId?.trim() || null;
  const draftUserIdRef = useRef(draftUserId);
  draftUserIdRef.current = draftUserId;
  // React state (`isSending`) may still be stale for two same-tick composer
  // events. Keep ownership by draft/user/session scope so an old draft that
  // is still waiting for defaults cannot block a newly visible chat.
  const sendInFlightRef = useRef<SendFlight | null>(null);
  // The in-memory copy is only a fast path. The durable source is the
  // user-scoped IndexedDB outbox; keeping the original client id here lets a
  // retry reuse it even while a previous async attempt is settling.
  const outboxRecordsRef = useRef<Map<string, ChatSendOutboxRecord>>(new Map());
  const reconciledClientMessageIdsRef = useRef<Set<string>>(new Set());
  const ackDeadlineTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // A session id alone cannot distinguish draft -> B -> a new draft, or
  // navigating away and Back. Every committed view lifecycle owns its work.
  const draftViewRef = useRef<symbol | null>(null);
  useLayoutEffect(() => {
    draftViewRef.current = Symbol("chat-draft-view");
    const invalidateDraftView = () => {
      draftViewRef.current = Symbol("chat-navigation");
    };
    window.addEventListener(CHAT_SESSION_NAVIGATION_EVENT, invalidateDraftView);
    window.addEventListener("popstate", invalidateDraftView);
    return () => {
      draftViewRef.current = null;
      window.removeEventListener(CHAT_SESSION_NAVIGATION_EVENT, invalidateDraftView);
      window.removeEventListener("popstate", invalidateDraftView);
    };
  }, [activeSessionId, draftUserId, normalizedNewSessionAgentId]);
  const {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
    bumpSessionForAssistant,
  } = useChatPersistence({
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
  });
  const branchSwitchInFlightRef = useRef<string | null>(null);

  const updateClientDeliveryState = useCallback(
    (
      sessionId: string,
      clientMessageId: string,
      state: ChatSendDeliveryState,
      error?: string,
      attachmentsReselectRequired = false,
    ) => {
      dispatchChatTimeline({
        type: "update_by_client_message_id",
        sessionId,
        clientMessageId,
        update: (message) => ({
          ...message,
          client_message_id: clientMessageId,
          metadata: {
            ...message.metadata,
            client_message_id: clientMessageId,
            delivery_state: state,
            ...(error ? { delivery_error: error } : {}),
            ...(attachmentsReselectRequired
              ? { attachments_reselect_required: true }
              : {}),
          },
        }),
      });
    },
    [dispatchChatTimeline],
  );

  const saveOutboxForSubmission = useCallback(
    (
      clientMessageId: string,
      sessionId: string,
      content: string,
      files?: File[],
      mentions?: MentionItem[],
      commandCapabilities?: ChatCommandCapability[],
      generationProfile?: string,
      projectId?: string | null,
      includeProjectContext?: boolean,
      appContext?: ChatAppContextSelection | null,
      toolsRequired?: boolean,
      executionProfileId?: string | null,
    ) => {
      const userId = draftUserIdRef.current;
      if (!userId) return;
      const record = createChatSendOutboxRecord({
        userId,
        clientMessageId,
        sessionId,
        content,
        files,
        mentions,
        commandCapabilities,
        generationProfile,
        projectId,
        includeProjectContext,
        appContext,
        toolsRequired,
        executionProfileId,
      });
      outboxRecordsRef.current.set(clientMessageId, record);
      // Persist without delaying the optimistic append or route lookup.
      void saveChatSendOutboxRecord(record);
    },
    [],
  );

  const settleOutbox = useCallback(
    (
      sessionId: string,
      clientMessageId: string,
      state: ChatSendDeliveryState,
      error?: string,
      attachmentsReselectRequired = false,
    ) => {
      const ackTimer = ackDeadlineTimersRef.current.get(clientMessageId);
      if (ackTimer) {
        clearTimeout(ackTimer);
        ackDeadlineTimersRef.current.delete(clientMessageId);
      }
      if (
        state !== "accepted" &&
        reconciledClientMessageIdsRef.current.has(clientMessageId)
      ) {
        // History/ACK already proved delivery. A late transport rejection
        // must not downgrade that row or recreate its outbox record.
        updateClientDeliveryState(sessionId, clientMessageId, "accepted");
        return;
      }
      if (state === "accepted") {
        const ownerUserId =
          outboxRecordsRef.current.get(clientMessageId)?.userId ??
          draftUserIdRef.current;
        outboxRecordsRef.current.delete(clientMessageId);
        void deleteChatSendOutboxRecord(ownerUserId, clientMessageId);
      } else {
        const record = outboxRecordsRef.current.get(clientMessageId);
        if (record) {
          const next = {
            ...record,
            sessionId,
            status: state,
            ...(error ? { error } : {}),
            updatedAt: Date.now(),
          };
          outboxRecordsRef.current.set(clientMessageId, next);
          void saveChatSendOutboxRecord(next);
        } else if (draftUserIdRef.current) {
          void updateChatSendOutboxRecord(draftUserIdRef.current, clientMessageId, {
            sessionId,
            status: state,
            error,
          });
        }
      }
      updateClientDeliveryState(
        sessionId,
        clientMessageId,
        state,
        error,
        attachmentsReselectRequired,
      );
    },
    [updateClientDeliveryState],
  );

  const scheduleAckDeadline = useCallback(
    (
      sessionId: string,
      clientMessageId: string,
      delayMs = PENDING_ACK_TIMEOUT_MS,
    ) => {
      const previous = ackDeadlineTimersRef.current.get(clientMessageId);
      if (previous) clearTimeout(previous);
      const timer = setTimeout(() => {
        ackDeadlineTimersRef.current.delete(clientMessageId);
        if (reconciledClientMessageIdsRef.current.has(clientMessageId)) return;
        settleOutbox(
          sessionId,
          clientMessageId,
          "uncertain",
          "送信結果を確認できませんでした。会話を再読み込みするか、同じ内容で再試行してください。",
        );
      }, Math.max(1, delayMs));
      ackDeadlineTimersRef.current.set(clientMessageId, timer);
    },
    [settleOutbox],
  );

  useEffect(
    () => () => {
      for (const timer of ackDeadlineTimersRef.current.values()) {
        clearTimeout(timer);
      }
      ackDeadlineTimersRef.current.clear();
    },
    [],
  );

  // Rehydrate unresolved sends after a reload/navigation. A persisted server
  // row wins immediately when its stable client id is present, so the outbox
  // record is removed and cannot create a duplicate delivered bubble.
  useEffect(() => {
    let cancelled = false;
    void listChatSendOutboxRecords(draftUserId).then((records) => {
      if (cancelled) return;
      for (const record of records) {
        const agedPending =
          record.status === "pending" &&
          Date.now() - record.updatedAt >= PENDING_ACK_TIMEOUT_MS;
        const effectiveRecord = agedPending
          ? {
              ...record,
              status: "uncertain" as const,
              error:
                "送信結果を確認できませんでした。会話を再読み込みするか、同じ内容で再試行してください。",
              updatedAt: Date.now(),
            }
          : record;
        if (agedPending) {
          outboxRecordsRef.current.set(effectiveRecord.clientMessageId, effectiveRecord);
          void saveChatSendOutboxRecord(effectiveRecord);
        }
        const matching = messagesRef.current.find(
          (message) => getMessageClientId(message) === effectiveRecord.clientMessageId,
        );
        if (matching && !matching.id.startsWith("temp-")) {
          void deleteChatSendOutboxRecord(draftUserId, effectiveRecord.clientMessageId);
          continue;
        }
        const provisional = effectiveRecord.sessionId.startsWith(
          OPTIMISTIC_NEW_CHAT_SESSION_PREFIX,
        );
        const visible = activeSessionId
          ? effectiveRecord.sessionId === activeSessionId
          : provisional;
        if (!visible) continue;
        outboxRecordsRef.current.set(effectiveRecord.clientMessageId, effectiveRecord);
        if (effectiveRecord.status === "pending") {
          scheduleAckDeadline(
            effectiveRecord.sessionId,
            effectiveRecord.clientMessageId,
            PENDING_ACK_TIMEOUT_MS -
              Math.max(0, Date.now() - effectiveRecord.updatedAt),
          );
        }
        const files = effectiveRecord.files?.filter(
          (file): file is File => typeof File !== "undefined" && file instanceof File,
        );
        const message = createLocalUserMessage(
          effectiveRecord.sessionId,
          effectiveRecord.content,
          effectiveRecord.clientMessageId,
          files,
          effectiveRecord.commandCapabilities,
          {
            delivery_state: effectiveRecord.status,
            ...(effectiveRecord.error ? { delivery_error: effectiveRecord.error } : {}),
            ...(effectiveRecord.attachments.length > 0 && !files?.length
              ? {
                  attachments: outboxAttachmentMetadata(effectiveRecord),
                  attachments_reselect_required: true,
                }
              : {}),
          },
        );
        dispatchChatTimeline({ type: "append", message });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [
    activeSessionId,
    dispatchChatTimeline,
    draftUserId,
    messagesRef,
    scheduleAckDeadline,
  ]);

  // A WebSocket send returning true only means that a frame was handed to the
  // browser socket. Reconcile that pending row when the authoritative history
  // later contains the same client id. The idempotency key makes this safe
  // across refreshes and late ACK/history races.
  useEffect(() => {
    for (const message of messages) {
      if (message.id.startsWith("temp-")) continue;
      const clientMessageId = getMessageClientId(message);
      if (!clientMessageId || reconciledClientMessageIdsRef.current.has(clientMessageId)) {
        continue;
      }
      const deliveryState = message.metadata?.delivery_state;
      const isKnownUnresolvedSend =
        outboxRecordsRef.current.has(clientMessageId) ||
        deliveryState === "pending" ||
        deliveryState === "uncertain" ||
        deliveryState === "failed";
      if (!isKnownUnresolvedSend) continue;
      reconciledClientMessageIdsRef.current.add(clientMessageId);
      const ownerUserId =
        outboxRecordsRef.current.get(clientMessageId)?.userId ??
        draftUserIdRef.current;
      const ackTimer = ackDeadlineTimersRef.current.get(clientMessageId);
      if (ackTimer) {
        clearTimeout(ackTimer);
        ackDeadlineTimersRef.current.delete(clientMessageId);
      }
      outboxRecordsRef.current.delete(clientMessageId);
      void deleteChatSendOutboxRecord(ownerUserId, clientMessageId);
      if (deliveryState !== "accepted") {
        updateClientDeliveryState(message.session_id, clientMessageId, "accepted");
      }
    }
  }, [messages, updateClientDeliveryState]);

  const {
    handleDeepResearchMessage,
    retryDeepResearchAssistantPersistence,
  } = useDeepResearchMessage({
    router,
    activeSessionId,
    activeSessionIdRef,
    activateSession,
    includeProjectContext,
    addSession,
    bumpSession,
    updateSidebarTitle,
    dispatchChatTimeline,
    dispatchGeneration,
    upsertSession,
    newSessionAgentId: normalizedNewSessionAgentId,
    onSessionAgentConsumed,
  });

  const dispatchMessageWithoutWebSocket = useCallback(
    async (
      sessionId: string,
      content: string,
      projectId?: string,
      generationProfile?: string,
      responseModel?: ChatResponseModelSelection,
      clientMessageId?: string,
      commandCapabilities?: ChatCommandCapability[],
      persistence?: {
        skipUserPersistence?: boolean;
        persistedUserMessageId?: string;
      },
      toolsRequired?: boolean,
      appContext?: ChatAppContextSelection | null,
      mentions?: MentionItem[],
      includeProjectContextOverride?: boolean,
      executionProfileId?: string | null,
    ) => {
      const helpSubmission = commandCapabilities?.includes("aoitalk_help") === true;
      const result = await chatApi.dispatchMessage(sessionId, {
        message: content,
        project_id: projectId,
        app_id: helpSubmission ? null : appContext?.appId ?? null,
        app_target_id: helpSubmission ? null : appContext?.targetId ?? null,
        generation_profile: generationProfile,
        planning_policy: loadStoredPlanningPolicy(
          typeof window !== "undefined" ? window.localStorage : null,
        ),
        include_project_context: helpSubmission
          ? false
          : includeProjectContextOverride ?? includeProjectContext,
        response_model: responseModel,
        client_message_id: clientMessageId,
        command_capabilities: commandCapabilities,
        tools_required: toolsRequired,
        skip_user_persistence: persistence?.skipUserPersistence,
        persisted_user_message_id: persistence?.persistedUserMessageId,
        mentions,
        ...(executionProfileId?.trim() ? { execution_profile_id: executionProfileId.trim() } : {}),
        ...(currentSession?.agent_id || normalizedNewSessionAgentId
          ? { expected_agent_id: currentSession?.agent_id ?? normalizedNewSessionAgentId }
          : {}),
      });
      if (clientMessageId && result.user_message_id) {
        dispatchChatTimeline({
          type: "promote_client_message",
          sessionId,
          clientMessageId,
          serverMessageId: result.user_message_id,
        });
        settleOutbox(sessionId, clientMessageId, "accepted");
      }
      if (result.agent_run_id) {
        dispatchGeneration({
          type: "dispatch_accepted",
          sessionId,
          clientMessageId,
          agentRunId: result.agent_run_id,
          statusMessage: "応答をキューに追加しました",
        });
      }
      bumpSession(sessionId);
      return result;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      bumpSession,
      currentSession?.agent_id,
      dispatchGeneration,
      includeProjectContext,
      normalizedNewSessionAgentId,
      settleOutbox,
    ],
  );

  const createDispatchFailureMessage = useCallback(
    (sessionId: string) =>
      createLocalMessage(sessionId, "assistant", DISPATCH_FAILURE_MESSAGE),
    [],
  );


  const [pendingVersion, setPendingVersion] = useState(0);
  const connectionRevisionRef = useRef(0);
  useLayoutEffect(() => { connectionRevisionRef.current += 1; }, [activeSessionId, isConnected]);

  // Only one attempt may own a pending client_message_id. The queue's abort
  // signal fences a slow attachment upload after timeout/navigation/unmount.
  useEffect(() => {
    const pending = pendingMessageRef.current;
    if (!isConnected || !pending || pending.sessionId !== activeSessionId) return;
    if (pending.dispatching) return;
    const connectionRevision = connectionRevisionRef.current;
    pending.dispatching = true;
    pending.setWaiting?.(false);
    void (async () => {
      try {
        const accepted = await sendMessage(
          pending.content,
          pending.projectId === null ? undefined : pending.projectId ?? effectiveProjectId,
          pending.files, pending.mentions, pending.generationProfile,
          loadStoredPlanningPolicy(typeof window !== "undefined" ? window.localStorage : null),
          pending.includeProjectContext, undefined, undefined,
          pending.sessionId, pending.clientMessageId, pending.commandCapabilities,
          pending.toolsRequired,
          pending.appContext ? { appId: pending.appContext.appId, targetId: pending.appContext.targetId } : null,
          pending.signal, pending.executionProfileId,
        );
        if (!accepted || pending.signal?.aborted || pendingMessageRef.current !== pending) return;
        pendingMessageRef.current = null;
        markWaitingResponse(pending.sessionId, pending.clientMessageId);
        pending.settle?.("accepted");
      } catch {
        if (pendingMessageRef.current === pending) pending.settle?.("failed");
      } finally {
        pending.dispatching = false;
        if (!pending.signal?.aborted && pendingMessageRef.current === pending) pending.setWaiting?.(true);
        if (connectionRevisionRef.current !== connectionRevision && !pending.signal?.aborted && pendingMessageRef.current === pending) {
          setPendingVersion((version) => version + 1);
        }
      }
    })();
  }, [activeSessionId, effectiveProjectId, isConnected, markWaitingResponse,
    pendingMessageRef, pendingVersion, sendMessage]);

  useEffect(() => () => {
    pendingMessageRef.current?.settle?.("failed");
  }, [pendingMessageRef]);

  const { handleCreateGroupChat } = useGroupChatCreate({
    router,
    activateSession,
    addSession,
    upsertSession,
    dispatchChatTimeline,
  });

  const queuePendingMessage = useCallback(
    (message: Omit<PendingMessage, "settle" | "signal" | "dispatching" | "setWaiting">): Promise<ChatComposerSendResult> =>
      new Promise((resolve) => {
        pendingMessageRef.current?.settle?.("failed");
        const controller = new AbortController();
        const deadline = Date.now() + PENDING_DISPATCH_TIMEOUT_MS + PENDING_UPLOAD_TIMEOUT_MS;
        let settled = false;
        let timeout: ReturnType<typeof setTimeout>;
        const pending: PendingMessage = {
          ...message,
          signal: controller.signal,
          setWaiting: (waiting) => {
            clearTimeout(timeout);
            timeout = setTimeout(() => pending.settle?.("failed"),
              Math.min(waiting ? PENDING_DISPATCH_TIMEOUT_MS : PENDING_UPLOAD_TIMEOUT_MS,
                Math.max(0, deadline - Date.now())));
          },
          settle: (result) => {
            if (settled) return;
            settled = true;
            clearTimeout(timeout);
            controller.abort();
            if (pendingMessageRef.current === pending) pendingMessageRef.current = null;
            if (result === "failed") {
              settleOutbox(
                pending.sessionId,
                pending.clientMessageId,
                "uncertain",
                "送信結果を確認できませんでした。会話を再読み込みするか、同じ内容で再試行してください。",
              );
              dispatchGeneration({ type: "failed", sessionId: pending.sessionId,
                clientMessageId: pending.clientMessageId, statusMessage: DISPATCH_FAILURE_MESSAGE,
                eventId: `dispatch:${pending.sessionId}:${pending.clientMessageId}:failed` });
              resolve("retained");
              return;
            }
            if (result === "accepted") {
              scheduleAckDeadline(pending.sessionId, pending.clientMessageId);
            }
            resolve(result);
          },
        };
        pending.setWaiting?.(true);
        pendingMessageRef.current = pending;
        setPendingVersion((version) => version + 1);
      }),
    [dispatchGeneration, pendingMessageRef, scheduleAckDeadline, settleOutbox],
  );

  // ─── メッセージ送信 ───
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: File[],
      mentions?: MentionItem[],
      generationProfile?: string,
      commandCapabilities?: ChatCommandCapability[],
      toolsRequired?: boolean,
      appContext?: ChatAppContextSelection | null,
      options?: ChatComposerSendOptions,
    ) => {
      const sendDraftView = draftViewRef.current;
      const sendUserId = draftUserIdRef.current ?? null;
      const sendSessionId = activeSessionIdRef.current;
      const sendAgentId = normalizedNewSessionAgentId;
      const fingerprint = createSendFingerprint({
        content,
        files,
        mentions,
        generationProfile,
        commandCapabilities,
        toolsRequired,
        appContext,
        effectiveProjectId,
        includeProjectContext,
        executionProfileId: normalizeRuntimeProfileId(options?.executionProfileId),
      });
      const submittedPendingSnapshot = sendSessionId
        ? null
        : (() => {
            hydratePendingNewChatLlmSettings(sendUserId);
            return getPendingNewChatLlmSettingsSnapshot(sendUserId);
          })();
      const currentFlight = sendInFlightRef.current;
      const sameScope =
        currentFlight?.draftView === sendDraftView &&
        currentFlight.userId === sendUserId &&
        currentFlight.sessionId === sendSessionId &&
        currentFlight.agentId === sendAgentId;
      // 連打防止。A flight from another draft/user/session is allowed to
      // finish independently and must not block the newly visible scope.
      if (sameScope) {
        return currentFlight?.fingerprint === fingerprint
          ? ("retained" as ChatComposerSendResult)
          : ("failed" as ChatComposerSendResult);
      }
      if (isSending) {
        return "failed" as ChatComposerSendResult;
      }
      const sendFlightToken = Symbol("chat-send-flight");
      sendInFlightRef.current = {
        token: sendFlightToken,
        draftView: sendDraftView,
        userId: sendUserId,
        sessionId: sendSessionId,
        agentId: sendAgentId,
        fingerprint,
      };
      try {
      const clientMessageId =
        options?.clientMessageId?.trim() || createClientMessageId();
      // A composer restore after an IndexedDB/File recovery path retains only
      // the stable client id and asks the user to reselect File bytes. Recover
      // the original immutable send options from the outbox before rebuilding
      // its record, so a current composer mode/project cannot change the
      // meaning of a retry.
      const restoredOutboxRecord = outboxRecordsRef.current.get(clientMessageId);
      // The runtime is frozen at the first submit: a retry replays the stored
      // value and never reads the composer's current selection.
      const executionProfileId = restoredOutboxRecord
        ? normalizeRuntimeProfileId(restoredOutboxRecord.executionProfileId)
        : normalizeRuntimeProfileId(options?.executionProfileId);
      if (restoredOutboxRecord) {
        content = restoredOutboxRecord.content;
        files = files?.length ? files : restoredOutboxRecord.files;
        mentions = restoredOutboxRecord.mentions;
        generationProfile = restoredOutboxRecord.generationProfile;
        commandCapabilities = restoredOutboxRecord.commandCapabilities;
        toolsRequired = restoredOutboxRecord.toolsRequired;
        appContext = restoredOutboxRecord.appContext;
      }
      const clientSessionId = createClientSessionId(clientMessageId);
      const hasCommandCapabilities = Boolean(commandCapabilities?.length);
      const isHelpSubmission = commandCapabilities?.includes("aoitalk_help") === true;
      // Deep Research returns before creating a regular-send outbox record.
      // Composer retries also carry their persisted mode; an outbox record is
      // authoritative for Chat even if a stale composer option says otherwise.
      const isDeepResearchSubmission =
        !restoredOutboxRecord &&
        !hasCommandCapabilities &&
        (options?.dispatchMode
          ? options.dispatchMode === "deep_research"
          : deepResearchEnabled);
      if (activeSessionId && !isDeepResearchSubmission) {
        dispatchGeneration({
          type: "dispatch_started",
          sessionId: activeSessionId,
          clientMessageId,
        });
      }
      const messageProjectId = isHelpSubmission
        ? undefined
        : restoredOutboxRecord
          ? restoredOutboxRecord.projectId ?? undefined
          : resolveMessageProjectId({
            content,
            projects: allProjects,
            sessionProjectId: currentSession?.project_id,
            fallbackProjectId: effectiveProjectId,
          });
      const messageIncludeProjectContext = isHelpSubmission
        ? false
        : restoredOutboxRecord
          ? restoredOutboxRecord.includeProjectContext ?? includeProjectContext
          : includeProjectContext;
      const messageAppContext = isHelpSubmission
        ? null
        : restoredOutboxRecord
          ? restoredOutboxRecord.appContext
          : appContext;
      if (isDeepResearchSubmission) {
        if (files?.length) {
          if (activeSessionId) {
            dispatchChatTimeline({
              type: "append",
              message: createLocalMessage(
                activeSessionId,
                "assistant",
                "Deep Researchではファイル添付をまだ送信できません。通常チャットに切り替えて送信してください。",
              ),
            });
          }
          if (activeSessionId) {
            dispatchGeneration({
              type: "failed",
              sessionId: activeSessionId,
              clientMessageId,
              statusMessage: "Deep Researchではファイル添付を送信できません",
              eventId: `dispatch:${activeSessionId}:${clientMessageId}:failed`,
            });
          }
          return "failed" as ChatComposerSendResult;
        }
        return (
          await handleDeepResearchMessage(
            content,
            messageProjectId,
            clientMessageId,
          )
        )
          ? ("accepted" as ChatComposerSendResult)
          : ("failed" as ChatComposerSendResult);
      }

      // セッション未選択時は自動作成（選択中のプロジェクトを紐付け）
      let sessionId = activeSessionId;
      const submittedDraftView = sendDraftView;
      const submittedHref = window.location.href;
      const submittedUserId = sendUserId;
      const submittedAgentId = sendAgentId;
      const ownsSubmittedDraftView = () =>
        submittedDraftView !== null &&
        draftViewRef.current === submittedDraftView &&
        draftUserIdRef.current === submittedUserId &&
        activeSessionIdRef.current === null &&
        window.location.href === submittedHref;
      const ownsSubmittedUserScope = () =>
        (draftUserIdRef.current ?? null) === submittedUserId;
      let generationReadyMain = null as {
        provider?: string;
        model?: string;
        effort?: string;
      } | null;
      const provisionalSessionId = `${OPTIMISTIC_NEW_CHAT_SESSION_PREFIX}${clientMessageId}`;
      let optimisticSessionId = sessionId ?? provisionalSessionId;
      const optimisticGenerationProfile = normalizeGenerationProfile(
        generationProfile,
      );

      // Register before any route/defaults/session/attachment await. The
      // record is the recovery source when an acknowledgement is ambiguous or
      // the page is reloaded while the request is in flight.
      saveOutboxForSubmission(
        clientMessageId,
        optimisticSessionId,
        content,
        files,
        mentions,
        commandCapabilities,
        generationProfile,
        isHelpSubmission ? null : messageProjectId,
        messageIncludeProjectContext,
        messageAppContext,
        toolsRequired,
        executionProfileId,
      );

      // 送信処理が REST / WebSocket / 設定保存のいずれを選ぶ場合でも、
      // 最初の server await より前にこの submission 専用の user bubble を
      // 1 件だけ追加する。新規チャットはまだ session が存在しないため、
      // provisional id で描画し、create 成功後に reducer で session id を
      // rebinding する（provisional id を active にすることはない）。
      dispatchChatTimeline({
        type: "append",
        message: createLocalUserMessage(
          optimisticSessionId,
          content,
          clientMessageId,
          files,
          commandCapabilities,
          optimisticGenerationProfile
            ? { generation_profile: optimisticGenerationProfile }
            : {},
        ),
      });

      if (!sessionId && !sendAgentId) {
        try {
          // Resolve from the captured user/draft scope after the local bubble
          // and composer clear boundary. This request may be slow, but it can
          // no longer hide the submitted turn from the user.
          generationReadyMain = (
            await resolveNewChatSendRoute(submittedUserId)
          ).route;
          if (!ownsSubmittedDraftView()) {
            settleOutbox(
              optimisticSessionId,
              clientMessageId,
              "uncertain",
              "送信中に表示が切り替わったため、送信結果を確認してください。",
            );
            return "retained" as ChatComposerSendResult;
          }
        } catch (error) {
          if (error instanceof NewChatSendRouteError) {
            toast.error(error.message);
          } else {
            toast.error(
              "Provider / Model を確定できなかったため送信できませんでした。設定を確認してから再試行してください。",
            );
          }
          settleOutbox(
            optimisticSessionId,
            clientMessageId,
            "failed",
            error instanceof Error ? error.message : "Provider / Model を確定できませんでした。",
          );
          return "retained" as ChatComposerSendResult;
        }
      }

      if (!sessionId) {
        try {
          const canPersistInitialMessage =
            !files?.length && !mentions?.length && !hasCommandCapabilities;
          const createCharacterName = await chatApi.getCurrentCharacterName();
          // The route was resolved before this server await. Check the draft
          // ownership again after character lookup so a late result can never
          // create or activate a session in a different view.
          if ((!submittedAgentId && !generationReadyMain) || !ownsSubmittedDraftView()) {
            throw new PendingLlmHandoffError(
              "送信中に新規チャットの表示が切り替わったため、送信を取り消しました。",
            );
          }
          const createInitialMessage = canPersistInitialMessage
            ? { content, client_message_id: clientMessageId }
            : undefined;
          const createAppContext = messageAppContext
            ? { appId: messageAppContext.appId, targetId: messageAppContext.targetId }
            : null;
          // Keep session creation idempotent for every new-chat shape. A
          // request can be accepted by the server while its response is lost
          // before the client learns the durable session id; retries must
          // replay the same canonical session aggregate as well as the same
          // initial message id.
          const createClientSessionIdForRequest = clientSessionId;
          const data = canPersistInitialMessage
            ? await chatApi.createSession(
                createCharacterName,
                messageProjectId,
                createInitialMessage,
                createAppContext,
                generationReadyMain,
                submittedAgentId ?? undefined,
                createClientSessionIdForRequest,
              )
            : submittedAgentId
              ? await chatApi.createSession(
                  createCharacterName,
                  messageProjectId,
                  createInitialMessage,
                  createAppContext,
                  generationReadyMain,
                  submittedAgentId,
                  createClientSessionIdForRequest,
                )
              : await chatApi.createSession(
                  createCharacterName,
                  messageProjectId,
                  createInitialMessage,
                  createAppContext,
                  generationReadyMain,
                  undefined,
                  createClientSessionIdForRequest,
                );
          if (
            submittedAgentId &&
            data.session.agent_id !== submittedAgentId
          ) {
            throw new Error(
              "サーバーでAgent bindingを確認できなかったため、応答生成を開始しませんでした。",
            );
          }
          if (!ownsSubmittedUserScope()) {
            settleOutbox(
              optimisticSessionId,
              clientMessageId,
              "uncertain",
              "表示中の利用者が変わったため、送信結果を確認してください。",
            );
            return "retained" as ChatComposerSendResult;
          }
          sessionId = data.session.id;
          const outboxRecord = outboxRecordsRef.current.get(clientMessageId);
          if (outboxRecord) {
            const reboundOutboxRecord = {
              ...outboxRecord,
              sessionId,
              updatedAt: Date.now(),
            };
            outboxRecordsRef.current.set(clientMessageId, reboundOutboxRecord);
            void saveChatSendOutboxRecord(reboundOutboxRecord);
          }
          // Registration and server dispatch belong to the submitted message;
          // activation/navigation belong only to the still-visible draft.
          addSession(data.session);
          if (submittedAgentId && data.session.agent_id === submittedAgentId && ownsSubmittedDraftView()) {
            onSessionAgentConsumed?.();
          }
          // 新規 session はまだ active にせず、先に optimistic bubble を
          // 実 session へ rebinding する。これにより hydration / navigation
          // が provisional id を観測する race を防ぐ。
          dispatchChatTimeline({
            type: "rebind_client_message_session",
            fromSessionId: provisionalSessionId,
            toSessionId: sessionId,
            clientMessageId,
          });
          optimisticSessionId = sessionId;
          const initialMessagePersisted = Boolean(data.initial_message);
          if (!submittedAgentId) {
            await awaitSessionLlmSettingsReady(sessionId);
            if (!ownsSubmittedUserScope()) {
              settleOutbox(
                optimisticSessionId,
                clientMessageId,
                "uncertain",
                "送信中に利用者が切り替わったため、送信結果を確認してください。",
              );
              return "retained" as ChatComposerSendResult;
            }
            try {
              const applied = await applyPendingNewChatLlmSettingsToSession(
                sessionId,
                submittedUserId,
                generationReadyMain,
                submittedPendingSnapshot ?? undefined,
              );
              if (!applied) {
                throw new PendingLlmHandoffError(
                  "表示中の Provider / Model をセッションへ確定できませんでした。",
                );
              }
            } catch (error) {
              console.error("Failed to apply pending new-chat LLM settings:", error);
              settleOutbox(
                optimisticSessionId,
                clientMessageId,
                "uncertain",
                error instanceof Error ? error.message : LLM_HANDOFF_FAILURE_MESSAGE,
              );
              if (!ownsSubmittedUserScope() || !ownsSubmittedDraftView()) {
                return "retained" as ChatComposerSendResult;
              }
              options?.onSessionPromoted?.(sessionId);
              activateSession(sessionId);
              const href = `/chat?s=${encodeURIComponent(sessionId)}`;
              if (!navigateChatSessionInPlace(href)) {
                router.push(href);
              }
              const failureMessage = createLocalMessage(
                sessionId,
                "assistant",
                error instanceof PendingLlmHandoffError
                  ? `${LLM_HANDOFF_FAILURE_MESSAGE} (${error.message})`
                  : LLM_HANDOFF_FAILURE_MESSAGE,
              );
              dispatchChatTimeline({ type: "append", message: failureMessage });
              dispatchGeneration({
                type: "failed",
                sessionId,
                clientMessageId,
                statusMessage: LLM_HANDOFF_FAILURE_MESSAGE,
                eventId: `dispatch:${sessionId}:${clientMessageId}:handoff-failed`,
              });
              return "retained" as ChatComposerSendResult;
            }
            if (!ownsSubmittedUserScope()) {
              settleOutbox(
                optimisticSessionId,
                clientMessageId,
                "uncertain",
                "送信中に利用者が切り替わったため、送信結果を確認してください。",
              );
              return "retained" as ChatComposerSendResult;
            }
          }
          if (data.initial_message) {
            // createSession が初回メッセージを同時保存した場合は、既存の
            // optimistic bubble を server id へ昇格させるだけで再 append
            // しない。REST dispatch の skipUserPersistence とも対応する。
            dispatchChatTimeline({
              type: "promote_client_message",
              sessionId,
              clientMessageId,
              serverMessageId: data.initial_message.id,
            });
            settleOutbox(sessionId, clientMessageId, "accepted");
          }
          const activateCreatedSession = ownsSubmittedDraftView();
          if (activateCreatedSession) {
            dispatchGeneration({ type: "reset", sessionId });
            dispatchGeneration({
              type: "dispatch_started",
              sessionId,
              clientMessageId,
            });
            options?.onSessionPromoted?.(sessionId);
            activateSession(sessionId);
            const href = `/chat?s=${encodeURIComponent(sessionId)}`;
            if (!navigateChatSessionInPlace(href)) {
              router.push(href);
            }
          }

          const knownAssistantIds = new Set<string>();
          // 添付は WebSocket 側でアップロード処理が必要なためキューへ保持する。
          // メンションだけなら REST dispatch でも同じ構造化 payload を送れるので、
          // 新規セッション直後や WS 未接続でも ID の意味を失わない。
          if (files?.length) {
            if (!activateCreatedSession) {
              // The current view may already own a different pending send.
              // A live socket can target the created session explicitly, but
              // never enqueue this stale submission in the new view's slot.
              const accepted = await sendMessage(
                content,
                messageProjectId,
                files,
                mentions,
                generationProfile,
                loadStoredPlanningPolicy(window.localStorage),
                messageIncludeProjectContext,
                undefined,
                undefined,
                sessionId,
                clientMessageId,
                commandCapabilities,
                toolsRequired,
                messageAppContext
                  ? { appId: messageAppContext.appId, targetId: messageAppContext.targetId }
                  : null,
                undefined,
                executionProfileId,
              );
              if (accepted) {
                bumpSession(sessionId);
                scheduleAckDeadline(sessionId, clientMessageId);
                return "accepted" as ChatComposerSendResult;
              }
              settleOutbox(
                optimisticSessionId,
                clientMessageId,
                "uncertain",
                "添付の送信結果を確認できませんでした。",
              );
              toast.error("会話の切替後に添付付きメッセージを送信できませんでした。作成された会話で再度送信してください。");
              return "retained" as ChatComposerSendResult;
            }
            return queuePendingMessage({
              sessionId,
              content,
              clientMessageId,
              projectId: isHelpSubmission ? null : messageProjectId,
              files,
              mentions,
              generationProfile,
              includeProjectContext: messageIncludeProjectContext,
              commandCapabilities,
              toolsRequired,
              executionProfileId,
              appContext: messageAppContext,
            });
          } else {
            try {
              await dispatchMessageWithoutWebSocket(
                sessionId,
                content,
                messageProjectId,
                generationProfile,
                undefined,
                clientMessageId,
                commandCapabilities,
                data.initial_message
                  ? {
                      skipUserPersistence: true,
                      persistedUserMessageId: data.initial_message.id,
                    }
                  : undefined,
                toolsRequired,
                messageAppContext,
                mentions,
                messageIncludeProjectContext,
                executionProfileId,
              );
              markWaitingResponse(sessionId, clientMessageId);
            } catch (err) {
              console.error("新規セッションのREST送信失敗:", err);
              notifyRuntimeRejection(err, executionProfileId);
              // createSession already committed the user row. A later
              // generation-dispatch failure must not downgrade that row to an
              // uncertain/retryable send after its outbox key was deleted.
              if (!initialMessagePersisted) {
                settleOutbox(
                  optimisticSessionId,
                  clientMessageId,
                  "uncertain",
                  DISPATCH_FAILURE_MESSAGE,
                );
              } else {
                updateClientDeliveryState(
                  optimisticSessionId,
                  clientMessageId,
                  "accepted",
                );
              }
              if (activeSessionIdRef.current === sessionId) {
                const failureMessage = createDispatchFailureMessage(sessionId);
                dispatchChatTimeline({ type: "append", message: failureMessage });
              }
              dispatchGeneration({
                type: "failed",
                sessionId,
                clientMessageId,
                statusMessage: DISPATCH_FAILURE_MESSAGE,
                eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
              });
              return "retained" as ChatComposerSendResult;
            }
            if (draftViewRef.current && activeSessionIdRef.current === sessionId) {
              void waitForPersistedAssistantResponse(
                sessionId,
                knownAssistantIds,
                data.session,
              );
            }
          }

          return files?.length
            ? ("pending" as ChatComposerSendResult)
            : ("accepted" as ChatComposerSendResult);
        } catch (err) {
          console.error("セッション自動作成失敗:", err);
          settleOutbox(
            optimisticSessionId,
            clientMessageId,
            "uncertain",
            "新規会話の作成結果を確認できませんでした。",
          );
          if (
            !sessionId &&
            ownsSubmittedUserScope() &&
            ownsSubmittedDraftView()
          ) {
            toast.error(
              "新規会話を開始できませんでした。入力内容は下書きに保持しています。",
            );
          }
          if (sessionId) {
            dispatchGeneration({
              type: "failed",
              sessionId,
              clientMessageId,
              statusMessage: "セッション自動作成に失敗しました",
              eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
            });
          }
          return "retained" as ChatComposerSendResult;
        }
      }
      try {
        await awaitSessionLlmSettingsReady(sessionId);
      } catch (err) {
        console.error("セッション設定の反映待機に失敗しました:", err);
        settleOutbox(
          optimisticSessionId,
          clientMessageId,
          "uncertain",
          DISPATCH_FAILURE_MESSAGE,
        );
        dispatchGeneration({
          type: "failed",
          sessionId,
          clientMessageId,
          statusMessage: DISPATCH_FAILURE_MESSAGE,
          eventId: `dispatch:${sessionId}:${clientMessageId}:settings-failed`,
        });
        return "retained" as ChatComposerSendResult;
      }
      const knownAssistantIds = new Set(
        messages
          .filter(
            (message) =>
              message.session_id === sessionId && message.role === "assistant",
          )
          .map((message) => message.id),
      );

      // WebSocket未接続の場合は保留して待機
      if (!isConnected) {
        if (files?.length) {
          bumpSession(sessionId);
          return queuePendingMessage({
            sessionId,
            content,
            clientMessageId,
            projectId: isHelpSubmission ? null : messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext: messageIncludeProjectContext,
            commandCapabilities,
            toolsRequired,
            executionProfileId,
            appContext: messageAppContext,
          });
        } else {
          try {
            await dispatchMessageWithoutWebSocket(
              sessionId,
              content,
              messageProjectId,
              generationProfile,
              undefined,
              clientMessageId,
              commandCapabilities,
              undefined,
              toolsRequired,
              messageAppContext,
              mentions,
              messageIncludeProjectContext,
              executionProfileId,
            );
            markWaitingResponse(sessionId, clientMessageId);
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              currentSession,
            );
          } catch (err) {
            console.error("WebSocket未接続時のREST送信失敗:", err);
            notifyRuntimeRejection(err, executionProfileId);
            settleOutbox(
              optimisticSessionId,
              clientMessageId,
              "uncertain",
              DISPATCH_FAILURE_MESSAGE,
            );
            const failureMessage = createDispatchFailureMessage(sessionId);
            dispatchChatTimeline({ type: "append", message: failureMessage });
            dispatchGeneration({
              type: "failed",
              sessionId,
              clientMessageId,
              statusMessage: DISPATCH_FAILURE_MESSAGE,
              eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
            });
            return "retained" as ChatComposerSendResult;
          }
        }
        return files?.length
          ? ("pending" as ChatComposerSendResult)
          : ("accepted" as ChatComposerSendResult);
      }

      // グループチャットは共有セッション用WebSocketで送信する。
      if (isGroupChat && sessionId) {
        let accepted: boolean;
        try {
          accepted = await sendMessage(
            content,
            messageProjectId,
            files,
            mentions,
            generationProfile,
            loadStoredPlanningPolicy(
              typeof window !== "undefined" ? window.localStorage : null,
            ),
            messageIncludeProjectContext,
            undefined,
            undefined,
            sessionId,
            clientMessageId,
            commandCapabilities,
            toolsRequired,
            messageAppContext
              ? { appId: messageAppContext.appId, targetId: messageAppContext.targetId }
              : null,
            undefined,
            executionProfileId,
          );
        } catch (err) {
          console.error("グループチャットのWebSocket送信失敗:", err);
          settleOutbox(
            optimisticSessionId,
            clientMessageId,
            files?.length ? "failed" : "uncertain",
            files?.length
              ? "添付を送信できませんでした。ファイルを確認して再試行してください。"
              : "送信結果を確認できませんでした。",
            Boolean(files?.length),
          );
          dispatchChatTimeline({
            type: "append",
            message: createDispatchFailureMessage(sessionId),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "retained" as ChatComposerSendResult;
        }
        if (!accepted) {
          return queuePendingMessage({
            sessionId,
            content,
            clientMessageId,
            projectId: isHelpSubmission ? null : messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext: messageIncludeProjectContext,
            commandCapabilities,
            toolsRequired,
            executionProfileId,
            appContext: messageAppContext,
          });
        } else {
          bumpSession(sessionId);
          scheduleAckDeadline(sessionId, clientMessageId);
          dispatchGeneration({
            type: "dispatch_accepted",
            sessionId,
            clientMessageId,
            statusMessage: "応答をキューに追加しました",
          });
        }
        return "accepted" as ChatComposerSendResult;
      }

      // 通常テキスト送信はRESTで先にDB保存してから生成をキューする。
      // ファイル/メンションはWebSocket側で添付ペイロードを組み立てる。
      if (files?.length || mentions?.length) {
        let accepted: boolean;
        try {
          accepted = await sendMessage(
            content,
            messageProjectId,
            files,
            mentions,
            generationProfile,
            loadStoredPlanningPolicy(
              typeof window !== "undefined" ? window.localStorage : null,
            ),
            messageIncludeProjectContext,
            undefined,
            undefined,
            sessionId,
            clientMessageId,
            commandCapabilities,
            toolsRequired,
            messageAppContext
              ? { appId: messageAppContext.appId, targetId: messageAppContext.targetId }
              : null,
            undefined,
            executionProfileId,
          );
        } catch (err) {
          console.error("添付/メンションのWebSocket送信失敗:", err);
          settleOutbox(
            optimisticSessionId,
            clientMessageId,
            "failed",
            "添付を送信できませんでした。ファイルを確認して再試行してください。",
            Boolean(files?.length),
          );
          dispatchChatTimeline({
            type: "append",
            message: createDispatchFailureMessage(sessionId),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "retained" as ChatComposerSendResult;
        }
        if (!accepted) {
          return queuePendingMessage({
            sessionId,
            content,
            clientMessageId,
            projectId: isHelpSubmission ? null : messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext: messageIncludeProjectContext,
            commandCapabilities,
            toolsRequired,
            executionProfileId,
            appContext: messageAppContext,
          });
        } else {
          bumpSession(sessionId);
          scheduleAckDeadline(sessionId, clientMessageId);
          markWaitingResponse(sessionId, clientMessageId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
        }
        return "accepted" as ChatComposerSendResult;
      } else {
        try {
          await dispatchMessageWithoutWebSocket(
            sessionId,
            content,
            messageProjectId,
            generationProfile,
            undefined,
            clientMessageId,
            commandCapabilities,
            undefined,
            toolsRequired,
            messageAppContext,
            mentions,
            messageIncludeProjectContext,
            executionProfileId,
          );
          markWaitingResponse(sessionId, clientMessageId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
          return "accepted" as ChatComposerSendResult;
        } catch (err) {
          console.error("メッセージ送信失敗:", err);
          notifyRuntimeRejection(err, executionProfileId);
          settleOutbox(
            optimisticSessionId,
            clientMessageId,
            "uncertain",
            DISPATCH_FAILURE_MESSAGE,
          );
          const failureMessage = createDispatchFailureMessage(sessionId);
          dispatchChatTimeline({ type: "append", message: failureMessage });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "retained" as ChatComposerSendResult;
        }
      }
      } finally {
        if (sendInFlightRef.current?.token === sendFlightToken) {
          sendInFlightRef.current = null;
        }
      }
    },
    [
      activeSessionId,
      activeSessionIdRef,
      isConnected,
      sendMessage,
      router,
      effectiveProjectId,
      allProjects,
      includeProjectContext,
      messages,
      activateSession,
      addSession,
      bumpSession,
      isSending,
      deepResearchEnabled,
      handleDeepResearchMessage,
      isGroupChat,
      dispatchMessageWithoutWebSocket,
      createDispatchFailureMessage,
      currentSession,
      dispatchChatTimeline,
      dispatchGeneration,
      markWaitingResponse,
      queuePendingMessage,
      waitForPersistedAssistantResponse,
      saveOutboxForSubmission,
      scheduleAckDeadline,
      settleOutbox,
      updateClientDeliveryState,
      normalizedNewSessionAgentId,
      onSessionAgentConsumed,
    ],
  );

  const handleRetryMessage = useCallback(
    async (message: ConversationMessage) => {
      const clientMessageId = getMessageClientId(message);
      if (!clientMessageId) return;
      const record =
        outboxRecordsRef.current.get(clientMessageId) ??
        (await getChatSendOutboxRecord(draftUserIdRef.current, clientMessageId));
      if (!record) {
        toast.error("再試行する送信記録が見つかりません。本文を入力し直してください。");
        return;
      }
      outboxRecordsRef.current.set(clientMessageId, record);
      const files = record.files?.filter(
        (file): file is File => typeof File !== "undefined" && file instanceof File,
      );
      const attachmentsMissing =
        record.attachments.length > 0 &&
        (!files || files.length !== record.attachments.length);
      if (attachmentsMissing) {
        requestChatSendRestore({
          userId: record.userId,
          sessionId: record.sessionId,
          clientMessageId: record.clientMessageId,
          content: record.content,
          mentions: record.mentions,
          commandCapabilities: record.commandCapabilities,
          attachments: outboxAttachmentMetadata(record),
        });
        toast.info("元の添付ファイルを再選択してから送信してください");
        return;
      }

      const result = await handleSendMessage(
        record.content,
        files,
        record.mentions,
        record.generationProfile,
        record.commandCapabilities,
        record.toolsRequired,
        record.appContext,
        {
          clientMessageId: record.clientMessageId,
          dispatchMode: "chat",
          executionProfileId: record.executionProfileId ?? null,
        },
      );
      if (result === "failed") {
        // The send path has already retained the row/outbox state. Keep the
        // retry action visible instead of turning an uncertain result into a
        // delivered message.
        settleOutbox(
          record.sessionId,
          record.clientMessageId,
          "uncertain",
          "送信結果を確認できませんでした。",
        );
      }
    },
    [handleSendMessage, settleOutbox],
  );

  const dispatchBranchMessage = useCallback(
    async (
      sourceMessage: ConversationMessage,
      content: string,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (
        !activeSessionId ||
        isSending ||
        displayIsWaitingResponse ||
        isStreaming
      ) {
        return;
      }
      const knownAssistantIds = new Set(
        messages
          .filter((message) => message.role === "assistant")
          .map((message) => message.id),
      );
      const commandCapabilities = commandCapabilitiesFromMessageMetadata(
        sourceMessage.metadata,
      );
      const isHelpBranch = commandCapabilities.includes("aoitalk_help");
      const generationProfile = resolveBranchGenerationProfile(
        sourceMessage,
        currentSession,
      );
      // 添付付きメッセージの再実行では元の添付を引き継ぐ。バイナリは保存されて
      // いないため、バックエンドがプロジェクト内パスから実体を読み直す。
      const attachments = attachmentsFromMessageMetadata(sourceMessage.metadata);
      const branchClientMessageId = `branch:${sourceMessage.id}:${Date.now()}`;
      dispatchGeneration({
        type: "dispatch_started",
        sessionId: activeSessionId,
        clientMessageId: branchClientMessageId,
      });
      try {
        const result = await chatApi.dispatchMessage(activeSessionId, {
          message: content,
          client_message_id: branchClientMessageId,
          project_id: isHelpBranch ? undefined : effectiveProjectId,
          generation_profile: generationProfile,
          include_project_context: isHelpBranch ? false : includeProjectContext,
          edit_message_id: sourceMessage.id,
          response_model: responseModel,
          command_capabilities:
            commandCapabilities.length > 0 ? commandCapabilities : undefined,
          attachments: attachments.length > 0 ? attachments : undefined,
          ...(currentSession?.agent_id
            ? { expected_agent_id: currentSession.agent_id }
            : {}),
        });
        if (result.agent_run_id) {
          dispatchGeneration({
            type: "dispatch_accepted",
            sessionId: activeSessionId,
            clientMessageId: branchClientMessageId,
            agentRunId: result.agent_run_id,
            statusMessage: "応答をキューに追加しました",
          });
        }
        markWaitingResponse(activeSessionId, branchClientMessageId);
        bumpSession(activeSessionId);
        void waitForPersistedAssistantResponse(
          activeSessionId,
          knownAssistantIds,
          currentSession,
        );
      } catch (err) {
        // 失敗を黙って捨てると「再実行を押しても何も起きない」ように見える。
        console.error("分岐メッセージ送信失敗:", err);
        dispatchChatTimeline({
          type: "append",
          message: createDispatchFailureMessage(activeSessionId),
        });
        dispatchGeneration({
          type: "failed",
          sessionId: activeSessionId,
          clientMessageId: branchClientMessageId,
          statusMessage: DISPATCH_FAILURE_MESSAGE,
          eventId: `dispatch:${activeSessionId}:${branchClientMessageId}:failed`,
        });
      }
    },
    [
      activeSessionId,
      bumpSession,
      createDispatchFailureMessage,
      dispatchChatTimeline,
      dispatchGeneration,
      effectiveProjectId,
      includeProjectContext,
      isSending,
      isStreaming,
      displayIsWaitingResponse,
      markWaitingResponse,
      messages,
      currentSession,
      waitForPersistedAssistantResponse,
    ],
  );

  const handleEditMessage = useCallback(
    (messageId: string, newContent: string) => {
      const source = messages.find((message) => message.id === messageId);
      if (!source) return;
      void dispatchBranchMessage(source, newContent);
    },
    [dispatchBranchMessage, messages],
  );

  const handleRerunMessage = useCallback(
    (
      message: ConversationMessage,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (message.role === "user") {
        void dispatchBranchMessage(message, message.content, responseModel);
        return;
      }

      const index = messages.findIndex((item) => item.id === message.id);
      const source = [...messages]
        .slice(0, index >= 0 ? index : messages.length)
        .reverse()
        .find((item) => item.role === "user");
      if (!source) return;
      void dispatchBranchMessage(source, source.content, responseModel);
    },
    [dispatchBranchMessage, messages],
  );

  /**
   * 既存ブランチの active path をサーバー側で切り替える。
   * ブランチ切替は新しい generation を開始せず、成功後に full refresh で
   * server-authoritative な履歴を取り直す。
   */
  const handleSwitchBranch = useCallback(
    async (message: ConversationMessage, targetBranchIndex: number) => {
      const sessionId = activeSessionId;
      if (!sessionId) return;

      if (isSending || displayIsWaitingResponse || isStreaming) {
        toast.info("応答生成中は分岐を切り替えられません");
        return;
      }

      const branchCount =
        typeof message.branch_count === "number" &&
        Number.isInteger(message.branch_count)
          ? message.branch_count
          : null;
      if (branchCount === null || branchCount <= 1) {
        toast.error("切り替え可能な分岐情報を取得できませんでした");
        return;
      }
      if (
        !Number.isInteger(targetBranchIndex) ||
        targetBranchIndex < 0 ||
        targetBranchIndex >= branchCount
      ) {
        toast.error("無効な分岐が指定されました");
        return;
      }

      const currentBranchIndex =
        typeof message.branch_index === "number" &&
        Number.isInteger(message.branch_index)
          ? message.branch_index
          : 0;
      if (
        currentBranchIndex < 0 ||
        currentBranchIndex >= branchCount
      ) {
        toast.error("現在の分岐情報が不正です");
        return;
      }
      if (currentBranchIndex === targetBranchIndex) return;

      const inFlightKey = `${sessionId}:${message.id}`;
      if (branchSwitchInFlightRef.current === inFlightKey) return;
      if (branchSwitchInFlightRef.current) return;
      branchSwitchInFlightRef.current = inFlightKey;

      try {
        const result = await chatApi.switchBranch(
          sessionId,
          message.id,
          targetBranchIndex,
        );
        if (!result?.success) {
          throw new Error("ブランチ切替APIが失敗を返しました");
        }

        const refreshed = await refreshPersistedMessages(sessionId, {
          forceFull: true,
        });
        if (!refreshed) {
          throw new Error("ブランチ切替後の履歴再取得に失敗しました");
        }
        toast.success("分岐を切り替えました");
      } catch (error) {
        console.error("分岐切替失敗:", error);
        toast.error("分岐を切り替えられませんでした。会話を再読み込みしてください");
      } finally {
        if (branchSwitchInFlightRef.current === inFlightKey) {
          branchSwitchInFlightRef.current = null;
        }
      }
    },
    [
      activeSessionId,
      displayIsWaitingResponse,
      isSending,
      isStreaming,
      refreshPersistedMessages,
    ],
  );

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    bumpSessionForAssistant,
    handleCreateGroupChat,
    handleSendMessage,
    handleRetryMessage,
    retryDeepResearchAssistantPersistence,
    handleEditMessage,
    handleRerunMessage,
    handleSwitchBranch,
  };
}
