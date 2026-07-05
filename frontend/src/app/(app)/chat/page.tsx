"use client";

import {
  Suspense,
  useState,
  useEffect,
  useCallback,
  useRef,
  useReducer,
  type CSSProperties,
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  chatApi,
  getLlmMode,
  getLlmModelCatalog,
  setLlmMode,
} from "@/lib/chat-api";
import type {
  ChatAttachmentMetadata,
  ChatCommandCapability,
  ChatResponseModelOption,
  ChatResponseModelSelection,
  ChatToolResultMetadata,
  ConversationGenerationStatus,
  ConversationMessage,
  ConversationSearchResult,
  ConversationSession,
  LlmCatalogModelOption,
  LlmCatalogProvider,
  LlmModelCatalogResponse,
  LlmMode,
} from "@/lib/chat-api";
import { commandCapabilitiesFromMessageMetadata } from "@/lib/chat-commands";
import { deepResearchApi, type DeepResearchJob } from "@/lib/deep-research-api";
import { useWebSocket } from "@/hooks/use-websocket";
import { explorerBookmarks, explorerSearch } from "@/lib/explorer-api";
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
import { GroupChatDialog } from "@/components/chat/group-chat-dialog";
import { SteeringPanel } from "@/components/chat/steering-panel";
import { useSidebar } from "@/components/ui/sidebar";
import {
  CHAT_SESSION_NAVIGATION_EVENT,
  navigateChatSessionInPlace,
  readChatSessionIdFromLocation,
} from "@/lib/chat-navigation";
import { getDroppedExplorerFiles } from "@/lib/file-drop";
import {
  chatTimelineReducer,
  initialChatTimelineState,
} from "@/lib/chat-state";
import { getWebSocketMessageAgentRunId } from "@/lib/chat-websocket-events";
import {
  AlertCircle,
  FolderOpen,
  RefreshCcw,
  Users,
  Sliders,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const LAST_SESSION_KEY = "aoitalk_last_session_id";
const PROJECT_CONTEXT_KEY = "aoitalk-chat-project-context";
const DEEP_RESEARCH_KEY = "aoitalk-chat-deep-research";
const TEMPORARY_FILE_DRAFT_SESSION_KEY = "__new_chat__";
const DISPATCH_FAILURE_MESSAGE =
  "送信は保存されましたが、応答生成を開始できませんでした。サーバーの生成処理が起動しているか確認してから、もう一度送信してください。";

type PendingMessage = {
  content: string;
  clientMessageId: string;
  projectId?: string;
  files?: File[];
  mentions?: { type: string; id: string; name: string }[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  commandCapabilities?: ChatCommandCapability[];
};

type ToolPermissionRequest = {
  requestId: string;
  toolName: string;
  description: string;
  toolArgs: Record<string, unknown>;
};

type ExternalModelPromptRequest = {
  requestId: string;
  provider: string;
  model: string;
  description: string;
  prompt: string;
  redactedPrompt: string;
  redactionFindings: { category: string; placeholder: string }[];
  notify: boolean;
};

function resolveProjectIdFromMessage(
  content: string,
  projects: Array<{
    id: string;
    name: string;
    slug?: string;
    aliases?: string[];
  }>,
): string | null {
  const normalizedContent = content.toLowerCase();
  const matches = projects.filter((project) => {
    const names = [project.name, project.slug, ...(project.aliases || [])]
      .filter((item): item is string => Boolean(item))
      .map((item) => item.toLowerCase());
    return names.some((name) => name && normalizedContent.includes(name));
  });
  return matches.length === 1 ? matches[0].id : null;
}

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function isChatToolResultMetadata(
  value: unknown,
): value is ChatToolResultMetadata {
  if (!value || typeof value !== "object") return false;
  const result = value as ChatToolResultMetadata;
  return (
    typeof result.output === "string" ||
    (Array.isArray(result.urls) &&
      result.urls.every((url) => typeof url === "string"))
  );
}

function createLocalAttachmentMetadata(
  files?: File[],
): ChatAttachmentMetadata[] | undefined {
  if (!files?.length) return undefined;
  return files.map((file) => ({
    name: file.name,
    size: file.size,
    mime_type: file.type || undefined,
  }));
}

function hasDraggedFiles(
  dataTransfer: DataTransfer | null,
): dataTransfer is DataTransfer {
  return Boolean(
    dataTransfer && Array.from(dataTransfer.types).includes("Files"),
  );
}

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

function createLocalMessage(
  sessionId: string,
  role: "user" | "assistant",
  content: string,
  metadata: Record<string, unknown> = {},
): ConversationMessage {
  return {
    id: `temp-${role}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    session_id: sessionId,
    role,
    content,
    metadata,
    created_at: new Date().toISOString(),
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  };
}

function createLocalUserMessage(
  sessionId: string,
  content: string,
  clientMessageId: string,
  files?: File[],
  commandCapabilities?: ChatCommandCapability[],
): ConversationMessage {
  const metadata: ConversationMessage["metadata"] = {
    client_message_id: clientMessageId,
  };
  if (commandCapabilities?.length) {
    metadata.command_capabilities = commandCapabilities;
  }
  const attachments = createLocalAttachmentMetadata(files);
  if (attachments) {
    metadata.attachments = attachments;
  }
  return createLocalMessage(sessionId, "user", content, metadata);
}

const API_KEY_REQUIRED_PROVIDERS = new Set(["openai", "gemini", "openrouter"]);

function modelLabel(model: LlmCatalogModelOption | undefined, fallback: string) {
  const label = model?.label?.trim();
  return label || fallback;
}

function buildResponseModelOptions(
  catalog: LlmModelCatalogResponse,
): ChatResponseModelOption[] {
  const currentProvider = catalog.current.provider;
  const currentModel = catalog.current.model;
  const providers = new Map(catalog.providers.map((provider) => [provider.id, provider]));
  const result: ChatResponseModelOption[] = [];
  const seen = new Set<string>();

  const addOption = (
    provider: LlmCatalogProvider | undefined,
    modelId: string | undefined,
    model: LlmCatalogModelOption | undefined,
  ) => {
    const normalizedProvider = provider?.id?.trim();
    const normalizedModel = modelId?.trim();
    if (!normalizedProvider || !normalizedModel) return;
    const key = `${normalizedProvider}:${normalizedModel}`;
    if (seen.has(key)) return;

    seen.add(key);
    const providerLabel = provider?.label || normalizedProvider;
    const displayModel = modelLabel(model, normalizedModel);
    const isCurrent =
      normalizedProvider === currentProvider && normalizedModel === currentModel;
    result.push({
      provider: normalizedProvider,
      model: normalizedModel,
      providerLabel,
      modelLabel: displayModel,
      label: isCurrent
        ? `${providerLabel} / ${displayModel} (現在)`
        : `${providerLabel} / ${displayModel}`,
      isCurrent,
    });
  };

  const currentCatalogProvider =
    providers.get(currentProvider) ?? {
      id: currentProvider,
      label: currentProvider,
      models: [],
    };
  const currentCatalogModel = currentCatalogProvider?.models.find(
    (model) => model.id === currentModel,
  );
  addOption(currentCatalogProvider, currentModel, currentCatalogModel);

  for (const provider of catalog.providers) {
    if (
      API_KEY_REQUIRED_PROVIDERS.has(provider.id) &&
      provider.settings?.api_key_configured === false &&
      provider.id !== currentProvider
    ) {
      continue;
    }

    const configuredModel = provider.configured_model?.trim();
    if (configuredModel) {
      addOption(
        provider,
        configuredModel,
        provider.models.find((model) => model.id === configuredModel),
      );
    }

    for (const model of provider.models) {
      addOption(provider, model.id, model);
    }
  }

  return result;
}

function formatDeepResearchProgress(job: DeepResearchJob): string {
  const latestEvent = job.events.at(-1);
  const questions = Object.entries(job.questions_by_iteration)
    .slice(-2)
    .flatMap(([iteration, items]) =>
      items.map((item) => `- ${iteration}: ${item}`),
    )
    .join("\n");
  return [
    "Deep Researchを実行中です。",
    "",
    `進捗: ${job.progress}% (${job.status})`,
    latestEvent ? `現在: ${latestEvent.message}` : null,
    job.sources.length > 0 ? `収集ソース: ${job.sources.length}件` : null,
    questions ? `\n検索クエリ:\n${questions}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function formatDeepResearchFinal(job: DeepResearchJob): string {
  if (job.status === "failed") {
    return `Deep Researchに失敗しました。\n\n${job.error || "原因不明のエラー"}`;
  }
  if (job.status === "cancelled") {
    return "Deep Researchはキャンセルされました。";
  }
  return (
    job.report_markdown ||
    "Deep Researchは完了しましたが、レポート本文が空でした。"
  );
}

function ChatPageInner() {
  const { isMobile, open: sidebarOpen } = useSidebar();
  const searchParams = useSearchParams();
  const router = useRouter();
  const searchParamSessionId = searchParams.get("s") || null;
  const [activeSessionId, setActiveSessionId] = useState(searchParamSessionId);
  const activeSessionIdRef = useRef(activeSessionId);
  const { selectedProjectId, allProjects } = useProject();
  const [includeProjectContext, setIncludeProjectContext] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(PROJECT_CONTEXT_KEY) === "true";
  });
  const [deepResearchEnabled, setDeepResearchEnabled] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(DEEP_RESEARCH_KEY) === "true";
  });
  const [llmMode, setLlmModeState] = useState<LlmMode>("");
  const [llmModeOptions, setLlmModeOptions] = useState<LlmMode[]>([]);
  const [llmModeLabels, setLlmModeLabels] = useState<Record<string, string>>({});
  const {
    addSession,
    updateSessionTitle: updateSidebarTitle,
    bumpSession,
  } = useChatSessions();

  useEffect(() => {
    setActiveSessionId(searchParamSessionId);
  }, [searchParamSessionId]);

  useEffect(() => {
    const syncActiveSessionId = () => {
      setActiveSessionId(readChatSessionIdFromLocation());
    };

    window.addEventListener(
      CHAT_SESSION_NAVIGATION_EVENT,
      syncActiveSessionId,
    );
    window.addEventListener("popstate", syncActiveSessionId);
    return () => {
      window.removeEventListener(
        CHAT_SESSION_NAVIGATION_EVENT,
        syncActiveSessionId,
      );
      window.removeEventListener("popstate", syncActiveSessionId);
    };
  }, []);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  const activateSession = useCallback((sessionId: string) => {
    activeSessionIdRef.current = sessionId;
    setActiveSessionId(sessionId);
  }, []);

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

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const result = await getLlmMode();
        if (!cancelled) {
          setLlmModeState(result.mode);
          setLlmModeOptions(
            result.available_modes?.length ? result.available_modes : [result.mode],
          );
          setLlmModeLabels(result.labels ?? {});
        }
      } catch (err) {
        console.warn("LLMモード取得に失敗:", err);
        if (!cancelled) {
          toast.error("LLMモードの取得に失敗しました");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleLlmModeChange = useCallback(async (nextMode: LlmMode) => {
    try {
      const result = await setLlmMode(nextMode);
      setLlmModeState(result.mode);
      setLlmModeOptions(
        result.available_modes?.length ? result.available_modes : [result.mode],
      );
      setLlmModeLabels(result.labels ?? {});
      toast.success(
        `LLM mode: ${result.labels?.[result.mode] ?? result.mode}`,
      );
    } catch (err) {
      console.error("LLMモード切り替えに失敗:", err);
      toast.error("LLMモードの切り替えに失敗しました");
    }
  }, []);

  // 最後の通常チャットセッションIDを復元（シナリオ系は専用導線からのみ復元）
  useEffect(() => {
    if (activeSessionId) return;

    const lastId = localStorage.getItem(LAST_SESSION_KEY);
    if (!lastId) return;

    let cancelled = false;

    (async () => {
      try {
        const data = await chatApi.resumeSession(lastId);
        if (cancelled) return;

        if (isScenarioWorkflowSession(data.session)) {
          localStorage.removeItem(LAST_SESSION_KEY);
          return;
        }

        router.replace(`/chat?s=${lastId}`);
      } catch {
        if (!cancelled) {
          localStorage.removeItem(LAST_SESSION_KEY);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeSessionId, router]);

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
  const [pendingAgentRunId, setPendingAgentRunId] = useState<string | null>(null);
  const [responseModelOptions, setResponseModelOptions] = useState<
    ChatResponseModelOption[]
  >([]);
  const [responseModelOptionsLoading, setResponseModelOptionsLoading] =
    useState(false);
  const [temporaryFilesBySession, setTemporaryFilesBySession] = useState<
    Record<string, File[]>
  >({});
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
  const [conversationSearchOpen, setConversationSearchOpen] = useState(false);
  const [currentSession, setCurrentSession] =
    useState<ConversationSession | null>(null);

  useEffect(() => {
    const handleTitleUpdated = (event: Event) => {
      const detail = (
        event as CustomEvent<{ sessionId?: unknown; title?: unknown }>
      ).detail;
      const sessionId = typeof detail?.sessionId === "string" ? detail.sessionId : "";
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

  const maybeGenerateLoadedSessionTitle = useCallback(
    async (
      session: ConversationSession,
      messages: ConversationMessage[],
    ) => {
      if (
        isScenarioWorkflowSession(session) ||
        !shouldRequestSessionTitleGeneration(session, messages)
      ) {
        return;
      }

      try {
        const titleResult = await chatApi.generateSessionTitle(session.id);
        if (!titleResult.title || activeSessionIdRef.current !== session.id) {
          return;
        }
        updateSidebarTitle(session.id, titleResult.title);
        setCurrentSession((prev) =>
          prev && prev.id === session.id
            ? { ...prev, title: titleResult.title }
            : prev,
        );
      } catch (err) {
        console.warn("セッションタイトル生成に失敗:", err);
      }
    },
    [updateSidebarTitle],
  );
  const isGroupChat = currentSession?.is_group_chat ?? false;

  useEffect(() => {
    let cancelled = false;
    setResponseModelOptionsLoading(true);

    getLlmModelCatalog()
      .then((catalog) => {
        if (!cancelled) {
          setResponseModelOptions(buildResponseModelOptions(catalog));
        }
      })
      .catch((err) => {
        console.warn("再生成モデル一覧の取得に失敗:", err);
        if (!cancelled) setResponseModelOptions([]);
      })
      .finally(() => {
        if (!cancelled) setResponseModelOptionsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // ステアリングパネル
  const [steeringVisible, setSteeringVisible] = useState(false);
  // RPセッションかどうか（character_typeが"assistant"でない場合にtrueとする近似判定）
  const isRpSession =
    currentSession != null && currentSession.character_name !== "aoi";
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
  const attachedFiles = temporaryFilesBySession[temporaryFileSessionKey] ?? [];
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
  const pendingSearchMessageIdRef = useRef<string | null>(null);

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

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "f"
      ) {
        event.preventDefault();
        setConversationSearchOpen(true);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    const messageId = pendingSearchMessageIdRef.current;
    if (!messageId || isLoadingMessages) return;
    if (!messages.some((message) => message.id === messageId)) return;

    const timer = window.setTimeout(() => {
      const element = document.querySelector<HTMLElement>(
        `[data-chat-message-id="${messageId}"]`,
      );
      element?.scrollIntoView({ behavior: "smooth", block: "center" });
      pendingSearchMessageIdRef.current = null;
    }, 100);

    return () => window.clearTimeout(timer);
  }, [isLoadingMessages, messages]);

  const handleSelectSearchResult = useCallback(
    (result: ConversationSearchResult) => {
      setConversationSearchOpen(false);
      pendingSearchMessageIdRef.current = result.message_id ?? null;
      activateSession(result.session_id);
      const href = `/chat?s=${encodeURIComponent(result.session_id)}`;
      if (!navigateChatSessionInPlace(href)) {
        router.push(href);
      }
    },
    [activateSession, router],
  );

  const refreshPersistedMessages = useCallback(async (sessionId: string) => {
    try {
      const data = await chatApi.getMessages(sessionId);
      const currentSessionId = activeSessionIdRef.current;
      if (currentSessionId && currentSessionId !== sessionId) return null;
      dispatchChatTimeline({
        type: "hydrate_persisted",
        sessionId,
        messages: data.messages,
      });
      return data.messages;
    } catch (err) {
      console.warn("保存済みメッセージの再取得に失敗:", err);
      return null;
    }
  }, []);

  const waitForPersistedAssistantResponse = useCallback(
    async (
      sessionId: string,
      knownAssistantIds: Set<string>,
      titleSession?: ConversationSession | null,
    ) => {
      const generation = ++responsePollGenerationRef.current;
      const timeoutAt = Date.now() + 300_000;

      const hasNewAssistantMessage = (
        persistedMessages: ConversationMessage[],
      ) =>
        persistedMessages.some(
          (message) =>
            message.role === "assistant" && !knownAssistantIds.has(message.id),
        );

      while (Date.now() < timeoutAt) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (responsePollGenerationRef.current !== generation) return;
        const currentSessionId = activeSessionIdRef.current;
        if (currentSessionId && currentSessionId !== sessionId) return;

        const persistedMessages = await refreshPersistedMessages(sessionId);
        if (!persistedMessages) continue;

        if (hasNewAssistantMessage(persistedMessages)) {
          clearWaitingResponse(sessionId);
          setRestoredGenerationStatus(null);
          if (titleSession) {
            void maybeGenerateLoadedSessionTitle(
              titleSession,
              persistedMessages,
            );
          }
          return;
        }
      }

      const finalMessages = await refreshPersistedMessages(sessionId);
      if (finalMessages && hasNewAssistantMessage(finalMessages)) {
        if (titleSession) {
          void maybeGenerateLoadedSessionTitle(titleSession, finalMessages);
        }
      }
      clearWaitingResponse(sessionId);
      setRestoredGenerationStatus(null);
    },
    [
      clearWaitingResponse,
      maybeGenerateLoadedSessionTitle,
      refreshPersistedMessages,
    ],
  );

  const refreshGenerationStatus = useCallback(
    async (
      sessionId: string,
      knownMessages: ConversationMessage[],
      titleSession?: ConversationSession | null,
    ) => {
      try {
        const status = await chatApi.getGenerationStatus(sessionId);
        if (
          activeSessionIdRef.current &&
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }
        if (status.running) {
          setRestoredGenerationStatus(status);
          markWaitingResponse(sessionId);
          setPendingAgentRunId(status.agent_run_id ?? null);
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
        } else {
          setRestoredGenerationStatus(null);
          if (status.status !== "idle") {
            clearWaitingResponse(sessionId);
            setPendingAgentRunId(null);
          }
        }
      } catch (err) {
        console.warn("生成状態の復元に失敗しました", err);
      }
    },
    [
      clearWaitingResponse,
      markWaitingResponse,
      waitForPersistedAssistantResponse,
    ],
  );

  const setAttachedFiles = useCallback(
    (next: File[] | ((prev: File[]) => File[])) => {
      setTemporaryFilesBySession((prev) => {
        const current = prev[temporaryFileSessionKey] ?? [];
        const resolved = typeof next === "function" ? next(current) : next;
        const updated = { ...prev };

        if (resolved.length === 0) {
          delete updated[temporaryFileSessionKey];
        } else {
          updated[temporaryFileSessionKey] = resolved;
        }

        return updated;
      });
    },
    [temporaryFileSessionKey],
  );

  const appendDroppedFiles = useCallback(
    async (dataTransfer: DataTransfer) => {
      const droppedItems = await getDroppedExplorerFiles(dataTransfer);
      const files = droppedItems.map((item) => item.file);
      if (files.length === 0) return;

      setAttachedFiles((prev) => [...prev, ...files]);
    },
    [setAttachedFiles],
  );

  const handleChatFileDragOver = useCallback(
    (event: ReactDragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = chatBusy ? "none" : "copy";
    },
    [chatBusy],
  );

  const handleChatFileDrop = useCallback(
    async (event: ReactDragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      if (chatBusy) return;

      await appendDroppedFiles(event.dataTransfer);
    },
    [appendDroppedFiles, chatBusy],
  );

  useEffect(() => {
    const handleWindowFileDragOver = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = chatBusy ? "none" : "copy";
    };

    const handleWindowFileDrop = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      if (chatBusy) return;

      void appendDroppedFiles(event.dataTransfer);
    };

    window.addEventListener("dragover", handleWindowFileDragOver);
    window.addEventListener("drop", handleWindowFileDrop);
    return () => {
      window.removeEventListener("dragover", handleWindowFileDragOver);
      window.removeEventListener("drop", handleWindowFileDrop);
    };
  }, [appendDroppedFiles, chatBusy]);

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
    ) => {
      const result = await chatApi.dispatchMessage(sessionId, {
        message: content,
        project_id: projectId,
        generation_profile: generationProfile,
        include_project_context: includeProjectContext,
        response_model: responseModel,
        client_message_id: clientMessageId,
        command_capabilities: commandCapabilities,
        skip_user_persistence: persistence?.skipUserPersistence,
        persisted_user_message_id: persistence?.persistedUserMessageId,
      });
      if (clientMessageId && result.user_message_id) {
        dispatchChatTimeline({
          type: "promote_client_message",
          sessionId,
          clientMessageId,
          serverMessageId: result.user_message_id,
        });
      }
      if (result.agent_run_id) {
        setPendingAgentRunId(result.agent_run_id);
      }
      bumpSession(sessionId);
      return result;
    },
    [bumpSession, includeProjectContext],
  );

  const createDispatchFailureMessage = useCallback(
    (sessionId: string) =>
      createLocalMessage(sessionId, "assistant", DISPATCH_FAILURE_MESSAGE),
    [],
  );

  const handleToolPermissionDecision = useCallback(
    (approved: boolean) => {
      if (!toolPermissionRequest) return;
      sendPermissionResponse(toolPermissionRequest.requestId, approved);
      setToolPermissionRequest(null);
    },
    [sendPermissionResponse, toolPermissionRequest],
  );

  const handleExternalModelPromptDecision = useCallback(
    (approved: boolean) => {
      if (!externalModelPromptRequest) return;
      sendExternalModelPromptResponse(
        externalModelPromptRequest.requestId,
        approved,
        approved ? externalModelPromptDraft : "",
      );
      setExternalModelPromptRequest(null);
      setExternalModelPromptDraft("");
    },
    [externalModelPromptDraft, externalModelPromptRequest, sendExternalModelPromptResponse],
  );

  const handleExternalModelPromptKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      if (
        event.key !== "Enter" ||
        event.shiftKey ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        event.nativeEvent.isComposing
      ) {
        return;
      }
      if (!externalModelPromptDraft.trim()) return;
      event.preventDefault();
      handleExternalModelPromptDecision(true);
    },
    [handleExternalModelPromptDecision, externalModelPromptDraft],
  );

  const handleStopGeneration = useCallback(async () => {
    if (!activeSessionId) return;
    responsePollGenerationRef.current += 1;
    clearWaitingResponse(activeSessionId);
    setRestoredGenerationStatus(null);
    setSteeringInstructions([]);
    if (streamingIntervalRef.current) {
      clearInterval(streamingIntervalRef.current);
      streamingIntervalRef.current = null;
    }
    setStreamingContent("");
    const sent = stopGeneration(activeSessionId);
    if (!sent) {
      try {
        await chatApi.stopGeneration(activeSessionId);
      } catch (err) {
        console.error("応答停止に失敗:", err);
        toast.error("応答停止に失敗しました");
      }
    }
  }, [activeSessionId, clearWaitingResponse, stopGeneration]);

  const handleSteerGeneration = useCallback(
    async (content: string) => {
      if (!activeSessionId) return;
      const instructionId = createClientMessageId();
      setSteeringInstructions((prev) => [
        ...prev,
        {
          id: instructionId,
          content,
          createdAt: new Date().toISOString(),
          status: "sending",
        },
      ]);

      const updateStatus = (status: SubmittedSteeringInstruction["status"]) => {
        setSteeringInstructions((prev) =>
          prev.map((item) =>
            item.id === instructionId ? { ...item, status } : item,
          ),
        );
      };

      const sent = sendSteering(content, activeSessionId);
      if (!sent) {
        try {
          await chatApi.steerGeneration(activeSessionId, content);
        } catch (err) {
          updateStatus("failed");
          console.error("追加指示の送信に失敗:", err);
          toast.error("追加指示の送信に失敗しました");
          return;
        }
      }
      updateStatus("queued");
      toast.success("追加指示を送信しました");
    },
    [activeSessionId, sendSteering],
  );

  // ─── セッション選択時にメッセージを取得 ───
  useEffect(() => {
    if (!activeSessionId) {
      dispatchChatTimeline({ type: "clear" });
      resetDisplayedGenerationState();
      setIsLoadingMessages(false);
      setSessionLoadError(null);
      setScenarioSession(null);
      setWritingSession(null);
      setRoleplaySession(null);
      setCurrentSession(null);
      setSteeringInstructions([]);
      return;
    }

    let cancelled = false;
    resetDisplayedGenerationState();
    setIsLoadingMessages(true);
    setSessionLoadError(null);
    // 現在のセッションのtempメッセージのみ保持（別セッションのものはクリア）
    dispatchChatTimeline({
      type: "keep_transient_for_session",
      sessionId: activeSessionId,
    });
    setSteeringInstructions([]);
    setScenarioSession(null);
    setWritingSession(null);
    setRoleplaySession(null);

    (async () => {
      const loadScenarioContext = async (session: ConversationSession) => {
        const characterName = session.character_name || "";
        const roleplayMatch = characterName.match(
          /^scenario_roleplay:([^:]+):([^:]+)$/,
        );

        if (roleplayMatch) {
          const [, scenarioId, characterId] = roleplayMatch;
          try {
            const scenario = await chatApi.getScenario(scenarioId);
            const character = scenario.characters?.find(
              (item) => item.id === characterId,
            );
            if (!cancelled && character) {
              setRoleplaySession({
                scenario: { id: scenario.id, title: scenario.title },
                character,
              });
            }
          } catch (err) {
            console.warn("シナリオロールプレイ情報取得失敗:", err);
          }
          return;
        }

        if (!isScenarioWorkflowSession(session)) {
          return;
        }

        const [playResult, writingResult] = await Promise.allSettled([
          chatApi.getScenarioPlaySessionByConversation(activeSessionId),
          chatApi.getWritingSessionByConversation(activeSessionId),
        ]);

        if (cancelled) return;

        setScenarioSession(
          playResult.status === "fulfilled" ? playResult.value : null,
        );
        setWritingSession(
          writingResult.status === "fulfilled" ? writingResult.value : null,
        );
      };

      try {
        const data = await chatApi.resumeSession(activeSessionId);
        if (!cancelled) {
          setCurrentSession(data.session);
          if (isScenarioWorkflowSession(data.session)) {
            if (localStorage.getItem(LAST_SESSION_KEY) === activeSessionId) {
              localStorage.removeItem(LAST_SESSION_KEY);
            }
          } else {
            localStorage.setItem(LAST_SESSION_KEY, activeSessionId);
          }
          // API結果で置き換え。temp-メッセージはAPIに未反映のもののみ残す
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: activeSessionId,
            messages: data.messages,
          });
          void refreshGenerationStatus(
            activeSessionId,
            data.messages,
            data.session,
          );
          void maybeGenerateLoadedSessionTitle(data.session, data.messages);
          await loadScenarioContext(data.session);
        }
      } catch (err) {
        console.error("セッション再開失敗:", err);
        if (!cancelled) {
          if (localStorage.getItem(LAST_SESSION_KEY) === activeSessionId) {
            localStorage.removeItem(LAST_SESSION_KEY);
          }
          dispatchChatTimeline({ type: "clear" });
          setCurrentSession(null);
          setScenarioSession(null);
          setWritingSession(null);
          setRoleplaySession(null);
          setSessionLoadError(
            "会話セッションを表示できませんでした。履歴データは残っている可能性があるため、再読み込みしてください。",
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoadingMessages(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    activeSessionId,
    maybeGenerateLoadedSessionTitle,
    refreshGenerationStatus,
    resetDisplayedGenerationState,
    router,
    sessionLoadAttempt,
  ]);

  useEffect(() => {
    if (!activeSessionId || !isConnected) return;
    void refreshGenerationStatus(
      activeSessionId,
      messagesRef.current,
      currentSession,
    );
  }, [activeSessionId, currentSession, isConnected, refreshGenerationStatus]);

  // ─── WebSocket接続時に保留メッセージを送信 ───
  useEffect(() => {
    if (isConnected && pendingMessageRef.current) {
      const pending = pendingMessageRef.current;
      pendingMessageRef.current = null;
      sendMessage(
        pending.content,
        pending.projectId ?? effectiveProjectId,
        pending.files,
        pending.mentions,
        pending.generationProfile,
        pending.includeProjectContext,
        undefined,
        undefined,
        activeSessionId,
        pending.clientMessageId,
        pending.commandCapabilities,
      );
      markWaitingResponse(activeSessionId);
    }
  }, [
    activeSessionId,
    effectiveProjectId,
    isConnected,
    markWaitingResponse,
    sendMessage,
  ]);

  const handleDeepResearchMessage = useCallback(
    async (content: string, projectId?: string) => {
      let sessionId = activeSessionId;
      const isNewSession = !sessionId;

      try {
        if (!sessionId) {
          const data = await chatApi.createSession("aoi", projectId);
          sessionId = data.session.id;
          addSession(data.session);
          setCurrentSession(data.session);
          activateSession(sessionId);
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }
        }

        const userMessage = await chatApi.addMessage(sessionId, {
          role: "user",
          content,
        });

        if (isNewSession) {
          dispatchChatTimeline({ type: "replace", messages: [userMessage.message] });
        } else {
          dispatchChatTimeline({ type: "append", message: userMessage.message });
        }

        const assistantTemp = createLocalMessage(
          sessionId,
          "assistant",
          "Deep Researchを開始しています。",
          { deep_research: true, status: "queued" },
        );
        dispatchChatTimeline({ type: "append", message: assistantTemp });
        markWaitingResponse(sessionId);
        bumpSession(sessionId);

        const started = await deepResearchApi.startJob({
          query: content,
          mode: "detailed",
          max_iterations: 3,
          questions_per_iteration: 3,
          max_results_per_query: 5,
          engines: ["searxng", "wikipedia", "arxiv", "openalex", "pubmed"],
          include_local_knowledge: includeProjectContext,
          project_id: projectId ?? null,
        });

        let current = started;
        const updateAssistantTemp = (job: DeepResearchJob) => {
          const content =
            job.status === "completed" || job.status === "failed"
              ? formatDeepResearchFinal(job)
              : formatDeepResearchProgress(job);
          const replacement = {
            ...assistantTemp,
            content,
            metadata: {
              ...assistantTemp.metadata,
              deep_research: true,
              job_id: job.id,
              status: job.status,
              progress: job.progress,
            },
          };
          dispatchChatTimeline({
            type: "replace_by_id",
            messageId: assistantTemp.id,
            message: replacement,
            appendIfMissing: true,
          });
        };

        updateAssistantTemp(current);

        while (!["completed", "failed", "cancelled"].includes(current.status)) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          current = await deepResearchApi.getJob(current.id);
          updateAssistantTemp(current);
        }

        const finalContent = formatDeepResearchFinal(current);
        const savedAssistant = await chatApi.addMessage(sessionId, {
          role: "assistant",
          content: finalContent,
        });
        dispatchChatTimeline({
          type: "replace_by_id",
          messageId: assistantTemp.id,
          message: {
            ...savedAssistant.message,
            metadata: {
              ...savedAssistant.message.metadata,
              deep_research: true,
              job_id: current.id,
              status: current.status,
              progress: current.progress,
            },
          },
          appendIfMissing: true,
        });
        try {
          const titleResult = await chatApi.generateSessionTitle(sessionId);
          if (titleResult.title) {
            updateSidebarTitle(sessionId, titleResult.title);
            setCurrentSession((prev) =>
              prev && prev.id === sessionId
                ? { ...prev, title: titleResult.title }
                : prev,
            );
          }
        } catch (err) {
          console.warn("セッションタイトル生成に失敗:", err);
        }
        bumpSession(sessionId);
      } catch (err) {
        console.error("Deep Research送信失敗:", err);
        if (sessionId) {
          const failedSessionId = sessionId;
          dispatchChatTimeline({
            type: "append",
            message: createLocalMessage(
              failedSessionId,
              "assistant",
              `Deep Researchに失敗しました。\n\n${
                err instanceof Error ? err.message : String(err)
              }`,
              { deep_research: true, status: "failed" },
            ),
          });
        }
      } finally {
        clearWaitingResponse(sessionId);
        setIsSending(false);
      }
    },
    [
      activeSessionId,
      addSession,
      activateSession,
      bumpSession,
      clearWaitingResponse,
      includeProjectContext,
      markWaitingResponse,
      router,
      updateSidebarTitle,
    ],
  );

  // ─── グループチャット作成 ───
  const handleCreateGroupChat = useCallback(
    async (
      characterNames: string[],
      projectId?: string,
      userIds?: string[],
      agentIds?: string[],
    ) => {
      try {
        const data = await chatApi.createGroupSession(
          characterNames,
          projectId,
          userIds,
          agentIds,
        );
        const session = data.session;
        addSession(session);
        setCurrentSession(session);

        // first_messages があれば初期メッセージとして追加
        if (data.first_messages && data.first_messages.length > 0) {
          const initialMsgs: ConversationMessage[] = data.first_messages.map(
            (fm, idx) => ({
              id: `group-init-${Date.now()}-${idx}`,
              session_id: session.id,
              role: "assistant" as const,
              content: fm.content,
              metadata: {
                character_name: fm.character_name,
                character_slug: fm.character_slug,
              },
              created_at: new Date().toISOString(),
              parent_message_id: null,
              branch_index: 0,
              is_active_branch: true,
            }),
          );
          dispatchChatTimeline({ type: "replace", messages: initialMsgs });
        } else {
          dispatchChatTimeline({ type: "clear" });
        }

        activateSession(session.id);
        const href = `/chat?s=${encodeURIComponent(session.id)}`;
        if (!navigateChatSessionInPlace(href)) {
          router.push(href);
        }
      } catch (err) {
        console.error("グループチャット作成失敗:", err);
      }
    },
    [activateSession, addSession, router],
  );

  // ─── メッセージ送信 ───
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: File[],
      mentions?: { type: string; id: string; name: string }[],
      generationProfile?: string,
      commandCapabilities?: ChatCommandCapability[],
    ) => {
      // 連打防止
      if (isSending) return;
      setIsSending(true);
      setRestoredGenerationStatus(null);
      setPendingAgentRunId(null);
      const clientMessageId = createClientMessageId();
      const messageProjectId =
        isScenarioChatSession
          ? undefined
          : (resolveProjectIdFromMessage(content, allProjects) ??
            effectiveProjectId);
      const hasCommandCapabilities = Boolean(commandCapabilities?.length);

      if (deepResearchEnabled && !hasCommandCapabilities) {
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
          setIsSending(false);
          return;
        }
        await handleDeepResearchMessage(content, messageProjectId);
        return;
      }

      // セッション未選択時は自動作成（選択中のプロジェクトを紐付け）
      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const canPersistInitialMessage =
            !files?.length && !mentions?.length && !hasCommandCapabilities;
          const data = await chatApi.createSession(
            "aoi",
            messageProjectId,
            canPersistInitialMessage
              ? { content, client_message_id: clientMessageId }
              : undefined,
          );
          sessionId = data.session.id;
          // サイドバーにセッションを追加
          addSession(data.session);
          setCurrentSession(data.session);
          activateSession(sessionId);
          // 新規セッションなのでメッセージをクリアしてからユーザーメッセージを追加
          const userMsg =
            data.initial_message ??
            createLocalUserMessage(
              sessionId,
              content,
              clientMessageId,
              files,
              commandCapabilities,
            );
          dispatchChatTimeline({ type: "replace", messages: [userMsg] });
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }

          const knownAssistantIds = new Set<string>();
          if (files?.length || mentions?.length) {
            pendingMessageRef.current = {
              content,
              clientMessageId,
              projectId: messageProjectId,
              files,
              mentions,
              generationProfile,
              includeProjectContext,
              commandCapabilities,
            };
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
              );
              markWaitingResponse(sessionId);
            } catch (err) {
              console.error("新規セッションのREST送信失敗:", err);
              const failureMessage = createDispatchFailureMessage(sessionId);
              dispatchChatTimeline({ type: "append", message: failureMessage });
              clearWaitingResponse(sessionId);
              setTimeout(() => setIsSending(false), 500);
              return;
            }
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              data.session,
            );
          }

          setTimeout(() => setIsSending(false), 500);
          return;
        } catch (err) {
          console.error("セッション自動作成失敗:", err);
          clearWaitingResponse(sessionId ?? null);
          setIsSending(false);
          return;
        }
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
        const userMsg = createLocalUserMessage(
          sessionId,
          content,
          clientMessageId,
          files,
          commandCapabilities,
        );
        dispatchChatTimeline({ type: "append", message: userMsg });
        if (files?.length || mentions?.length) {
          pendingMessageRef.current = {
            content,
            clientMessageId,
            projectId: messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext,
            commandCapabilities,
          };
          bumpSession(sessionId);
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
            );
            markWaitingResponse(sessionId);
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              currentSession,
            );
          } catch (err) {
            console.error("WebSocket未接続時のREST送信失敗:", err);
            const failureMessage = createDispatchFailureMessage(sessionId);
            dispatchChatTimeline({ type: "append", message: failureMessage });
            clearWaitingResponse(sessionId);
          }
        }
        setTimeout(() => setIsSending(false), 500);
        return;
      }

      // ユーザーメッセージをローカルに即座追加
      const userMsg = createLocalUserMessage(
        sessionId,
        content,
        clientMessageId,
        files,
        commandCapabilities,
      );
      dispatchChatTimeline({ type: "append", message: userMsg });

      // グループチャットは共有セッション用WebSocketで送信する。
      if (isGroupChat && sessionId) {
        bumpSession(sessionId);
        sendMessage(
          content,
          messageProjectId,
          files,
          mentions,
          generationProfile,
          includeProjectContext,
          undefined,
          undefined,
          sessionId,
          clientMessageId,
          commandCapabilities,
        );
        setIsSending(false);
        clearWaitingResponse(sessionId);
        return;
      }

      // 通常テキスト送信はRESTで先にDB保存してから生成をキューする。
      // ファイル/メンションはWebSocket側で添付ペイロードを組み立てる。
      if (files?.length || mentions?.length) {
        sendMessage(
          content,
          messageProjectId,
          files,
          mentions,
          generationProfile,
          includeProjectContext,
          undefined,
          undefined,
          sessionId,
          clientMessageId,
          commandCapabilities,
        );
        bumpSession(sessionId);
        markWaitingResponse(sessionId);
        void waitForPersistedAssistantResponse(
          sessionId,
          knownAssistantIds,
          currentSession,
        );
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
          );
          markWaitingResponse(sessionId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
        } catch (err) {
          console.error("メッセージ送信失敗:", err);
          const failureMessage = createDispatchFailureMessage(sessionId);
          dispatchChatTimeline({ type: "append", message: failureMessage });
          clearWaitingResponse(sessionId);
        }
      }
      setTimeout(() => setIsSending(false), 500);
    },
    [
      activeSessionId,
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
      clearWaitingResponse,
      isSending,
      deepResearchEnabled,
      handleDeepResearchMessage,
      isGroupChat,
      dispatchMessageWithoutWebSocket,
      createDispatchFailureMessage,
      currentSession,
      isScenarioChatSession,
      markWaitingResponse,
      waitForPersistedAssistantResponse,
    ],
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
      const commandCapabilities =
        commandCapabilitiesFromMessageMetadata(sourceMessage.metadata);
      setIsSending(true);
      setPendingAgentRunId(null);
      try {
        const result = await chatApi.dispatchMessage(activeSessionId, {
          message: content,
          project_id: effectiveProjectId,
          generation_profile: "autonomous_work",
          include_project_context: includeProjectContext,
          edit_message_id: sourceMessage.id,
          response_model: responseModel,
          command_capabilities:
            commandCapabilities.length > 0 ? commandCapabilities : undefined,
        });
        if (result.agent_run_id) {
          setPendingAgentRunId(result.agent_run_id);
        }
        markWaitingResponse(activeSessionId);
        bumpSession(activeSessionId);
        void waitForPersistedAssistantResponse(
          activeSessionId,
          knownAssistantIds,
          currentSession,
        );
      } catch (err) {
        console.error("分岐メッセージ送信失敗:", err);
        clearWaitingResponse(activeSessionId);
      } finally {
        setTimeout(() => setIsSending(false), 500);
      }
    },
    [
      activeSessionId,
      bumpSession,
      clearWaitingResponse,
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
    (message: ConversationMessage, responseModel?: ChatResponseModelSelection) => {
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

  // ─── WebSocketメッセージ受信処理 ───
  useEffect(() => {
    if (!lastMessage) return;

    // 同じメッセージの再処理を防止（依存配列の他の値が変わった場合にも対応）
    const msgKey = JSON.stringify(lastMessage);
    if (processedMsgRef.current === msgKey) return;
    processedMsgRef.current = msgKey;

    const isForeignSessionEvent = (sessionId: unknown) =>
      typeof sessionId === "string" &&
      sessionId.length > 0 &&
      Boolean(activeSessionId) &&
      sessionId !== activeSessionId;

    if (lastMessage.type === "llm_mode_change") {
      const data = lastMessage.data as
        | {
            mode?: unknown;
            available_modes?: unknown;
            labels?: unknown;
          }
        | undefined;
      if (typeof data?.mode === "string" && data.mode.length > 0) {
        setLlmModeState(data.mode);
        if (
          Array.isArray(data.available_modes) &&
          data.available_modes.every((item) => typeof item === "string")
        ) {
          setLlmModeOptions(data.available_modes);
        }
        if (data.labels && typeof data.labels === "object") {
          setLlmModeLabels(data.labels as Record<string, string>);
        }
      }
      return;
    }

    if (lastMessage.type === "bgm_change") {
      const data = lastMessage.data as
        | { bgm_id: string; volume: number }
        | undefined;
      const bgm_id = data?.bgm_id;
      const volume = data?.volume;

      if (bgm_id === "stop") {
        stopAudio();
      } else if (bgm_id) {
        // BGMの解決（ブックマークから検索）
        (async () => {
          try {
            const bookmarkData = await explorerBookmarks();
            const bgmBookmark = bookmarkData.success
              ? bookmarkData.bookmarks.find(
                  (b) =>
                    b.name === "BGM" || b.path.toLowerCase().includes("bgm"),
                )
              : undefined;
            const searchRoot = bgmBookmark ? bgmBookmark.path : "";

            const searchRes = await explorerSearch(bgm_id, searchRoot, 1);
            if (searchRes.success && searchRes.results.length > 0) {
              const file = searchRes.results[0];
              play({
                name: file.name,
                path: file.path,
                type: "audio",
              });
              if (volume !== undefined) setVolume(volume);
            } else {
              // 1つも見つからない場合はファイル名完全一致を試みる
              // 検索でヒットしない場合もあるため
              console.warn(`BGM not found in search: ${bgm_id}`);
            }
          } catch (e) {
            console.error("BGM resolution failed:", e);
          }
        })();
      }
      return;
    }

    if (lastMessage.type === "external_llm_permission_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data) {
        setToolPermissionRequest({
          requestId: String(data.request_id || ""),
          toolName: String(data.tool_name || "tool"),
          description: String(data.description || "ツール実行を許可しますか？"),
          toolArgs:
            data.tool_args && typeof data.tool_args === "object"
              ? (data.tool_args as Record<string, unknown>)
              : {},
        });
      }
      return;
    }

    if (lastMessage.type === "external_model_prompt_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data) {
        const prompt = String(data.original_prompt || data.prompt || "");
        const redactedPrompt = String(data.redacted_prompt || prompt);
        const redactionFindings = Array.isArray(data.redaction_findings)
          ? data.redaction_findings
              .map((item) =>
                item && typeof item === "object"
                  ? {
                      category: String(
                        (item as Record<string, unknown>).category || "",
                      ),
                      placeholder: String(
                        (item as Record<string, unknown>).placeholder || "",
                      ),
                    }
                  : null,
              )
              .filter(
                (item): item is { category: string; placeholder: string } =>
                  Boolean(item?.category && item.placeholder),
              )
          : [];
        const request = {
          requestId: String(data.request_id || ""),
          provider: String(data.provider || ""),
          model: String(data.model || ""),
          description: String(
            data.description || "分担先モデルへ送るプロンプトを確認してください",
          ),
          prompt,
          redactedPrompt,
          redactionFindings,
          notify: data.notify !== false,
        };
        setExternalModelPromptRequest(request);
        setExternalModelPromptDraft(redactedPrompt);
        if (request.notify) {
          toast.info("外部モデル送信の確認が必要です");
        }
      }
      return;
    }

    if (lastMessage.type === "new_message") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (isForeignSessionEvent(data?.session_id)) return;
      if (data && data.type === "assistant") {
        clearWaitingResponse(activeSessionId);
        setRestoredGenerationStatus(null);
        setSteeringInstructions([]);
        const content = (data.message as string) || "";
        if (content && activeSessionId) {
          const agentRunId =
            getWebSocketMessageAgentRunId(lastMessage) ?? pendingAgentRunId;
          const metadata: ConversationMessage["metadata"] = {
            character: data.character,
            ...(typeof data.session_id === "string" && data.session_id
              ? {}
              : { transient_source: "unscoped_ws_new_message" }),
          };
          if (agentRunId) {
            metadata.agent_run_id = agentRunId;
          }
          dispatchChatTimeline({
            type: "append",
            message: createLocalMessage(
              activeSessionId,
              "assistant",
              content,
              metadata,
            ),
          });
          void refreshPersistedMessages(activeSessionId);
        }
      }
      // user型のnew_messageは保存済みIDを含まないため、応答側で履歴を再取得してtemp表示を置き換える
      return;
    }

    if (lastMessage.type === "conversation_persisted") {
      const sessionId = (lastMessage.session_id as string) || "";
      if (activeSessionId && sessionId === activeSessionId) {
        if (lastMessage.role === "assistant") {
          responsePollGenerationRef.current += 1;
          clearWaitingResponse(sessionId);
          setRestoredGenerationStatus(null);
          setSteeringInstructions([]);
        }
        void (async () => {
          const persistedMessages = await refreshPersistedMessages(
            activeSessionId,
          );
          if (
            lastMessage.role === "assistant" &&
            currentSession?.id === activeSessionId &&
            persistedMessages
          ) {
            void maybeGenerateLoadedSessionTitle(
              currentSession,
              persistedMessages,
            );
          }
        })();
      }
      return;
    }

    if (lastMessage.type === "conversation_title_updated") {
      const sessionId = (lastMessage.session_id as string) || "";
      const title = (lastMessage.title as string) || "";
      if (sessionId && title) {
        updateSidebarTitle(sessionId, title);
        setCurrentSession((prev) =>
          prev && prev.id === sessionId ? { ...prev, title } : prev,
        );
      }
      return;
    }

    if (lastMessage.type === "generated_image") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      const content = (lastMessage.content as string) || "";
      if (content && activeSessionId) {
        dispatchChatTimeline({
          type: "append_to_last_assistant",
          sessionId: activeSessionId,
          content,
        });
      }
      return;
    }

    if (
      lastMessage.type === "tool_start" ||
      lastMessage.type === "tool_end" ||
      lastMessage.type === "status_update" ||
      lastMessage.type === "reasoning_progress" ||
      lastMessage.type === "steering_update"
    ) {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      setRestoredGenerationStatus(null);
      if (
        lastMessage.type === "tool_end" &&
        isChatToolResultMetadata(lastMessage.tool_result)
      ) {
        const nextResults = [
          ...liveToolResultsRef.current,
          lastMessage.tool_result,
        ];
        liveToolResultsRef.current = nextResults;
        setLiveToolResults(nextResults);
      }
    }

    if (lastMessage.type === "stream_start") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      setRestoredGenerationStatus(null);
      clearWaitingResponse(activeSessionId);
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      // ストリーミングバッファを定期的に反映
      streamingIntervalRef.current = setInterval(() => {
        setStreamingContent(streamBuffer.current);
      }, 50);
    }

    if (lastMessage.type === "stream_cancelled") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      responsePollGenerationRef.current += 1;
      clearWaitingResponse(activeSessionId);
      setRestoredGenerationStatus(null);
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
        streamingIntervalRef.current = null;
      }
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      setSteeringInstructions([]);
      toast.info(
        typeof lastMessage.message === "string"
          ? lastMessage.message
          : "応答生成を停止しました",
      );
      if (activeSessionId) {
        void refreshPersistedMessages(activeSessionId);
      }
      return;
    }

    if (lastMessage.type === "stream_end" || lastMessage.type === "response") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      responsePollGenerationRef.current += 1;
      clearWaitingResponse(activeSessionId);
      setRestoredGenerationStatus(null);
      setSteeringInstructions([]);
      // インターバル停止
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
        streamingIntervalRef.current = null;
      }

      const finalContent =
        (lastMessage.content as string) || streamBuffer.current;
      if (finalContent && activeSessionId) {
        const toolResults = liveToolResultsRef.current;
        const streamEndAgentRunId =
          getWebSocketMessageAgentRunId(lastMessage) ?? pendingAgentRunId;
        const assistantMetadata: ConversationMessage["metadata"] = {};
        if (toolResults.length > 0) {
          assistantMetadata.tool_results = toolResults;
        }
        if (streamEndAgentRunId) {
          assistantMetadata.agent_run_id = streamEndAgentRunId;
        }
        dispatchChatTimeline({
          type: "append",
          message: createLocalMessage(
            activeSessionId,
            "assistant",
            finalContent,
            assistantMetadata,
          ),
        });
      }

      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      if (activeSessionId) {
        void refreshPersistedMessages(activeSessionId);
      }
    }
  }, [
    lastMessage,
    activeSessionId,
    refreshPersistedMessages,
    streamBuffer,
    play,
    stopAudio,
    setVolume,
    updateSidebarTitle,
    currentSession,
    maybeGenerateLoadedSessionTitle,
    clearWaitingResponse,
    pendingAgentRunId,
  ]);

  // クリーンアップ
  useEffect(() => {
    return () => {
      responsePollGenerationRef.current += 1;
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
      }
    };
  }, []);

  return (
    <div
      className="relative flex h-full overflow-hidden"
      style={chatViewportStyle}
      onDragOver={handleChatFileDragOver}
      onDrop={handleChatFileDrop}
    >
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* ヘッダーバー：グループチャット作成・ステアリングトグル */}
        <div className="flex items-center gap-1.5 border-b border-border/60 bg-background/42 px-3 py-1.5 backdrop-blur-xl">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setGroupDialogOpen(true)}
            title="グループチャットを作成"
          >
            <Users className="size-3.5 mr-1" />
            グループ
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
                ""})
            </span>
          )}
        </div>

        {/* メッセージ */}
        {isLoadingMessages ? (
          <div className="flex flex-1 items-center justify-center">
            <div className="rounded-2xl border border-white/65 bg-white/52 px-5 py-4 text-sm text-muted-foreground shadow-[inset_0_1px_rgba(255,255,255,0.76)] backdrop-blur-xl dark:border-white/12 dark:bg-card/70 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)]">
              メッセージを読み込み中...
            </div>
          </div>
        ) : sessionLoadError ? (
          <div className="flex flex-1 items-center justify-center px-4">
            <div className="flex max-w-md flex-col items-center gap-3 rounded-2xl border border-white/65 bg-white/58 px-6 py-5 text-center shadow-[inset_0_1px_rgba(255,255,255,0.76)] backdrop-blur-xl dark:border-white/12 dark:bg-card/75 dark:shadow-[inset_0_1px_rgba(255,255,255,0.12)]">
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
        />
      </div>

      <Dialog
        open={externalModelPromptRequest != null}
        onOpenChange={(open) => {
          if (!open) handleExternalModelPromptDecision(false);
        }}
      >
        <DialogContent showCloseButton={false} className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>外部モデル送信の確認</DialogTitle>
            <DialogDescription>
              {externalModelPromptRequest?.description}
            </DialogDescription>
          </DialogHeader>
          {externalModelPromptRequest && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>{externalModelPromptRequest.provider}</span>
                <span>/</span>
                <span>{externalModelPromptRequest.model}</span>
              </div>
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-medium">秘匿版プロンプト</span>
                  <span className="text-[10px] text-muted-foreground">
                    Enterで送信 / Shift+Enterで改行
                  </span>
                </div>
                <textarea
                  autoFocus
                  value={externalModelPromptDraft}
                  onChange={(event) => setExternalModelPromptDraft(event.target.value)}
                  onKeyDown={handleExternalModelPromptKeyDown}
                  className="min-h-52 w-full rounded-md border border-input bg-background p-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                />
              </div>
              {externalModelPromptRequest.redactionFindings.length > 0 && (
                <div className="flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                  {externalModelPromptRequest.redactionFindings.map((finding, index) => (
                    <span
                      key={`${finding.placeholder}-${index}`}
                      className="rounded border bg-muted/40 px-2 py-1"
                    >
                      {finding.category}: {finding.placeholder}
                    </span>
                  ))}
                </div>
              )}
              <div className="space-y-2">
                <span className="text-xs font-medium">原文プロンプト</span>
                <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/30 p-3 text-xs">
                  {externalModelPromptRequest.prompt}
                </pre>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => handleExternalModelPromptDecision(false)}
            >
              キャンセル
            </Button>
            <Button
              onClick={() => handleExternalModelPromptDecision(true)}
              disabled={!externalModelPromptDraft.trim()}
            >
              この内容で送信
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={toolPermissionRequest != null}
        onOpenChange={(open) => {
          if (!open) handleToolPermissionDecision(false);
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>ツール実行の確認</DialogTitle>
            <DialogDescription>
              {toolPermissionRequest?.description}
            </DialogDescription>
          </DialogHeader>
          {toolPermissionRequest && (
            <div className="rounded-md border bg-muted/40 p-3 font-mono text-xs break-words">
              <div className="font-sans text-muted-foreground">
                {toolPermissionRequest.toolName}
              </div>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap">
                {JSON.stringify(toolPermissionRequest.toolArgs, null, 2)}
              </pre>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => handleToolPermissionDecision(false)}
            >
              いいえ
            </Button>
            <Button onClick={() => handleToolPermissionDecision(true)}>
              はい
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* シナリオ情報パネル */}
      {(scenarioSession || writingSession || roleplaySession) && (
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
      )}

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
