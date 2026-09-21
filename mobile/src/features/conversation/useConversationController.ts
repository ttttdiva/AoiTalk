import { readLocalTaskContext } from "./local-task-context";
import { useProjectStore } from "../../stores/project";
import { getToken, getTokenAuthScope } from "../../lib/auth";
import { getConfiguredApiServerFingerprint } from "../../lib/api-client";
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
import { applyRemoteConversationMessages } from "../../repositories/conversations";
import { runForegroundSqliteWrite } from "../../db/sqlite-write-coordinator";
import { prepareDirectConversationHandoff } from "./direct-handoff";
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
  getCachedLlmModelCatalog,
  getCachedSkillSlashCommands,
  refreshLlmModelCatalog,
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
import { resolveServerModelEffort, snapshotServerModelTarget } from "./server-model-effort";
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
import { ConversationSubmissionQueue, RetainedSubmissionError, conversationMessageIdentity, useSubmissionTimeline } from "./conversation-submissions";

type ControllerArgs = {
  sessionId?: string | null;
  isAuthenticated: boolean;
  userId?: string | null;
  userRole?: string | null;
  selectedProjectId?: string | null;
  onSessionPromoted?: (sessionId: string) => void;
  initialAppContext?: ChatAppContext | null;
};

/** Direct cloud providers require Internet, unlike a LAN AoiTalk server. */
export function canDispatchDirectCloud(
  networkOnline: boolean | null | undefined,
): boolean {
  return networkOnline !== false;
}

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
          : job.status === "interrupted"
            ? "failed"
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
  retryPendingMessage: (message: ConversationMessage, route?: "server" | "direct") => Promise<void>;
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
  refreshResponseModelOptions: () => Promise<void>;
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
  // `online` is Internet-only.  Conversation REST/WebSocket transport uses
  // the physical route so a LAN AoiTalk server remains usable without WAN.
  const networkConnected = useNetworkStore(
    (state) => state.connected ?? state.online,
  );
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
  const { messages, setMessages, submitMessage, promoteMessages } = useSubmissionTimeline(sessionId ?? "");
  const submissionQueueRef = useRef(new ConversationSubmissionQueue());
  const backgroundFlushRef = useRef<Promise<unknown> | null>(null);
  const latestSubmissionIdRef = useRef<string | null>(null);
  const promotedSubmissionSessionsRef = useRef(new Map<string, string>());
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
  const responseModelOptionsRef = useRef<ChatResponseModelOption[]>([]);
  const responseTargetRef = useRef<ChatResponseTarget>({ kind: "server" });
  const appContextAttemptRef = useRef<string | null>(null);
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
    return () => {
      mountedRef.current = false;
      loadRequestRef.current += 1;
      llmPreferenceHydrationRef.current += 1;
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
    target: responseTarget.kind,
    connected: networkConnected,
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
      target: responseTarget.kind,
      connected: networkConnected,
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

  const refreshResponseModelOptionsForScope = useCallback(
    async (scope: string, force = false) => {
      if (!isAuthenticated) return;
      if (force || responseModelOptionsRef.current.length === 0) {
        setResponseModelOptionsLoading(true);
      }
      try {
        const catalog = await (force
          ? refreshLlmModelCatalog(scope)
          : getCachedLlmModelCatalog(scope));
        if (llmPreferenceScopeRef.current !== scope) return;

        const options = buildResponseModelOptions(catalog);
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

  const refreshResponseModelOptions = useCallback(async () => {
    if (!isAuthenticated) return;
    const scope =
      llmPreferenceScopeRef.current ??
      (await resolveCurrentChatLlmPreferenceScope(
        userId ? `auth:${userId}` : undefined,
      ));
    await refreshResponseModelOptionsForScope(scope, true);
  }, [isAuthenticated, refreshResponseModelOptionsForScope, userId]);

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

  const selectedServerEffort = useMemo(
    () => resolveServerModelEffort(responseTarget, responseModelOptions),
    [responseTarget, responseModelOptions],
  );
  const changeLlmMode = useCallback((mode: string) => {
    const selected = resolveServerModelEffort(responseTargetRef.current, responseModelOptionsRef.current);
    if (!selected.model || !selected.options.includes(mode)) return;
    changeResponseTarget({ kind: "server", responseModel: {
      provider: selected.model.provider,
      model: selected.model.model,
      reasoning_effort: mode,
    } });
    setLlmModeSyncStatus("idle");
  }, [changeResponseTarget]);
  const refreshLlmMode = refreshResponseModelOptions;

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

  const deliverConversationCommand = useCallback(
    async (command: SendConversationCommand, optimistic: ConversationMessage, signal: AbortSignal, acceptedScope: Promise<{ auth: string; server: string }>) => {
      await backgroundFlushRef.current;
      const scope = await acceptedScope;
      const assertDirectScope = async () => {
        if (getTokenAuthScope(await getToken()) !== scope.auth || await getConfiguredApiServerFingerprint() !== scope.server) {
          throw new Error("送信中に接続先またはアカウントが変わりました。元の接続先で再試行してください。");
        }
      };
      await assertDirectScope();
      const sourceSessionId = optimistic.session_id;
      const sessionId = promotedSubmissionSessionsRef.current.get(sourceSessionId)
        ?? await getPromotedConversationSessionId(sourceSessionId).catch(() => null)
        ?? sourceSessionId;
      const session = sessionStateRef.current?.id === sessionId
        ? sessionStateRef.current : await conversationsRepo.getSessionLocal(sessionId);
      const history = messagesStateRef.current;
      const cutoff = history.findIndex((message) => conversationMessageIdentity(message) === optimistic.id);
      const messages = cutoff >= 0 ? history.slice(0, cutoff) : history.filter((message) => (message.created_at ?? "") <= (optimistic.created_at ?? ""));
      const promote = (remoteId: string) => {
        promotedSubmissionSessionsRef.current.set(sourceSessionId, remoteId);
        promoteMessages(sourceSessionId, remoteId);
        onSessionPromoted?.(remoteId);
      };
      // The optimistic identity is persisted before any profile/network lookup.
      let localMessage = await conversationsRepo.appendLocalMessage(sessionId, "user", command.message.trim(), { ...optimistic.metadata, dispatch_auth_scope: scope.auth, dispatch_server_fingerprint: scope.server }, {
        id: optimistic.id, created_at: optimistic.created_at ?? new Date().toISOString(),
      }).catch((failure: unknown) => {
        throw Object.assign(new Error(errorTextOf(failure, "メッセージを保存できませんでした。")), { persistenceFailed: true });
      });
      let sendFailure: string | null = null;
      const reportSendError = (message: string | null) => { sendFailure = message; if (latestSubmissionIdRef.current === optimistic.id) setError(message); };
      const text = command.message.trim();
      if (!text) return;
      if (!tryStartConversationOperation(exclusiveOperationRef, "send")) {
        reportSendError(
          "キャラクター変更または別の送信が完了してから再試行してください。",
        );
        throw new Error("キャラクター変更または別の送信が完了してから再試行してください。");
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
        const taskScope = {
          projectId: (command.projectId === undefined ? effectiveProjectId : command.projectId) ?? null,
          spaceId: useProjectStore.getState().selectedSpaceId,
        };
        const requestedTarget = snapshotServerModelTarget(command.target ?? { kind: "server" }, responseModelOptionsRef.current);
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
        reportSendError("組み込みコマンドとSkillsはServerモデルで実行してください。");
        return;
      }
      if (appContextSelected && requestedTarget.kind === "direct") {
        reportSendError("App context付きChatではDirect/端末モデルを利用できません。");
        return;
      }
      if (appContextSelected && !session?.user_id) {
        reportSendError("Appを紐付ける前にServerへ接続してください。");
        return;
      }
      if (requiresServerRuntime && !isAuthenticated) {
        reportSendError("組み込みコマンド、Skills、モデル指定はログイン中のみ利用できます。");
        return;
      }
      if (requestedTarget.kind === "direct" && !canDispatchDirectCloud(networkOnline)) {
        reportSendError("Directモデルにはインターネット接続が必要です。");
        return;
      }

      let directSettings: MobileLlmSettings | null = null;
      let fallbackFromServer = false;
      const serverTransportUnavailable =
        requestedTarget.kind === "server" &&
        (!networkConnected || isServerKnownUnreachable());
      const serverKnownUnreachable =
        requestedTarget.kind === "server" &&
        !appContextSelected &&
        !requiresServerFeature &&
        serverTransportUnavailable;
      if (requestedTarget.kind === "direct") {
        directSettings = await getDirectMobileLlmSettings(requestedTarget.selection);
      } else if (serverKnownUnreachable) {
        directSettings = await getConfiguredFallbackMobileLlmSettings("server");
        fallbackFromServer = Boolean(directSettings);
      }

      reportSendError(null);
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
      const deliveryMetadata = {
        submission_id: command.submissionId,
        persistence_error: false,
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
      };
      await conversationsRepo.mergeMessageMetadata(localMessage.id, deliveryMetadata);
      localMessage = { ...localMessage, metadata: { ...localMessage.metadata, ...deliveryMetadata } };
      setMessages((prev) => upsertConversationMessage(prev, localMessage));

      let directHandoffPrepared = false;
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
        await assertDirectScope();
        await conversationsRepo.beginDirectReply(localMessage);
        const assertNotInterrupted = () => {
          if (signal.aborted) throw Object.assign(new Error("次の入力で応答を中断しました。"), { name: "AbortError" });
        };
        let assistantMessage: ConversationMessage;
        let persistedMetadata: Record<string, unknown>;
        try {
          assertNotInterrupted();
          if (!directHandoffPrepared) {
            await prepareDirectConversationHandoff({
              sessionId,
              canStopServer: Boolean(isAuthenticated && session?.user_id &&
                networkConnected && !isServerKnownUnreachable()),
              signal,
              assertScope: assertDirectScope,
              retireServerGeneration: () => {
                const previous = getGenerationIdentity();
                if (previous?.sessionId !== sessionId) return;
                clearServerGenerationState("cancel", previous);
                generationEventGateRef.current.complete(previous);
              },
              persistInterruptedMessages: async (interrupted) => {
                await runForegroundSqliteWrite(() => applyRemoteConversationMessages(interrupted));
                setMessages((current) => interrupted.reduce(upsertConversationMessage, current));
              },
            });
            directHandoffPrepared = true;
            // Retiring the Server lifecycle clears its indicators. This turn
            // now owns them, including when a provider fallback reuses it.
            if (mountedRef.current) { setIsStreaming(true); setIsWaiting(true); }
          }
          const taskContext = await readLocalTaskContext(taskScope);
          await assertDirectScope();
          const reply = await generateCharacterAwareDirectReply(
            directSettings,
            messages.filter((message) => message.id !== localMessage.id),
            text,
            resolveCharacterSnapshot,
            async (settings, history, input, profile) => {
              assertNotInterrupted();
              await assertDirectScope();
              effectiveMetadata.character_slug = profile?.slug;
              effectiveMetadata.character_profile_source = (profile as { id?: string } | null)?.id === "offline-project-manager" ? "bundled" : "device-cache";
              effectiveMetadata.task_scope = taskScope;
              return generateMobileLlmReply(settings, history, input, profile, { signal }, taskContext);
            },
          );
          await assertDirectScope();
          assertNotInterrupted();
          assistantMessage = await conversationsRepo.completeDirectReply(localMessage, reply.content, {
            provider: directSettings.provider, model: directSettings.model,
            ...(directSettings.reasoningEffort ? { reasoning_effort: directSettings.reasoningEffort } : {}),
            ...(reply.assistantPayload ? { [KIMI_ASSISTANT_PAYLOAD_METADATA_KEY]: reply.assistantPayload } : {}),
            ...effectiveMetadata,
          });
          persistedMetadata = buildDirectReplyPersistedMetadata({ ...effectiveMetadata, direct_error: null });
        } catch (failure) {
          await assertDirectScope();
          if (signal.aborted) {
            const interrupted = { pending: false, delivery_route: "direct", message_state: "interrupted", direct_error: null };
            await conversationsRepo.mergeMessageMetadata(localMessage.id, interrupted);
            setMessages((prev) => prev.map((message) => message.id === localMessage.id
              ? { ...message, metadata: { ...message.metadata, ...interrupted } } : message));
            return;
          }
          const directError = errorTextOf(failure, "Direct応答に失敗しました。");
          const failedMetadata = { pending: false, delivery_route: "direct", message_state: "direct-failed", direct_error: directError };
          await conversationsRepo.mergeMessageMetadata(localMessage.id, failedMetadata);
          setMessages((prev) => prev.map((message) => message.id === localMessage.id
            ? { ...message, metadata: { ...message.metadata, ...failedMetadata } } : message));
          throw failure;
        }
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
        if (assistantMessage.session_id !== sessionId) promote(assistantMessage.session_id);
        if (session) {
          void maybeGenerateLocalTitle(
            session,
            [...messages, localMessage, assistantMessage],
            directSettings,
          );
        }
      };

      const markDirectUnavailable = async () => {
        const directUnavailableMessage =
          "Directモデルにはインターネット接続が必要です。";
        const failedMetadata = {
          pending: false,
          message_state: "direct-failed",
          delivery_route: "direct",
          direct_error: directUnavailableMessage,
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
        reportSendError(directUnavailableMessage);
        setIsWaiting(false);
      };

      const keepServerFallbackPending = async () => {
        const directUnavailableMessage =
          "Directフォールバックにはインターネット接続が必要です。";
        const pendingMetadata = {
          pending: true,
          message_state: "queued",
          delivery_route: "server",
          direct_error: directUnavailableMessage,
        };
        await conversationsRepo.mergeMessageMetadata(
          localMessage.id,
          pendingMetadata,
        );
        setMessages((prev) =>
          prev.map((message) =>
            message.id === localMessage.id
              ? { ...message, metadata: { ...message.metadata, ...pendingMetadata } }
              : message,
          ),
        );
        reportSendError(directUnavailableMessage);
        setIsWaiting(false);
      };

      const errorText = errorTextOf;
      const combinedFallbackError = describeFallbackFailure;

      if (directSettings && !canDispatchDirectCloud(networkOnline)) {
        if (fallbackFromServer) {
          await keepServerFallbackPending();
        } else {
          await markDirectUnavailable();
        }
        return;
      }

      if (directSettings) {
        setIsStreaming(true);
        try {
          await appendDirectReply(directSettings, {
            ...(requestedTarget.kind === "direct" ? { direct_selected: true } : {}),
            ...(fallbackFromServer ? { fallback_from_server: true } : {}),
          });
        } catch (directError) {
          reportSendError(errorText(directError, "Direct応答に失敗しました。"));
        } finally {
          setIsStreaming(false);
          setIsWaiting(false);
        }
        return;
      }

      // Keep the local pending message, but do not even start a Server
      // request when the physical path is absent or a recent transport
      // failure is still inside its retry TTL.  This also protects the
      // promotion/dispatch path from accidentally probing while offline.
      if (serverTransportUnavailable) {
        reportSendError("AoiTalkサーバーへの接続を確認できるまで送信を保留します。");
        setIsWaiting(false);
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
          promote(remoteSessionId);
        } catch (promotionError) {
          if (isLikelyConnectivityFailure(promotionError)) {
            useNetworkStore.getState().setServerReachable(false);
          }
          const promotedSessionId =
            await getPromotedConversationSessionId(sessionId).catch(() => null);
          if (promotedSessionId) {
            // 別経路で既に昇格済み。その Server セッションへ切り替える。
            promote(promotedSessionId);
            setIsWaiting(false);
            return;
          }
          // 昇格に失敗しても、フォールバックが解決できればローカル応答へ流す。
          const fallback = appContextSelected
            ? null
            : await getConfiguredFallbackMobileLlmSettings("server").catch(
                () => null,
              );
          if (fallback && !canDispatchDirectCloud(networkOnline)) {
            await keepServerFallbackPending();
            return;
          }
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
              reportSendError(combinedFallbackError(promotionError, fallbackError));
            } finally {
              setIsStreaming(false);
              setIsWaiting(false);
            }
            return;
          }
          reportSendError(
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
          if (!canDispatchDirectCloud(networkOnline)) {
            await markDirectUnavailable();
            return;
          }
          setIsStreaming(true);
          try {
            await appendDirectReply(directMain);
          } catch (directError) {
            const fallback = isDirectProvider(settings.provider)
              ? await getConfiguredFallbackMobileLlmSettings(settings.provider)
              : null;
            if (!fallback) {
              reportSendError(errorText(directError, "メッセージ送信に失敗しました。"));
              return;
            }
            try {
              await appendDirectReply(fallback, {
                fallback_from_direct: true,
                main_error: errorText(directError, "direct failed"),
              });
            } catch (fallbackError) {
              reportSendError(combinedFallbackError(directError, fallbackError));
            }
          } finally {
            setIsStreaming(false);
            setIsWaiting(false);
          }
        } else {
          reportSendError(
            "Directモデルまたはフォールバックモデルを設定してください。",
          );
          setIsWaiting(false);
        }
        return;
      }

      const settings = await getMobileLlmSettings();
      if (
        isDirectProvider(settings.provider) &&
        canDispatchDirectCloud(networkOnline) &&
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
              reportSendError(combinedFallbackError(directError, fallbackError));
            }
            return;
          }
          reportSendError(errorText(directError, "メッセージ送信に失敗しました。"));
        } finally {
          setIsStreaming(false);
          setIsWaiting(false);
        }
        return;
      }
      if (
        isDirectProvider(settings.provider) &&
        !canDispatchDirectCloud(networkOnline) &&
        !appContextSelected &&
        !requiresServerRuntime &&
        !forceServer
      ) {
        await markDirectUnavailable();
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
          reasoningEffort: requestedTarget.kind === "server" ? requestedTarget.responseModel?.reasoning_effort ?? undefined : undefined,
          fallback: false,
        });
      }
      let dispatchedGeneration: ReturnType<typeof beginServerGeneration> = null;
      const onHandoff = () => {
        const previous = getGenerationIdentity();
        if (previous) {
          clearServerGenerationState("cancel", previous);
          generationEventGateRef.current.complete(previous);
        }
        dispatchedGeneration = beginServerGeneration(sessionId, localMessage.id);
        if (!dispatchedGeneration) throw new Error("新しい応答を開始できませんでした。");
        generationEventGateRef.current.bindTransportId(localMessage.id, dispatchedGeneration);
      };
      try {
        await assertDirectScope();
        await dispatchPendingConversationMessage(
          sessionId,
          localMessage,
          pendingDispatchPayload(localMessage),
          { checkRemoteDuplicate: false, onHandoff },
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
        if (dispatchedGeneration) completeServerGeneration(dispatchedGeneration);
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
            reportSendError(null);
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
          reportSendError(
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
        reportSendError(dispatchErrorText);
        setIsWaiting(false);
      }
      } finally {
        finishConversationOperation(exclusiveOperationRef, "send");
        setIsWaiting(false);
        if (sendFailure) throw new RetainedSubmissionError(sendFailure);
      }
    },
    [
      isAuthenticated,
      networkOnline,
      networkConnected,
      beginServerGeneration,
      completeServerGeneration,
      maybeGenerateLocalTitle,
      onSessionPromoted,
      scheduleRefresh,
      effectiveProjectId,
      promoteMessages,
      getGenerationIdentity,
      clearServerGenerationState,
      setMessages,
      userId,
    ],
  );

  const sendConversationCommand = useCallback((command: SendConversationCommand): Promise<void> => {
    const text = command.message.trim();
    if (!text) return Promise.resolve();
    if (!sessionId || !sessionStateRef.current) return Promise.reject(new Error("会話を読み込んでから再試行してください。"));
    if (exclusiveOperationRef.current && exclusiveOperationRef.current !== "send" && exclusiveOperationRef.current !== "background-flush") {
      return Promise.reject(new Error("キャラクターまたはプロジェクトの変更が完了してから送信してください。"));
    }
    const target = snapshotServerModelTarget(command.target ?? { kind: "server" }, responseModelOptionsRef.current);
    const id = command.retryMessageId ?? command.submissionId ?? `mobile-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const retry = command.retryMessageId ? messagesStateRef.current.find((message) => message.id === id) : undefined;
    if (command.retryMessageId && (!retry || retry.role !== "user" || retry.content.trim() !== text ||
      (!retry.metadata?.pending && retry.metadata?.message_state !== "direct-failed"))) {
      return Promise.reject(new Error("この入力は再試行できません。履歴を確認してください。"));
    }
    const now = new Date().toISOString();
    const snapshot: SendConversationCommand = { ...command, message: text, target,
      projectId: command.projectId === undefined ? effectiveProjectId : command.projectId,
      appId: command.appId ?? sessionStateRef.current.app_id,
      appTargetId: command.appTargetId ?? sessionStateRef.current.app_target_id,
      submissionId: command.submissionId ?? id,
    };
    const optimistic: ConversationMessage = { ...(retry ?? {}),
      id, session_id: sessionId, role: "user", content: text,
      created_at: retry?.created_at ?? now, updated_at: now,
      parent_message_id: null, branch_index: 0, is_active_branch: true,
      metadata: { ...retry?.metadata, local_only: true, submission_id: snapshot.submissionId,
        pending: isAuthenticated && target.kind === "server", message_state: "queued",
        delivery_route: target.kind, anonymous_only: !isAuthenticated,
        ...buildPendingDispatchMetadata({ message: text, projectId: snapshot.projectId,
          appId: snapshot.appId, appTargetId: snapshot.appTargetId,
          includeProjectContext: snapshot.includeProjectContext ?? Boolean(snapshot.projectId),
          agentMode: snapshot.agentMode ?? "confirm", editMessageId: snapshot.editMessageId,
          responseModel: target.kind === "server" ? target.responseModel : undefined,
          commandCapabilities: snapshot.commandCapabilities, attachments: snapshot.attachments }),
      },
    };
    const acceptedScope = Promise.all([getToken(), getConfiguredApiServerFingerprint()])
      .then(([token, server]) => ({ auth: getTokenAuthScope(token), server }));
    void acceptedScope.catch(() => undefined);
    latestSubmissionIdRef.current = id;
    submitMessage(optimistic);
    setError(null);
    setIsWaiting(true);
    return submissionQueueRef.current.enqueue(id, (signal) => deliverConversationCommand(snapshot, optimistic, signal, acceptedScope))
      .catch((failure: unknown) => {
        const message = errorTextOf(failure, "送信できませんでした。履歴から再試行してください。");
        setMessages((current) => current.map((item) => item.id === id ? { ...item,
          metadata: { ...item.metadata, direct_error: message,
            ...(failure && typeof failure === "object" && "persistenceFailed" in failure ? { persistence_error: true } : {}),
            message_state: item.metadata?.delivery_route === "direct" ? "direct-failed" : "queued" },
        } : item));
        if (latestSubmissionIdRef.current === id) { setError(message); setIsWaiting(false); }
        throw new RetainedSubmissionError(message);
      });
  }, [deliverConversationCommand, effectiveProjectId, isAuthenticated, sessionId, setMessages, submitMessage]);

  useEffect(() => {
    const queue = submissionQueueRef.current;
    return () => queue.cancelDirect();
  }, []);

  const retryPendingMessage = useCallback(
    async (message: ConversationMessage, route: "server" | "direct" = "server") => {
      if (!sessionId) return;
      if (route === "server" && message.metadata?.persistence_error) {
        const payload = pendingDispatchPayload(message);
        await sendConversationCommand({ message: message.content, retryMessageId: message.id,
          projectId: payload.project_id, appId: payload.app_id, appTargetId: payload.app_target_id,
          includeProjectContext: payload.include_project_context, agentMode: payload.agent_mode,
          commandCapabilities: payload.command_capabilities, attachments: payload.attachments,
          target: { kind: "server", responseModel: payload.response_model ?? undefined },
        });
        return;
      }
      if (route === "direct") {
        if (retryingMessageIds.includes(message.id)) return;
        setRetryingMessageIds((current) => [...current, message.id]);
        try {
          const settings = responseTarget.kind === "direct"
            ? await getDirectMobileLlmSettings(responseTarget.selection)
            : await getConfiguredFallbackMobileLlmSettings("server") ?? await getMobileLlmSettings();
          if (!settings || !isDirectProvider(settings.provider)) {
            throw new Error("Directモデルまたはフォールバックモデルを設定してから再試行してください。");
          }
          const payload = pendingDispatchPayload(message, {
            projectId: effectiveProjectId, appId: session?.app_id, appTargetId: session?.app_target_id,
            includeProjectContext: Boolean(effectiveProjectId), agentMode: "confirm",
          });
          await sendConversationCommand({
            message: payload.message, retryMessageId: message.id,
            projectId: payload.project_id ?? null,
            appId: payload.app_id, appTargetId: payload.app_target_id,
            includeProjectContext: payload.include_project_context,
            agentMode: payload.agent_mode, commandCapabilities: payload.command_capabilities,
            attachments: payload.attachments,
            target: { kind: "direct", selection: { provider: settings.provider, model: settings.model, reasoningEffort: settings.reasoningEffort } },
          });
        } catch (error) {
          setError(errorTextOf(error, "Directでの再試行に失敗しました。元の入力は履歴に残っています。"));
        } finally {
          setRetryingMessageIds((current) => current.filter((id) => id !== message.id));
        }
        return;
      }
      if (!isAuthenticated) {
        setError("サーバーへの再送にはログインが必要です。Directで再試行できます。");
        return;
      }
      if (!networkConnected || (!networkServerReachable && isServerKnownUnreachable())) {
        setError("AoiTalkサーバーへの接続を確認できるまで再送を保留します。");
        return;
      }
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
      networkConnected,
      networkServerReachable,
      beginServerGeneration,
      completeServerGeneration,
      refreshFromServer,
      retryingMessageIds,
      responseTarget,
      sendConversationCommand,
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
            job.status === "cancelled" ||
            job.status === "interrupted"
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
        // SearXNG is the local authority; Personal deployments retain the
        // deep client fallback while Enterprise never inherits public egress.
        engines: ["searxng"],
        include_local_knowledge: Boolean(effectiveProjectId),
        ...(requestedSessionId ? { session_id: requestedSessionId } : {}),
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
        }).catch((error: unknown) => {
          setError(errorTextOf(error, "再生成に失敗しました。履歴から再試行できます。"));
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
        }).catch((error: unknown) => {
          setError(errorTextOf(error, "再生成に失敗しました。履歴から再試行できます。"));
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
      const flight = runExclusiveConversationOperation(
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
      backgroundFlushRef.current = flight;
      try {
        const result = await flight;
        return result.started ? result.value : null;
      } finally {
        if (backgroundFlushRef.current === flight) backgroundFlushRef.current = null;
      }
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
      setResponseTarget(preferences.responseTarget);
      setResponseModelOptions(preferences.responseModelOptions);
      setLlmPreferencesReady(true);

      // A former global-mode retry must never mutate another model's config.
      llmPreferencesRef.current = { ...preferences, modeSyncPending: false };

      if (isAuthenticated) {
        // cacheをpaintした後にだけserver revalidationを開始する。
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
    isAuthenticated,
    refreshResponseModelOptionsForScope,
    userId,
  ]);

  useEffect(() => {
    void refreshSkillCommands();
  }, [refreshSkillCommands]);

  useEffect(() => {
    if (
      !sessionId ||
      !session ||
      session.user_id ||
      !isAuthenticated ||
      !networkConnected ||
      (!networkServerReachable && isServerKnownUnreachable()) ||
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
    networkConnected,
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
        case "llm_mode_change":
          // This event describes the server default, not this turn's selection.
          break;
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
          if (!generationEventGateRef.current.acceptsStart(msg)) break;
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
          if (!generationEventGateRef.current.acceptsToken(msg, getGenerationIdentity())) break;
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
    llmMode: selectedServerEffort.value,
    llmModeOptions: selectedServerEffort.options,
    llmModeLabels: selectedServerEffort.labels,
    llmModeKind: selectedServerEffort.kind,
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
    refreshResponseModelOptions,
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
