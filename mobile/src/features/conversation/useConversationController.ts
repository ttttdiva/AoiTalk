import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentRun,
  ChatResponseModelOption,
  ChatResponseModelSelection,
  ConversationMessage,
  ConversationSession,
  WSMessage,
} from "../../types/api";
import {
  chatApi,
  type ChatAppContext,
  type GenerationSteerResponse,
  type LlmModeResponse,
} from "../../lib/chat-api";
import { isApiHttpError } from "../../lib/api-client";
import { taskApi } from "../../lib/task-api";
import { ChatWebSocket } from "../../lib/websocket";
import {
  generateMobileLlmReply,
  getConfiguredDirectMobileLlmSettings,
  getConfiguredFallbackMobileLlmSettings,
  getMobileLlmSettings,
  getDirectMobileLlmSettings,
  isDirectProvider,
  KIMI_ASSISTANT_PAYLOAD_METADATA_KEY,
  type MobileLlmSettings,
} from "../../lib/mobile-llm";
import {
  conversationsRepo,
  dispatchPendingConversationMessage,
  flushPendingConversation,
  getPromotedConversationSessionId,
} from "../../repositories";
import { chatRepo } from "../../repositories/chat";
import { appsRepo } from "../../repositories/apps";
import { isServerKnownUnreachable, useNetworkStore } from "../../stores/network";
import { deepResearchApi, type DeepResearchJob } from "../../lib/deep-research-api";
import {
  buildCommandRegistry,
  buildConnectionCapability,
  buildSessionCapabilities,
  buildTransportState,
  buildUserCapability,
  inferSessionKind,
} from "./capabilities";
import {
  groupMessageKey,
  selectVisibleMessages,
  upsertConversationMessage,
} from "./timeline";
import type {
  ConversationControllerSnapshot,
  ConversationDiagnostics,
  ConversationJob,
  EffectiveGenerationRoute,
  LlmSelectionSyncStatus,
  PermissionRequest,
  SendConversationCommand,
} from "./models";
import { appContextCompatibleWithProject } from "./app-context";
import { type SkillSlashCommand } from "./chat-commands";
import {
  getCachedLlmMode,
  getCachedLlmModelCatalog,
  getCachedSkillSlashCommands,
  primeLlmMode,
} from "./llm-meta-cache";
import {
  describeFallbackFailure,
  errorTextOf,
  isLikelyConnectivityFailure,
} from "./fallback-error";
import {
  buildConversationTitlePrompt,
  cleanGeneratedConversationTitle,
  shouldGenerateConversationTitle,
} from "./title";
import {
  buildDirectReplyPersistedMetadata,
  buildRetryableServerDispatchMetadata,
  createCharacterProfileSnapshotResolver,
  finishConversationOperation,
  generateCharacterAwareDirectReply,
  getCharacterChangeAvailability,
  runExclusiveConversationOperation,
  tryStartConversationOperation,
  type ConversationExclusiveOperation,
} from "./character-session";
import { buildReplaceableConversationFallbackTitle } from "./local-title-fallback";
import { buildResponseModelOptions } from "./response-model-options";
import {
  attemptPendingRetry,
  findAcceptedRemoteMessage,
} from "./pending-retry";
import {
  buildPendingDispatchMetadata,
  pendingDispatchPayload,
} from "./pending-dispatch-payload";
import {
  cancelledAssistantMessages,
  isAssistantPersistenceEvent,
} from "./cancelled-generation";
import {
  SERVER_DEFAULT_MODE,
  createDefaultChatLlmPreferences,
  normalizeLlmMode,
  normalizeResponseTarget,
  normalizeTargetAgainstServerOptions,
  readChatLlmPreferences,
  resolveCurrentChatLlmPreferenceScope,
  writeChatLlmPreferences,
  type ChatLlmPreferences,
  type ChatResponseTarget,
} from "./chat-llm-preferences";
import {
  LatestSelectionSynchronizer,
  type LatestSelectionSyncEvent,
  type LatestSelectionTask,
} from "./latest-selection-sync";
import {
  createStreamBuffer,
  type StreamBuffer,
} from "./stream-buffer";
import { conversationPerformanceDiagnostics } from "./performance-diagnostics";
import { useConversationGeneration } from "./useConversationGeneration";
import type { GenerationIdentity } from "./generation-reducer";
import {
  useConversationFocusRecovery,
  useConversationRuntimeFocus,
} from "./useConversationRuntime";
import { loadConversationRemoteData } from "./useConversationData";
import { useConversationDurableTimeline } from "./useConversationTimeline";
import { ConversationGenerationEventGate } from "./conversation-generation-events";

type ControllerArgs = {
  sessionId?: string | null;
  isAuthenticated: boolean;
  userId?: string | null;
  userRole?: string | null;
  selectedProjectId?: string | null;
  onSessionPromoted?: (sessionId: string) => void;
  initialAppContext?: ChatAppContext | null;
};

type BranchSwitchRuntime = {
  fetchBranches: (
    sessionId: string,
    messageId: string,
  ) => Promise<ConversationMessage[]>;
  switchBranch: (
    sessionId: string,
    messageId: string,
    branchIndex: number,
  ) => Promise<unknown>;
  refresh: () => Promise<void>;
};

function branchAtIndex(
  branches: ConversationMessage[],
  branchIndex: number,
): ConversationMessage | null {
  const projected = branches.find(
    (branch) => branch.branch_index === branchIndex,
  );
  if (projected) return projected;
  // Legacy siblings without projection metadata retain their array-order
  // behavior. A sparse projected list must not mistake its first row for index 0.
  if (branches.some((branch) => typeof branch.branch_index === "number")) {
    return null;
  }
  return branches[branchIndex] ?? null;
}

export async function switchConversationBranchWithFallback(args: {
  sessionId: string;
  message: ConversationMessage;
  nextIndex: number;
  localMessages: ConversationMessage[];
  runtime: BranchSwitchRuntime;
}): Promise<boolean> {
  const { sessionId, message, nextIndex, localMessages, runtime } = args;
  const branchCount = message.branch_count;
  if (
    !Number.isInteger(nextIndex) ||
    nextIndex < 0 ||
    (typeof branchCount === "number" &&
      branchCount > 0 &&
      nextIndex >= branchCount)
  ) {
    return false;
  }

  const groupKey = groupMessageKey(message);
  const localSiblings = localMessages.filter(
    (entry) => groupMessageKey(entry) === groupKey,
  );
  let target = branchAtIndex(localSiblings, nextIndex);
  if (!target) {
    const fetched = await runtime.fetchBranches(sessionId, message.id);
    target = branchAtIndex(fetched, nextIndex);
  }
  if (!target) return false;

  await runtime.switchBranch(sessionId, message.id, nextIndex);
  await runtime.refresh();
  return true;
}

function deepResearchToConversationJob(job: DeepResearchJob): ConversationJob {
  const latestEvent = job.events.at(-1);
  return {
    id: job.id,
    type: "deep_research",
    title: `Deep Research: ${job.query}`,
    status:
      job.status === "running"
        ? "running"
        : job.status === "completed"
          ? "completed"
          : job.status,
    progress: job.progress,
    progressText: latestEvent?.message,
    resultText: job.report_markdown,
    error: job.error,
    sourceScope: String(job.metadata?.project_id ?? "all"),
    createdAt: job.created_at,
    updatedAt: job.updated_at,
  };
}

function nextRefreshTimers(callback: () => void) {
  const cancels = [1500, 5000, 15000].map((delay) => {
    const stopTracking = conversationPerformanceDiagnostics.trackActive(
      "timer",
      "conversation-refresh",
    );
    let active = true;
    const finish = () => {
      if (!active) return;
      active = false;
      stopTracking();
    };
    const timer = setTimeout(() => {
      finish();
      callback();
    }, delay);
    return () => {
      clearTimeout(timer);
      finish();
    };
  });
  return () => cancels.forEach((cancel) => cancel());
}

function conversationMessageSignature(message: ConversationMessage): string {
  return JSON.stringify([
    message.id,
    message.session_id,
    message.role,
    message.content,
    message.metadata,
    message.created_at,
    message.updated_at,
    message.token_count,
    message.branch_count,
    message.parent_message_id,
    message.branch_index,
    message.is_active_branch,
  ]);
}

function areConversationMessagesEqual(
  left: readonly ConversationMessage[],
  right: readonly ConversationMessage[],
): boolean {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  return left.every(
    (message, index) =>
      conversationMessageSignature(message) ===
      conversationMessageSignature(right[index]),
  );
}

function conversationSessionSignature(session: ConversationSession): string {
  return JSON.stringify([
    session.id,
    session.user_id,
    session.character_name,
    session.title,
    session.session_start,
    session.last_activity,
    session.message_count,
    session.is_active,
    session.project_id,
    session.is_group_chat,
    session.app_id,
    session.app_target_id,
    session.development_status,
    session.last_read_at,
    session.is_unread,
  ]);
}

function areConversationSessionsEqual(
  left: ConversationSession | null,
  right: ConversationSession | null,
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  return conversationSessionSignature(left) === conversationSessionSignature(right);
}

function extractActivityMessage(message: WSMessage): string | null {
  const data =
    message.data && typeof message.data === "object"
      ? (message.data as Record<string, unknown>)
      : null;
  const value =
    typeof message.message === "string"
      ? message.message
      : typeof data?.message === "string"
        ? data.message
        : typeof message.content === "string"
          ? message.content
          : null;
  return value && value.trim() ? value.trim() : null;
}

function parseLlmModePayload(value: unknown): LlmModeResponse | null {
  const data =
    value && typeof value === "object"
      ? (value as Record<string, unknown>)
      : null;
  if (!data || typeof data.mode !== "string" || !data.mode.trim()) return null;
  return {
    mode: data.mode,
    available_modes: Array.isArray(data.available_modes)
      ? data.available_modes.filter((item): item is string => typeof item === "string")
      : undefined,
    labels:
      data.labels && typeof data.labels === "object" && !Array.isArray(data.labels)
        ? (data.labels as Record<string, string>)
        : undefined,
    kind: typeof data.kind === "string" ? data.kind : undefined,
    provider: typeof data.provider === "string" ? data.provider : undefined,
    model: typeof data.model === "string" ? data.model : undefined,
    success: typeof data.success === "boolean" ? data.success : undefined,
    message: typeof data.message === "string" ? data.message : undefined,
  };
}

type LlmModeSyncResult =
  | { kind: "accepted"; state: LlmModeResponse }
  | {
      kind: "rejected";
      state: LlmModeResponse;
      requestedMode: string;
    };

export async function syncLatestLlmMode(
  task: LatestSelectionTask<string>,
): Promise<LlmModeSyncResult> {
  try {
    const state = normalizeLlmMode(await chatApi.setLlmMode(task.value));
    return state.mode === task.value
      ? { kind: "accepted", state }
      : { kind: "rejected", state, requestedMode: task.value };
  } catch (error) {
    if (!isApiHttpError(error) || error.status !== 400) throw error;
    // 400は接続失敗と区別し、サーバーが採用中の値へ明示的に正規化する。
    const state = normalizeLlmMode(await chatApi.getLlmMode());
    return { kind: "rejected", state, requestedMode: task.value };
  }
}

export function useConversationController(
  args: ControllerArgs,
): ConversationControllerSnapshot & {
  branchSelections: Record<string, number>;
  load: () => Promise<void>;
  refreshFromServer: () => Promise<void>;
  stopGeneration: () => Promise<void>;
  sendConversationCommand: (command: SendConversationCommand) => Promise<void>;
  retryPendingMessage: (message: ConversationMessage) => Promise<void>;
  respondPermission: (requestId: string, approved: boolean) => void;
  startDeepResearch: (query: string) => Promise<void>;
  editMessage: (message: ConversationMessage, content: string) => Promise<void>;
  rerunMessage: (
    message: ConversationMessage,
    responseModel?: ChatResponseModelSelection,
  ) => Promise<void>;
  loadBranches: (messageId: string) => Promise<void>;
  switchBranch: (message: ConversationMessage, nextIndex: number) => Promise<void>;
  changeLlmMode: (mode: string) => void;
  changeResponseTarget: (target: ChatResponseTarget) => void;
  refreshLlmMode: () => Promise<void>;
  refreshSkillCommands: () => Promise<void>;
  changeCharacter: (slug: string) => Promise<void>;
  serverGenerationActive: boolean;
  agentRuns: Record<string, AgentRun | null>;
  agentRunErrors: Record<string, boolean>;
  retryAgentRun: (runId: string) => void;
  updateSessionTitle: (title: string) => Promise<void>;
  changeProject: (projectId: string | null) => Promise<void>;
  bindAppContext: (context: ChatAppContext | null) => Promise<void>;
  steerGeneration: (message: string) => Promise<GenerationSteerResponse>;
  groupRespond: (message: string, strategy?: string) => Promise<void>;
  forkConversation: (fromMessageId: string, title?: string | null) => Promise<string>;
  getContextSnapshot: ReturnType<typeof chatRepo.getContextSnapshot> extends Promise<infer T>
    ? () => Promise<T>
    : never;
} {
  const {
    sessionId,
    isAuthenticated,
    userId,
    selectedProjectId,
    userRole,
    onSessionPromoted,
    initialAppContext,
  } = args;
  const networkOnline = useNetworkStore((state) => state.online);
  const networkServerReachable = useNetworkStore(
    (state) => state.serverReachable,
  );
  const networkServerCheckedAt = useNetworkStore(
    (state) => state.serverCheckedAt,
  );
  const { isFocused, focusEpoch } = useConversationRuntimeFocus();
  const focusedRef = useRef(isFocused);
  const focusEpochRef = useRef(focusEpoch);
  focusedRef.current = isFocused;
  focusEpochRef.current = focusEpoch;
  const [session, setSession] = useState<ConversationSession | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const sessionStateRef = useRef<ConversationSession | null>(null);
  const messagesStateRef = useRef<ConversationMessage[]>([]);
  const pendingMessagesRef = useRef(0);
  sessionStateRef.current = session;
  messagesStateRef.current = messages;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isWaiting, setIsWaiting] = useState(false);
  const {
    active: serverGenerationActive,
    identity: getGenerationIdentity,
    activateSession: activateGenerationSession,
    begin: beginServerGeneration,
    startStreaming: markServerGenerationStreaming,
    requestCancel: markServerGenerationCancelling,
    complete: completeServerGeneration,
  } = useConversationGeneration(sessionId ?? "");
  const [streamContent, setStreamContent] = useState("");
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [activityMessage, setActivityMessage] = useState<string | null>(null);
  const [llmMode, setLlmMode] = useState(
    () => createDefaultChatLlmPreferences().mode.mode,
  );
  const [llmModeOptions, setLlmModeOptions] = useState<string[]>(
    () => createDefaultChatLlmPreferences().mode.available_modes ?? [],
  );
  const [llmModeLabels, setLlmModeLabels] = useState<Record<string, string>>(
    () => createDefaultChatLlmPreferences().mode.labels ?? {},
  );
  const [llmModeKind, setLlmModeKind] = useState<string | null>(
    () => createDefaultChatLlmPreferences().mode.kind ?? null,
  );
  const [llmModeSyncStatus, setLlmModeSyncStatus] =
    useState<LlmSelectionSyncStatus>("idle");
  const [llmSelectionMessage, setLlmSelectionMessage] = useState<string | null>(null);
  const [llmPreferencesReady, setLlmPreferencesReady] = useState(false);
  const [effectiveGeneration, setEffectiveGeneration] =
    useState<EffectiveGenerationRoute | null>(null);
  const [responseModelOptions, setResponseModelOptions] = useState<
    ChatResponseModelOption[]
  >([]);
  const [responseModelOptionsLoading, setResponseModelOptionsLoading] =
    useState(false);
  const [responseTarget, setResponseTarget] = useState<ChatResponseTarget>({
    kind: "server",
  });
  const [skillCommands, setSkillCommands] = useState<SkillSlashCommand[]>([]);
  const [retryingMessageIds, setRetryingMessageIds] = useState<string[]>([]);
  const [branchSelections, setBranchSelections] = useState<Record<string, number>>({});
  const [pendingPermissions, setPendingPermissions] = useState<PermissionRequest[]>([]);
  const [jobs, setJobs] = useState<ConversationJob[]>([]);
  const [lastRefreshAt, setLastRefreshAt] = useState<string | null>(null);
  const [agentRuns, setAgentRuns] = useState<Record<string, AgentRun | null>>({});
  const [agentRunErrors, setAgentRunErrors] = useState<Record<string, boolean>>({});
  const wsRef = useRef<ChatWebSocket>(new ChatWebSocket());
  const generationEventGateRef = useRef(new ConversationGenerationEventGate());
  const streamBufferRef = useRef<StreamBuffer | null>(null);
  const jobPollersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({});
  const jobPollerStopsRef = useRef<Record<string, () => void>>({});
  const jobPollFlightsRef = useRef(new Set<string>());
  const jobPollingLifecycleRef = useRef(0);
  const mountedRef = useRef(true);
  const activeGenerationSessionRef = useRef(sessionId);
  const generationLifecycleRef = useRef(0);
  activeGenerationSessionRef.current = sessionId;
  const loadRequestRef = useRef(0);
  const failedCancelledMessageFlightsRef = useRef(
    new Map<string, Promise<ConversationMessage>>(),
  );
  const scheduledRefreshCancelRef = useRef<(() => void) | null>(null);
  const terminalRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const terminalRefreshTimerStopRef = useRef<(() => void) | null>(null);
  const terminalRefreshCancelFallbackRef = useRef(false);
  const terminalRefreshSequenceRef = useRef(0);
  const refreshFlightRef = useRef<{
    sessionId: string | null | undefined;
    requestId: number;
    promise: Promise<void>;
  } | null>(null);
  const titleGenerationInFlightRef = useRef(new Set<string>());
  const manuallyRenamedSessionIdsRef = useRef(new Set<string>());
  const exclusiveOperationRef =
    useRef<ConversationExclusiveOperation | null>(null);
  const llmPreferenceScopeRef = useRef<string | null>(null);
  const llmPreferenceHydrationRef = useRef(0);
  const llmPreferencesRef = useRef<ChatLlmPreferences>(
    createDefaultChatLlmPreferences(),
  );
  const llmModeRef = useRef("");
  const responseModelOptionsRef = useRef<ChatResponseModelOption[]>([]);
  const responseTargetRef = useRef<ChatResponseTarget>({ kind: "server" });
  const appContextAttemptRef = useRef<string | null>(null);
  const llmSyncEventHandlerRef = useRef<
    (event: LatestSelectionSyncEvent<string, LlmModeSyncResult>) => void
  >(() => undefined);
  const llmModeSynchronizerRef = useRef<
    LatestSelectionSynchronizer<string, LlmModeSyncResult> | null
  >(null);
  if (!llmModeSynchronizerRef.current) {
    llmModeSynchronizerRef.current = new LatestSelectionSynchronizer(
      syncLatestLlmMode,
      (event) => llmSyncEventHandlerRef.current(event),
    );
  }
  llmModeRef.current = llmMode;
  responseModelOptionsRef.current = responseModelOptions;
  responseTargetRef.current = responseTarget;

  if (!streamBufferRef.current) {
    streamBufferRef.current = createStreamBuffer({
      identity: {
        sessionId: sessionId ?? "",
        lifecycleId: 0,
      },
      onPublish: (publication) => {
        if (
          !mountedRef.current ||
          activeGenerationSessionRef.current !== publication.sessionId ||
          getGenerationIdentity()?.lifecycleId !== publication.lifecycleId
        ) {
          return;
        }
        conversationPerformanceDiagnostics.increment(
          "stream",
          "react-publication",
        );
        setStreamContent(publication.text);
      },
    });
  }

  useEffect(() => {
    const stopTrackingController = conversationPerformanceDiagnostics.trackActive(
      "controller",
      "conversation",
    );
    mountedRef.current = true;
    // React StrictModeのeffect再セットアップでも、cleanup済みqueueを再利用しない。
    if (!llmModeSynchronizerRef.current) {
      llmModeSynchronizerRef.current = new LatestSelectionSynchronizer(
        syncLatestLlmMode,
        (event) => llmSyncEventHandlerRef.current(event),
      );
    }
    return () => {
      mountedRef.current = false;
      loadRequestRef.current += 1;
      llmPreferenceHydrationRef.current += 1;
      llmModeSynchronizerRef.current?.dispose();
      llmModeSynchronizerRef.current = null;
      streamBufferRef.current?.flush("unmount");
      scheduledRefreshCancelRef.current?.();
      if (terminalRefreshTimerRef.current) {
        clearTimeout(terminalRefreshTimerRef.current);
        terminalRefreshTimerRef.current = null;
      }
      terminalRefreshTimerStopRef.current?.();
      terminalRefreshTimerStopRef.current = null;
      terminalRefreshCancelFallbackRef.current = false;
      terminalRefreshSequenceRef.current += 1;
      stopTrackingController();
    };
  }, []);

  useEffect(() => {
    return () => {
      scheduledRefreshCancelRef.current?.();
      scheduledRefreshCancelRef.current = null;
      if (terminalRefreshTimerRef.current) {
        clearTimeout(terminalRefreshTimerRef.current);
        terminalRefreshTimerRef.current = null;
      }
      terminalRefreshTimerStopRef.current?.();
      terminalRefreshTimerStopRef.current = null;
      terminalRefreshCancelFallbackRef.current = false;
      terminalRefreshSequenceRef.current += 1;
    };
  }, [sessionId]);

  const sessionKind = useMemo(() => inferSessionKind(session), [session]);
  const effectiveProjectId = session ? session.project_id : selectedProjectId;
  const transportState = buildTransportState({
    isAuthenticated,
    isConnected,
    online: networkOnline,
  });
  const pendingMessages = useMemo(
    () =>
      messages.filter(
        (message) =>
          message.role === "user" &&
          Boolean(message.metadata?.local_only) &&
          Boolean(message.metadata?.pending),
      ).length,
    [messages],
  );
  pendingMessagesRef.current = pendingMessages;
  const runState = useMemo<ConversationDiagnostics["runState"]>(() => {
    if (pendingPermissions.some((request) => request.status === "pending")) {
      return "permission-required";
    }
    if (jobs.some((job) => job.status === "queued" || job.status === "running")) {
      return "job-running";
    }
    if (activeTool) return "tool-running";
    if (isStreaming) return "streaming";
    if (isWaiting) return "waiting";
    return "idle";
  }, [activeTool, isStreaming, isWaiting, jobs, pendingPermissions]);
  const syncState = pendingMessages > 0 ? "pending-upload" : isWaiting ? "pending-refresh" : "clean";
  const sessionCapabilities = buildSessionCapabilities({
    sessionKind,
    isAuthenticated,
    selectedProjectId: effectiveProjectId,
  });
  const diagnostics: ConversationDiagnostics = {
    userCapability: buildUserCapability(isAuthenticated, userRole),
    sessionKind,
    sessionCapabilities,
    connectionCapability: buildConnectionCapability({
      isAuthenticated,
      isConnected,
      online: networkOnline,
      serverReachable: networkServerReachable,
    }),
    transportState,
    runState,
    syncState,
    activeTool,
    activityMessage,
    pendingMessages,
    lastRefreshAt,
    serverCheckedAt: networkServerCheckedAt,
  };
  const commands = buildCommandRegistry({
    isAuthenticated,
    sessionKind,
    capabilities: sessionCapabilities,
    transportState,
    runState,
    selectedProjectId: effectiveProjectId,
  });
  const visibleMessages = useMemo(
    () => selectVisibleMessages(messages, branchSelections),
    [branchSelections, messages],
  );

  const loadAgentRun = useCallback(
    (runId: string) => {
      const requestedSessionId = sessionId;
      const isCurrentSession = () =>
        mountedRef.current &&
        activeGenerationSessionRef.current === requestedSessionId;
      setAgentRuns((current) => ({ ...current, [runId]: null }));
      setAgentRunErrors((current) => ({ ...current, [runId]: false }));
      void chatApi
        .getAgentRun(runId)
        .then((run) => {
          if (isCurrentSession()) {
            setAgentRuns((current) => ({ ...current, [runId]: run }));
          }
        })
        .catch(() => {
          if (isCurrentSession()) {
            setAgentRunErrors((current) => ({ ...current, [runId]: true }));
          }
        });
    },
    [sessionId],
  );

  const retryAgentRun = useCallback(
    (runId: string) => loadAgentRun(runId),
    [loadAgentRun],
  );

  useEffect(() => {
    const runIds = [
      ...new Set(
        visibleMessages
          .filter(
            (message) =>
              message.role === "assistant" &&
              message.metadata?.generation_status === "cancelled",
          )
          .map((message) => String(message.metadata?.agent_run_id ?? "").trim())
          .filter(Boolean),
      ),
    ];
    for (const runId of runIds) {
      if (agentRuns[runId] !== undefined) continue;
      loadAgentRun(runId);
    }
  }, [agentRuns, loadAgentRun, visibleMessages]);
  const pendingPermissionEvents = useMemo(
    () => pendingPermissions.filter((request) => request.status === "pending"),
    [pendingPermissions],
  );
  const timeline = useConversationDurableTimeline({
    messages: visibleMessages,
    permissions: pendingPermissionEvents,
    jobs,
    activeTool,
    activityMessage,
  });

  useEffect(() => {
    if (!sessionId || pendingMessages === 0 || !networkServerCheckedAt) return;
    let cancelled = false;
    void conversationsRepo.listMessagesLocal(sessionId).then((localMessages) => {
      if (!cancelled && mountedRef.current) {
        setMessages((current) =>
          areConversationMessagesEqual(current, localMessages)
            ? current
            : localMessages,
        );
      }
    });
    return () => {
      cancelled = true;
    };
  }, [networkServerCheckedAt, pendingMessages, sessionId]);

  const applyGeneratedTitle = useCallback(
    async (
      targetSessionId: string,
      title: string,
      source: "llm" | "fallback",
      syncServer = false,
    ) => {
      if (manuallyRenamedSessionIdsRef.current.has(targetSessionId)) return;
      await conversationsRepo.updateTitle(targetSessionId, title, {
        syncServer,
        source,
      });
      if (!mountedRef.current || targetSessionId !== sessionId) return;
      setSession((current) =>
        current?.id === targetSessionId ? { ...current, title } : current,
      );
    },
    [sessionId],
  );

  const maybeGenerateLocalTitle = useCallback(
    async (
      targetSession: ConversationSession,
      titleMessages: ConversationMessage[],
      settings: MobileLlmSettings,
    ) => {
      if (
        !shouldGenerateConversationTitle(targetSession, titleMessages) ||
        titleGenerationInFlightRef.current.has(targetSession.id)
      ) {
        return;
      }
      const prompt = buildConversationTitlePrompt(titleMessages);
      if (!prompt) return;
      titleGenerationInFlightRef.current.add(targetSession.id);
      try {
        let titleApplied = false;
        try {
          const reply = await generateMobileLlmReply(settings, [], prompt);
          const generatedTitle = cleanGeneratedConversationTitle(reply.content);
          if (generatedTitle) {
            await applyGeneratedTitle(
              targetSession.id,
              generatedTitle,
              "llm",
              Boolean(targetSession.user_id),
            );
            titleApplied = true;
          }
        } catch {
          // タイトル生成・保存失敗は会話本文の成功を壊さない。
        }
        if (!titleApplied) {
          const fallbackTitle =
            buildReplaceableConversationFallbackTitle(titleMessages);
          if (fallbackTitle) {
            try {
              await applyGeneratedTitle(
                targetSession.id,
                fallbackTitle,
                "fallback",
                Boolean(targetSession.user_id),
              );
            } catch {
              // fallback titleの保存失敗も会話本文の成功を壊さない。
            }
          }
        }
        // 後続送信ではfallback titleを置換対象として再試行できる。
      } finally {
        titleGenerationInFlightRef.current.delete(targetSession.id);
      }
    },
    [applyGeneratedTitle],
  );

  const maybeGenerateServerTitle = useCallback(
    async (
      targetSession: ConversationSession,
      titleMessages: ConversationMessage[],
    ) => {
      if (
        !targetSession.user_id ||
        !shouldGenerateConversationTitle(targetSession, titleMessages) ||
        titleGenerationInFlightRef.current.has(targetSession.id)
      ) {
        return;
      }
      titleGenerationInFlightRef.current.add(targetSession.id);
      try {
        const result = await chatApi.generateSessionTitle(targetSession.id);
        if (result.title) {
          await applyGeneratedTitle(
            targetSession.id,
            result.title,
            result.source === "fallback" ? "fallback" : "llm",
          );
        }
      } catch {
        // WebSocketイベントでも回収できるため、明示生成失敗は非致命。
      } finally {
        titleGenerationInFlightRef.current.delete(targetSession.id);
      }
    },
    [applyGeneratedTitle],
  );

  const load = useCallback(async () => {
    if (!sessionId) return;
    const requestId = ++loadRequestRef.current;
    const isCurrentRequest = () =>
      mountedRef.current && requestId === loadRequestRef.current;
    setLoading(true);
    setError(null);
    try {
      const [localSession, localMessages] = await Promise.all([
        conversationsRepo.getSessionLocal(sessionId),
        conversationsRepo.listMessagesLocal(sessionId),
      ]);
      if (!isCurrentRequest()) return;
      setSession(localSession);
      setMessages(localMessages);

      const hasLocalData = Boolean(localSession) || localMessages.length > 0;
      if (hasLocalData || !isAuthenticated) {
        setLoading(false);
      }
      if (!isAuthenticated) return;

      // Opening a session acknowledges any completed App-agent response.
      void conversationsRepo.markSessionRead(sessionId);

      // 新規チャットは端末内で先に作る。local-only IDへresumeSessionを投げると、
      // 画面表示直後に不要なサーバー待ちと404を発生させるため、送信時の昇格へ委ねる。
      if (localSession && !localSession.user_id) return;

      const refreshRemote = async () => {
        try {
          // resumeはsession metadataだけを回収する。message payloadはSQLiteの
          // cursorを正本にしたfull/delta endpointへ一本化する。
          const remote = await loadConversationRemoteData(sessionId);
          if (!isCurrentRequest()) return;
          setSession(remote.session);
          setMessages((current) =>
            areConversationMessagesEqual(current, remote.messages)
              ? current
              : remote.messages,
          );
          setLastRefreshAt(new Date().toISOString());
          setLoading(false);
          void maybeGenerateServerTitle(remote.session, remote.messages);
        } catch (refreshError) {
          if (!isCurrentRequest()) return;
          if (isLikelyConnectivityFailure(refreshError)) {
            useNetworkStore.getState().setServerReachable(false);
          }
          if (!hasLocalData) {
            setError("会話セッションを表示できませんでした。");
          }
          setLoading(false);
        }
      };

      if (hasLocalData) {
        void refreshRemote();
      } else {
        await refreshRemote();
      }
    } catch (loadError) {
      if (!isCurrentRequest()) return;
      setError(loadError instanceof Error ? loadError.message : "会話を読み込めませんでした。");
      setLoading(false);
    }
  }, [isAuthenticated, maybeGenerateServerTitle, sessionId]);

  const refreshFromServer = useCallback(() => {
    const requestedSessionId = sessionId;
    const requestId = loadRequestRef.current;
    const existing = refreshFlightRef.current;
    if (
      existing &&
      existing.sessionId === requestedSessionId &&
      existing.requestId === requestId
    ) {
      return existing.promise;
    }

    const flight = (async () => {
      const isCurrentRequest = () =>
        mountedRef.current &&
        requestId === loadRequestRef.current &&
        activeGenerationSessionRef.current === requestedSessionId;
      if (!requestedSessionId || !isAuthenticated) {
        if (requestedSessionId) {
          const localMessages =
            await conversationsRepo.listMessagesLocal(requestedSessionId);
          if (isCurrentRequest()) {
            setMessages((current) =>
              areConversationMessagesEqual(current, localMessages)
                ? current
                : localMessages,
            );
          }
        }
        return;
      }
      try {
        const [refreshResult, localSession] = await Promise.all([
          conversationsRepo.refreshMessagesDetailed(requestedSessionId),
          conversationsRepo.getSessionLocal(requestedSessionId),
        ]);
        const localMessages = refreshResult.messages;
        if (!isCurrentRequest()) return;
        const messagesChanged = !areConversationMessagesEqual(
          messagesStateRef.current,
          localMessages,
        );
        const sessionChanged = !areConversationSessionsEqual(
          sessionStateRef.current,
          localSession,
        );
        if (messagesChanged) setMessages(localMessages);
        if (sessionChanged) setSession(localSession);
        if (messagesChanged || sessionChanged) {
          setLastRefreshAt(new Date().toISOString());
        }
        if (localSession) {
          void maybeGenerateServerTitle(localSession, localMessages);
        }
      } catch (refreshError) {
        if (isLikelyConnectivityFailure(refreshError)) {
          useNetworkStore.getState().setServerReachable(false);
        }
        throw refreshError;
      }
    })();
    let trackedFlight: Promise<void>;
    trackedFlight = flight.finally(() => {
      if (refreshFlightRef.current?.promise === trackedFlight) {
        refreshFlightRef.current = null;
      }
    });
    refreshFlightRef.current = {
      sessionId: requestedSessionId,
      requestId,
      promise: trackedFlight,
    };
    return trackedFlight;
  }, [
    isAuthenticated,
    maybeGenerateServerTitle,
    sessionId,
  ]);

  const upsertServerMessage = useCallback((message: ConversationMessage) => {
    setMessages((current) => {
      const index = current.findIndex((candidate) => candidate.id === message.id);
      if (index < 0) return [...current, message];
      return current.map((candidate) =>
        candidate.id === message.id ? message : candidate,
      );
    });
  }, []);

  const preserveFailedCancelledMessage = useCallback(
    async (
      targetSessionId: string,
      content: string,
      agentRunId?: string,
      stopOperationKey?: string,
    ) => {
      const stopKey = `${targetSessionId}:${
        stopOperationKey || agentRunId || "unknown-run"
      }`;
      let flight = failedCancelledMessageFlightsRef.current.get(stopKey);
      if (!flight) {
        flight = (async () => {
          const existing = (
            await conversationsRepo.listMessagesLocal(targetSessionId)
          ).find(
            (message) =>
              message.metadata?.cancelled_stop_key === stopKey,
          );
          if (existing) return existing;
          return conversationsRepo.appendLocalMessage(
            targetSessionId,
            "assistant",
            content,
            {
              agent_run_id: agentRunId || undefined,
              generation_status: "cancelled",
              partial: true,
              persistence_failed: true,
              local_only: true,
              cancelled_stop_key: stopKey,
            },
          );
        })();
        failedCancelledMessageFlightsRef.current.set(stopKey, flight);
      }
      try {
        upsertServerMessage(await flight);
      } finally {
        if (failedCancelledMessageFlightsRef.current.get(stopKey) === flight) {
          failedCancelledMessageFlightsRef.current.delete(stopKey);
        }
      }
    },
    [upsertServerMessage],
  );

  const finalizeStream = useCallback(
    (
      reason: "terminal" | "cancel" | "error",
      expectedIdentity?: GenerationIdentity,
    ) => {
      const identity = expectedIdentity ?? getGenerationIdentity();
      if (!identity) return streamBufferRef.current?.snapshot().text ?? "";
      return (
        streamBufferRef.current?.finalize(
          {
            sessionId: identity.sessionId,
            lifecycleId: identity.lifecycleId,
          },
          reason,
        ) ?? streamBufferRef.current?.snapshot().text ?? ""
      );
    },
    [getGenerationIdentity],
  );

  const clearServerGenerationState = useCallback((
    reason: "terminal" | "cancel" | "error" = "terminal",
    expectedIdentity?: GenerationIdentity,
  ) => {
    if (expectedIdentity) {
      const current = getGenerationIdentity();
      if (
        !current ||
        current.sessionId !== expectedIdentity.sessionId ||
        current.lifecycleId !== expectedIdentity.lifecycleId ||
        current.requestId !== expectedIdentity.requestId
      ) {
        return;
      }
    }
    finalizeStream(reason, expectedIdentity);
    completeServerGeneration(expectedIdentity);
    setIsStreaming(false);
    setIsWaiting(false);
    setActiveTool(null);
    setActivityMessage(null);
    setStreamContent("");
  }, [completeServerGeneration, finalizeStream, getGenerationIdentity]);

  const restoreServerGenerationState = useCallback(async () => {
    if (!sessionId || !isAuthenticated || !session?.user_id) return;
    const requestedSessionId = sessionId;
    const requestGeneration = ++generationLifecycleRef.current;
    try {
      const status = await chatApi.getGenerationStatus(requestedSessionId);
      if (
        !mountedRef.current ||
        activeGenerationSessionRef.current !== requestedSessionId ||
        generationLifecycleRef.current !== requestGeneration
      ) {
        return;
      }
      if (!status.running) {
        clearServerGenerationState();
        return;
      }
      const waiting = status.status === "queued" || status.status === "waiting";
      const identity = beginServerGeneration(
        requestedSessionId,
        status.agent_run_id ?? `status-${requestGeneration}`,
      );
      if (!identity) return;
      generationEventGateRef.current.bindTransportId(
        status.agent_run_id,
        identity,
      );
      streamBufferRef.current?.switchIdentity({
        sessionId: identity.sessionId,
        lifecycleId: identity.lifecycleId,
      });
      if (!waiting) markServerGenerationStreaming();
      setIsWaiting(waiting);
      setIsStreaming(!waiting);
      setActiveTool(status.active_tool ?? null);
      setActivityMessage(status.message ?? "応答を生成しています...");
    } catch {
      // WebSocketの後続イベントで復元できるため、状態取得失敗は非致命。
    }
  }, [
    clearServerGenerationState,
    beginServerGeneration,
    isAuthenticated,
    markServerGenerationStreaming,
    session?.user_id,
    sessionId,
  ]);

  const stopGeneration = useCallback(async () => {
    if (!sessionId || !serverGenerationActive) return;
    generationLifecycleRef.current += 1;
    const stoppingIdentity = markServerGenerationCancelling();
    if (!stoppingIdentity) return;
    const isCurrentStop = () => {
      const current = getGenerationIdentity();
      return Boolean(
        current &&
        current.sessionId === stoppingIdentity.sessionId &&
        current.lifecycleId === stoppingIdentity.lifecycleId &&
        current.requestId === stoppingIdentity.requestId,
      );
    };
    setError(null);
    const sentOverWebSocket = wsRef.current.stopGeneration();
    try {
      if (sentOverWebSocket) {
        await new Promise<void>((resolve) => setTimeout(resolve, 1_000));
        try {
          const status = await chatApi.getGenerationStatus(sessionId);
          if (!status.running) {
            await refreshFromServer().catch(() => undefined);
            clearServerGenerationState("terminal", stoppingIdentity);
            return;
          }
        } catch {
          // WebSocket停止の確認に失敗した場合はREST停止へフォールバックする。
        }
      }
      const result = await chatApi.stopGeneration(sessionId);
      const savedMessages =
        result.messages ?? (result.message ? [result.message] : []);
      for (const message of savedMessages) {
        upsertServerMessage(message);
      }
      await refreshFromServer().catch(() => undefined);
      if (result.status === "cancellation_pending") {
        if (!isCurrentStop()) return;
        setIsWaiting(true);
        setIsStreaming(false);
        setActiveTool(null);
        setActivityMessage("停止処理を継続しています…");
        return;
      }
      const failedRunIds = result.persistence_failed_run_ids ?? [];
      const failedBufferRunId =
        failedRunIds.length === 1
          ? failedRunIds[0]
          : failedRunIds.length > 1
            ? undefined
            : (result.agent_run_id ?? undefined);
      const failedBufferKey =
        failedRunIds.length > 0
          ? [...failedRunIds].sort().join("-")
          : (failedBufferRunId ?? "unknown-run");
      const shouldPreserveLiveBuffer =
        isCurrentStop() &&
        result.persistence_failed &&
        Boolean(streamBufferRef.current?.snapshot().text.trim());
      if (shouldPreserveLiveBuffer) {
        await preserveFailedCancelledMessage(
          sessionId,
          streamBufferRef.current?.snapshot().text ?? "",
          failedBufferRunId,
          failedBufferKey,
        );
      }
      if (result.persistence_failed && isCurrentStop()) {
        setError("停止しましたが、一部の途中応答を保存できませんでした。");
      }
    } catch (stopError) {
      try {
        const status = await chatApi.getGenerationStatus(sessionId);
        await refreshFromServer().catch(() => undefined);
        if (!status.running) {
          clearServerGenerationState("terminal", stoppingIdentity);
          return;
        }
      } catch {
        // 元の停止エラーを表示する。
      }
      if (isCurrentStop()) {
        setError(errorTextOf(stopError, "応答生成を停止できませんでした。"));
      }
      return;
    }
    clearServerGenerationState("cancel", stoppingIdentity);
  }, [
    clearServerGenerationState,
    getGenerationIdentity,
    markServerGenerationCancelling,
    preserveFailedCancelledMessage,
    refreshFromServer,
    serverGenerationActive,
    sessionId,
    upsertServerMessage,
  ]);

  useEffect(() => {
    generationLifecycleRef.current += 1;
    const lifecycleId = generationLifecycleRef.current;
    streamBufferRef.current?.switchIdentity({
      sessionId: sessionId ?? "",
      lifecycleId,
    });
    generationEventGateRef.current.reset();
    activateGenerationSession(sessionId ?? "");
    jobPollingLifecycleRef.current += 1;
    for (const poller of Object.values(jobPollersRef.current)) {
      clearInterval(poller);
    }
    for (const stopTracking of Object.values(jobPollerStopsRef.current)) {
      stopTracking();
    }
    jobPollersRef.current = {};
    jobPollerStopsRef.current = {};
    jobPollFlightsRef.current.clear();
    clearServerGenerationState();
    setJobs([]);
    setAgentRuns({});
    setAgentRunErrors({});
  }, [activateGenerationSession, clearServerGenerationState, sessionId]);

  const cancelScheduledRefresh = useCallback(() => {
    scheduledRefreshCancelRef.current?.();
    scheduledRefreshCancelRef.current = null;
    terminalRefreshCancelFallbackRef.current = false;
    terminalRefreshSequenceRef.current += 1;
  }, []);

  const scheduleTerminalRefresh = useCallback(
    (delay = 350, cancelFallbackOnSuccess = false) => {
      if (!focusedRef.current) return;
      if (cancelFallbackOnSuccess) {
        terminalRefreshCancelFallbackRef.current = true;
      }
      const scheduleSequence = ++terminalRefreshSequenceRef.current;
      if (terminalRefreshTimerRef.current) {
        clearTimeout(terminalRefreshTimerRef.current);
      }
      terminalRefreshTimerStopRef.current?.();
      terminalRefreshTimerStopRef.current =
        conversationPerformanceDiagnostics.trackActive(
          "timer",
          "conversation-terminal-refresh",
        );
      terminalRefreshTimerRef.current = setTimeout(() => {
        terminalRefreshTimerRef.current = null;
        terminalRefreshTimerStopRef.current?.();
        terminalRefreshTimerStopRef.current = null;
        if (!focusedRef.current) return;
        const shouldCancelFallback = terminalRefreshCancelFallbackRef.current;
        terminalRefreshCancelFallbackRef.current = false;
        void refreshFromServer()
          .then(() => {
            if (
              shouldCancelFallback &&
              terminalRefreshSequenceRef.current === scheduleSequence
            ) {
              cancelScheduledRefresh();
            }
          })
          .catch(() => undefined);
      }, delay);
    },
    [cancelScheduledRefresh, refreshFromServer],
  );

  const scheduleRefresh = useCallback(() => {
    if (!focusedRef.current) return () => undefined;
    cancelScheduledRefresh();
    const cancel = nextRefreshTimers(() => {
      if (!focusedRef.current) return;
      void refreshFromServer()
        .catch(() => undefined)
        .finally(() => setIsWaiting(false));
    });
    scheduledRefreshCancelRef.current = cancel;
    return cancel;
  }, [cancelScheduledRefresh, refreshFromServer]);

  const recoverFocusedRuntime = useCallback((epoch: number) => {
    void restoreServerGenerationState();
    if (epoch > 1) {
      void refreshFromServer().catch(() => undefined);
    }
  }, [refreshFromServer, restoreServerGenerationState]);

  const stopBlurredRuntime = useCallback(() => {
    cancelScheduledRefresh();
    if (terminalRefreshTimerRef.current) {
      clearTimeout(terminalRefreshTimerRef.current);
      terminalRefreshTimerRef.current = null;
    }
    terminalRefreshTimerStopRef.current?.();
    terminalRefreshTimerStopRef.current = null;
    jobPollingLifecycleRef.current += 1;
    for (const poller of Object.values(jobPollersRef.current)) {
      clearInterval(poller);
    }
    for (const stopTracking of Object.values(jobPollerStopsRef.current)) {
      stopTracking();
    }
    jobPollersRef.current = {};
    jobPollerStopsRef.current = {};
    jobPollFlightsRef.current.clear();
  }, [cancelScheduledRefresh]);

  useConversationFocusRecovery({
    enabled: Boolean(sessionId && session?.user_id),
    focusEpoch,
    isFocused,
    onRecover: recoverFocusedRuntime,
    onBlur: stopBlurredRuntime,
  });

  const persistLlmPreferences = useCallback(
    (patch: Partial<ChatLlmPreferences>) => {
      const scope = llmPreferenceScopeRef.current;
      if (!scope) return;
      const next: ChatLlmPreferences = {
        ...llmPreferencesRef.current,
        ...patch,
        version: 1,
        updatedAt: Date.now(),
      };
      llmPreferencesRef.current = next;
      void writeChatLlmPreferences(scope, next).catch(() => {
        if (llmPreferenceScopeRef.current !== scope || !mountedRef.current) return;
        setLlmModeSyncStatus("unsynced");
        setLlmSelectionMessage(
          "選択を端末へ保存できませんでした。アプリを閉じる前に再試行してください。",
        );
      });
    },
    [],
  );

  const applyLlmModeView = useCallback((value: LlmModeResponse) => {
    const result = normalizeLlmMode(value);
    llmModeRef.current = result.mode;
    setLlmMode(result.mode);
    setLlmModeOptions(result.available_modes ?? [result.mode]);
    setLlmModeLabels(result.labels ?? {});
    setLlmModeKind(result.kind ?? null);
    return result;
  }, []);

  const applyServerLlmMode = useCallback(
    (value: LlmModeResponse, scope: string) => {
      if (llmPreferenceScopeRef.current !== scope) return;
      const serverState = normalizeLlmMode(value);
      primeLlmMode(serverState, scope);
      const pendingTask = llmModeSynchronizerRef.current?.pendingTask();
      if (
        pendingTask?.scope === scope &&
        pendingTask.value !== serverState.mode
      ) {
        // 遅延response/WebSocket Aで、最新ローカル選択Bを上書きしない。
        const localMode = pendingTask.value;
        const localState = normalizeLlmMode({
          ...serverState,
          mode: localMode,
          available_modes: [
            localMode,
            ...(serverState.available_modes ?? []).filter((mode) => mode !== localMode),
          ],
          labels: {
            ...(serverState.labels ?? {}),
            ...(llmPreferencesRef.current.mode.labels ?? {}),
          },
        });
        applyLlmModeView(localState);
        persistLlmPreferences({ mode: localState, modeSyncPending: true });
        return;
      }

      applyLlmModeView(serverState);
      persistLlmPreferences({ mode: serverState, modeSyncPending: false });
    },
    [applyLlmModeView, persistLlmPreferences],
  );

  llmSyncEventHandlerRef.current = (
    event: LatestSelectionSyncEvent<string, LlmModeSyncResult>,
  ) => {
    if (event.status === "idle") {
      setLlmModeSyncStatus("idle");
      return;
    }
    if (llmPreferenceScopeRef.current !== event.task.scope) return;
    if (event.status === "pending" || event.status === "syncing") {
      setLlmModeSyncStatus(event.status);
      if (event.status === "pending") setLlmSelectionMessage(null);
      return;
    }
    if (event.status === "failure") {
      setLlmModeSyncStatus("unsynced");
      setLlmSelectionMessage(
        "Effortは端末へ保存済みです。サーバーへ未同期のため、再接続後に再試行します。",
      );
      persistLlmPreferences({ modeSyncPending: true });
      return;
    }

    const normalized = applyLlmModeView(event.result.state);
    primeLlmMode(normalized, event.task.scope);
    persistLlmPreferences({ mode: normalized, modeSyncPending: false });
    if (event.result.kind === "rejected") {
      setLlmModeSyncStatus("rejected");
      const label = normalized.labels?.[normalized.mode] ?? normalized.mode;
      setLlmSelectionMessage(
        `${event.result.requestedMode} はサーバーで利用できないため、${label} に戻しました。`,
      );
    } else {
      setLlmModeSyncStatus("synced");
      setLlmSelectionMessage(null);
    }
  };

  const refreshLlmModeForScope = useCallback(
    async (scope: string) => {
      if (!isAuthenticated) return;
      try {
        applyServerLlmMode(await getCachedLlmMode(scope), scope);
      } catch {
        // 永続cache/defaultを維持する。offlineで空表示へ戻さない。
      }
    },
    [applyServerLlmMode, isAuthenticated],
  );

  const refreshLlmMode = useCallback(async () => {
    const scope =
      llmPreferenceScopeRef.current ??
      (await resolveCurrentChatLlmPreferenceScope(
        isAuthenticated
          ? userId
            ? `auth:${userId}`
            : undefined
          : "anonymous",
      ));
    await refreshLlmModeForScope(scope);
  }, [isAuthenticated, refreshLlmModeForScope, userId]);

  const refreshResponseModelOptionsForScope = useCallback(
    async (scope: string) => {
      if (!isAuthenticated) return;
      if (responseModelOptionsRef.current.length === 0) {
        setResponseModelOptionsLoading(true);
      }
      try {
        const [catalogResult, settingsResult] = await Promise.allSettled([
          getCachedLlmModelCatalog(scope),
          taskApi.getUserSettings(),
        ]);
        if (catalogResult.status === "rejected") throw catalogResult.reason;
        if (llmPreferenceScopeRef.current !== scope) return;

        let options: ChatResponseModelOption[];
        if (settingsResult.status === "fulfilled") {
          options = buildResponseModelOptions(
            catalogResult.value,
            settingsResult.value,
          );
        } else if (responseModelOptionsRef.current.length > 0) {
          // hidden provider設定を取得できない時は、既に絞り込み済みのcacheを維持する。
          options = responseModelOptionsRef.current;
        } else {
          // 初回にvisibilityを取得できない場合、非表示providerを漏らさず現在値だけ出す。
          options = buildResponseModelOptions(catalogResult.value).filter(
            (option) => option.isCurrent,
          );
        }
        responseModelOptionsRef.current = options;
        setResponseModelOptions(options);

        const normalized = normalizeTargetAgainstServerOptions(
          responseTargetRef.current,
          options,
        );
        if (normalized.target !== responseTargetRef.current) {
          responseTargetRef.current = normalized.target;
          setResponseTarget(normalized.target);
        }
        if (normalized.message) setLlmSelectionMessage(normalized.message);
        persistLlmPreferences({
          responseModelOptions: options,
          responseTarget: normalized.target,
        });
      } catch {
        // offline/timeoutでもcache候補を消さない。
      } finally {
        if (llmPreferenceScopeRef.current === scope) {
          setResponseModelOptionsLoading(false);
        }
      }
    },
    [isAuthenticated, persistLlmPreferences],
  );

  const changeResponseTarget = useCallback(
    (value: ChatResponseTarget) => {
      const target = normalizeResponseTarget(value);
      responseTargetRef.current = target;
      setResponseTarget(target);
      // 次の応答モデルを変更したら、前回生成の実効route表示を一旦外し、
      // 新しい選択をheaderへ戻す。
      setEffectiveGeneration(null);
      setLlmSelectionMessage(null);
      persistLlmPreferences({ responseTarget: target });
    },
    [persistLlmPreferences],
  );

  const changeLlmMode = useCallback(
    (mode: string) => {
      const next = mode.trim();
      const scope = llmPreferenceScopeRef.current;
      if (!next || !scope) return;
      const localState = normalizeLlmMode({
        ...llmPreferencesRef.current.mode,
        mode: next,
        available_modes: [
          next,
          ...(llmPreferencesRef.current.mode.available_modes ?? []).filter(
            (option) => option !== next,
          ),
        ],
      });
      applyLlmModeView(localState);

      if (!isAuthenticated || next === SERVER_DEFAULT_MODE) {
        setLlmModeSyncStatus("idle");
        setLlmSelectionMessage(null);
        persistLlmPreferences({ mode: localState, modeSyncPending: false });
        return;
      }

      persistLlmPreferences({ mode: localState, modeSyncPending: true });
      llmModeSynchronizerRef.current?.enqueue(scope, next, {
        defer: !networkOnline || isServerKnownUnreachable(),
      });
    },
    [applyLlmModeView, isAuthenticated, networkOnline, persistLlmPreferences],
  );

  const refreshSkillCommands = useCallback(async () => {
    if (!isAuthenticated) {
      setSkillCommands([]);
      return;
    }
    try {
      setSkillCommands(
        await getCachedSkillSlashCommands(
          effectiveProjectId,
          llmPreferenceScopeRef.current ?? undefined,
        ),
      );
    } catch {
      setSkillCommands([]);
    }
  }, [effectiveProjectId, isAuthenticated]);

  const changeCharacter = useCallback(
    async (slug: string) => {
      if (!sessionId || !session) {
        throw new Error("会話セッションの読み込み完了後に変更できます。");
      }
      const availability = getCharacterChangeAvailability(
        session,
        runState,
        pendingMessages,
      );
      if (!availability.allowed) throw new Error(availability.reason);
      if (
        !tryStartConversationOperation(
          exclusiveOperationRef,
          "character-update",
        )
      ) {
        throw new Error(
          "送信・同期または別のキャラクター変更が完了してから再試行してください。",
        );
      }
      try {
        const updated = await conversationsRepo.updateCharacter(sessionId, slug);
        if (mountedRef.current) setSession(updated);
      } finally {
        finishConversationOperation(
          exclusiveOperationRef,
          "character-update",
        );
      }
    },
    [pendingMessages, runState, session, sessionId],
  );

  const updateSessionTitle = useCallback(
    async (nextTitle: string) => {
      if (!sessionId) throw new Error("会話セッションが見つかりません。");
      const normalized = nextTitle.trim();
      if (!normalized) throw new Error("タイトルを入力してください。");
      manuallyRenamedSessionIdsRef.current.add(sessionId);
      try {
        await conversationsRepo.updateTitle(sessionId, normalized, {
          requireServerSuccess: true,
        });
        loadRequestRef.current += 1;
      } catch (error) {
        manuallyRenamedSessionIdsRef.current.delete(sessionId);
        throw error;
      }
      if (mountedRef.current) {
        setSession((current) =>
          current?.id === sessionId
            ? { ...current, title: normalized }
            : current,
        );
      }
    },
    [sessionId],
  );

  const changeProject = useCallback(
    async (projectId: string | null) => {
      if (!sessionId || !session) {
        throw new Error("会話セッションの読み込み完了後に変更できます。");
      }
      if (runState !== "idle" || pendingMessages > 0) {
        throw new Error("送信・同期の完了後にプロジェクトを変更してください。");
      }
      if (session.app_id && projectId && projectId !== session.project_id) {
        const bindings = await appsRepo.listProjectApps(projectId);
        if (!appContextCompatibleWithProject(session.app_id, projectId, bindings)) {
          throw new Error("選択中のAppが有効化されていないProjectには移動できません。");
        }
      }
      if (
        !tryStartConversationOperation(
          exclusiveOperationRef,
          "project-update",
        )
      ) {
        throw new Error("別の会話操作が完了してから再試行してください。");
      }
      try {
        const updated = await conversationsRepo.updateProject(
          sessionId,
          projectId,
        );
        loadRequestRef.current += 1;
        if (mountedRef.current) setSession(updated);
      } finally {
        finishConversationOperation(exclusiveOperationRef, "project-update");
      }
    },
    [pendingMessages, runState, session, sessionId],
  );

  const bindAppContext = useCallback(
    async (context: ChatAppContext | null) => {
      if (!sessionId || !session) {
        throw new Error("会話セッションの読み込み完了後にAppを選択してください。");
      }
      if (runState !== "idle" || pendingMessages > 0) {
        throw new Error("送信・同期の完了後にAppを選択してください。");
      }
      if (!context && session.user_id === "") return;
      const updated = await chatRepo.bindAppContext(sessionId, context);
      if (updated.id !== sessionId) onSessionPromoted?.(updated.id);
      if (mountedRef.current) setSession(updated);
    },
    [onSessionPromoted, pendingMessages, runState, session, sessionId],
  );

  const steerGeneration = useCallback(
    async (message: string): Promise<GenerationSteerResponse> => {
      if (!sessionId || !serverGenerationActive) {
        throw new Error("生成中のみ指示を追加できます。");
      }
      return chatApi.steerGeneration(sessionId, message, {
        agentRunId: getGenerationIdentity()?.requestId ?? null,
      });
    },
    [getGenerationIdentity, serverGenerationActive, sessionId],
  );

  const groupRespond = useCallback(
    async (message: string, strategy?: string) => {
      if (!sessionId || !session?.is_group_chat) {
        throw new Error("グループチャットのセッションではありません。");
      }
      await chatApi.groupRespond(sessionId, message, strategy);
      await refreshFromServer();
    },
    [refreshFromServer, session?.is_group_chat, sessionId],
  );

  const forkConversation = useCallback(
    async (fromMessageId: string, title?: string | null): Promise<string> => {
      if (!sessionId || !isAuthenticated) {
        throw new Error("フォークにはログインが必要です。");
      }
      const forked = await chatRepo.forkSession(sessionId, fromMessageId, title);
      return forked.id;
    },
    [isAuthenticated, sessionId],
  );

  const getContextSnapshot = useCallback(
    () => chatRepo.getContextSnapshot(sessionId ?? ""),
    [sessionId],
  );

  const sendConversationCommand = useCallback(
    async (command: SendConversationCommand) => {
      if (!sessionId) return;
      const text = command.message.trim();
      if (!text || isStreaming || serverGenerationActive) return;
      if (!tryStartConversationOperation(exclusiveOperationRef, "send")) {
        setError(
          "キャラクター変更または別の送信が完了してから再試行してください。",
        );
        return;
      }
      const resolveCharacterSnapshot = createCharacterProfileSnapshotResolver(
        session?.character_name,
        undefined,
        {
          sessionId,
          authScope: userId ? `auth:${userId}` : isAuthenticated ? undefined : "anonymous",
          strict: true,
        },
      );
      try {
        const requestedTarget = command.target ?? { kind: "server" as const };
      const selectedAppId = command.appId ?? session?.app_id ?? null;
      const selectedAppTargetId =
        command.appTargetId ?? session?.app_target_id ?? null;
      const appContextSelected = Boolean(selectedAppId);
      const forceServer = command.target?.kind === "server";
      const requiresServerRuntime =
        (requestedTarget.kind === "server" && Boolean(requestedTarget.responseModel)) ||
        Boolean(command.commandCapabilities?.length) ||
        text.startsWith("/");
      const requiresServerFeature =
        Boolean(command.commandCapabilities?.length) || text.startsWith("/");
      if (requestedTarget.kind === "direct" && requiresServerFeature) {
        setError("組み込みコマンドとSkillsはServerモデルで実行してください。");
        return;
      }
      if (appContextSelected && requestedTarget.kind === "direct") {
        setError("App context付きChatではDirect/端末モデルを利用できません。");
        return;
      }
      if (appContextSelected && !session?.user_id) {
        setError("Appを紐付ける前にServerへ接続してください。");
        return;
      }
      if (requiresServerRuntime && !isAuthenticated) {
        setError("組み込みコマンド、Skills、モデル指定はログイン中のみ利用できます。");
        return;
      }

      let directSettings: MobileLlmSettings | null = null;
      let fallbackFromServer = false;
      const serverKnownUnreachable =
        requestedTarget.kind === "server" &&
        !appContextSelected &&
        !requiresServerFeature &&
        isServerKnownUnreachable();
      if (requestedTarget.kind === "direct") {
        directSettings = await getDirectMobileLlmSettings(requestedTarget.selection);
      } else if (serverKnownUnreachable) {
        directSettings = await getConfiguredFallbackMobileLlmSettings("server");
        fallbackFromServer = Boolean(directSettings);
      }

      setError(null);
      setIsWaiting(true);
      const usesDirect = Boolean(directSettings);
      const dispatchMetadata = buildPendingDispatchMetadata({
        message: text,
        projectId: command.projectId ?? effectiveProjectId,
        appId: selectedAppId,
        appTargetId: selectedAppTargetId,
        includeProjectContext:
          command.includeProjectContext ??
          Boolean(command.projectId ?? effectiveProjectId),
        agentMode: command.agentMode ?? "confirm",
        editMessageId: command.editMessageId,
        responseModel:
          requestedTarget.kind === "server"
            ? requestedTarget.responseModel
            : undefined,
        commandCapabilities: command.commandCapabilities,
        attachments: command.attachments,
      });
      const localMessage = await conversationsRepo.appendLocalMessage(sessionId, "user", text, {
        local_only: true,
        pending: isAuthenticated && !usesDirect,
        anonymous_only: !isAuthenticated,
        message_state: usesDirect
          ? "direct-running"
          : isAuthenticated
            ? "queued"
            : "local-draft",
        delivery_route: usesDirect ? "direct" : "server",
        ...dispatchMetadata,
      });
      setMessages((prev) => upsertConversationMessage(prev, localMessage));

      const appendDirectReply = async (
        directSettings: MobileLlmSettings,
        metadata: Record<string, unknown> = {},
      ) => {
        const effectiveMetadata: Record<string, unknown> = {
          ...metadata,
          effective_provider: directSettings.provider,
          effective_model: directSettings.model,
          ...(directSettings.reasoningEffort
            ? { effective_reasoning_effort: directSettings.reasoningEffort }
            : {}),
        };
        const effectiveRoute: EffectiveGenerationRoute = {
          kind: "direct",
          provider: directSettings.provider,
          model: directSettings.model,
          ...(directSettings.reasoningEffort
            ? { reasoningEffort: directSettings.reasoningEffort }
            : {}),
          fallback: Boolean(
            effectiveMetadata.fallback_from_server ||
              effectiveMetadata.fallback_from_direct,
          ),
        };
        if (mountedRef.current) setEffectiveGeneration(effectiveRoute);
        const reply = await generateCharacterAwareDirectReply(
          directSettings,
          messages,
          text,
          resolveCharacterSnapshot,
        );
        const assistantMessage = await conversationsRepo.appendLocalMessage(
          sessionId,
          "assistant",
          reply.content,
          {
            local_only: true,
            direct_cloud: true,
            provider: directSettings.provider,
            model: directSettings.model,
            ...(directSettings.reasoningEffort
              ? { reasoning_effort: directSettings.reasoningEffort }
              : {}),
            ...(reply.assistantPayload
              ? {
                  [KIMI_ASSISTANT_PAYLOAD_METADATA_KEY]: reply.assistantPayload,
                }
              : {}),
            ...effectiveMetadata,
          },
        );
        const persistedMetadata =
          buildDirectReplyPersistedMetadata(effectiveMetadata);
        await conversationsRepo.mergeMessageMetadata(
          localMessage.id,
          persistedMetadata,
        );
        setMessages((prev) => [
          ...prev.map((message) =>
            message.id === localMessage.id
              ? {
                  ...message,
                  metadata: {
                    ...message.metadata,
                    ...persistedMetadata,
                  },
                }
              : message,
          ),
          assistantMessage,
        ]);
        if (session) {
          void maybeGenerateLocalTitle(
            session,
            [...messages, localMessage, assistantMessage],
            directSettings,
          );
        }
      };

      const errorText = errorTextOf;
      const combinedFallbackError = describeFallbackFailure;

      if (directSettings) {
        setIsStreaming(true);
        try {
          await appendDirectReply(directSettings, {
            ...(requestedTarget.kind === "direct" ? { direct_selected: true } : {}),
            ...(fallbackFromServer ? { fallback_from_server: true } : {}),
          });
        } catch (directError) {
          const failedMetadata = {
            pending: fallbackFromServer,
            message_state: fallbackFromServer ? "queued" : "direct-failed",
            delivery_route: fallbackFromServer ? "server" : "direct",
            direct_error: errorText(directError, "Direct応答に失敗しました。"),
          };
          await conversationsRepo.mergeMessageMetadata(
            localMessage.id,
            failedMetadata,
          );
          setMessages((prev) =>
            prev.map((message) =>
              message.id === localMessage.id
                ? { ...message, metadata: { ...message.metadata, ...failedMetadata } }
                : message,
            ),
          );
          setError(errorText(directError, "Direct応答に失敗しました。"));
        } finally {
          setIsStreaming(false);
          setIsWaiting(false);
        }
        return;
      }

      // 事前ゲートで送信をブロックしない: フォールバックが解決できる場合は
      // 既に上の directSettings 分岐で処理済み。解決できない場合はここで
      // エラー return せず、通常の Server 送信を試みる（失敗すれば dispatch の
      // catch 内で即時フォールバック経路に乗る）。

      if (
        isAuthenticated &&
        requestedTarget.kind === "server" &&
        !session?.user_id
      ) {
        try {
          const remoteSessionId = await flushPendingConversation(sessionId);
          if (remoteSessionId === sessionId) {
            throw new Error("ローカルチャットをServerへ接続できませんでした。");
          }
          onSessionPromoted?.(remoteSessionId);
        } catch (promotionError) {
          if (isLikelyConnectivityFailure(promotionError)) {
            useNetworkStore.getState().setServerReachable(false);
          }
          const promotedSessionId =
            await getPromotedConversationSessionId(sessionId).catch(() => null);
          if (promotedSessionId) {
            // 別経路で既に昇格済み。その Server セッションへ切り替える。
            onSessionPromoted?.(promotedSessionId);
            setIsWaiting(false);
            return;
          }
          // 昇格に失敗しても、フォールバックが解決できればローカル応答へ流す。
          const fallback = appContextSelected
            ? null
            : await getConfiguredFallbackMobileLlmSettings("server").catch(
                () => null,
              );
          if (fallback) {
            setIsStreaming(true);
            try {
              await appendDirectReply(fallback, {
                fallback_from_server: true,
                promotion_error: errorText(
                  promotionError,
                  "ローカルチャットをServerへ接続できませんでした。",
                ),
              });
            } catch (fallbackError) {
              setError(combinedFallbackError(promotionError, fallbackError));
            } finally {
              setIsStreaming(false);
              setIsWaiting(false);
            }
            return;
          }
          setError(
            errorText(
              promotionError,
              "ローカルチャットをServerへ接続できませんでした。",
            ),
          );
          setIsWaiting(false);
        }
        return;
      }

      if (!isAuthenticated) {
        const settings = await getMobileLlmSettings();
        const directMain = isDirectProvider(settings.provider)
          ? settings
          : await getConfiguredDirectMobileLlmSettings(settings.provider);
        if (directMain) {
          setIsStreaming(true);
          try {
            await appendDirectReply(directMain);
          } catch (directError) {
            const fallback = isDirectProvider(settings.provider)
              ? await getConfiguredFallbackMobileLlmSettings(settings.provider)
              : null;
            if (!fallback) {
              setError(errorText(directError, "メッセージ送信に失敗しました。"));
              return;
            }
            try {
              await appendDirectReply(fallback, {
                fallback_from_direct: true,
                main_error: errorText(directError, "direct failed"),
              });
            } catch (fallbackError) {
              setError(combinedFallbackError(directError, fallbackError));
            }
          } finally {
            setIsStreaming(false);
            setIsWaiting(false);
          }
        } else {
          setError(
            "Directモデルまたはフォールバックモデルを設定してください。",
          );
          setIsWaiting(false);
        }
        return;
      }

      const settings = await getMobileLlmSettings();
      if (
        isDirectProvider(settings.provider) &&
        !appContextSelected &&
        !requiresServerRuntime &&
        !forceServer
      ) {
        setIsStreaming(true);
        try {
          await appendDirectReply(settings);
        } catch (directError) {
          const fallback = await getConfiguredFallbackMobileLlmSettings(
            settings.provider,
          );
          if (fallback) {
            try {
              await appendDirectReply(fallback, {
                fallback_from_direct: true,
                main_error: errorText(directError, "direct failed"),
              });
            } catch (fallbackError) {
              setError(combinedFallbackError(directError, fallbackError));
            }
            return;
          }
          setError(errorText(directError, "メッセージ送信に失敗しました。"));
        } finally {
          setIsStreaming(false);
          setIsWaiting(false);
        }
        return;
      }

      generationLifecycleRef.current += 1;
      const selectedServerOption =
        requestedTarget.kind === "server" && requestedTarget.responseModel
          ? responseModelOptionsRef.current.find(
              (option) =>
                option.provider === requestedTarget.responseModel?.provider &&
                option.model === requestedTarget.responseModel?.model,
            )
          : responseModelOptionsRef.current.find((option) => option.isCurrent);
      if (mountedRef.current) {
        setEffectiveGeneration({
          kind: "server",
          provider: selectedServerOption?.provider,
          model: selectedServerOption?.model,
          reasoningEffort: llmModeRef.current || undefined,
          fallback: false,
        });
      }
      const dispatchedGeneration = beginServerGeneration(
        sessionId,
        localMessage.id,
      );
      if (!dispatchedGeneration) {
        setIsWaiting(false);
        setError("進行中の応答が完了してから再試行してください。");
        return;
      }
      generationEventGateRef.current.bindTransportId(
        localMessage.id,
        dispatchedGeneration,
      );
      try {
        await dispatchPendingConversationMessage(
          sessionId,
          localMessage,
          pendingDispatchPayload(localMessage),
          { checkRemoteDuplicate: false },
        );
        setMessages((prev) =>
          prev.map((message) =>
            message.id === localMessage.id
              ? {
                  ...message,
                  metadata: {
                    ...message.metadata,
                    pending: false,
                    message_state: "dispatched",
                  },
                }
              : message,
          ),
        );
        scheduleRefresh();
      } catch (dispatchError) {
        completeServerGeneration(dispatchedGeneration);
        const connectivityFailure = isLikelyConnectivityFailure(dispatchError);
        if (connectivityFailure) {
          useNetworkStore.getState().setServerReachable(false);
        }
        const dispatchErrorText = errorText(
          dispatchError,
          "メッセージ送信に失敗しました。",
        );

        // POSTの応答だけ失われた場合、Serverでは既に受理・生成開始済みのことがある。
        // POST開始後の通信失敗ではDirectへ切り替えず、二重応答を防ぐ。
        if (connectivityFailure) {
          let accepted = false;
          try {
            const remoteMessages = await chatApi.getMessages(sessionId);
            accepted = Boolean(
              findAcceptedRemoteMessage(remoteMessages, localMessage.id),
            );
            if (accepted) {
              try {
                await conversationsRepo.pruneSentLocalMessages(
                  sessionId,
                  remoteMessages,
                );
                await conversationsRepo.saveLocalMessages(
                  sessionId,
                  remoteMessages,
                );
                const reconciledMessages =
                  await conversationsRepo.listMessagesLocal(sessionId);
                if (mountedRef.current) {
                  setMessages(reconciledMessages);
                  setLastRefreshAt(new Date().toISOString());
                }
              } catch {
                // 受理済みなら表示更新失敗だけでDirectへ切り替えない。
              }
            }
          } catch {
            // 受理状況を確認できない間はpendingを維持し、二重応答を生成しない。
          }

          if (accepted) {
            setError(null);
            setIsWaiting(false);
            scheduleRefresh();
            return;
          }

          const failedMetadata =
            buildRetryableServerDispatchMetadata(dispatchErrorText);
          await conversationsRepo.mergeMessageMetadata(
            localMessage.id,
            failedMetadata,
          );
          setMessages((prev) =>
            prev.map((message) =>
              message.id === localMessage.id
                ? {
                    ...message,
                    metadata: { ...message.metadata, ...failedMetadata },
                  }
                : message,
            ),
          );
          setError(
            "送信結果を確認できませんでした。接続復旧後に自動確認します。",
          );
          setIsWaiting(false);
          return;
        }

        const failedMetadata =
          buildRetryableServerDispatchMetadata(dispatchErrorText);
        await conversationsRepo.mergeMessageMetadata(
          localMessage.id,
          failedMetadata,
        );
        setMessages((prev) =>
          prev.map((message) =>
            message.id === localMessage.id
              ? { ...message, metadata: { ...message.metadata, ...failedMetadata } }
              : message,
          ),
        );
        setError(dispatchErrorText);
        setIsWaiting(false);
      }
      } finally {
        finishConversationOperation(exclusiveOperationRef, "send");
      }
    },
    [
      isAuthenticated,
      isStreaming,
      serverGenerationActive,
      beginServerGeneration,
      completeServerGeneration,
      messages,
      maybeGenerateLocalTitle,
      onSessionPromoted,
      scheduleRefresh,
      effectiveProjectId,
      session?.user_id,
      session?.character_name,
      session?.app_id,
      session?.app_target_id,
      sessionId,
    ],
  );

  const retryPendingMessage = useCallback(
    async (message: ConversationMessage) => {
      if (!sessionId || !isAuthenticated) return;
      if (
        retryingMessageIds.includes(message.id) ||
        !tryStartConversationOperation(exclusiveOperationRef, "send")
      ) {
        setError("別の送信が完了してから再試行してください。");
        return;
      }
      setError(null);
      setIsWaiting(true);
      generationLifecycleRef.current += 1;
      const retryGeneration = beginServerGeneration(
        sessionId,
        `retry-${message.id}`,
      );
      if (!retryGeneration) {
        setIsWaiting(false);
        setError("進行中の応答が完了してから再試行してください。");
        return;
      }
      generationEventGateRef.current.bindTransportId(
        message.id,
        retryGeneration,
      );
      setRetryingMessageIds((current) => [...current, message.id]);
      try {
        const result = await attemptPendingRetry(() =>
          dispatchPendingConversationMessage(
            sessionId,
            message,
            pendingDispatchPayload(message, {
              projectId: effectiveProjectId,
              appId: session?.app_id,
              appTargetId: session?.app_target_id,
              includeProjectContext: Boolean(effectiveProjectId),
              agentMode: "confirm",
            }),
            { checkRemoteDuplicate: true },
          ),
        );
        if (!result.ok) {
          completeServerGeneration(retryGeneration);
          if (result.connectivityFailure) {
            useNetworkStore.getState().setServerReachable(false);
          }
          setError(
            errorTextOf(result.error, "未送信メッセージの再送に失敗しました。"),
          );
          return;
        }
        setMessages((prev) =>
          prev.map((candidate) =>
            candidate.id === message.id
              ? {
                  ...candidate,
                  metadata: {
                    ...candidate.metadata,
                    pending: false,
                    message_state: "dispatched",
                  },
                }
              : candidate,
          ),
        );
        try {
          await refreshFromServer();
        } catch (refreshError) {
          setError(
            errorTextOf(
              refreshError,
              "再送には成功しましたが、会話の更新に失敗しました。",
            ),
          );
        }
      } finally {
        setRetryingMessageIds((current) =>
          current.filter((id) => id !== message.id),
        );
        setIsWaiting(false);
        finishConversationOperation(exclusiveOperationRef, "send");
      }
    },
    [
      isAuthenticated,
      beginServerGeneration,
      completeServerGeneration,
      refreshFromServer,
      retryingMessageIds,
      effectiveProjectId,
      session?.app_id,
      session?.app_target_id,
      sessionId,
    ],
  );

  const respondPermission = useCallback((requestId: string, approved: boolean) => {
    wsRef.current.sendPermissionResponse(requestId, approved);
    setPendingPermissions((prev) =>
      prev.map((request) =>
        request.requestId === requestId
          ? { ...request, status: approved ? "approved" : "denied" }
          : request,
      ),
    );
  }, []);

  const pollDeepResearchJob = useCallback(
    (jobId: string) => {
      if (!focusedRef.current || jobPollersRef.current[jobId]) return;
      const requestedSessionId = sessionId;
      const pollingLifecycle = jobPollingLifecycleRef.current;
      const isCurrentSession = () =>
        mountedRef.current &&
        activeGenerationSessionRef.current === requestedSessionId &&
        jobPollingLifecycleRef.current === pollingLifecycle;
      const flightKey = `${pollingLifecycle}:${jobId}`;
      jobPollerStopsRef.current[jobId] =
        conversationPerformanceDiagnostics.trackActive(
          "timer",
          "conversation-job-poller",
        );
      jobPollersRef.current[jobId] = setInterval(() => {
        if (!focusedRef.current) return;
        if (jobPollFlightsRef.current.has(flightKey)) return;
        jobPollFlightsRef.current.add(flightKey);
        void (async () => {
          const job = await deepResearchApi.getJob(jobId);
          if (!isCurrentSession()) return;
          const next = deepResearchToConversationJob(job);
          setJobs((prev) => prev.map((item) => (item.id === jobId ? next : item)));
          if (
            job.status === "completed" ||
            job.status === "failed" ||
            job.status === "cancelled"
          ) {
            const poller = jobPollersRef.current[jobId];
            if (poller) clearInterval(poller);
            delete jobPollersRef.current[jobId];
            jobPollerStopsRef.current[jobId]?.();
            delete jobPollerStopsRef.current[jobId];
            if (
              requestedSessionId &&
              job.status === "completed" &&
              job.report_markdown
            ) {
              const content = `Deep Research 完了\n\n${job.report_markdown}`;
              await conversationsRepo.appendLocalMessage(
                requestedSessionId,
                "assistant",
                content,
                {
                  local_only: true,
                  job_id: job.id,
                  job_type: "deep_research",
                  message_state: "persisted",
                },
              );
              if (!isCurrentSession()) return;
              await refreshFromServer().catch(() => undefined);
            }
          }
        })()
          .catch((pollError) => {
            if (isCurrentSession()) {
              setError(
                pollError instanceof Error
                  ? pollError.message
                  : "ジョブ更新に失敗しました。",
              );
            }
          })
          .finally(() => {
            jobPollFlightsRef.current.delete(flightKey);
          });
      }, 4000);
    },
    [refreshFromServer, sessionId],
  );

  const startDeepResearch = useCallback(
    async (query: string) => {
      if (!query.trim() || !isAuthenticated || !focusedRef.current) return;
      const requestedSessionId = sessionId;
      const pollingLifecycle = jobPollingLifecycleRef.current;
      const job = await deepResearchApi.startJob({
        query: query.trim(),
        mode: "report",
        max_iterations: 2,
        questions_per_iteration: 3,
        max_results_per_query: 5,
        engines: ["duckduckgo"],
        include_local_rag: Boolean(effectiveProjectId),
        project_id: effectiveProjectId ?? null,
      });
      if (
        !mountedRef.current ||
        activeGenerationSessionRef.current !== requestedSessionId ||
        jobPollingLifecycleRef.current !== pollingLifecycle
      ) {
        return;
      }
      const mapped = deepResearchToConversationJob(job);
      setJobs((prev) => [mapped, ...prev.filter((item) => item.id !== mapped.id)]);
      pollDeepResearchJob(job.id);
    },
    [effectiveProjectId, isAuthenticated, pollDeepResearchJob, sessionId],
  );

  useEffect(() => {
    if (!isFocused) return;
    for (const job of jobs) {
      if (job.status === "queued" || job.status === "running") {
        pollDeepResearchJob(job.id);
      }
    }
  }, [isFocused, jobs, pollDeepResearchJob]);

  const editMessage = useCallback(
    async (message: ConversationMessage, content: string) => {
      await sendConversationCommand({
        message: content,
        projectId: effectiveProjectId,
        includeProjectContext: Boolean(effectiveProjectId),
        agentMode: "confirm",
        editMessageId: message.id,
      });
    },
    [effectiveProjectId, sendConversationCommand],
  );

  const rerunMessage = useCallback(
    async (
      message: ConversationMessage,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (message.role === "user") {
        await sendConversationCommand({
          message: message.content,
          projectId: effectiveProjectId,
          includeProjectContext: Boolean(effectiveProjectId),
          agentMode: "confirm",
          editMessageId: message.id,
          target: { kind: "server", responseModel },
        });
        return;
      }
      const index = messages.findIndex((entry) => entry.id === message.id);
      const source = [...messages]
        .slice(0, index >= 0 ? index : messages.length)
        .reverse()
        .find((entry) => entry.role === "user");
      if (source) {
        await sendConversationCommand({
          message: source.content,
          projectId: effectiveProjectId,
          includeProjectContext: Boolean(effectiveProjectId),
          agentMode: "confirm",
          editMessageId: source.id,
          target: { kind: "server", responseModel },
        });
      }
    },
    [effectiveProjectId, messages, sendConversationCommand],
  );

  const loadBranches = useCallback(
    async (messageId: string) => {
      if (!sessionId || !isAuthenticated) return;
      const branches = await conversationsRepo.fetchBranches(sessionId, messageId);
      if (!branches.length) return;
      const activeIndex = branches.findIndex((entry) => entry.is_active_branch);
      const key = groupMessageKey(branches[0]);
      setBranchSelections((prev) => ({
        ...prev,
        [key]: activeIndex >= 0 ? activeIndex : 0,
      }));
      await refreshFromServer();
    },
    [isAuthenticated, refreshFromServer, sessionId],
  );

  const switchBranch = useCallback(
    async (message: ConversationMessage, nextIndex: number) => {
      if (!sessionId) return;
      setError(null);
      try {
        const switched = await switchConversationBranchWithFallback({
          sessionId,
          message,
          nextIndex,
          localMessages: messages,
          runtime: {
            fetchBranches: (targetSessionId, messageId) =>
              conversationsRepo.fetchBranches(targetSessionId, messageId),
            switchBranch: (targetSessionId, messageId, branchIndex) =>
              conversationsRepo.switchBranch(
                targetSessionId,
                messageId,
                branchIndex,
              ),
            refresh: refreshFromServer,
          },
        });
        if (!switched) return;
        setBranchSelections((prev) => ({
          ...prev,
          [groupMessageKey(message)]: nextIndex,
        }));
      } catch (branchError) {
        setError(errorTextOf(branchError, "分岐を切り替えられませんでした。"));
      }
    },
    [messages, refreshFromServer, sessionId],
  );

  const flushPendingInBackground = useCallback(
    async (targetSessionId: string): Promise<string | null> => {
      const result = await runExclusiveConversationOperation(
        exclusiveOperationRef,
        "background-flush",
        async () => {
          if (mountedRef.current) setIsWaiting(true);
          try {
            return await flushPendingConversation(targetSessionId);
          } finally {
            if (mountedRef.current) setIsWaiting(false);
          }
        },
      );
      return result.started ? result.value : null;
    },
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!initialAppContext?.appId || !session || !isAuthenticated) return;
    const key = `${session.id}:${initialAppContext.appId}:${initialAppContext.appTargetId ?? ""}:${initialAppContext.projectId ?? ""}`;
    if (
      session.app_id === initialAppContext.appId &&
      (session.app_target_id ?? null) === (initialAppContext.appTargetId ?? null)
    ) {
      appContextAttemptRef.current = key;
      return;
    }
    if (appContextAttemptRef.current === key) return;
    appContextAttemptRef.current = key;
    void bindAppContext(initialAppContext).catch((bindingError) => {
      appContextAttemptRef.current = null;
      if (mountedRef.current) {
        setError(errorTextOf(bindingError, "Appコンテキストの設定に失敗しました。"));
      }
    });
  }, [bindAppContext, initialAppContext, isAuthenticated, session]);

  useEffect(() => {
    const requestId = ++llmPreferenceHydrationRef.current;
    const isCurrent = () =>
      mountedRef.current && requestId === llmPreferenceHydrationRef.current;
    const defaults = createDefaultChatLlmPreferences();

    // account/server切替直後に前scopeの候補を一瞬表示しない。
    llmPreferenceScopeRef.current = null;
    llmPreferencesRef.current = defaults;
    setEffectiveGeneration(null);
    responseTargetRef.current = defaults.responseTarget;
    responseModelOptionsRef.current = defaults.responseModelOptions;
    applyLlmModeView(defaults.mode);
    setResponseTarget(defaults.responseTarget);
    setResponseModelOptions(defaults.responseModelOptions);
    setResponseModelOptionsLoading(false);
    setLlmPreferencesReady(false);
    setLlmModeSyncStatus("idle");
    setLlmSelectionMessage(null);

    void (async () => {
      const accountScope = isAuthenticated
        ? userId
          ? `auth:${userId}`
          : undefined
        : "anonymous";
      const scope = await resolveCurrentChatLlmPreferenceScope(accountScope);
      const cached = await readChatLlmPreferences(scope);
      if (!isCurrent()) return;

      const preferences = cached ?? createDefaultChatLlmPreferences();
      llmPreferenceScopeRef.current = scope;
      llmPreferencesRef.current = preferences;
      responseTargetRef.current = preferences.responseTarget;
      responseModelOptionsRef.current = preferences.responseModelOptions;
      llmModeSynchronizerRef.current?.setScope(scope);
      applyLlmModeView(preferences.mode);
      setResponseTarget(preferences.responseTarget);
      setResponseModelOptions(preferences.responseModelOptions);
      setLlmPreferencesReady(true);

      if (
        preferences.modeSyncPending &&
        isAuthenticated &&
        preferences.mode.mode !== SERVER_DEFAULT_MODE
      ) {
        const network = useNetworkStore.getState();
        llmModeSynchronizerRef.current?.enqueue(
          scope,
          preferences.mode.mode,
          {
            immediate: true,
            defer: !network.online || isServerKnownUnreachable(),
          },
        );
      }

      if (isAuthenticated) {
        // cacheをpaintした後にだけserver revalidationを開始する。
        void refreshLlmModeForScope(scope);
        void refreshResponseModelOptionsForScope(scope);
      }
    })().catch(() => {
      if (isCurrent()) setLlmPreferencesReady(true);
    });

    return () => {
      if (llmPreferenceHydrationRef.current === requestId) {
        llmPreferenceHydrationRef.current += 1;
      }
    };
  }, [
    applyLlmModeView,
    isAuthenticated,
    refreshLlmModeForScope,
    refreshResponseModelOptionsForScope,
    userId,
  ]);

  useEffect(() => {
    if (!isAuthenticated || (!networkOnline && !networkServerReachable)) return;
    llmModeSynchronizerRef.current?.retry();
  }, [isAuthenticated, networkOnline, networkServerReachable]);

  useEffect(() => {
    void refreshSkillCommands();
  }, [refreshSkillCommands]);

  useEffect(() => {
    if (
      !sessionId ||
      !session ||
      session.user_id ||
      !isAuthenticated ||
      !networkOnline ||
      !networkServerReachable ||
      pendingMessages === 0 ||
      runState !== "idle"
    ) {
      return;
    }
    void flushPendingInBackground(sessionId)
      .then((remoteSessionId) => {
        if (!remoteSessionId) return;
        if (remoteSessionId === sessionId) {
          scheduleTerminalRefresh(0);
          return;
        }
        onSessionPromoted?.(remoteSessionId);
      })
      .catch(() => undefined);
  }, [
    isAuthenticated,
    networkOnline,
    networkServerReachable,
    flushPendingInBackground,
    onSessionPromoted,
    pendingMessages,
    runState,
    scheduleTerminalRefresh,
    session,
    sessionId,
  ]);

  useEffect(() => {
    if (!isFocused || !sessionId || !isAuthenticated || !session?.user_id) {
      setIsConnected(false);
      return;
    }

    const ws = wsRef.current;
    const runtimeFocusEpoch = focusEpoch;
    const isCurrentRuntime = () =>
      mountedRef.current &&
      focusedRef.current &&
      focusEpochRef.current === runtimeFocusEpoch &&
      activeGenerationSessionRef.current === sessionId;
    ws.setOnConnectionChange((connected) => {
      if (!isCurrentRuntime()) return;
      setIsConnected(connected);
      if (connected) {
        llmModeSynchronizerRef.current?.retry();
        if (pendingMessagesRef.current > 0) {
          void flushPendingInBackground(sessionId)
            .then((remoteSessionId) => {
              if (!remoteSessionId) return undefined;
              if (remoteSessionId === sessionId) {
                scheduleTerminalRefresh(0);
                return undefined;
              }
              onSessionPromoted?.(remoteSessionId);
              return undefined;
            })
            .catch(() => undefined);
        }
      }
    });
    ws.setOnMessage((msg: WSMessage) => {
      if (!isCurrentRuntime()) return;
      switch (msg.type) {
        case "llm_mode_change": {
          const payload = parseLlmModePayload(msg.data ?? msg);
          if (payload) {
            const scope = llmPreferenceScopeRef.current;
            if (scope) applyServerLlmMode(payload, scope);
            else applyLlmModeView(payload);
          }
          break;
        }
        case "external_llm_permission_request": {
          const data = (msg.data ?? {}) as Record<string, unknown>;
          const requestId = String(data.request_id ?? "");
          if (!requestId) return;
          setPendingPermissions((prev) => [
            {
              requestId,
              toolName: String(data.tool_name ?? data.tool ?? "tool"),
              description: String(data.description ?? "このツール実行を許可しますか？"),
              riskSummary:
                typeof data.risk_summary === "string" ? data.risk_summary : undefined,
              toolArgs:
                data.tool_args && typeof data.tool_args === "object"
                  ? (data.tool_args as Record<string, unknown>)
                  : {},
              receivedAt: new Date().toISOString(),
              status: "pending",
            },
            ...prev.filter((request) => request.requestId !== requestId),
          ]);
          break;
        }
        case "tool_start":
          setActiveTool(String(msg.tool ?? "unknown"));
          setActivityMessage(
            extractActivityMessage(msg) ??
              `${String(msg.tool ?? "ツール")} を実行しています...`,
          );
          break;
        case "tool_end":
          setActiveTool(null);
          setActivityMessage(
            extractActivityMessage(msg) ?? "ツール実行が完了しました。",
          );
          break;
        case "reasoning_progress":
        case "status_update":
          setActivityMessage(extractActivityMessage(msg));
          break;
        case "new_message":
          setActivityMessage(null);
          setIsWaiting(false);
          {
            const data =
              msg.data && typeof msg.data === "object"
                ? (msg.data as Record<string, unknown>)
                : {};
            const role = String(data.type ?? msg.role ?? "");
            const clientMessageId = String(data.client_message_id ?? "").trim();
            const isLocalUserMessage =
              role === "user" &&
              Boolean(clientMessageId) &&
              messagesStateRef.current.some(
                (message) =>
                  message.id === clientMessageId ||
                  message.metadata?.client_message_id === clientMessageId,
              );
            // 自分が楽観表示済みのuserイベントだけは再取得しない。
            // 外部端末の入力や保存されないassistant/system通知は反映する。
            if (!isLocalUserMessage) scheduleTerminalRefresh(0);
          }
          break;
        case "conversation_persisted": {
          if (isAssistantPersistenceEvent(msg)) {
            const expectedIdentity =
              generationEventGateRef.current.matchingTerminal(
                msg,
                getGenerationIdentity(),
                { allowIdentityless: false },
              );
            if (expectedIdentity) {
              generationLifecycleRef.current += 1;
              clearServerGenerationState("terminal", expectedIdentity);
              generationEventGateRef.current.complete(expectedIdentity);
            }
            // グループ応答ではassistant保存イベントが複数回届くため、
            // stream_endとの順序に依存せず、最後のイベントから一度だけ
            // 取得するようdebounceする。
            scheduleTerminalRefresh(350, Boolean(expectedIdentity));
          }
          break;
        }
        case "stream_start":
          generationLifecycleRef.current += 1;
          {
            const data =
              msg.data && typeof msg.data === "object"
                ? (msg.data as Record<string, unknown>)
                : {};
            const requestId = String(
              msg.agent_run_id ?? data.agent_run_id ?? `ws-${Date.now()}`,
            );
            const identity =
              getGenerationIdentity() ??
              beginServerGeneration(sessionId, requestId);
            if (!identity) break;
            generationEventGateRef.current.bind(msg, identity);
            streamBufferRef.current?.switchIdentity({
              sessionId: identity.sessionId,
              lifecycleId: identity.lifecycleId,
            });
            markServerGenerationStreaming();
          }
          setIsWaiting(false);
          setIsStreaming(true);
          setActiveTool(null);
          setActivityMessage(extractActivityMessage(msg) ?? "応答を生成しています...");
          setStreamContent("");
          break;
        case "stream_token":
          if (msg.content) {
            const identity = getGenerationIdentity();
            if (identity) {
              streamBufferRef.current?.append(
                {
                  sessionId: identity.sessionId,
                  lifecycleId: identity.lifecycleId,
                },
                String(msg.content),
              );
            }
          }
          break;
        case "stream_end":
        case "response": {
          const expectedIdentity =
            generationEventGateRef.current.matchingTerminal(
              msg,
              getGenerationIdentity(),
              { allowIdentityless: true },
            );
          if (expectedIdentity) {
            generationLifecycleRef.current += 1;
            clearServerGenerationState("terminal", expectedIdentity);
            generationEventGateRef.current.complete(expectedIdentity);
          }
          scheduleTerminalRefresh();
          break;
        }
        case "stream_cancelled": {
          const expectedIdentity =
            generationEventGateRef.current.matchingTerminal(
              msg,
              getGenerationIdentity(),
              { allowIdentityless: true },
            );
          if (!expectedIdentity) {
            for (const persistedMessage of cancelledAssistantMessages(
              msg,
              sessionId,
              "",
            )) {
              upsertServerMessage(persistedMessage);
            }
            scheduleTerminalRefresh(0);
            break;
          }
          generationLifecycleRef.current += 1;
          if (msg.status === "cancellation_pending") {
            markServerGenerationCancelling();
            setIsWaiting(true);
            setIsStreaming(false);
            setActiveTool(null);
            setActivityMessage("停止処理を継続しています…");
            break;
          }
          cancelScheduledRefresh();
          const partialStreamContent = finalizeStream(
            "cancel",
            expectedIdentity,
          );
          const persistedMessages = cancelledAssistantMessages(
            msg,
            sessionId,
            partialStreamContent,
          );
          for (const persistedMessage of persistedMessages) {
            upsertServerMessage(persistedMessage);
          }
          const failedRunIds = Array.isArray(msg.persistence_failed_run_ids)
            ? msg.persistence_failed_run_ids.filter(
                (item): item is string => typeof item === "string",
              )
            : [];
          const liveRunId = String(msg.agent_run_id ?? "").trim();
          const failedBufferRunId =
            failedRunIds.length === 1
              ? failedRunIds[0]
              : failedRunIds.length > 1
                ? undefined
                : (liveRunId || undefined);
          const failedBufferKey =
            failedRunIds.length > 0
              ? [...failedRunIds].sort().join("-")
              : (failedBufferRunId ?? "unknown-run");
          if (
            msg.persistence_failed === true &&
            partialStreamContent.trim()
          ) {
            void preserveFailedCancelledMessage(
              sessionId,
              partialStreamContent,
              failedBufferRunId,
              failedBufferKey,
            );
          }
          clearServerGenerationState("cancel", expectedIdentity);
          generationEventGateRef.current.complete(expectedIdentity);
          scheduleTerminalRefresh(0);
          if (msg.persistence_failed === true) {
            setError("停止しましたが、一部の途中応答を保存できませんでした。");
          }
          break;
        }
        case "conversation_title_updated":
        case "title_updated": {
          const data =
            msg.data && typeof msg.data === "object"
              ? (msg.data as Record<string, unknown>)
              : {};
          const eventSessionId = String(msg.session_id ?? data.session_id ?? "");
          const title = String(msg.title ?? data.title ?? "").trim();
          const source =
            msg.source === "fallback" || data.source === "fallback"
              ? "fallback"
              : "llm";
          if (title && (!eventSessionId || eventSessionId === sessionId)) {
            void applyGeneratedTitle(sessionId, title, source);
          }
          break;
        }
      }
    });
    void ws.connect(sessionId);
    return () => ws.disconnect();
  }, [
    applyLlmModeView,
    applyServerLlmMode,
    applyGeneratedTitle,
    beginServerGeneration,
    cancelScheduledRefresh,
    clearServerGenerationState,
    finalizeStream,
    focusEpoch,
    flushPendingInBackground,
    getGenerationIdentity,
    isAuthenticated,
    isFocused,
    markServerGenerationCancelling,
    markServerGenerationStreaming,
    onSessionPromoted,
    preserveFailedCancelledMessage,
    scheduleTerminalRefresh,
    session?.user_id,
    sessionId,
    upsertServerMessage,
  ]);

  useEffect(() => {
    return () => {
      for (const poller of Object.values(jobPollersRef.current)) {
        clearInterval(poller);
      }
      for (const stopTracking of Object.values(jobPollerStopsRef.current)) {
        stopTracking();
      }
      jobPollersRef.current = {};
      jobPollerStopsRef.current = {};
      jobPollFlightsRef.current.clear();
    };
  }, []);

  return {
    session,
    messages,
    visibleMessages,
    timeline,
    diagnostics,
    commands,
    pendingPermissions,
    jobs,
    loading,
    error,
    streamContent,
    llmMode,
    llmModeOptions,
    llmModeLabels,
    llmModeKind,
    llmModeSyncStatus,
    llmSelectionMessage,
    llmPreferencesReady,
    effectiveGeneration,
    responseModelOptions,
    responseModelOptionsLoading,
    responseTarget,
    skillCommands,
    retryingMessageIds,
    branchSelections,
    load,
    refreshFromServer,
    stopGeneration,
    sendConversationCommand,
    retryPendingMessage,
    respondPermission,
    startDeepResearch,
    editMessage,
    rerunMessage,
    loadBranches,
    switchBranch,
    changeLlmMode,
    changeResponseTarget,
    refreshLlmMode,
    refreshSkillCommands,
    changeCharacter,
    serverGenerationActive,
    agentRuns,
    agentRunErrors,
    retryAgentRun,
    updateSessionTitle,
    changeProject,
    bindAppContext,
    steerGeneration,
    groupRespond,
    forkConversation,
    getContextSnapshot,
  };
}
