"use client";

import {
  Suspense,
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useReducer,
} from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { getGenerationReadyNewChatMainRoute } from "@/hooks/use-chat-session-route";
import { hasExplicitSessionRoute } from "@/lib/chat-session-route";
import { applyPendingNewChatLlmSettingsToSession } from "@/lib/new-chat-llm-settings-store";
import { PendingLlmHandoffError } from "@/lib/chat-session-route-handoff";
import {
  safeLocalStorageGetItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";
import { storyApi } from "@/lib/story/api";
import { normalizeEpisode, objectOf } from "@/lib/story/view-model";
import type {
  ChatToolResultMetadata,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import { useWebSocket } from "@/hooks/use-websocket";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import { MessageList } from "@/components/chat/message-list";
import {
  ChatComposer,
  type SubmittedSteeringInstruction,
} from "@/components/chat/chat-composer";
import type { ChatAppContextSelection } from "@/components/chat/app-context-picker";
import { ConversationSearchDialog } from "@/components/chat/conversation-search-dialog";
import { useProject } from "@/contexts/project-context";
import {
  useChatSessions,
} from "@/contexts/chat-session-context";
import { StoryChatAuthoringWorkspace } from "@/components/story/chat/story-chat-authoring-workspace";
import { GroupChatDialog } from "@/components/chat/group-chat-dialog";
import { SteeringPanel } from "@/components/chat/steering-panel";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import { useSidebar } from "@/components/ui/sidebar";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useChatLlmMode } from "@/hooks/use-chat-llm-mode";
import { useResponseModelOptions } from "@/hooks/use-response-model-options";
import { useChatAttachedFiles } from "@/hooks/use-chat-attached-files";
import { useContextSnapshot } from "@/hooks/use-context-snapshot";
import { useActiveChatSession } from "@/hooks/use-active-chat-session";
import {
  chatTimelineReducer,
  initialChatTimelineState,
} from "@/lib/chat-state";
import {
  chatGenerationReducer,
  initialChatGenerationState,
  selectGenerationActiveTool,
  selectGenerationActivityMessage,
  selectGenerationAgentRunId,
  selectGenerationEpochKey,
  selectGenerationIsBusy,
  selectGenerationIsStreaming,
  selectGenerationShowsActivity,
  selectGenerationStartedAt,
  selectGenerationTerminalKey,
} from "@/lib/chat-generation-state";
import { useChatWebSocketEvents } from "@/hooks/use-chat-websocket-events";
import {
  useChatMessaging,
  type PendingMessage,
} from "@/hooks/use-chat-messaging";
import { useChatGenerationControls } from "@/hooks/use-chat-generation-controls";
import { useChatSessionTransientCleanup } from "@/hooks/use-chat-session-transient-cleanup";
import { useConversationSearch } from "@/hooks/use-conversation-search";
import { useRelatedTasksPanel } from "@/hooks/use-related-tasks-panel";
import { ChatSidebar } from "@/components/layout/sidebar/chat-sidebar";
import { ChatContextRail } from "@/components/chat/chat-context-rail";
import {
  useWorkspaceShellRegistration,
} from "@/components/layout/shell-context";
import {
  AlertCircle,
  CheckSquare,
  FolderOpen,
  MoreHorizontal,
  RefreshCcw,
  Users,
  Sliders,
  BookOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { appsApi } from "@/lib/apps-api";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AskUserQuestionDialog,
  ExternalModelPromptDialog,
  PlanApprovalDialog,
  ToolPermissionDialog,
  type AskUserQuestionRequest,
  type ExternalModelPromptRequest,
  type PlanApprovalRequest,
  type ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

const PROJECT_CONTEXT_KEY = "aoitalk-chat-project-context";
const DEEP_RESEARCH_KEY = "aoitalk-chat-deep-research";
const TEMPORARY_FILE_DRAFT_SESSION_KEY = "__new_chat__";

function isStoryWorkflowSession(session: ConversationSession) {
  const characterName = session.character_name || "";
  return (
    characterName.startsWith("story_") ||
    session.title?.startsWith("[執筆]")
  );
}

/**
 * 構造操作は `{results:[{op, episode_id}], ...graph}`、エピソード作成は `{episode:{...}}` を返す。
 * 生の snake_case を各所で読まないよう、ここで view-model の正規化を通して ID だけを取り出す。
 */
function extractStoryEpisodeId(value: unknown): string | null {
  const record = objectOf(value);
  const firstResult = Array.isArray(record.results) ? objectOf(record.results[0]) : {};
  const candidates = [
    firstResult.episode_id,
    normalizeEpisode(record.created ?? value).id,
    record.episode_id,
    record.created_episode_id,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

type StoryWritingSessionView = {
  id: string;
  workId: string;
  episodeId: string | null;
};

/** story API の執筆セッション応答を camelCase へ寄せる。生の snake_case は画面側へ持ち出さない。 */
function normalizeWritingSession(value: unknown): StoryWritingSessionView | null {
  const record = objectOf(value);
  const id = typeof record.id === "string" ? record.id : "";
  const workId = typeof record.work_id === "string" ? record.work_id : "";
  if (!id || !workId) return null;
  return {
    id,
    workId,
    episodeId: typeof record.episode_id === "string" ? record.episode_id : null,
  };
}

function ChatPageInner() {
  const { isMobile } = useSidebar();
  const searchParams = useSearchParams();
  const router = useRouter();
  const currentUserId = useCurrentUserId();
  const searchParamSessionId = searchParams.get("s") || null;
  const appQueryId = searchParams.get("app_id") || null;
  const appQueryTargetId = searchParams.get("app_target_id") || null;
  const appQueryProjectId = searchParams.get("project_id") || null;
  const { selectedProjectId, allProjects, initialLoadComplete } = useProject();
  const {
    addSession,
    upsertSession,
    updateSession,
    updateSessionTitle: updateSidebarTitle,
    bumpSession,
    sessions,
    requestedSessionId,
    clearRequestedSession,
    consumeGenerationReadySession,
  } = useChatSessions();
  const { activeSessionId, activeSessionIdRef, activateSession } =
    useActiveChatSession({
      searchParamSessionId,
      requestedSessionId,
      onRequestedSessionConsumed: clearRequestedSession,
      suppressLastSessionRestore: Boolean(appQueryId),
      router,
      allProjects,
      sessions,
    });

  useEffect(() => {
    if (!activeSessionId) return;
    updateSession(activeSessionId, (session) => ({
      ...session,
      is_unread: false,
    }));
    void chatApi.markSessionRead(activeSessionId).catch(() => {
      // 読み込みは継続し、次回の履歴取得で状態を再同期する。
    });
  }, [activeSessionId, updateSession]);
  const [includeProjectContext, setIncludeProjectContext] = useState(() => {
    if (typeof window === "undefined") return false;
    return safeLocalStorageGetItem(PROJECT_CONTEXT_KEY) === "true";
  });
  const [deepResearchEnabled, setDeepResearchEnabled] = useState(() => {
    if (typeof window === "undefined") return false;
    return safeLocalStorageGetItem(DEEP_RESEARCH_KEY) === "true";
  });
  const {
    llmMode,
    llmModeOptions,
    llmModeLabels,
    llmModeLoading,
    llmModeError,
    setLlmModeState,
    setLlmModeOptions,
    setLlmModeLabels,
    handleLlmModeChange,
  } = useChatLlmMode();

  useEffect(() => {
    safeLocalStorageSetItem(
      PROJECT_CONTEXT_KEY,
      includeProjectContext ? "true" : "false",
    );
  }, [includeProjectContext]);

  useEffect(() => {
    safeLocalStorageSetItem(
      DEEP_RESEARCH_KEY,
      deepResearchEnabled ? "true" : "false",
    );
  }, [deepResearchEnabled]);

  // ─── 状態 ───
  const [chatTimeline, dispatchChatTimeline] = useReducer(
    chatTimelineReducer,
    initialChatTimelineState,
  );
  const messages = chatTimeline.messages;
  const messagesRef = useRef<ConversationMessage[]>(messages);
  const [generationState, dispatchGeneration] = useReducer(
    chatGenerationReducer,
    initialChatGenerationState,
  );
  const generationStateRef = useRef(generationState);
  const processedEventIdsRef = useRef<Set<string>>(new Set());
  const processedLegacyMessageRef = useRef<unknown>(null);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  const [sessionLoadError, setSessionLoadError] = useState<string | null>(null);
  const [sessionLoadAttempt, setSessionLoadAttempt] = useState(0);
  const { responseModelOptions, responseModelOptionsLoading } =
    useResponseModelOptions();
  const [writingSession, setWritingSession] =
    useState<
      Awaited<ReturnType<typeof storyApi.getWritingSessionByConversation>>
    >(null);
  // 画面からは正規化済みビューだけを参照する（生の snake_case を持ち回らない）。
  const writingView = useMemo(
    () => normalizeWritingSession(writingSession),
    [writingSession],
  );
  const [mobileAuthoringOpen, setMobileAuthoringOpen] = useState(false);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    generationStateRef.current = generationState;
  }, [generationState]);

  useEffect(() => {
    dispatchGeneration({ type: "session_changed", sessionId: activeSessionId });
  }, [activeSessionId]);

  // Task Open in Chat creates an intentionally empty session.  Treat that
  // known-local session as idle immediately; status hydration may still run,
  // but a missing optional status endpoint must not strand the composer in an
  // unknown/blocked state.
  useEffect(() => {
    if (!activeSessionId || !consumeGenerationReadySession(activeSessionId)) return;
    dispatchGeneration({ type: "session_initialized", sessionId: activeSessionId });
  }, [activeSessionId, consumeGenerationReadySession, sessions]);

  // グループチャット
  const [groupDialogOpen, setGroupDialogOpen] = useState(false);
  const currentSession = useMemo(
    () =>
      activeSessionId
        ? sessions.find((session) => session.id === activeSessionId) ?? null
        : null,
    [activeSessionId, sessions],
  );
  const [appContext, setAppContext] = useState<ChatAppContextSelection | null>(
    null,
  );
  const [appContextSessionId, setAppContextSessionId] = useState<string | null>(
    null,
  );
  const [appQueryContextStatus, setAppQueryContextStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >(() => (appQueryId ? "loading" : "idle"));
  const [appQueryContextError, setAppQueryContextError] = useState<string | null>(null);
  const [appQueryContextAttempt, setAppQueryContextAttempt] = useState(0);
  // A global App chat must not inherit the currently selected Project by
  // accident.  The Project scope is only the one explicitly present in the
  // session or URL; otherwise this is an App-only conversation.
  const appProjectId = currentSession
    ? currentSession.project_id ?? undefined
    : appQueryId
      ? appQueryProjectId ?? undefined
      : selectedProjectId ?? undefined;

  useEffect(() => {
    if (!appQueryId || activeSessionId) {
      if (!appQueryId) {
        setAppQueryContextStatus("idle");
        setAppQueryContextError(null);
      }
      return;
    }
    let cancelled = false;
    setAppQueryContextStatus("loading");
    setAppQueryContextError(null);
    setAppContext(null);
    setAppContextSessionId(null);
    void (async () => {
      try {
        const [context, targets] = await Promise.all([
          appsApi.getContext(appQueryId, appQueryProjectId || undefined),
          appsApi.getTargets(appQueryId, appQueryProjectId || undefined),
        ]);
        if (cancelled) return;
        const target =
          targets.targets.find((item) => item.id === appQueryTargetId) ||
          targets.targets.find((item) => item.target_key === context.target_key) ||
          targets.targets[0];
        if (!target) {
          setAppQueryContextStatus("error");
          setAppQueryContextError("App Targetを取得できませんでした");
          return;
        }
        setAppContext({
          appId: context.app.id,
          appName: context.app.name,
          targetId: target.id,
          targetKey: target.target_key,
          targetDisplayName: target.display_name,
        });
        setAppContextSessionId(null);
        setAppQueryContextStatus("ready");
      } catch (error) {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "App contextを読み込めませんでした";
          setAppQueryContextStatus("error");
          setAppQueryContextError(message);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, appQueryContextAttempt, appQueryId, appQueryProjectId, appQueryTargetId]);

  useEffect(() => {
    if (!activeSessionId || currentSession?.id !== activeSessionId) {
      return;
    }
    if (!currentSession.app_id) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const [context, targets] = await Promise.all([
          appsApi.getContext(currentSession.app_id!, appProjectId),
          appsApi.getTargets(currentSession.app_id!, appProjectId),
        ]);
        if (cancelled) return;
        const target =
          targets.targets.find(
            (item) => item.id === currentSession.app_target_id,
          ) ||
          targets.targets.find(
            (item) => item.target_key === context.target_key,
          ) ||
          targets.targets[0];
        if (!target) {
          setAppContext(null);
          setAppContextSessionId(activeSessionId);
          return;
        }
        setAppContext({
          appId: context.app.id,
          appName: context.app.name,
          targetId: target.id,
          targetKey: target.target_key,
          targetDisplayName: target.display_name,
        });
        setAppContextSessionId(activeSessionId);
      } catch {
        if (!cancelled) {
          setAppContext(null);
          setAppContextSessionId(activeSessionId);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    activeSessionId,
    appProjectId,
    currentSession?.app_id,
    currentSession?.app_target_id,
    currentSession?.id,
  ]);
  // A picker selection must also be available before a new session exists and
  // when attaching an App to an existing session that currently has no scope.
  const effectiveAppContext =
    appContextSessionId === (activeSessionId || null) ? appContext : null;
  const appQueryContextPending = Boolean(
    (appQueryId && !activeSessionId && appQueryContextStatus !== "ready") ||
      (activeSessionId && currentSession?.id !== activeSessionId) ||
      (activeSessionId &&
        currentSession?.id === activeSessionId &&
        currentSession.app_id &&
        appContextSessionId !== activeSessionId),
  );
  const handleAppContextChange = useCallback(
    (next: ChatAppContextSelection | null) => {
      setAppContext(next);
      setAppContextSessionId(activeSessionId || null);
    },
    [activeSessionId],
  );
  const [characterTypeResolution, setCharacterTypeResolution] = useState<{
    characterName: string;
    characterType: string | null;
  } | null>(null);

  useEffect(() => {
    const characterName = currentSession?.character_name?.trim() || "";
    if (!characterName) return;

    let cancelled = false;
    chatApi
      .getCharacterType(characterName)
      .then((characterType) => {
        if (!cancelled) {
          setCharacterTypeResolution({ characterName, characterType });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCharacterTypeResolution({ characterName, characterType: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [currentSession?.character_name]);

  const currentCharacterType =
    characterTypeResolution != null &&
    currentSession?.character_name != null &&
    characterTypeResolution.characterName === currentSession.character_name
      ? characterTypeResolution.characterType
      : null;

  const isGroupChat = currentSession?.is_group_chat ?? false;

  // ステアリングパネル
  const [steeringVisible, setSteeringVisible] = useState(false);
  // RPセッションかどうか。キャラクター種別を取得できない旧セッションは
  // 従来のaoi判定へフォールバックする。
  const isRpSession =
    currentSession != null &&
    (currentCharacterType
      ? currentCharacterType !== "assistant"
      : currentSession.character_name !== "aoi");
  const isStoryChatSession =
    (currentSession ? isStoryWorkflowSession(currentSession) : false) ||
    Boolean(writingView);
  const isSending =
    generationState.lifecycle.phase === "dispatching" &&
    generationState.lifecycle.sessionId === activeSessionId;
  const isWaitingResponse = selectGenerationIsBusy(
    generationState,
    activeSessionId,
  );
  const effectiveProjectId = isStoryChatSession ? undefined : appProjectId;
  const displayedProjectId = currentSession
    ? currentSession.project_id ?? null
    : appQueryId
      ? appQueryProjectId ?? null
      : selectedProjectId ?? null;
  const displayedProject = displayedProjectId
    ? allProjects.find((project) => project.id === displayedProjectId)
    : undefined;
  const displayedProjectName = displayedProject?.name;
  const projectAssociationLabel = currentSession
    ? currentSession.project_id
      ? (displayedProjectName ?? "不明なプロジェクト")
      : "プロジェクトなし"
    : displayedProjectName;
  const temporaryFileSessionKey =
    activeSessionId ?? TEMPORARY_FILE_DRAFT_SESSION_KEY;
  // WebSocket接続後に送信する保留メッセージ
  const pendingMessageRef = useRef<PendingMessage | null>(null);

  // Live Voice は通常の送信より先に開始できるため、まず Chat の永続
  // ConversationSession を一つだけ作成してから音声セッションへ渡す。
  // StrictMode や start の連打で POST が重複しないよう、作成中の Promise
  // を共有する。
  const liveVoiceSessionEnsureRef = useRef<Promise<string> | null>(null);
  const ensureLiveVoiceConversationSession = useCallback(async () => {
    const existingSessionId = activeSessionIdRef.current;
    if (existingSessionId) return existingSessionId;

    const inFlightRequest = liveVoiceSessionEnsureRef.current;
    if (inFlightRequest) return inFlightRequest;

    const request = (async () => {
      const generationReadyMain = getGenerationReadyNewChatMainRoute();
      if (!hasExplicitSessionRoute(generationReadyMain)) {
        throw new PendingLlmHandoffError(
          "Provider / Model の authoritative route を確定できないため、音声セッションを開始しませんでした。",
        );
      }
      const characterName = await chatApi.getCurrentCharacterName();
      const appContext = effectiveAppContext
        ? {
            appId: effectiveAppContext.appId,
            targetId: effectiveAppContext.targetId,
          }
        : null;
      const created = await chatApi.createSession(
        characterName,
        effectiveProjectId,
        undefined,
        appContext,
        generationReadyMain,
      );
      const sessionId = created.session.id;
      addSession(created.session);
      const applied = await applyPendingNewChatLlmSettingsToSession(
        sessionId,
        currentUserId,
        generationReadyMain,
      );
      if (!applied) {
        throw new PendingLlmHandoffError(
          "表示中の Provider / Model をセッションへ確定できませんでした。",
        );
      }
      if (!activeSessionIdRef.current) {
        activateSession(sessionId);
        router.push(`/chat?s=${encodeURIComponent(sessionId)}`);
      }
      return sessionId;
    })();

    liveVoiceSessionEnsureRef.current = request;
    try {
      return await request;
    } finally {
      if (liveVoiceSessionEnsureRef.current === request) {
        liveVoiceSessionEnsureRef.current = null;
      }
    }
  }, [
    activateSession,
    addSession,
    currentUserId,
    effectiveAppContext,
    effectiveProjectId,
    router,
    activeSessionIdRef,
  ]);

  // WebSocket
  const activeAgentRunIdForSocket = selectGenerationAgentRunId(
    generationState,
    activeSessionId,
  );
  const {
    isConnected,
    lastMessage,
    connectionGeneration,
    streamBuffer,
    sendMessage,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    sendHumanInteractionResponse,
    stopGeneration,
    sendSteering,
  } = useWebSocket(activeSessionId, activeAgentRunIdForSocket);
  const isStreaming = selectGenerationIsStreaming(generationState, activeSessionId);
  const activeTool = selectGenerationActiveTool(generationState, activeSessionId);
  const activityMessage = selectGenerationActivityMessage(
    generationState,
    activeSessionId,
  );
  const generationStartedAt = selectGenerationStartedAt(
    generationState,
    activeSessionId,
  );
  const activeGenerationKey = selectGenerationEpochKey(
    generationState,
    activeSessionId,
  );
  const showGenerationActivity = selectGenerationShowsActivity(
    generationState,
    activeSessionId,
  );
  const activeAgentRunId = activeAgentRunIdForSocket;
  const displayIsWaitingResponse = isWaitingResponse;
  const displayActiveTool = activeTool;
  const displayAgentRunId = activeAgentRunId;
  const latestAssistantAgentRunId = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role !== "assistant") continue;
      const agentRunId = message.metadata?.agent_run_id;
      if (typeof agentRunId === "string" && agentRunId.trim()) {
        return agentRunId;
      }
    }
    return null;
  }, [messages]);
  const relatedAgentRunId = displayAgentRunId ?? latestAssistantAgentRunId;
  const chatBusy = selectGenerationIsBusy(generationState, activeSessionId);
  const generationInputBlocked =
    chatBusy ||
    Boolean(
      activeSessionId &&
        generationState.lifecycle.sessionId !== activeSessionId,
    ) ||
    generationState.lifecycle.phase === "hydrating" ||
    generationState.lifecycle.phase === "unknown";

  // サイドバーの進行中アイコンをauthoritative lifecycleから即時反映する。
  // 会話履歴の定期取得は15秒間隔なので、それを待つと開始・終了の反応が遅れる。
  useEffect(() => {
    if (!activeSessionId) return;
    updateSession(activeSessionId, (session) => {
      if (chatBusy) {
        if (
          session.development_status === "working" &&
          session.message_count > 0
        ) {
          return session;
        }
        return {
          ...session,
          development_status: "working",
          // 初回送信直後は message_count が 0 のままなので進行中判定に届かない。
          message_count: Math.max(session.message_count ?? 0, 1),
        };
      }
      if (
        !["completed", "cancelled", "failed"].includes(
          generationState.lifecycle.phase,
        ) ||
        generationState.lastTerminal?.sessionId !== activeSessionId ||
        session.development_status !== "working"
      ) {
        return session;
      }
      return { ...session, development_status: "waiting_for_user" };
    });
  }, [activeSessionId, chatBusy, generationState, updateSession]);

  const {
    attachedFiles,
    setAttachedFiles,
    handleChatFileDragOver,
    handleChatFileDrop,
  } = useChatAttachedFiles({ temporaryFileSessionKey, chatBusy });

  // タスク作成・更新やAgent Run完了はチャット右レールへ即時反映する。
  useEffect(() => {
    const type = lastMessage?.type ?? "";
    if (
      type === "task_created" ||
      type === "task_updated" ||
      type === "tool_end" ||
      type === "stream_end" ||
      type === "run.succeeded" ||
      type === "run.failed" ||
      type === "run.cancelled"
    ) {
      window.dispatchEvent(new Event("aoitalk-task-updated"));
    }
  }, [lastMessage]);
  const [toolPermissionRequest, setToolPermissionRequest] =
    useState<ToolPermissionRequest | null>(null);
  const [externalModelPromptRequest, setExternalModelPromptRequest] =
    useState<ExternalModelPromptRequest | null>(null);
  const [externalModelPromptDraft, setExternalModelPromptDraft] = useState("");
  const [askUserQuestionRequest, setAskUserQuestionRequest] =
    useState<AskUserQuestionRequest | null>(null);
  const [askUserQuestionDraft, setAskUserQuestionDraft] = useState("");
  const [askUserQuestionChoices, setAskUserQuestionChoices] = useState<string[]>(
    [],
  );
  const [planApprovalRequest, setPlanApprovalRequest] =
    useState<PlanApprovalRequest | null>(null);
  const [planApprovalDraft, setPlanApprovalDraft] = useState("");
  const [planApprovalFeedbackDraft, setPlanApprovalFeedbackDraft] = useState("");

  // ストリーミング内容を反映するための状態
  const [streamingContent, setStreamingContent] = useState("");
  const [liveToolResults, setLiveToolResults] = useState<
    ChatToolResultMetadata[]
  >([]);
  const liveToolResultsRef = useRef<ChatToolResultMetadata[]>([]);
  const [, setSteeringInstructions] = useState<
    SubmittedSteeringInstruction[]
  >([]);
  const streamingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(
    null,
  );
  const responsePollGenerationRef = useRef(0);
  const clearStreamingInterval = useCallback(() => {
    if (streamingIntervalRef.current) {
      clearInterval(streamingIntervalRef.current);
      streamingIntervalRef.current = null;
    }
  }, []);
  useChatSessionTransientCleanup({
    activeSessionId,
    responsePollGenerationRef,
    clearStreamingInterval,
    streamBuffer,
    liveToolResultsRef,
    processedEventIdsRef,
    processedLegacyMessageRef,
    pendingMessageRef,
    setStreamingContent,
    setLiveToolResults,
    setSteeringInstructions,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
  });

  const { contextSnapshot, contextSnapshotStatus } = useContextSnapshot({
    activeSessionId,
    activeSessionIdRef,
    includeProjectContext,
    llmMode,
    messagesLength: messages.length,
    liveToolResultsLength: liveToolResults.length,
    chatBusy,
  });

  const markWaitingResponse = useCallback((
    sessionId: string | null,
    clientMessageId?: string | null,
  ) => {
    if (!sessionId) return;
    dispatchGeneration({
      type: "dispatch_accepted",
      sessionId,
      clientMessageId,
      statusMessage: "応答をキューに追加しました",
    });
  }, []);

  const generationTerminalKey = selectGenerationTerminalKey(
    generationState,
    activeSessionId,
  );

  // BGM再生
  const { play, stop: stopAudio, setVolume } = useAudioPlayer();

  const {
    conversationSearchOpen,
    setConversationSearchOpen,
    handleSelectSearchResult,
  } = useConversationSearch({
    router,
    searchParams,
    activateSession,
    isLoadingMessages,
    messages,
  });

  const {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    bumpSessionForAssistant,
    handleCreateGroupChat,
    handleSendMessage,
    handleEditMessage,
    handleRerunMessage,
    handleSwitchBranch,
  } = useChatMessaging({
    router,
    activeSessionId,
    activeSessionIdRef,
    activateSession,
    allProjects,
    effectiveProjectId,
    isStoryChatSession,
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
    setWritingSession,
  });

  const handleSendMessageFromComposer = useCallback(
    (...args: Parameters<typeof handleSendMessage>) => {
      if (appQueryContextPending) {
        if (appQueryContextStatus === "error") {
          toast.error(appQueryContextError || "App contextを読み込めませんでした");
        } else {
          toast.message("App contextを読み込み中です。完了してから送信してください");
        }
        return false;
      }
      return handleSendMessage(...args);
    },
    [appQueryContextError, appQueryContextPending, appQueryContextStatus, handleSendMessage],
  );

  const {
    handleToolPermissionDecision,
    handleExternalModelPromptDecision,
    handleExternalModelPromptKeyDown,
    handleStopGeneration,
    handleSteerGeneration,
  } = useChatGenerationControls({
    activeSessionId,
    activeSessionIdRef,
    toolPermissionRequest,
    externalModelPromptRequest,
    externalModelPromptDraft,
    responsePollGenerationRef,
    streamBuffer,
    liveToolResultsRef,
    streamingIntervalRef,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    stopGeneration,
    sendSteering,
    generationAgentRunId: displayAgentRunId,
    generationStateRef,
    dispatchGeneration,
    refreshPersistedMessages,
    dispatchChatTimeline,
    setSteeringInstructions,
    setStreamingContent,
    setLiveToolResults,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
  });

  useChatWebSocketEvents({
    lastMessage,
    activeSessionId,
    connectionGeneration,
    streamBuffer,
    currentSession,
    processedEventIdsRef,
    processedLegacyMessageRef,
    liveToolResultsRef,
    streamingIntervalRef,
    responsePollGenerationRef,
    dispatchChatTimeline,
    refreshPersistedMessages,
    bumpSessionForAssistant,
    maybeGenerateLoadedSessionTitle,
    updateSidebarTitle,
    setLlmModeState,
    setLlmModeOptions,
    setLlmModeLabels,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
    setAskUserQuestionRequest,
    setAskUserQuestionDraft,
    setAskUserQuestionChoices,
    setPlanApprovalRequest,
    setPlanApprovalDraft,
    setPlanApprovalFeedbackDraft,
    setSteeringInstructions,
    setStreamingContent,
    setLiveToolResults,
    generationState,
    dispatchGeneration,
    play,
    stopAudio,
    setVolume,
  });

  const handleAskUserQuestionSubmit = useCallback(() => {
    if (!askUserQuestionRequest) return;
    if (askUserQuestionRequest.sessionId !== activeSessionId) {
      setAskUserQuestionRequest(null);
      return;
    }
    sendHumanInteractionResponse(
      askUserQuestionRequest.requestId,
      {
        action: "answer",
        answer: askUserQuestionDraft,
        selected_choices: askUserQuestionChoices,
        revision: askUserQuestionRequest.revision,
      },
      askUserQuestionRequest.sessionId,
      "ask_user_question_response",
    );
    setAskUserQuestionRequest(null);
    setAskUserQuestionDraft("");
    setAskUserQuestionChoices([]);
  }, [
    activeSessionId,
    askUserQuestionChoices,
    askUserQuestionDraft,
    askUserQuestionRequest,
    sendHumanInteractionResponse,
  ]);

  const handleAskUserQuestionCancel = useCallback(() => {
    if (!askUserQuestionRequest) return;
    sendHumanInteractionResponse(
      askUserQuestionRequest.requestId,
      {
        action: "cancel",
        cancelled: true,
        revision: askUserQuestionRequest.revision,
      },
      askUserQuestionRequest.sessionId,
      "ask_user_question_response",
    );
    setAskUserQuestionRequest(null);
    setAskUserQuestionDraft("");
    setAskUserQuestionChoices([]);
  }, [askUserQuestionRequest, sendHumanInteractionResponse]);

  const handlePlanApprovalApprove = useCallback(() => {
    if (!planApprovalRequest) return;
    sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      {
        action: "approve",
        plan_text: planApprovalDraft,
        revision: planApprovalRequest.revision,
      },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalDraft("");
    setPlanApprovalFeedbackDraft("");
  }, [planApprovalDraft, planApprovalRequest, sendHumanInteractionResponse]);

  const handlePlanApprovalFeedback = useCallback(() => {
    if (!planApprovalRequest) return;
    sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      {
        action: "feedback",
        feedback: planApprovalFeedbackDraft,
        plan_text: planApprovalDraft,
        revision: planApprovalRequest.revision,
      },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalDraft("");
    setPlanApprovalFeedbackDraft("");
  }, [
    planApprovalDraft,
    planApprovalFeedbackDraft,
    planApprovalRequest,
    sendHumanInteractionResponse,
  ]);

  const handlePlanApprovalCancel = useCallback(() => {
    if (!planApprovalRequest) return;
    sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      {
        action: "cancel",
        cancelled: true,
        revision: planApprovalRequest.revision,
      },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalDraft("");
    setPlanApprovalFeedbackDraft("");
  }, [planApprovalRequest, sendHumanInteractionResponse]);

  // クリーンアップ
  useEffect(() => {
    return () => {
      responsePollGenerationRef.current += 1;
      clearStreamingInterval();
    };
  }, [clearStreamingInterval]);

  const handleForkMessage = useCallback(
    async (message: ConversationMessage) => {
      if (!activeSessionId) return;
      try {
        const result = await chatApi.forkSession(activeSessionId, message.id);
        addSession(result.session);
        activateSession(result.session.id);
        router.push(`/chat?s=${encodeURIComponent(result.session.id)}`);
        toast.success("独立した会話へフォークしました");
      } catch (error) {
        console.error("会話フォークエラー:", error);
        toast.error("会話をフォークできませんでした");
      }
    },
    [activeSessionId, activateSession, addSession, router],
  );
  const handleForkStoryMessage = useCallback(
    async (message: ConversationMessage) => {
      const sourceEpisodeId = writingView?.episodeId;
      if (!activeSessionId || !writingView?.workId || !sourceEpisodeId) {
        toast.error("分岐元のStory本文が選択されていません");
        return;
      }
      try {
        const branchResult = await storyApi.updateStructure(
          writingView.workId,
          { ops: [{ op: "duplicate_as_branch", episode_id: sourceEpisodeId, choice_label: "チャットからの分岐", new_title: "チャットからの分岐" }] },
        );
        const branchEpisodeId = extractStoryEpisodeId(branchResult);
        if (!branchEpisodeId) throw new Error("Story Studio の分岐先を取得できませんでした");
        const forked = await chatApi.forkSession(activeSessionId, message.id);
        const clonedWriting = normalizeWritingSession(
          await storyApi.getWritingSessionByConversation(forked.session.id),
        );
        if (clonedWriting) {
          await storyApi.updateWritingSession(clonedWriting.id, { episode_id: branchEpisodeId });
        } else {
          await storyApi.startWriting(writingView.workId, {
            episode_id: branchEpisodeId,
            conversation_session_id: forked.session.id,
          });
        }
        addSession(forked.session);
        activateSession(forked.session.id);
        router.push(`/chat?s=${encodeURIComponent(forked.session.id)}`);
        toast.success("物語とチャットを独立してフォークしました");
      } catch (error) {
        console.error("物語フォークエラー:", error);
        toast.error("物語とチャットをフォークできませんでした");
      }
    },
    [activeSessionId, activateSession, addSession, router, writingView],
  );
  const {
    selectedRelatedTaskId,
    setSelectedRelatedTaskId,
    mobileRailOpen,
    setMobileRailOpen,
    handleRelatedTasksChange,
    notifyTaskUpdated,
  } = useRelatedTasksPanel({ activeSessionId, isMobile });
  // ChatSidebar is the single history data owner.  It already subscribes to
  // ChatSessionProvider and owns the refresh/new-session/title/menu actions;
  // registering the same component here keeps the route-local navigation
  // visible even when the generic AppSidebar is only mounted as Quick Panel.
  const chatWorkspaceNavigation = useMemo(
    () => (
      <div
        className="flex min-h-0 flex-1 flex-col overflow-y-auto border-r border-border-subtle bg-surface-charcoal"
        data-testid="chat-workspace-navigation"
        data-shell-workspace="chat"
      >
        <Suspense fallback={<div className="p-3 text-xs text-muted-foreground">会話履歴を読み込み中…</div>}>
          <ChatSidebar />
        </Suspense>
      </div>
    ),
    [],
  );

  // Chatの履歴はWorkspace Navigation（AppSidebar）が正本として保持し、
  // 会話セッションに紐づく情報レールをShared Shellへ常設登録する。
  // 実行制御（stop/steer/permission/WebSocket）はこのページに残す。
  const chatContextRail = useMemo(
    () => (
      <ChatContextRail
        sessionId={activeSessionId}
        agentRunId={relatedAgentRunId}
        generationKey={activeGenerationKey}
        generationStartedAt={generationStartedAt}
        activityMessage={activityMessage}
        generationLive={isStreaming || chatBusy}
        onTaskClick={setSelectedRelatedTaskId}
        onTasksChange={handleRelatedTasksChange}
        messages={messages}
        currentSession={currentSession}
        projectName={displayedProjectName}
        contextSnapshot={contextSnapshot}
        contextSnapshotStatus={contextSnapshotStatus}
        persistent
      />
    ),
    [
      activeGenerationKey,
      activeSessionId,
      activityMessage,
      chatBusy,
      generationStartedAt,
      handleRelatedTasksChange,
      isStreaming,
      messages,
      currentSession,
      displayedProjectName,
      setSelectedRelatedTaskId,
      relatedAgentRunId,
      contextSnapshot,
      contextSnapshotStatus,
    ],
  );
  useWorkspaceShellRegistration({
    workspaceNavigation: chatWorkspaceNavigation,
    contextRail: chatContextRail,
    priority: 40,
    id: "chat-workspace",
    routeKey: "/chat",
    contextRailPersistent: true,
  });

  return (
    <div
      className="chat-viewport-root relative flex h-full min-h-0 overflow-hidden bg-background text-on-surface"
      onDragOver={handleChatFileDragOver}
      onDrop={handleChatFileDrop}
    >
      {!isMobile && writingView && (
        <div className="min-w-0 flex-1 border-r border-border-subtle bg-background">
          <StoryChatAuthoringWorkspace
            workId={writingView.workId}
            episodeId={writingView.episodeId}
            writingSessionId={writingView.id}
            onAskAgent={(instruction) => {
              void handleSendMessageFromComposer(instruction);
            }}
          />
        </div>
      )}
      <div
        className={
          !isMobile && writingView
            ? "flex w-[36%] min-w-0 shrink flex-col overflow-hidden bg-background"
            : "flex min-w-0 flex-1 flex-col overflow-hidden bg-background"
        }
      >
        {/* ヘッダーバー：チャット操作・ステアリングトグル */}
        <div
          className="flex min-h-12 shrink-0 items-center gap-2 border-b border-border-subtle bg-background px-4 py-2"
          data-chat-toolbar="true"
        >
          <DropdownMenu>
            <DropdownMenuTrigger
              className="inline-flex size-7 items-center justify-center rounded text-text-secondary transition-colors hover:bg-surface-slate hover:text-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary"
              title="チャットメニュー"
              aria-label="チャットメニュー"
            >
              <MoreHorizontal className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem onClick={() => setGroupDialogOpen(true)}>
                <Users className="size-4" />
                グループチャットを作成
              </DropdownMenuItem>
              {isMobile && writingView && (
                <DropdownMenuItem onClick={() => setMobileAuthoringOpen(true)}>
                  <BookOpen className="size-4" />
                  本文とアウトラインを表示
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          {isMobile && (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="text-text-secondary hover:bg-surface-slate hover:text-primary"
              title="関連情報を開く"
              aria-label="関連情報を開く"
              onClick={() => setMobileRailOpen(true)}
            >
              <CheckSquare className="size-4" />
            </Button>
          )}
          {projectAssociationLabel && (
            <span className="inline-flex min-w-0 max-w-[52%] items-center gap-1 rounded border border-border-subtle bg-surface-charcoal px-2 py-1 text-[11px] text-text-secondary">
              <FolderOpen className="size-3.5 shrink-0" />
              <span className="truncate">
                プロジェクト: {projectAssociationLabel}
              </span>
            </span>
          )}
          {(chatBusy || isStreaming) && (
            <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-primary" role="status">
              <span className="size-2 animate-pulse rounded-full bg-primary" />
              生成中…
            </span>
          )}
          {isRpSession && activeSessionId && (
            <Button
              variant={steeringVisible ? "secondary" : "ghost"}
              size="sm"
              onClick={() => setSteeringVisible((v) => !v)}
              title="ステアリングパネル"
              className="h-7 rounded border border-transparent px-2 text-text-secondary hover:bg-surface-slate hover:text-primary data-[state=on]:border-primary/50"
            >
              <Sliders className="size-3.5" />
            </Button>
          )}
          {isGroupChat && (
            <span className="ml-auto truncate text-[11px] text-text-secondary">
              グループチャット (
              {currentSession?.participants
                ?.map((p) => p.display_name || p.participant_id)
                .join(", ") ||
                currentSession?.group_character_names?.join(", ") ||
                ""}
              )
            </span>
          )}
        </div>

        {/* メッセージ */}
        {isLoadingMessages ? (
          <div className="flex flex-1 items-center justify-center bg-background">
            <div className="rounded-md border border-border-subtle bg-surface-container-low px-5 py-4 text-sm text-text-secondary">
              メッセージを読み込み中...
            </div>
          </div>
        ) : sessionLoadError ? (
          <div className="flex flex-1 items-center justify-center bg-background px-4">
            <div className="flex max-w-md flex-col items-center gap-3 rounded-md border border-border-subtle bg-surface-container-low px-6 py-5 text-center">
              <AlertCircle className="size-8 text-destructive" />
              <div className="text-sm font-medium">会話を開けませんでした</div>
              <div className="text-sm text-muted-foreground">
                {sessionLoadError}
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setSessionLoadAttempt((value) => value + 1)}
              >
                <RefreshCcw className="mr-1 size-3.5" />
                再読み込み
              </Button>
            </div>
          </div>
        ) : (
          <MessageList
            messages={messages}
            emptyMessage={
              activeSessionId
                ? "この会話にはまだメッセージがありません。"
                : "メッセージを送信して会話を開始しましょう。"
            }
            isStreaming={isStreaming}
            isWaitingResponse={displayIsWaitingResponse}
            streamingContent={streamingContent}
            liveToolResults={liveToolResults}
            activeTool={displayActiveTool}
            activityMessage={activityMessage}
            activeAgentRunId={displayAgentRunId}
            generationKey={activeGenerationKey}
            generationStartedAt={generationStartedAt}
            showGenerationActivity={showGenerationActivity}
            onTaskClick={setSelectedRelatedTaskId}
            onEditMessage={handleEditMessage}
            onForkMessage={handleForkMessage}
            onForkStoryMessage={
              writingView ? handleForkStoryMessage : undefined
            }
            onRerunMessage={handleRerunMessage}
            onSwitchBranch={handleSwitchBranch}
            responseModelOptions={responseModelOptions}
            responseModelOptionsLoading={responseModelOptionsLoading}
          />
        )}

        {/* ステアリングパネル */}
        {isRpSession && activeSessionId && (
          <SteeringPanel
            sessionId={activeSessionId}
            isVisible={steeringVisible}
          />
        )}

        {appQueryContextPending && (
          <div className="flex items-center justify-between gap-3 border-t border-border bg-card px-4 py-2 text-xs text-muted-foreground">
            <span>
              {appQueryContextStatus === "error"
                ? appQueryContextError || "App contextを読み込めませんでした"
                : "App contextを読み込み中です。準備が完了するまで送信できません。"}
            </span>
            {appQueryContextStatus === "error" && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setAppQueryContextAttempt((value) => value + 1)}
              >
                再試行
              </Button>
            )}
          </div>
        )}
        <ChatComposer
          onSend={handleSendMessageFromComposer}
          onSteer={handleSteerGeneration}
          onStop={handleStopGeneration}
          disabled={generationInputBlocked || appQueryContextPending}
          busy={chatBusy}
          generationTerminalKey={generationTerminalKey}
          attachedFiles={attachedFiles}
          onAttachedFilesChange={setAttachedFiles}
          projectContextEnabled={includeProjectContext}
          onProjectContextToggle={setIncludeProjectContext}
          deepResearchEnabled={deepResearchEnabled}
          onDeepResearchToggle={setDeepResearchEnabled}
          llmMode={llmMode}
          llmModeOptions={llmModeOptions}
          llmModeLabels={llmModeLabels}
          llmModeLoading={llmModeLoading}
          llmModeError={llmModeError}
          onLlmModeChange={handleLlmModeChange}
          projectId={effectiveProjectId}
          projectScopeReady={initialLoadComplete ?? true}
          sessionId={activeSessionId}
          contextSnapshot={contextSnapshot}
          contextSnapshotStatus={contextSnapshotStatus}
          appContext={effectiveAppContext}
          onAppContextChange={handleAppContextChange}
          ensureLiveVoiceConversationSession={ensureLiveVoiceConversationSession}
        />
      </div>

      {isMobile && (
        <Sheet
          open={mobileRailOpen}
          onOpenChange={(open) => {
            setMobileRailOpen(open);
          }}
        >
          <SheetContent side="right" className="w-[min(92vw,24rem)] p-0">
            <SheetHeader className="sr-only">
              <SheetTitle>Context Rail</SheetTitle>
            </SheetHeader>
            <div className="flex min-h-0 flex-1 flex-col">
              {mobileRailOpen && (
                <ChatContextRail
                  key={activeSessionId ?? "no-session"}
                  sessionId={activeSessionId}
                  agentRunId={relatedAgentRunId}
                  generationKey={activeGenerationKey}
                  generationStartedAt={generationStartedAt}
                  activityMessage={activityMessage}
                  generationLive={isStreaming || chatBusy}
                  onTaskClick={setSelectedRelatedTaskId}
                  onTasksChange={handleRelatedTasksChange}
                  onClose={() => setMobileRailOpen(false)}
                  messages={messages}
                  currentSession={currentSession}
                  projectName={displayedProjectName}
                  contextSnapshot={contextSnapshot}
                  contextSnapshotStatus={contextSnapshotStatus}
                />
              )}
            </div>
          </SheetContent>
        </Sheet>
      )}

      {isMobile && writingView && (
        <Sheet open={mobileAuthoringOpen} onOpenChange={setMobileAuthoringOpen}>
          <SheetContent side="left" className="w-screen max-w-none p-0">
            <SheetHeader className="sr-only">
              <SheetTitle>シナリオ執筆</SheetTitle>
            </SheetHeader>
            <StoryChatAuthoringWorkspace
              workId={writingView.workId}
              episodeId={writingView.episodeId}
              writingSessionId={writingView.id}
              onAskAgent={async (instruction) => {
                await handleSendMessageFromComposer(instruction);
                setMobileAuthoringOpen(false);
              }}
            />
          </SheetContent>
        </Sheet>
      )}

      <TaskDetailModal
        taskId={selectedRelatedTaskId}
        open={selectedRelatedTaskId != null}
        onOpenChange={(open) => {
          if (!open) setSelectedRelatedTaskId(null);
        }}
        onTaskUpdated={notifyTaskUpdated}
      />

      <ExternalModelPromptDialog
        request={
          externalModelPromptRequest?.sessionId === activeSessionId
            ? externalModelPromptRequest
            : null
        }
        draft={externalModelPromptDraft}
        onDraftChange={setExternalModelPromptDraft}
        onKeyDown={handleExternalModelPromptKeyDown}
        onDecision={handleExternalModelPromptDecision}
      />

      <ToolPermissionDialog
        request={
          toolPermissionRequest?.sessionId === activeSessionId
            ? toolPermissionRequest
            : null
        }
        onDecision={handleToolPermissionDecision}
      />

      <AskUserQuestionDialog
        request={
          askUserQuestionRequest?.sessionId === activeSessionId
            ? askUserQuestionRequest
            : null
        }
        draft={askUserQuestionDraft}
        selectedChoices={askUserQuestionChoices}
        onDraftChange={setAskUserQuestionDraft}
        onSelectedChoicesChange={setAskUserQuestionChoices}
        onSubmit={handleAskUserQuestionSubmit}
        onCancel={handleAskUserQuestionCancel}
      />

      <PlanApprovalDialog
        request={
          planApprovalRequest?.sessionId === activeSessionId
            ? planApprovalRequest
            : null
        }
        draft={planApprovalDraft}
        feedbackDraft={planApprovalFeedbackDraft}
        onDraftChange={setPlanApprovalDraft}
        onFeedbackDraftChange={setPlanApprovalFeedbackDraft}
        onApprove={handlePlanApprovalApprove}
        onFeedback={handlePlanApprovalFeedback}
        onCancel={handlePlanApprovalCancel}
      />

      {/* グループチャット作成ダイアログ */}
      <GroupChatDialog
        open={groupDialogOpen}
        onOpenChange={setGroupDialogOpen}
        onCreateGroup={handleCreateGroupChat}
        projectId={effectiveProjectId}
      />
      <ConversationSearchDialog
        open={conversationSearchOpen}
        onOpenChange={setConversationSearchOpen}
        projectId={selectedProjectId}
        onSelectResult={handleSelectSearchResult}
      />
    </div>
  );
}

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center">
          <div className="text-sm text-muted-foreground">読み込み中...</div>
        </div>
      }
    >
      <ChatPageInner />
    </Suspense>
  );
}
