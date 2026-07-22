import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ChatResponseModelOption,
  ChatResponseModelSelection,
  ConversationMessage,
  ConversationSession,
  LlmCatalogModelOption,
  LlmCatalogProvider,
  LlmModelCatalogResponse,
  WSMessage,
} from "../../types/api";
import { chatApi, type LlmModeResponse } from "../../lib/chat-api";
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
  flushPendingConversation,
  getPromotedConversationSessionId,
} from "../../repositories";
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
  buildTimeline,
  groupMessageKey,
  selectVisibleMessages,
} from "./timeline";
import type {
  ConversationControllerSnapshot,
  ConversationDiagnostics,
  ConversationJob,
  PermissionRequest,
  SendConversationCommand,
} from "./models";
import {
  sanitizeChatCommandCapabilities,
  type SkillSlashCommand,
} from "./chat-commands";
import {
  describeFallbackFailure,
  errorTextOf,
  isLikelyConnectivityFailure,
} from "./fallback-error";

type ControllerArgs = {
  sessionId?: string | null;
  isAuthenticated: boolean;
  userRole?: string | null;
  selectedProjectId?: string | null;
  onSessionPromoted?: (sessionId: string) => void;
};

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
  const timers = [
    setTimeout(callback, 1500),
    setTimeout(callback, 5000),
    setTimeout(callback, 15000),
  ];
  return () => timers.forEach(clearTimeout);
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

const API_KEY_REQUIRED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "kimi",
  "openrouter",
]);

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
  const currentCatalogModel = currentCatalogProvider.models.find(
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

export function useConversationController(
  args: ControllerArgs,
): ConversationControllerSnapshot & {
  branchSelections: Record<string, number>;
  load: () => Promise<void>;
  refreshFromServer: () => Promise<void>;
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
  changeLlmMode: (mode: string) => Promise<void>;
  refreshLlmMode: () => Promise<void>;
  refreshSkillCommands: () => Promise<void>;
} {
  const {
    sessionId,
    isAuthenticated,
    selectedProjectId,
    userRole,
    onSessionPromoted,
  } = args;
  const network = useNetworkStore();
  const [session, setSession] = useState<ConversationSession | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isWaiting, setIsWaiting] = useState(false);
  const [streamContent, setStreamContent] = useState("");
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [activityMessage, setActivityMessage] = useState<string | null>(null);
  const [llmMode, setLlmMode] = useState("");
  const [llmModeOptions, setLlmModeOptions] = useState<string[]>([]);
  const [llmModeLabels, setLlmModeLabels] = useState<Record<string, string>>({});
  const [llmModeKind, setLlmModeKind] = useState<string | null>(null);
  const [responseModelOptions, setResponseModelOptions] = useState<
    ChatResponseModelOption[]
  >([]);
  const [responseModelOptionsLoading, setResponseModelOptionsLoading] =
    useState(false);
  const [skillCommands, setSkillCommands] = useState<SkillSlashCommand[]>([]);
  const [branchSelections, setBranchSelections] = useState<Record<string, number>>({});
  const [pendingPermissions, setPendingPermissions] = useState<PermissionRequest[]>([]);
  const [jobs, setJobs] = useState<ConversationJob[]>([]);
  const [lastRefreshAt, setLastRefreshAt] = useState<string | null>(null);
  const wsRef = useRef<ChatWebSocket>(new ChatWebSocket());
  const streamBufferRef = useRef("");
  const jobPollersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({});
  const mountedRef = useRef(true);
  const loadRequestRef = useRef(0);
  const scheduledRefreshCancelRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loadRequestRef.current += 1;
      scheduledRefreshCancelRef.current?.();
    };
  }, []);

  useEffect(() => {
    return () => {
      scheduledRefreshCancelRef.current?.();
      scheduledRefreshCancelRef.current = null;
    };
  }, [sessionId]);

  const sessionKind = useMemo(() => inferSessionKind(session), [session]);
  const transportState = buildTransportState({
    isAuthenticated,
    isConnected,
    online: network.online,
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
    selectedProjectId,
  });
  const diagnostics: ConversationDiagnostics = {
    userCapability: buildUserCapability(isAuthenticated, userRole),
    sessionKind,
    sessionCapabilities,
    connectionCapability: buildConnectionCapability({
      isAuthenticated,
      isConnected,
      online: network.online,
      serverReachable: network.serverReachable,
    }),
    transportState,
    runState,
    syncState,
    activeTool,
    activityMessage,
    pendingMessages,
    lastRefreshAt,
  };
  const commands = buildCommandRegistry({
    isAuthenticated,
    sessionKind,
    capabilities: sessionCapabilities,
    transportState,
    runState,
    selectedProjectId,
  });
  const visibleMessages = useMemo(
    () => selectVisibleMessages(messages, branchSelections),
    [branchSelections, messages],
  );
  const timeline = useMemo(
    () =>
      buildTimeline({
        messages: visibleMessages,
        permissions: pendingPermissions.filter((request) => request.status === "pending"),
        jobs,
        activeTool,
        activityMessage,
        streamContent,
        pendingMessages,
      }),
    [
      activeTool,
      activityMessage,
      isAuthenticated,
      jobs,
      pendingMessages,
      pendingPermissions,
      streamContent,
      visibleMessages,
    ],
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

      const refreshRemote = async () => {
        try {
          const remote = await chatApi.resumeSession(sessionId);
          if (!isCurrentRequest()) return;
          setSession(remote.session);
          setMessages((current) => {
            const remoteIds = new Set(remote.messages.map((message) => message.id));
            const localPending = current.filter(
              (message) =>
                !remoteIds.has(message.id) &&
                (Boolean(message.metadata?.local_only) ||
                  Boolean(message.metadata?.pending)),
            );
            return [...remote.messages, ...localPending].sort((left, right) =>
              String(left.created_at ?? "").localeCompare(
                String(right.created_at ?? ""),
              ),
            );
          });
          setLastRefreshAt(new Date().toISOString());
          setLoading(false);
          void conversationsRepo
            .saveLocalMessages(sessionId, remote.messages)
            .catch(() => undefined);
        } catch {
          if (!isCurrentRequest()) return;
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
  }, [isAuthenticated, sessionId]);

  const refreshFromServer = useCallback(async () => {
    const requestId = loadRequestRef.current;
    const isCurrentRequest = () =>
      mountedRef.current && requestId === loadRequestRef.current;
    if (!sessionId || !isAuthenticated) {
      if (sessionId) {
        const localMessages = await conversationsRepo.listMessagesLocal(sessionId);
        if (isCurrentRequest()) setMessages(localMessages);
      }
      return;
    }
    const fresh = await conversationsRepo.refreshMessages(sessionId);
    if (!isCurrentRequest()) return;
    setMessages((current) => {
      const freshIds = new Set(fresh.map((message) => message.id));
      const localPending = current.filter(
        (message) =>
          !freshIds.has(message.id) &&
          (Boolean(message.metadata?.local_only) ||
            Boolean(message.metadata?.pending)),
      );
      return [...fresh, ...localPending].sort((left, right) =>
        String(left.created_at ?? "").localeCompare(
          String(right.created_at ?? ""),
        ),
      );
    });
    const localSession = await conversationsRepo.getSessionLocal(sessionId);
    if (!isCurrentRequest()) return;
    setSession(localSession);
    setLastRefreshAt(new Date().toISOString());
  }, [isAuthenticated, sessionId]);

  const scheduleRefresh = useCallback(() => {
    scheduledRefreshCancelRef.current?.();
    const cancel = nextRefreshTimers(() => {
      void refreshFromServer().finally(() => setIsWaiting(false));
    });
    scheduledRefreshCancelRef.current = cancel;
    return cancel;
  }, [refreshFromServer]);

  const applyLlmMode = useCallback((result: LlmModeResponse) => {
    const options =
      result.available_modes && result.available_modes.length > 0
        ? result.available_modes
        : [result.mode];
    setLlmMode(result.mode);
    setLlmModeOptions(options);
    setLlmModeLabels(result.labels ?? {});
    setLlmModeKind(result.kind ?? null);
  }, []);

  const refreshLlmMode = useCallback(async () => {
    if (!isAuthenticated) {
      setLlmMode("");
      setLlmModeOptions([]);
      setLlmModeLabels({});
      setLlmModeKind(null);
      return;
    }
    try {
      applyLlmMode(await chatApi.getLlmMode());
    } catch {
      // LLM mode is an online convenience; chat itself can continue without it.
    }
  }, [applyLlmMode, isAuthenticated]);

  const refreshResponseModelOptions = useCallback(async () => {
    if (!isAuthenticated) {
      setResponseModelOptions([]);
      setResponseModelOptionsLoading(false);
      return;
    }

    setResponseModelOptionsLoading(true);
    try {
      const catalog = await chatApi.getLlmModelCatalog();
      setResponseModelOptions(buildResponseModelOptions(catalog));
    } catch {
      setResponseModelOptions([]);
    } finally {
      setResponseModelOptionsLoading(false);
    }
  }, [isAuthenticated]);

  const changeLlmMode = useCallback(
    async (mode: string) => {
      const next = mode.trim();
      if (!next) return;
      applyLlmMode(await chatApi.setLlmMode(next));
    },
    [applyLlmMode],
  );

  const refreshSkillCommands = useCallback(async () => {
    if (!isAuthenticated) {
      setSkillCommands([]);
      return;
    }
    try {
      setSkillCommands(await chatApi.listSkillSlashCommands(selectedProjectId));
    } catch {
      setSkillCommands([]);
    }
  }, [isAuthenticated, selectedProjectId]);

  const sendConversationCommand = useCallback(
    async (command: SendConversationCommand) => {
      if (!sessionId) return;
      const text = command.message.trim();
      if (!text || isStreaming) return;
      const requestedTarget = command.target ?? { kind: "server" as const };
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
      if (requiresServerRuntime && !isAuthenticated) {
        setError("組み込みコマンド、Skills、モデル指定はログイン中のみ利用できます。");
        return;
      }

      let directSettings: MobileLlmSettings | null = null;
      let fallbackFromServer = false;
      const serverKnownUnreachable =
        requestedTarget.kind === "server" &&
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
        ...(command.commandCapabilities?.length
          ? { command_capabilities: command.commandCapabilities }
          : {}),
        ...(requestedTarget.kind === "server" && requestedTarget.responseModel
          ? { response_model: requestedTarget.responseModel }
          : {}),
      });
      setMessages((prev) => [...prev, localMessage]);

      const appendDirectReply = async (
        directSettings: MobileLlmSettings,
        metadata: Record<string, unknown> = {},
      ) => {
        const reply = await generateMobileLlmReply(directSettings, messages, text);
        const assistantMessage = await conversationsRepo.appendLocalMessage(
          sessionId,
          "assistant",
          reply.content,
          {
            local_only: true,
            direct_cloud: true,
            provider: directSettings.provider,
            model: directSettings.model,
            ...(reply.assistantPayload
              ? {
                  [KIMI_ASSISTANT_PAYLOAD_METADATA_KEY]: reply.assistantPayload,
                }
              : {}),
            ...metadata,
          },
        );
        await conversationsRepo.mergeMessageMetadata(localMessage.id, {
          pending: false,
          message_state: "persisted",
          direct_cloud: true,
          ...metadata,
        });
        setMessages((prev) => [...prev, assistantMessage]);
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

      if (serverKnownUnreachable) {
        setError("Serverに接続できません。Directモデルを選ぶか、フォールバックモデルを設定してください。");
        setIsWaiting(false);
        return;
      }

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
          setError(
            errorText(
              promotionError,
              "ローカルチャットをServerへ接続できませんでした。",
            ),
          );
          const promotedSessionId =
            await getPromotedConversationSessionId(sessionId).catch(() => null);
          if (promotedSessionId) {
            onSessionPromoted?.(promotedSessionId);
          }
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
          setIsWaiting(false);
        }
        return;
      }

      const settings = await getMobileLlmSettings();
      if (
        isDirectProvider(settings.provider) &&
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

      try {
        await chatApi.dispatchMessage(sessionId, {
          message: text,
          project_id: command.projectId ?? selectedProjectId ?? undefined,
          include_project_context:
            command.includeProjectContext ?? Boolean(command.projectId ?? selectedProjectId),
          agent_mode: command.agentMode ?? "confirm",
          edit_message_id: command.editMessageId,
          response_model:
            requestedTarget.kind === "server"
              ? requestedTarget.responseModel
              : undefined,
          command_capabilities: command.commandCapabilities,
        });
        await conversationsRepo.markPendingMessageQueued(localMessage.id);
        scheduleRefresh();
      } catch (dispatchError) {
        if (isLikelyConnectivityFailure(dispatchError)) {
          useNetworkStore.getState().setServerReachable(false);
        }
        const failedMetadata = {
          pending: false,
          message_state: "delivery-unknown",
          delivery_error: errorText(
            dispatchError,
            "メッセージ送信に失敗しました。",
          ),
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
        setError(errorText(dispatchError, "メッセージ送信に失敗しました。"));
        setIsWaiting(false);
      }
    },
    [
      isAuthenticated,
      isStreaming,
      messages,
      onSessionPromoted,
      scheduleRefresh,
      selectedProjectId,
      session?.user_id,
      sessionId,
    ],
  );

  const retryPendingMessage = useCallback(
    async (message: ConversationMessage) => {
      if (!sessionId || !isAuthenticated) return;
      const storedResponseModel = message.metadata?.response_model;
      const responseModel =
        storedResponseModel &&
        typeof storedResponseModel === "object" &&
        typeof (storedResponseModel as Record<string, unknown>).provider === "string" &&
        typeof (storedResponseModel as Record<string, unknown>).model === "string"
          ? {
              provider: (storedResponseModel as Record<string, string>).provider,
              model: (storedResponseModel as Record<string, string>).model,
            }
          : undefined;
      await chatApi.dispatchMessage(sessionId, {
        message: message.content,
        project_id: selectedProjectId ?? undefined,
        include_project_context: Boolean(selectedProjectId),
        agent_mode: "confirm",
        command_capabilities: sanitizeChatCommandCapabilities(
          message.metadata?.command_capabilities,
        ),
        response_model: responseModel,
      });
      await conversationsRepo.markPendingMessageQueued(message.id);
      await refreshFromServer();
    },
    [isAuthenticated, refreshFromServer, selectedProjectId, sessionId],
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
      if (jobPollersRef.current[jobId]) return;
      jobPollersRef.current[jobId] = setInterval(() => {
        void (async () => {
          const job = await deepResearchApi.getJob(jobId);
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
            if (sessionId && job.status === "completed" && job.report_markdown) {
              const content = `Deep Research 完了\n\n${job.report_markdown}`;
              await conversationsRepo.appendLocalMessage(sessionId, "assistant", content, {
                local_only: true,
                job_id: job.id,
                job_type: "deep_research",
                message_state: "persisted",
              });
              await refreshFromServer().catch(() => undefined);
            }
          }
        })().catch((pollError) => {
          setError(pollError instanceof Error ? pollError.message : "ジョブ更新に失敗しました。");
        });
      }, 4000);
    },
    [refreshFromServer, sessionId],
  );

  const startDeepResearch = useCallback(
    async (query: string) => {
      if (!query.trim() || !isAuthenticated) return;
      const job = await deepResearchApi.startJob({
        query: query.trim(),
        mode: "report",
        max_iterations: 2,
        questions_per_iteration: 3,
        max_results_per_query: 5,
        engines: ["duckduckgo"],
        include_local_rag: Boolean(selectedProjectId),
        project_id: selectedProjectId ?? null,
      });
      const mapped = deepResearchToConversationJob(job);
      setJobs((prev) => [mapped, ...prev.filter((item) => item.id !== mapped.id)]);
      pollDeepResearchJob(job.id);
    },
    [isAuthenticated, pollDeepResearchJob, selectedProjectId],
  );

  const editMessage = useCallback(
    async (message: ConversationMessage, content: string) => {
      await sendConversationCommand({
        message: content,
        projectId: selectedProjectId,
        includeProjectContext: Boolean(selectedProjectId),
        agentMode: "confirm",
        editMessageId: message.id,
      });
    },
    [selectedProjectId, sendConversationCommand],
  );

  const rerunMessage = useCallback(
    async (
      message: ConversationMessage,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (message.role === "user") {
        await sendConversationCommand({
          message: message.content,
          projectId: selectedProjectId,
          includeProjectContext: Boolean(selectedProjectId),
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
          projectId: selectedProjectId,
          includeProjectContext: Boolean(selectedProjectId),
          agentMode: "confirm",
          editMessageId: source.id,
          target: { kind: "server", responseModel },
        });
      }
    },
    [messages, selectedProjectId, sendConversationCommand],
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
      const siblings = messages.filter(
        (entry) => groupMessageKey(entry) === groupMessageKey(message),
      );
      if (!siblings[nextIndex]) return;
      await conversationsRepo.switchBranch(sessionId, message.id, nextIndex);
      setBranchSelections((prev) => ({
        ...prev,
        [groupMessageKey(message)]: nextIndex,
      }));
      await refreshFromServer();
    },
    [messages, refreshFromServer, sessionId],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void refreshLlmMode();
  }, [refreshLlmMode]);

  useEffect(() => {
    void refreshResponseModelOptions();
  }, [refreshResponseModelOptions]);

  useEffect(() => {
    void refreshSkillCommands();
  }, [refreshSkillCommands]);

  useEffect(() => {
    if (
      !sessionId ||
      !session ||
      session.user_id ||
      !isAuthenticated ||
      !network.online ||
      !network.serverReachable
    ) {
      return;
    }
    void flushPendingConversation(sessionId)
      .then((remoteSessionId) => {
        if (remoteSessionId !== sessionId) {
          onSessionPromoted?.(remoteSessionId);
        }
      })
      .catch(() => undefined);
  }, [
    isAuthenticated,
    network.online,
    network.serverReachable,
    onSessionPromoted,
    session,
    sessionId,
  ]);

  useEffect(() => {
    if (!sessionId || !isAuthenticated || !session?.user_id) {
      setIsConnected(false);
      return;
    }

    const ws = wsRef.current;
    ws.setOnConnectionChange((connected) => {
      setIsConnected(connected);
      if (connected) {
        void flushPendingConversation(sessionId)
          .then((remoteSessionId) => {
            if (remoteSessionId === sessionId) return refreshFromServer();
            onSessionPromoted?.(remoteSessionId);
            return undefined;
          })
          .catch(() => undefined);
      }
    });
    ws.setOnMessage((msg: WSMessage) => {
      switch (msg.type) {
        case "llm_mode_change": {
          const payload = parseLlmModePayload(msg.data ?? msg);
          if (payload) applyLlmMode(payload);
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
        case "conversation_persisted":
          setActivityMessage(null);
          setIsWaiting(false);
          void refreshFromServer();
          break;
        case "stream_start":
          setIsWaiting(false);
          setIsStreaming(true);
          setActiveTool(null);
          setActivityMessage(extractActivityMessage(msg) ?? "応答を生成しています...");
          streamBufferRef.current = "";
          setStreamContent("");
          break;
        case "stream_token":
          if (msg.content) {
            streamBufferRef.current += String(msg.content);
            setStreamContent(streamBufferRef.current);
          }
          break;
        case "stream_end":
        case "response":
          setIsStreaming(false);
          setActiveTool(null);
          setActivityMessage(null);
          setStreamContent("");
          streamBufferRef.current = "";
          void refreshFromServer();
          break;
        case "title_updated":
          if (msg.title) {
            void conversationsRepo.updateTitle(sessionId, String(msg.title));
            setSession((prev) => (prev ? { ...prev, title: String(msg.title) } : prev));
          }
          break;
      }
    });
    void ws.connect(sessionId);
    return () => ws.disconnect();
  }, [
    applyLlmMode,
    isAuthenticated,
    onSessionPromoted,
    refreshFromServer,
    session?.user_id,
    sessionId,
  ]);

  useEffect(() => {
    return () => {
      for (const poller of Object.values(jobPollersRef.current)) {
        clearInterval(poller);
      }
      jobPollersRef.current = {};
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
    responseModelOptions,
    responseModelOptionsLoading,
    skillCommands,
    branchSelections,
    load,
    refreshFromServer,
    sendConversationCommand,
    retryPendingMessage,
    respondPermission,
    startDeepResearch,
    editMessage,
    rerunMessage,
    loadBranches,
    switchBranch,
    changeLlmMode,
    refreshLlmMode,
    refreshSkillCommands,
  };
}
