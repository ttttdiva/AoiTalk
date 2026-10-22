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
  safeSessionStorage,
} from "@/lib/safe-storage";
import { useContextRailVisibility } from "@/hooks/use-context-rail-visibility";
import {
  hasPersistedChatComposerDraft,
  NEW_CHAT_COMPOSER_DRAFT_KEY,
} from "@/lib/chat-composer-draft-store";
import type {
  ChatToolResultMetadata,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import { useWebSocket } from "@/hooks/use-websocket";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import { MessageList } from "@/components/chat/message-list";
import { MemoryVisualization } from "@/components/chat/memory-visualization/memory-visualization";
import { useMemorySelection } from "@/hooks/use-memory-selection";
import {
  ChatComposer,
  type SubmittedSteeringInstruction,
} from "@/components/chat/chat-composer";
import type { ChatAppContextSelection } from "@/components/chat/app-context-picker";
import { ConversationSearchDialog } from "@/components/chat/conversation-search-dialog";
import { ChatAgentSelector } from "@/components/chat/chat-agent-selector";
import { useProject } from "@/contexts/project-context";
import {
  useChatSessions,
} from "@/contexts/chat-session-context";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
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
import { ChatWaveBackground } from "@/components/chat/chat-wave-background";
import { useChatBackgroundPreference } from "@/lib/chat-background-preference";
import { useTheme } from "@/contexts/theme-context";
import {
  useWorkspaceShellRegistration,
} from "@/components/layout/shell-context";
import {
  AlertCircle,
  CheckSquare,
  FolderOpen,
  MoreHorizontal,
  PanelRightClose,
  PanelRightOpen,
  RefreshCcw,
  Users,
  Sliders,
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
  buildPlanApprovalResponsePayload,
  type AskUserQuestionRequest,
  type ExternalModelPromptRequest,
  type PlanApprovalRequest,
  type ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

const PROJECT_CONTEXT_KEY = "aoitalk-chat-project-context";
const DEEP_RESEARCH_KEY = "aoitalk-chat-deep-research";
const PENDING_AGENT_KEY = "aoitalk-chat-pending-agent";
const TEMPORARY_FILE_DRAFT_SESSION_KEY = "__new_chat__";

function ChatPageInner() {
  const { isMobile } = useSidebar();
  const { resolvedTheme } = useTheme();
  const chatBackgroundPreference = useChatBackgroundPreference();
  const chatViewportRef = useRef<HTMLDivElement>(null);
  const searchParams = useSearchParams();
  const router = useRouter();
  const currentUserId = useCurrentUserId();
  const {
    visible: contextRailVisible,
    hydrated: contextRailVisibilityHydrated,
    toggle: toggleContextRailVisibility,
  } = useContextRailVisibility(currentUserId);
  const hasPersistedNewChatDraft = hasPersistedChatComposerDraft(
    NEW_CHAT_COMPOSER_DRAFT_KEY,
    currentUserId,
  );
  const [memoryMode, setMemoryMode] = useState(searchParams.get("view") === "memory");
  const [memoryRailOpen, setMemoryRailOpen] = useState(false);
  useEffect(() => { if (!memoryMode) setMemoryRailOpen(false); }, [memoryMode]);
  const contextRailToggleOpen = contextRailVisible;
  const handleContextRailToggle = useCallback(() => {
    toggleContextRailVisibility();
  }, [toggleContextRailVisibility]);
  const memoryRailPresentation = useMemo(() => memoryMode && !isMobile
    ? { open: memoryRailOpen, onOpenChange: setMemoryRailOpen } : undefined,
  [memoryMode, memoryRailOpen, isMobile]);
  const memoryView = searchParams.get("memory_view") === "process" ? "process" : "graph";
  const memoryPreferenceKey = currentUserId ? `aoitalk.chat.display.${currentUserId}` : null;
  useEffect(() => {
    if (!memoryPreferenceKey) return;
    const routeView = searchParams.get("view");
    const selected = routeView === "memory" || (routeView !== "normal" && safeLocalStorageGetItem(memoryPreferenceKey) === "memory");
    setMemoryMode(selected);
    if (routeView === "memory" || routeView === "normal") safeLocalStorageSetItem(memoryPreferenceKey, routeView);
  }, [memoryPreferenceKey, searchParams]);
  const changeChatDisplay = (mode: "normal" | "memory", view?: "graph" | "process") => {
    setMemoryMode(mode === "memory");
    if (memoryPreferenceKey) safeLocalStorageSetItem(memoryPreferenceKey, mode);
    const query = new URLSearchParams(searchParams.toString());
    query.set("view", mode);
    if (view) query.set("memory_view", view);
    router.push(`/chat?${query}`);
  };
  const runtime = useOptionalRuntimeContext();
  const refreshCharacters = runtime?.refreshCharacters;
  const searchParamSessionId = searchParams.get("s") || null;
  const agentQueryId = searchParams.get("agent_id")?.trim() || null;
  const appQueryId = searchParams.get("app_id") || null;
  const appQueryTargetId = searchParams.get("app_target_id") || null;
  const appQueryProjectId = searchParams.get("project_id") || null;
  const { selectedProjectId, selectedSpaceId, allProjects, initialLoadComplete } = useProject();
  const {
    addSession,
    markSessionRead,
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
      // An Agent deeplink is an explicit new-Chat intent.  A canonical `s`
      // route still wins because activeSessionId is initialized from it and
      // the restore hook exits before consulting last-session storage.
      suppressLastSessionRestore: Boolean(appQueryId || agentQueryId),
      hasUnsavedNewChatDraft: hasPersistedNewChatDraft,
      router,
      allProjects,
      sessions,
    });

  // Character metadata is session-scoped on the backend. Refresh it whenever
  // the active conversation changes so the picker follows the restored
  // session. A blank route must also refresh the global character after Back;
  // otherwise a worker session's character remains visible in the new Chat.
  useEffect(() => {
    void refreshCharacters?.(activeSessionId);
  }, [activeSessionId, refreshCharacters]);

  useEffect(() => {
    if (!activeSessionId) return;
    void markSessionRead(activeSessionId);
  }, [activeSessionId, markSessionRead]);
  const [includeProjectContext, setIncludeProjectContext] = useState(() => {
    if (typeof window === "undefined") return false;
    return safeLocalStorageGetItem(PROJECT_CONTEXT_KEY) === "true";
  });
  const [deepResearchEnabled, setDeepResearchEnabled] = useState(() => {
    if (typeof window === "undefined") return false;
    return safeLocalStorageGetItem(DEEP_RESEARCH_KEY) === "true";
  });
  // Keep the server and first client render identical.  The per-tab pending
  // intent is hydrated after mount; an explicit URL Agent always wins.
  const [pendingAgentId, setPendingAgentId] = useState<string | null>(agentQueryId);
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
    if (activeSessionId) {
      // Existing-session navigation (including an AoiDots worker detail)
      // must not discard the Agent selected on the originating blank Chat.
      // Creation is the only authoritative consumer and clears it through
      // onSessionAgentConsumed after the server binding is confirmed.
      return;
    }
    const nextAgentId =
      agentQueryId || safeSessionStorage.getItem(PENDING_AGENT_KEY)?.trim() || null;
    setPendingAgentId((current) =>
      current === nextAgentId ? current : nextAgentId,
    );
  }, [activeSessionId, agentQueryId]);

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

  // A selector on the blank Chat route is session-scoped input for the next
  // server-created session. Keep it across a reload in this tab only, but
  // clear it as soon as that exact binding is persisted by canonical create.
  useEffect(() => {
    if (pendingAgentId) {
      safeSessionStorage.setItem(PENDING_AGENT_KEY, pendingAgentId);
    } else {
      safeSessionStorage.removeItem(PENDING_AGENT_KEY);
    }
  }, [pendingAgentId]);

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
    useResponseModelOptions({
      catalog: runtime?.llmCatalog ?? null,
      // RuntimeProvider owns the /llm/models request. Skip the independent
      // SWR fetch whenever that shared context is mounted; standalone callers
      // keep the historical fetch behaviour.
      shared: runtime != null,
    });
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
  const [activeSessionSnapshot, setActiveSessionSnapshot] = useState<{
    ownerKey: string | null | undefined;
    session: ConversationSession;
  } | null>(null);
  const activeSessionHydrationRef = useRef({
    sessionId: null as string | null,
    requestId: 0,
  });
  const currentSession = useMemo(
    () => {
      if (!activeSessionId) return null;
      const snapshot = activeSessionSnapshot;
      return (
        (currentUserId
          ? sessions.find(
              (session) =>
                session.id === activeSessionId &&
                session.user_id === currentUserId,
            )
          : null) ??
        (currentUserId &&
        snapshot &&
        snapshot.ownerKey === currentUserId &&
        snapshot.session.id === activeSessionId
          ? snapshot.session
          : null)
      );
    },
    [activeSessionId, activeSessionSnapshot, currentUserId, sessions],
  );
  useEffect(() => {
    if (!currentUserId || !activeSessionId) {
      activeSessionHydrationRef.current = {
        sessionId: null,
        requestId: activeSessionHydrationRef.current.requestId + 1,
      };
      setActiveSessionSnapshot(null);
      return;
    }

    const listed = currentUserId
      ? sessions.find(
          (session) =>
            session.id === activeSessionId &&
            session.user_id === currentUserId,
        )
      : null;
    if (listed) {
      activeSessionHydrationRef.current.sessionId = activeSessionId;
      setActiveSessionSnapshot((previous) => {
        if (
          previous?.ownerKey === currentUserId &&
          previous.session.id === listed.id &&
          previous.session === listed
        ) {
          return previous;
        }
        return { ownerKey: currentUserId, session: listed };
      });
      return;
    }
    if (
      activeSessionSnapshot?.ownerKey === currentUserId &&
      activeSessionSnapshot.session.id === activeSessionId
    ) return;
    if (activeSessionHydrationRef.current.sessionId === activeSessionId) return;

    const requestId = activeSessionHydrationRef.current.requestId + 1;
    activeSessionHydrationRef.current = { sessionId: activeSessionId, requestId };
    const requestedSessionId = activeSessionId;
    const requestedOwnerKey = currentUserId;
    let cancelled = false;
    void chatApi
      .resumeSession(requestedSessionId, false)
      .then(({ session }) => {
        if (
          cancelled ||
          activeSessionIdRef.current !== requestedSessionId ||
          currentUserId !== requestedOwnerKey ||
          activeSessionHydrationRef.current.requestId !== requestId
        ) {
          return;
        }
        setActiveSessionSnapshot({ ownerKey: requestedOwnerKey, session });
        upsertSession(session);
      })
      .catch(() => {
        if (
          !cancelled &&
          activeSessionIdRef.current === requestedSessionId &&
          currentUserId === requestedOwnerKey &&
          activeSessionHydrationRef.current.requestId === requestId
        ) {
          setActiveSessionSnapshot(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, activeSessionIdRef, activeSessionSnapshot, currentUserId, sessions, upsertSession]);
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
  const isSending =
    generationState.lifecycle.phase === "dispatching" &&
    generationState.lifecycle.sessionId === activeSessionId;
  const isWaitingResponse = selectGenerationIsBusy(
    generationState,
    activeSessionId,
  );
  const effectiveProjectId = appProjectId;
  const displayedProjectId = currentSession
    ? currentSession.project_id ?? null
    : appQueryId
      ? appQueryProjectId ?? null
      : selectedProjectId ?? null;
  const displayedProject = displayedProjectId
    ? allProjects.find((project) => project.id === displayedProjectId)
    : undefined;
  const displayedProjectName = displayedProject?.name;
  // Existing sessions own their canonical project scope, including explicit
  // null. Only a blank/new Chat may inherit the global project selection for
  // Agent catalog discovery.
  const agentCatalogProjectId = activeSessionId
    ? currentSession?.id === activeSessionId
      ? currentSession.project_id ?? null
      : null
    : effectiveProjectId ?? null;
  const composerAgentId = activeSessionId
    ? currentSession?.agent_id ?? null
    : pendingAgentId;
  const pendingNewSessionAgentId = activeSessionId ? null : pendingAgentId;
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
      const created = pendingNewSessionAgentId
        ? await chatApi.createSession(
            characterName,
            effectiveProjectId,
            undefined,
            appContext,
            generationReadyMain,
            pendingNewSessionAgentId,
          )
        : await chatApi.createSession(
            characterName,
            effectiveProjectId,
            undefined,
            appContext,
            generationReadyMain,
          );
      if (
        pendingNewSessionAgentId &&
        created.session.agent_id !== pendingNewSessionAgentId
      ) {
        throw new Error(
          "サーバーでAgent bindingを確認できなかったため、音声セッションを開始しませんでした。",
        );
      }
      const sessionId = created.session.id;
      addSession(created.session);
      if (
        pendingNewSessionAgentId &&
        created.session.agent_id === pendingNewSessionAgentId
      ) {
        setPendingAgentId(null);
      }
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
    pendingNewSessionAgentId,
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
  } = useWebSocket(
    activeSessionId,
    activeAgentRunIdForSocket,
    currentSession?.agent_id ?? null,
  );
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
  const latestVoiceAssistantMessage = (() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role !== "assistant" || !message.content.trim()) continue;
      return { id: message.id, text: message.content };
    }
    return null;
  })();
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
    promoteAttachedFiles,
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

  const { contextSnapshot, contextSnapshotStatus, contextBinding } = useContextSnapshot({
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
    handleRetryMessage,
    retryDeepResearchAssistantPersistence,
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
    newSessionAgentId: pendingNewSessionAgentId,
    onSessionAgentConsumed: () => setPendingAgentId(null),
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

  const memorySelection = useMemorySelection({
    sessionId: activeSessionId, lifecycle: generationState.lifecycle, messages,
    enabled: memoryMode, busy: chatBusy, toolRevision: liveToolResults.length,
  });

  useChatWebSocketEvents({
    lastMessage,
    onMemorySelectionEvent: memorySelection.onEvent,
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
    sendExternalModelPromptResponse,
    setAskUserQuestionRequest,
    setAskUserQuestionDraft,
    setAskUserQuestionChoices,
    setPlanApprovalRequest,
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
    const payload = buildPlanApprovalResponsePayload(planApprovalRequest);
    if (!payload) return;
    sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      payload,
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalFeedbackDraft("");
  }, [planApprovalRequest, sendHumanInteractionResponse]);

  const handlePlanApprovalFeedback = useCallback(() => {
    if (!planApprovalRequest) return;
    sendHumanInteractionResponse(
      planApprovalRequest.requestId,
      {
        action: "feedback",
        feedback: planApprovalFeedbackDraft,
        plan_text: planApprovalRequest.planText,
        revision: planApprovalRequest.revision,
      },
      planApprovalRequest.sessionId,
      "plan_approval_response",
    );
    setPlanApprovalRequest(null);
    setPlanApprovalFeedbackDraft("");
  }, [
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
        contextBinding={contextBinding}
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
      contextBinding,
    ],
  );
  useWorkspaceShellRegistration({
    workspaceNavigation: chatWorkspaceNavigation,
    contextRail: chatContextRail,
    priority: 40,
    id: "chat-workspace",
    routeKey: "/chat",
    contextRailPersistent: true,
    contextRailPersistentVisible: contextRailVisible,
    contextRailPresentation: memoryRailPresentation,
  });

  const chatMessageContent = (
    isLoadingMessages ? (
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
            onRerunMessage={handleRerunMessage}
            onRetryMessage={handleRetryMessage}
            onRetryMessagePersistence={retryDeepResearchAssistantPersistence}
            onSwitchBranch={handleSwitchBranch}
            responseModelOptions={responseModelOptions}
            responseModelOptionsLoading={responseModelOptionsLoading}
          />
        )
  );

  return (
    <div
      ref={chatViewportRef}
      className="chat-viewport-root relative flex h-full min-h-0 overflow-hidden bg-background text-on-surface"
      onDragOver={handleChatFileDragOver}
      onDrop={handleChatFileDrop}
    >
      <div className="relative isolate flex min-w-0 flex-1 flex-col overflow-hidden">
        <ChatWaveBackground
          enabled={chatBackgroundPreference.enabled}
          active={!memoryMode}
          dark={resolvedTheme === "dark"}
          viewportRef={chatViewportRef}
        />
        <div className={`relative z-10 flex min-w-0 flex-1 flex-col overflow-hidden ${chatBackgroundPreference.enabled === true && !memoryMode ? "bg-transparent" : "bg-background"} ${memoryMode ? "memory-chat-column" : ""}`} data-memory-chat={memoryMode || undefined}>
        {/* ヘッダーバー：チャット操作・ステアリングトグル */}
        <div
          className="flex min-h-12 shrink-0 items-center gap-2 border-b border-border-subtle bg-background px-4 py-2 min-[768px]:flex-wrap min-[768px]:gap-1 min-[768px]:px-2 min-[900px]:gap-2 min-[900px]:px-4"
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
            </DropdownMenuContent>
          </DropdownMenu>
          <div
            className="min-w-0 min-[768px]:min-w-[12rem] min-[768px]:max-w-full min-[768px]:shrink-0 min-[768px]:overflow-hidden"
            data-testid="chat-agent-toolbar-slot"
          >
            <ChatAgentSelector
              sessionId={activeSessionId}
              sessionAgentId={currentSession?.agent_id ?? null}
              pendingAgentId={pendingNewSessionAgentId}
              projectId={agentCatalogProjectId}
              disabled={appQueryContextPending}
              busy={chatBusy || isStreaming}
              isGroupChat={isGroupChat}
              onPendingAgentChange={setPendingAgentId}
              onSessionUpdated={upsertSession}
            />
          </div>
          <div className="ml-auto flex gap-1" role="group" aria-label="チャットの表示">
            <Button variant={memoryMode ? "ghost" : "secondary"} size="sm" aria-pressed={!memoryMode} onClick={() => changeChatDisplay("normal")}>通常</Button>
            <Button variant={memoryMode ? "secondary" : "ghost"} size="sm" aria-pressed={memoryMode} onClick={() => changeChatDisplay("memory", "graph")}>記憶可視化</Button>
          </div>
          {isMobile ? (
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
          ) : !memoryMode ? (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              data-testid="chat-context-rail-toggle"
              className="text-text-secondary hover:bg-surface-slate hover:text-primary"
              title={contextRailToggleOpen ? "Context Railを閉じる" : "Context Railを開く"}
              aria-label={contextRailToggleOpen ? "Context Railを閉じる" : "Context Railを開く"}
              aria-expanded={contextRailToggleOpen}
              disabled={!contextRailVisibilityHydrated}
              onClick={handleContextRailToggle}
            >
              {contextRailToggleOpen ? (
                <PanelRightClose className="size-4" aria-hidden="true" />
              ) : (
                <PanelRightOpen className="size-4" aria-hidden="true" />
              )}
            </Button>
          ) : null}
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

        {memoryMode && !isLoadingMessages && !sessionLoadError ? (
          <MemoryVisualization
            key={activeGenerationKey ?? activeSessionId ?? "new"}
            items={memorySelection.items} stages={memorySelection.stages}
            history={memorySelection.history} liveObserved={memorySelection.liveObserved}
            toolRecall={memorySelection.toolRecall} recheckToolReference={memorySelection.recheckToolReference}
            liveToolObserved={(chatBusy || isStreaming) && liveToolResults.some(result => result.tool?.endsWith("search_past_chats") || result.tool?.endsWith("semantic_memory_search"))}
            status={memorySelection.status} itemsPending={memorySelection.itemsPending} generationKey={activeGenerationKey}
            agentRunId={displayAgentRunId} busy={chatBusy || isStreaming}
            terminalPhase={generationState.lifecycle.sessionId === activeSessionId ? generationState.lifecycle.phase : "idle"}
            view={memoryView} onViewChange={(view) => changeChatDisplay("memory", view)}
            messages={messages} streamingContent={streamingContent} activityMessage={activityMessage}
            pastSearchStatus={displayActiveTool?.includes("search_past_chats") ? "過去会話検索を実行中" : "過去会話検索は別の実検索として表示します"}
            characterName={currentSession?.character_name || "アシスタント"}
            composer={null} conversationHistory={chatMessageContent}
          />
        ) : chatMessageContent}

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
        <div className={memoryMode ? "memory-host-composer" : undefined}>
        <ChatComposer
          onSend={handleSendMessageFromComposer}
          onSteer={handleSteerGeneration}
          onStop={handleStopGeneration}
          disabled={generationInputBlocked || appQueryContextPending}
          busy={chatBusy}
          generationTerminalKey={generationTerminalKey}
          attachedFiles={attachedFiles}
          onAttachedFilesChange={setAttachedFiles}
          onAttachmentsPromoted={promoteAttachedFiles}
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
          agentId={composerAgentId}
          contextSnapshot={contextSnapshot}
          contextSnapshotStatus={contextSnapshotStatus}
          appContext={effectiveAppContext}
          onAppContextChange={handleAppContextChange}
          ensureLiveVoiceConversationSession={ensureLiveVoiceConversationSession}
          voiceAssistantMessage={latestVoiceAssistantMessage}
        />
        </div>
        </div>
      </div>

      {isMobile && (
        <Sheet
          open={mobileRailOpen}
          onOpenChange={(open) => {
            setMobileRailOpen(open);
          }}
        >
          <SheetContent
            side="right"
            showCloseButton={false}
            className="w-[min(92vw,24rem)] p-0"
          >
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
                  contextBinding={contextBinding}
                />
              )}
            </div>
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
        feedbackDraft={planApprovalFeedbackDraft}
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
        spaceId={selectedSpaceId}
        sessionId={activeSessionId}
        messageId={[...messages].reverse().find((message) => message.role === "user" &&
          message.metadata?.attachments?.length && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(message.id))?.id}
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
