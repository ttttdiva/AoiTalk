"use client";

import {
  Suspense,
  useState,
  useEffect,
  useCallback,
  useRef,
  useReducer,
  type CSSProperties,
} from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type {
  ChatToolResultMetadata,
  ConversationGenerationStatus,
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
import { ConversationSearchDialog } from "@/components/chat/conversation-search-dialog";
import { useProject } from "@/contexts/project-context";
import {
  CHAT_SESSION_TITLE_UPDATED_EVENT,
  useChatSessions,
} from "@/contexts/chat-session-context";
import { ScenarioPanel } from "@/components/chat/scenario-panel";
import { RelatedInformationPanel } from "@/components/chat/related-information-panel";
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
import { useChatWebSocketEvents } from "@/hooks/use-chat-websocket-events";
import {
  useChatMessaging,
  type PendingMessage,
} from "@/hooks/use-chat-messaging";
import { useChatGenerationControls } from "@/hooks/use-chat-generation-controls";
import { useConversationSearch } from "@/hooks/use-conversation-search";
import { useRelatedTasksPanel } from "@/hooks/use-related-tasks-panel";
import {
  AlertCircle,
  FolderOpen,
  Info,
  RefreshCcw,
  Users,
  Sliders,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ExternalModelPromptDialog,
  ToolPermissionDialog,
  type ExternalModelPromptRequest,
  type ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

const PROJECT_CONTEXT_KEY = "aoitalk-chat-project-context";
const DEEP_RESEARCH_KEY = "aoitalk-chat-deep-research";
const TEMPORARY_FILE_DRAFT_SESSION_KEY = "__new_chat__";

function isScenarioWorkflowSession(session: ConversationSession) {
  const characterName = session.character_name || "";
  return (
    /^scenario_roleplay:[^:]+:[^:]+$/.test(characterName) ||
    characterName.startsWith("scenario_") ||
    characterName.startsWith("trpg_room_") ||
    session.title?.startsWith("[シナリオ]") ||
    session.title?.startsWith("[執筆]") ||
    session.title?.startsWith("[TRPG]")
  );
}

function ChatPageInner() {
  const { isMobile, open: sidebarOpen } = useSidebar();
  const searchParams = useSearchParams();
  const router = useRouter();
  const searchParamSessionId = searchParams.get("s") || null;
  const { selectedProjectId, allProjects } = useProject();
  const {
    addSession,
    updateSessionTitle: updateSidebarTitle,
    bumpSession,
    sessions,
    fetchSessions,
  } = useChatSessions();
  const { activeSessionId, activeSessionIdRef, activateSession } =
    useActiveChatSession({
      searchParamSessionId,
      router,
      allProjects,
      sessions,
    });
  const [includeProjectContext, setIncludeProjectContext] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(PROJECT_CONTEXT_KEY) === "true";
  });
  const [deepResearchEnabled, setDeepResearchEnabled] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(DEEP_RESEARCH_KEY) === "true";
  });
  const {
    llmMode,
    llmModeOptions,
    llmModeLabels,
    setLlmModeState,
    setLlmModeOptions,
    setLlmModeLabels,
    handleLlmModeChange,
  } = useChatLlmMode();

  // Sidebarが閉じている/モバイル表示でも履歴移動のデータを保持する。
  useEffect(() => {
    void fetchSessions();
  }, [fetchSessions]);

  useEffect(() => {
    localStorage.setItem(
      PROJECT_CONTEXT_KEY,
      includeProjectContext ? "true" : "false",
    );
  }, [includeProjectContext]);

  useEffect(() => {
    localStorage.setItem(
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
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  const [sessionLoadError, setSessionLoadError] = useState<string | null>(null);
  const [sessionLoadAttempt, setSessionLoadAttempt] = useState(0);
  const [isSending, setIsSending] = useState(false);
  const [waitingResponseSessionIds, setWaitingResponseSessionIds] = useState<
    string[]
  >([]);
  const [restoredGenerationStatus, setRestoredGenerationStatus] =
    useState<ConversationGenerationStatus | null>(null);
  const [pendingAgentRunId, setPendingAgentRunId] = useState<string | null>(
    null,
  );
  const { responseModelOptions, responseModelOptionsLoading } =
    useResponseModelOptions();
  const [scenarioSession, setScenarioSession] =
    useState<
      Awaited<ReturnType<typeof chatApi.getScenarioPlaySessionByConversation>>
    >(null);
  const [writingSession, setWritingSession] =
    useState<
      Awaited<ReturnType<typeof chatApi.getWritingSessionByConversation>>
    >(null);
  const [roleplaySession, setRoleplaySession] = useState<{
    scenario: { id: string; title: string };
    character: {
      id: string;
      name: string;
      role?: string;
      description?: string;
    };
  } | null>(null);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // グループチャット
  const [groupDialogOpen, setGroupDialogOpen] = useState(false);
  const [currentSession, setCurrentSession] =
    useState<ConversationSession | null>(null);
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

  useEffect(() => {
    const handleTitleUpdated = (event: Event) => {
      const detail = (
        event as CustomEvent<{ sessionId?: unknown; title?: unknown }>
      ).detail;
      const sessionId =
        typeof detail?.sessionId === "string" ? detail.sessionId : "";
      const title = typeof detail?.title === "string" ? detail.title : "";
      if (!sessionId) return;
      setCurrentSession((prev) =>
        prev && prev.id === sessionId ? { ...prev, title } : prev,
      );
    };

    window.addEventListener(
      CHAT_SESSION_TITLE_UPDATED_EVENT,
      handleTitleUpdated,
    );
    return () => {
      window.removeEventListener(
        CHAT_SESSION_TITLE_UPDATED_EVENT,
        handleTitleUpdated,
      );
    };
  }, []);

  useEffect(() => {
    const handleCharacterUpdated = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          sessionId?: unknown;
          characterName?: unknown;
          characterSlug?: unknown;
        }>
      ).detail;
      const sessionId =
        typeof detail?.sessionId === "string" ? detail.sessionId : "";
      const characterName =
        typeof detail?.characterSlug === "string" && detail.characterSlug.trim()
          ? detail.characterSlug.trim()
          : typeof detail?.characterName === "string"
            ? detail.characterName.trim()
          : "";
      if (!sessionId || !characterName) return;
      setCurrentSession((prev) =>
        prev && prev.id === sessionId
          ? { ...prev, character_name: characterName }
          : prev,
      );
    };

    window.addEventListener(
      "aoi-character-changed",
      handleCharacterUpdated,
    );
    return () => {
      window.removeEventListener(
        "aoi-character-changed",
        handleCharacterUpdated,
      );
    };
  }, []);

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
  const isScenarioChatSession =
    (currentSession ? isScenarioWorkflowSession(currentSession) : false) ||
    Boolean(scenarioSession || writingSession || roleplaySession);
  const isWaitingResponse = Boolean(
    activeSessionId && waitingResponseSessionIds.includes(activeSessionId),
  );
  const effectiveProjectId = isScenarioChatSession
    ? undefined
    : (currentSession?.project_id ?? selectedProjectId ?? undefined);
  const displayedProjectId =
    currentSession?.project_id ?? (!activeSessionId ? selectedProjectId : null);
  const displayedProjectName = displayedProjectId
    ? allProjects.find((project) => project.id === displayedProjectId)?.name
    : null;
  const projectAssociationLabel = currentSession
    ? currentSession.project_id
      ? (displayedProjectName ?? "不明なプロジェクト")
      : "プロジェクトなし"
    : displayedProjectName;
  const temporaryFileSessionKey =
    activeSessionId ?? TEMPORARY_FILE_DRAFT_SESSION_KEY;
  const chatViewportStyle = {
    "--chat-viewport-offset":
      sidebarOpen && !isMobile ? "calc(var(--sidebar-width) / -2)" : "0px",
  } as CSSProperties;

  // WebSocket接続後に送信する保留メッセージ
  const pendingMessageRef = useRef<PendingMessage | null>(null);

  // WebSocket
  const {
    isConnected,
    lastMessage,
    isStreaming,
    activeTool,
    activityMessage,
    activeAgentRunId,
    streamBuffer,
    sendMessage,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    stopGeneration,
    sendSteering,
  } = useWebSocket(activeSessionId);
  const restoredGenerationRunning =
    restoredGenerationStatus?.running === true && !isStreaming;
  const displayIsWaitingResponse =
    isWaitingResponse || restoredGenerationRunning;
  const displayActiveTool =
    activeTool ??
    (restoredGenerationRunning
      ? (restoredGenerationStatus?.active_tool ?? null)
      : null);
  const displayAgentRunId = isStreaming
    ? activeAgentRunId
    : restoredGenerationRunning
      ? (restoredGenerationStatus?.agent_run_id ?? null)
      : displayIsWaitingResponse
        ? pendingAgentRunId
        : null;
  const chatBusy = isStreaming || isSending || displayIsWaitingResponse;

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

  // ストリーミング内容を反映するための状態
  const [streamingContent, setStreamingContent] = useState("");
  const [liveToolResults, setLiveToolResults] = useState<
    ChatToolResultMetadata[]
  >([]);
  const liveToolResultsRef = useRef<ChatToolResultMetadata[]>([]);
  const [steeringInstructions, setSteeringInstructions] = useState<
    SubmittedSteeringInstruction[]
  >([]);
  const streamingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(
    null,
  );
  const responsePollGenerationRef = useRef(0);

  const { contextSnapshot, contextSnapshotStatus } = useContextSnapshot({
    activeSessionId,
    activeSessionIdRef,
    includeProjectContext,
    llmMode,
    messagesLength: messages.length,
    liveToolResultsLength: liveToolResults.length,
    chatBusy,
  });

  const markWaitingResponse = useCallback((sessionId: string | null) => {
    if (!sessionId) return;
    setWaitingResponseSessionIds((prev) =>
      prev.includes(sessionId) ? prev : [...prev, sessionId],
    );
  }, []);

  const clearWaitingResponse = useCallback((sessionId: string | null) => {
    if (!sessionId) {
      return;
    }
    setWaitingResponseSessionIds((prev) =>
      prev.includes(sessionId) ? prev.filter((id) => id !== sessionId) : prev,
    );
  }, []);

  const resetDisplayedGenerationState = useCallback(() => {
    responsePollGenerationRef.current += 1;
    if (streamingIntervalRef.current) {
      clearInterval(streamingIntervalRef.current);
      streamingIntervalRef.current = null;
    }
    setStreamingContent("");
    liveToolResultsRef.current = [];
    setLiveToolResults([]);
    setRestoredGenerationStatus(null);
    setPendingAgentRunId(null);
  }, []);

  // 処理済みメッセージのタイムスタンプを追跡（重複処理防止）
  const processedMsgRef = useRef<string | null>(null);

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
    handleCreateGroupChat,
    handleSendMessage,
    handleEditMessage,
    handleRerunMessage,
  } = useChatMessaging({
    router,
    activeSessionId,
    activeSessionIdRef,
    activateSession,
    allProjects,
    effectiveProjectId,
    isScenarioChatSession,
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
    bumpSession,
    updateSidebarTitle,
    sendMessage,
    dispatchChatTimeline,
    markWaitingResponse,
    clearWaitingResponse,
    resetDisplayedGenerationState,
    setIsSending,
    setRestoredGenerationStatus,
    setPendingAgentRunId,
    setCurrentSession,
    setSteeringInstructions,
    setIsLoadingMessages,
    setSessionLoadError,
    setScenarioSession,
    setWritingSession,
    setRoleplaySession,
  });

  const {
    handleToolPermissionDecision,
    handleExternalModelPromptDecision,
    handleExternalModelPromptKeyDown,
    handleStopGeneration,
    handleSteerGeneration,
  } = useChatGenerationControls({
    activeSessionId,
    toolPermissionRequest,
    externalModelPromptRequest,
    externalModelPromptDraft,
    responsePollGenerationRef,
    streamingIntervalRef,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    stopGeneration,
    sendSteering,
    clearWaitingResponse,
    setRestoredGenerationStatus,
    setSteeringInstructions,
    setStreamingContent,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
  });

  useChatWebSocketEvents({
    lastMessage,
    activeSessionId,
    streamBuffer,
    pendingAgentRunId,
    currentSession,
    processedMsgRef,
    liveToolResultsRef,
    streamingIntervalRef,
    responsePollGenerationRef,
    dispatchChatTimeline,
    clearWaitingResponse,
    refreshPersistedMessages,
    maybeGenerateLoadedSessionTitle,
    updateSidebarTitle,
    setLlmModeState,
    setLlmModeOptions,
    setLlmModeLabels,
    setToolPermissionRequest,
    setExternalModelPromptRequest,
    setExternalModelPromptDraft,
    setRestoredGenerationStatus,
    setSteeringInstructions,
    setStreamingContent,
    setLiveToolResults,
    setCurrentSession,
    play,
    stopAudio,
    setVolume,
  });

  // クリーンアップ
  useEffect(() => {
    return () => {
      responsePollGenerationRef.current += 1;
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
      }
    };
  }, []);

  const hasScenarioPanel = Boolean(
    scenarioSession || writingSession || roleplaySession,
  );
  const {
    relatedPanelOpen,
    setRelatedPanelOpen,
    relatedTaskCount,
    selectedRelatedTaskId,
    setSelectedRelatedTaskId,
    mobileRailOpen,
    setMobileRailOpen,
    handleRelatedPanelToggle,
    handleRelatedTasksChange,
    notifyTaskUpdated,
  } = useRelatedTasksPanel({ activeSessionId, isMobile, hasScenarioPanel });
  const renderScenarioPanel = () => (
    <ScenarioPanel
      session={scenarioSession}
      writingSession={writingSession}
      roleplaySession={roleplaySession}
      onRoll={(expression) =>
        sendMessage(
          `/roll ${expression}`,
          effectiveProjectId,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          undefined,
          activeSessionId,
        )
      }
    />
  );

  return (
    <div
      className="relative flex h-full overflow-hidden"
      style={chatViewportStyle}
      onDragOver={handleChatFileDragOver}
      onDrop={handleChatFileDrop}
    >
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* ヘッダーバー：グループチャット作成・ステアリングトグル */}
        <div className="flex items-center gap-1.5 border-b border-border bg-card px-3 py-1.5">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setGroupDialogOpen(true)}
            title="グループチャットを作成"
          >
            <Users className="size-3.5 mr-1" />
            グループ
          </Button>
          <Button
            variant={relatedPanelOpen ? "secondary" : "ghost"}
            size="sm"
            onClick={handleRelatedPanelToggle}
            title="チャットの関連情報"
          >
            <Info className="mr-1 size-3.5" />
            関連情報
            {relatedTaskCount > 0 && (
              <span className="ml-1 rounded-full bg-muted px-1.5 text-[10px]">
                {relatedTaskCount}
              </span>
            )}
          </Button>
          {projectAssociationLabel && (
            <span className="inline-flex min-w-0 max-w-[52%] items-center gap-1 rounded-md border border-border/60 bg-background/60 px-2 py-1 text-xs text-muted-foreground">
              <FolderOpen className="size-3.5 shrink-0" />
              <span className="truncate">
                プロジェクト: {projectAssociationLabel}
              </span>
            </span>
          )}
          {isRpSession && activeSessionId && (
            <Button
              variant={steeringVisible ? "secondary" : "ghost"}
              size="sm"
              onClick={() => setSteeringVisible((v) => !v)}
              title="ステアリングパネル"
            >
              <Sliders className="size-3.5" />
            </Button>
          )}
          {isGroupChat && (
            <span className="ml-auto text-xs text-muted-foreground">
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
          <div className="flex flex-1 items-center justify-center">
            <div className="rounded-2xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
              メッセージを読み込み中...
            </div>
          </div>
        ) : sessionLoadError ? (
          <div className="flex flex-1 items-center justify-center px-4">
            <div className="flex max-w-md flex-col items-center gap-3 rounded-2xl border border-border bg-card px-6 py-5 text-center">
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
            onEditMessage={handleEditMessage}
            onRerunMessage={handleRerunMessage}
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

        <ChatComposer
          onSend={handleSendMessage}
          onSteer={handleSteerGeneration}
          onStop={handleStopGeneration}
          disabled={chatBusy}
          busy={chatBusy}
          attachedFiles={attachedFiles}
          onAttachedFilesChange={setAttachedFiles}
          projectContextEnabled={includeProjectContext}
          onProjectContextToggle={setIncludeProjectContext}
          deepResearchEnabled={deepResearchEnabled}
          onDeepResearchToggle={setDeepResearchEnabled}
          llmMode={llmMode}
          llmModeOptions={llmModeOptions}
          llmModeLabels={llmModeLabels}
          onLlmModeChange={handleLlmModeChange}
          steeringInstructions={steeringInstructions}
          onClearSteeringInstructions={() => setSteeringInstructions([])}
          projectId={effectiveProjectId}
          sessionId={activeSessionId}
          contextSnapshot={contextSnapshot}
          contextSnapshotStatus={contextSnapshotStatus}
        />
      </div>

      {!isMobile && (relatedPanelOpen || hasScenarioPanel) && (
        <aside className="flex h-full w-80 shrink-0 flex-col border-l bg-muted/10">
          {relatedPanelOpen && (
            <RelatedInformationPanel
              sessionId={activeSessionId}
              onTaskClick={setSelectedRelatedTaskId}
              onTasksChange={handleRelatedTasksChange}
            />
          )}
          {hasScenarioPanel && (
            <div className="min-h-0 flex-1 overflow-hidden">
              {renderScenarioPanel()}
            </div>
          )}
        </aside>
      )}

      {isMobile && (
        <Sheet
          open={mobileRailOpen || relatedPanelOpen}
          onOpenChange={(open) => {
            setMobileRailOpen(open);
            if (!open) setRelatedPanelOpen(false);
          }}
        >
          <SheetContent side="right" className="w-[min(92vw,24rem)] p-0">
            <SheetHeader className="border-b px-4 py-3">
              <SheetTitle>関連情報</SheetTitle>
            </SheetHeader>
            <div className="flex min-h-0 flex-1 flex-col">
              {relatedPanelOpen && (
                <RelatedInformationPanel
                  sessionId={activeSessionId}
                  onTaskClick={setSelectedRelatedTaskId}
                  onTasksChange={handleRelatedTasksChange}
                />
              )}
              {hasScenarioPanel && (
                <div className="min-h-0 flex-1 overflow-hidden">
                  {renderScenarioPanel()}
                </div>
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
        request={externalModelPromptRequest}
        draft={externalModelPromptDraft}
        onDraftChange={setExternalModelPromptDraft}
        onKeyDown={handleExternalModelPromptKeyDown}
        onDecision={handleExternalModelPromptDecision}
      />

      <ToolPermissionDialog
        request={toolPermissionRequest}
        onDecision={handleToolPermissionDecision}
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
