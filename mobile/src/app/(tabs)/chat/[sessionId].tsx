import React, {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Alert,
  FlatList,
  Keyboard,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import * as Clipboard from "expo-clipboard";
import { useLocalSearchParams, useRouter } from "expo-router";
import {
  Button,
  Dialog,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { AoiDotsContext } from "../../../features/aoidots/context";
import { useConversationController } from "../../../features/conversation/useConversationController";
import { buildDurableTimeline } from "../../../features/conversation/timeline";
import type { TimelineItem } from "../../../features/conversation/models";
import { ChatHeaderStatus } from "../../../features/conversation/components/ChatHeaderStatus";
import { ChatTimeline } from "../../../features/conversation/components/ChatTimeline";
import { ChatTimelineFrame } from "../../../features/conversation/components/ChatTimelineFrame";
import { parseCanonicalAppLink } from "../../../features/conversation/components/app-links";
import {
  ChatComposer,
  type ChatComposerSubmission,
} from "../../../features/conversation/components/ChatComposer";
import { ChatDialogHost } from "../../../features/conversation/components/ChatDialogHost";
import { ChatScreenShell } from "../../../features/conversation/components/ChatScreenShell";
import { useReducedMotion } from "../../../features/ui/use-reduced-motion";
import type { ConversationMessage } from "../../../types/api";
import type { ChatAppContext, ContextRequestSnapshot } from "../../../lib/chat-api";
import {
  getFallbackConfig,
  getMainSlot,
  getProviderProfile,
  getDirectReasoningEffortOptions,
  isDirectProvider,
  type DirectMobileLlmSelection,
} from "../../../lib/mobile-llm";
import {
  DIRECT_PROVIDER_ORDER,
  getProviderLabel,
  getSeedModelIds,
  mergeModelIds,
  readCachedModels,
  type DirectMobileLlmProvider,
} from "../../../lib/cloud-model-catalog";
import {
  buildModelPickerGroups,
  type ModelPickerEntry,
} from "../../../features/conversation/model-picker";
import { ChatTextInput } from "../../../features/conversation/chat-input-theme";
import { getPendingMessageRetryRoute } from "../../../features/conversation/message-actions";
import { conversationsRepo } from "../../../repositories";
import type { ConversationAgent } from "../../../types/api";
import type { OperationsWorkReference } from "../../../lib/operations-api";

type DirectModelOption = DirectMobileLlmSelection & { label: string };

export default function ChatScreen() {
  const { sessionId, appId: rawAppId, appTargetId: rawAppTargetId, projectId: rawProjectId, taskId: rawTaskId } =
    useLocalSearchParams<{
      sessionId: string;
      appId?: string;
      appTargetId?: string;
      projectId?: string;
      taskId?: string;
    }>();
  const routeAppId = Array.isArray(rawAppId) ? rawAppId[0] : rawAppId;
  const routeAppTargetId = Array.isArray(rawAppTargetId)
    ? rawAppTargetId[0]
    : rawAppTargetId;
  const routeProjectId = Array.isArray(rawProjectId) ? rawProjectId[0] : rawProjectId;
  const routeTaskId = Array.isArray(rawTaskId) ? rawTaskId[0] : rawTaskId;
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { user, isAuthenticated } = useAuth();
  const {
    projects,
    selectedProjectId,
    setSelectedProjectId,
  } = useProject();
  const aoiDots = useContext(AoiDotsContext);
  const chatSurfaceActive = aoiDots?.isOpen !== true;
  const flatListRef = useRef<FlatList<TimelineItem>>(null);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const promotedSessionTargetRef = useRef<string | null>(null);
  const voiceSessionAliasesRef = useRef(new Map<string, string>());
  const reduceMotion = useReducedMotion();
  const [editTarget, setEditTarget] = useState<ConversationMessage | null>(
    null,
  );
  const [editContent, setEditContent] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const editTargetIdRef = useRef<string | null>(null);
  const [modelTarget, setModelTarget] = useState<ConversationMessage | null>(
    null,
  );
  const [llmModeVisible, setLlmModeVisible] = useState(false);
  const [responseModelVisible, setResponseModelVisible] = useState(false);
  const [directModelOptions, setDirectModelOptions] = useState<DirectModelOption[]>([]);
  // モデル選択ダイアログの 1段目(プロバイダー)→2段目(モデル) 遷移用。null=1段目。
  const [modelPickerProvider, setModelPickerProvider] = useState<string | null>(
    null,
  );
  const [modelPickerFilter, setModelPickerFilter] = useState("");
  const [rerunPickerProvider, setRerunPickerProvider] = useState<string | null>(
    null,
  );
  const [pendingQueueVisible, setPendingQueueVisible] = useState(false);
  const [keyboardVisible, setKeyboardVisible] = useState(false);
  const [actionTargetId, setActionTargetId] = useState<string | null>(null);
  const [titleDialogVisible, setTitleDialogVisible] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaving, setTitleSaving] = useState(false);
  const [titleError, setTitleError] = useState<string | null>(null);
  const [contextVisible, setContextVisible] = useState(false);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextMain, setContextMain] = useState<ContextRequestSnapshot | null>(null);
  const [contextError, setContextError] = useState<string | null>(null);

  const handleSessionPromoted = useCallback(
    (remoteSessionId: string) => {
      promotedSessionTargetRef.current = remoteSessionId;
      voiceSessionAliasesRef.current.set(
        remoteSessionId, voiceSessionAliasesRef.current.get(sessionId) ?? sessionId,
      );
      router.setParams({
          sessionId: remoteSessionId,
          ...(routeAppId ? { appId: routeAppId } : {}),
          ...(routeAppTargetId ? { appTargetId: routeAppTargetId } : {}),
          ...(routeProjectId ? { projectId: routeProjectId } : {}),
          ...(routeTaskId ? { taskId: routeTaskId } : {}),
      });
    },
    [routeAppId, routeAppTargetId, routeProjectId, routeTaskId, router, sessionId],
  );

  const controller = useConversationController({
    sessionId,
    isAuthenticated,
    userId: user?.user_id,
    userRole: user?.role,
    selectedProjectId,
    onSessionPromoted: handleSessionPromoted,
    initialAppContext: routeAppId
      ? ({
          appId: routeAppId,
          appTargetId: routeAppTargetId ?? null,
          projectId: routeProjectId ?? null,
        } satisfies ChatAppContext)
      : null,
  });
  const responseModel =
    controller.responseTarget.kind === "server"
      ? controller.responseTarget.responseModel ?? null
      : null;
  const directResponse =
    controller.responseTarget.kind === "direct"
      ? controller.responseTarget.selection
      : null;
  const contentBelongsToRoute =
    controller.session?.id === sessionId ||
    controller.messages.some((message) => message.session_id === sessionId);
  const promotionInProgress = promotedSessionTargetRef.current === sessionId;
  const hasConversationContent = contentBelongsToRoute || promotionInProgress;
  useEffect(() => {
    if (
      promotedSessionTargetRef.current === sessionId &&
      controller.session?.id === sessionId
    ) {
      promotedSessionTargetRef.current = null;
    }
  }, [controller.session?.id, sessionId]);
  const currentProjectId = controller.session
    ? controller.session.project_id
    : routeProjectId ?? selectedProjectId;
  const includeProjectContext = Boolean(currentProjectId);
  const appContextSelected = Boolean(controller.session?.app_id || routeAppId);
  const agentBoundToServer = Boolean(
    controller.session?.agent_id || controller.session?.agent_binding_pending,
  );
  editTargetIdRef.current = editTarget?.id ?? null;

  const openContextSnapshot = useCallback(() => {
    setContextVisible(true);
    setContextLoading(true);
    setContextError(null);
    void controller
      .getContextSnapshot()
      .then((result) => setContextMain(result.main))
      .catch((error) =>
        setContextError(error instanceof Error ? error.message : "コンテキスト取得に失敗しました。"),
      )
      .finally(() => setContextLoading(false));
  }, [controller.getContextSnapshot]);

  const durableTimeline = useMemo(
    () =>
      buildDurableTimeline({
        messages: controller.visibleMessages,
        permissions: controller.pendingPermissions,
        jobs: controller.jobs,
        activeTool: controller.diagnostics.activeTool,
        activityMessage: controller.diagnostics.activityMessage,
      }),
    [
      controller.diagnostics.activeTool,
      controller.diagnostics.activityMessage,
      controller.jobs,
      controller.pendingPermissions,
      controller.visibleMessages,
    ],
  );

  const sendEnabled = controller.commands.find(
    (command) => command.id === "send-message",
  )?.enabled;
  const pendingMessages = useMemo(
    () =>
      controller.messages.filter(
        (message) =>
          message.role === "user" &&
          Boolean(message.metadata?.local_only) &&
          (Boolean(message.metadata?.pending) ||
            Boolean(message.metadata?.persistence_error) ||
            message.metadata?.message_state === "direct-failed"),
      ),
    [controller.messages],
  );
  const directEffortOptions = directResponse
    ? getDirectReasoningEffortOptions(directResponse.provider, directResponse.model)
    : [];
  const currentLlmModeLabel = directResponse
    ? directResponse.reasoningEffort || directEffortOptions[0] || "標準"
    : controller.llmMode
      ? controller.llmModeLabels[controller.llmMode] ?? controller.llmMode
      : "自動";
  const llmSyncLabel =
    controller.llmModeSyncStatus === "pending"
      ? "Effortをバックグラウンド同期予定"
      : controller.llmModeSyncStatus === "syncing"
        ? "Effortをバックグラウンド同期中"
        : null;
  const currentResponseModelLabel = useMemo(() => {
    if (directResponse) {
      return `${getProviderLabel(directResponse.provider)} / ${directResponse.model}`;
    }
    const option = responseModel
      ? controller.responseModelOptions.find(
          (item) =>
            item.provider === responseModel.provider &&
            item.model === responseModel.model,
        )
      : controller.responseModelOptions.find((item) => item.isCurrent);
    return option?.modelLabel ?? responseModel?.model ?? "サーバー自動";
  }, [controller.responseModelOptions, directResponse, responseModel]);

  // 「次の応答モデル」: Direct + Server をプロバイダー単位で 2段階化する。
  const visibleDirectModelOptions = directModelOptions;
  const responseModelGroups = useMemo(
    () =>
      buildModelPickerGroups(
        visibleDirectModelOptions.map((option) => ({
          ...option,
          isCurrent:
            directResponse?.provider === option.provider &&
            directResponse?.model === option.model,
        })),
        controller.responseModelOptions,
      ),
    [controller.responseModelOptions, directResponse, visibleDirectModelOptions],
  );
  const activeResponseGroup = useMemo(
    () =>
      responseModelGroups.find((group) => group.key === modelPickerProvider) ??
      null,
    [responseModelGroups, modelPickerProvider],
  );
  const activeResponseModels = useMemo(() => {
    if (!activeResponseGroup) return [];
    const query = modelPickerFilter.trim().toLowerCase();
    if (!query) return activeResponseGroup.models;
    return activeResponseGroup.models.filter((entry) =>
      `${entry.model} ${entry.label} ${entry.routeLabel ?? ""}`
        .toLowerCase()
        .includes(query),
    );
  }, [activeResponseGroup, modelPickerFilter]);
  // 「別モデルでこの回答を再生成」: Server カタログのみを 2段階化する。
  const rerunModelGroups = useMemo(
    () => buildModelPickerGroups([], controller.responseModelOptions),
    [controller.responseModelOptions],
  );
  const activeRerunGroup = useMemo(
    () =>
      rerunModelGroups.find((group) => group.key === rerunPickerProvider) ?? null,
    [rerunModelGroups, rerunPickerProvider],
  );

  const closeResponseModelDialog = useCallback(() => {
    setResponseModelVisible(false);
    setModelPickerProvider(null);
    setModelPickerFilter("");
  }, []);

  const closeModelTargetDialog = useCallback(() => {
    setModelTarget(null);
    setRerunPickerProvider(null);
  }, []);

  const selectResponseModelEntry = useCallback((entry: ModelPickerEntry) => {
    if ((appContextSelected || agentBoundToServer) && entry.kind === "direct") return;
    if (entry.kind === "direct") {
      const efforts = getDirectReasoningEffortOptions(
        entry.provider as DirectMobileLlmProvider,
        entry.model,
      );
      const previousEffort = directResponse?.reasoningEffort;
      controller.changeResponseTarget({
        kind: "direct",
        selection: {
          provider: entry.provider as DirectMobileLlmProvider,
          model: entry.model,
            reasoningEffort: efforts.includes(previousEffort ?? "")
              ? previousEffort
              : efforts.includes("medium")
                ? "medium"
                : efforts[0],
        },
      });
    } else {
      controller.changeResponseTarget({
        kind: "server",
        responseModel: { provider: entry.provider, model: entry.model },
      });
    }
    setResponseModelVisible(false);
    setModelPickerProvider(null);
    setModelPickerFilter("");
  }, [agentBoundToServer, appContextSelected, controller.changeResponseTarget, directResponse]);

  useEffect(() => {
    if ((appContextSelected || agentBoundToServer) && directResponse) {
      controller.changeResponseTarget({ kind: "server" });
    }
  }, [agentBoundToServer, appContextSelected, controller.changeResponseTarget, directResponse]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [main, fallback] = await Promise.all([
        getMainSlot(),
        getFallbackConfig(),
      ]);
      const preferredModels = new Map<DirectMobileLlmProvider, string[]>();
      if (isDirectProvider(main.provider)) {
        preferredModels.set(main.provider, main.model ? [main.model] : []);
      }
      if (fallback.enabled) {
        preferredModels.set(
          fallback.provider,
          mergeModelIds(
            preferredModels.get(fallback.provider) ?? [],
            fallback.model ? [fallback.model] : [],
          ),
        );
      }
      const providerModels = new Map<DirectMobileLlmProvider, string[]>();
      for (const provider of DIRECT_PROVIDER_ORDER) {
        const profile = await getProviderProfile(provider);
        if (!profile.apiKey.trim()) continue;
        const cached = await readCachedModels(provider);
        const merged = mergeModelIds(
          preferredModels.get(provider) ?? [],
          mergeModelIds(cached, getSeedModelIds(provider)),
        );
        providerModels.set(provider, merged);
      }
      if (cancelled) return;
      setDirectModelOptions(
        Array.from(providerModels.entries()).flatMap(([provider, models]) =>
            models.map((model) => ({
              provider,
              model,
              label: model,
            })),
        ),
      );
    })().catch(() => {
      if (!cancelled) setDirectModelOptions([]);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  const saveSessionTitle = useCallback(async () => {
    const normalized = titleDraft.trim();
    if (!normalized) {
      setTitleError("タイトルを入力してください。");
      return;
    }
    setTitleSaving(true);
    setTitleError(null);
    try {
      await controller.updateSessionTitle(normalized);
      setTitleDialogVisible(false);
    } catch (error) {
      setTitleError(
        error instanceof Error ? error.message : "タイトルの更新に失敗しました。",
      );
    } finally {
      setTitleSaving(false);
    }
  }, [controller, titleDraft]);

  const changeChatProject = useCallback(
    async (projectId: string | null) => {
      await controller.changeProject(projectId);
      await setSelectedProjectId(projectId);
    },
    [controller.changeProject, setSelectedProjectId],
  );

  const openSameAgentChat = useCallback(async () => {
    const current = controller.session;
    if (!current?.agent_id) return;
    const agent: ConversationAgent = controller.agentCatalog.find(
      (candidate) => candidate.id === current.agent_id,
    ) ?? {
      id: current.agent_id,
      display_name: current.agent_display_name ?? `Agent ${current.agent_id.slice(0, 8)}`,
      state: current.agent_state,
      latest_revision_id: current.latest_revision_id,
      character_id: current.agent_character_id,
    };
    const created = await conversationsRepo.createLocalSession(
      current.character_name,
      current.project_id,
      user?.user_id,
      agent,
    );
    router.push({
      pathname: "/(tabs)/chat/[sessionId]",
      params: {
        sessionId: created.id,
        ...(current.project_id ? { projectId: current.project_id } : {}),
      },
    });
  }, [controller.agentCatalog, controller.session, router, user?.user_id]);

  const scheduleScrollToEnd = useCallback((delay = 48) => {
    if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    scrollTimerRef.current = setTimeout(() => {
      scrollTimerRef.current = null;
      flatListRef.current?.scrollToEnd({ animated: true });
    }, delay);
  }, []);

  useEffect(() => {
    scheduleScrollToEnd();
  }, [durableTimeline.length, Boolean(controller.streamContent), scheduleScrollToEnd]);

  useEffect(() => {
    const showSubscription = Keyboard.addListener("keyboardDidShow", () => {
      setKeyboardVisible(true);
      if (!editTargetIdRef.current) scheduleScrollToEnd(64);
    });
    const hideSubscription = Keyboard.addListener("keyboardDidHide", () => {
      setKeyboardVisible(false);
    });
    return () => {
      showSubscription.remove();
      hideSubscription.remove();
      if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    };
  }, [scheduleScrollToEnd]);

  const sendComposerMessage = useCallback(
    (submission: ChatComposerSubmission) => {
      return controller.sendConversationCommand({
          message: submission.content,
          submissionId: submission.submissionId,
          projectId: currentProjectId,
          appId: controller.session?.app_id ?? routeAppId ?? null,
          appTargetId: controller.session?.app_target_id ?? routeAppTargetId ?? null,
          includeProjectContext,
          agentMode: "confirm",
          target: controller.responseTarget,
          commandCapabilities: submission.capabilities,
        });
    },
    [
      controller.session?.app_id,
      controller.session?.app_target_id,
      controller.responseTarget,
      controller.sendConversationCommand,
      currentProjectId,
      includeProjectContext,
      routeAppId,
      routeAppTargetId,
    ],
  );

  const openEdit = useCallback((message: ConversationMessage) => {
    setActionTargetId(null);
    setEditTarget(message);
    setEditContent(message.content);
    setEditError(null);
  }, []);

  const cancelEdit = useCallback(() => {
    if (editSaving) return;
    setEditTarget(null);
    setEditContent("");
    setEditError(null);
    Keyboard.dismiss();
  }, [editSaving]);

  const submitEdit = useCallback(async () => {
    if (!editTarget || !editContent.trim() || editSaving) return;
    setEditSaving(true);
    setEditError(null);
    try {
      await controller.editMessage(editTarget, editContent.trim());
      setEditTarget(null);
      setEditContent("");
      Keyboard.dismiss();
    } catch (error) {
      setEditError(
        error instanceof Error
          ? error.message
          : "編集に失敗しました。入力内容は保持されています。",
      );
    } finally {
      setEditSaving(false);
    }
  }, [controller.editMessage, editContent, editSaving, editTarget]);

  const copyMessage = useCallback(async (message: ConversationMessage) => {
    await Clipboard.setStringAsync(message.content);
  }, []);

  const rerunMessage = useCallback(
    (message: ConversationMessage) => {
      void controller.rerunMessage(message);
    },
    [controller.rerunMessage],
  );

  const retryMessage = useCallback(
    (message: ConversationMessage) => {
      void controller.retryPendingMessage(
        message,
        getPendingMessageRetryRoute(message),
      );
    },
    [controller.retryPendingMessage],
  );

  const forkMessage = useCallback(
    (message: ConversationMessage) => {
      void controller
        .forkConversation(message.id)
        .then((forkedId) =>
          router.replace({
            pathname: "/(tabs)/chat/[sessionId]",
            params: { sessionId: forkedId },
          }),
        )
        .catch((error) =>
          Alert.alert(
            "Chat",
            error instanceof Error
              ? error.message
              : "会話のフォークに失敗しました。",
          ),
        );
    },
    [controller.forkConversation, router],
  );

  const changeLlmMode = useCallback(
    (mode: string) => {
      controller.changeLlmMode(mode);
      setLlmModeVisible(false);
    },
    [controller.changeLlmMode],
  );

  const openMessageActions = useCallback((message: ConversationMessage) => {
    if (editTargetIdRef.current) return;
    setActionTargetId((current) =>
      current === message.id ? null : message.id,
    );
  }, []);
  const dismissMessageActions = useCallback(() => setActionTargetId(null), []);
  const switchMessageBranch = useCallback(
    (message: ConversationMessage, nextIndex: number) => {
      void controller.switchBranch(message, nextIndex);
    },
    [controller.switchBranch],
  );
  const openPendingQueue = useCallback(() => setPendingQueueVisible(true), []);
  const openResponseModel = useCallback(() => {
    setModelPickerFilter("");
    setResponseModelVisible(true);
    void controller.refreshResponseModelOptions();
  }, [controller.refreshResponseModelOptions]);
  const openLlmMode = useCallback(() => setLlmModeVisible(true), []);
  const openWork = useCallback(
    (reference: OperationsWorkReference) => {
      if (reference.kind !== "work" || !reference.id) return;
      router.push({
        pathname: "/operations/work/[workId]",
        params: { workId: reference.id },
      });
    },
    [router],
  );
  const openAppLink = useCallback(
    (href: string): boolean => {
      const target = parseCanonicalAppLink(href);
      if (!target) return true;
      router.push({
        pathname: "/(tabs)/apps/[appId]",
        params: {
          appId: target.appId,
          ...(target.projectId ? { projectId: target.projectId } : {}),
        },
      });
      return false;
    },
    [router],
  );

  return (
    <ChatScreenShell
      loading={controller.loading && !hasConversationContent}
      error={controller.error}
      onReload={() => void controller.load()}
    >
      <ChatTimelineFrame
        topInset={insets.top}
        active={chatSurfaceActive}
        character={controller.session?.character_name ?? "assistant"}
        state={controller.diagnostics.runState !== "idle" ? "thinking" : "idle"}
        messages={controller.messages}
        historyReady={!controller.loading}
        sessionKey={sessionId}
      >
        <ChatTimeline
          listRef={flatListRef}
          header={
            <ChatHeaderStatus
              diagnostics={controller.diagnostics}
              session={controller.session}
              projects={projects}
              currentProjectId={currentProjectId ?? null}
              pendingCount={pendingMessages.length}
              onChangeProject={changeChatProject}
              onChangeCharacter={controller.changeCharacter}
              agents={controller.agentCatalog}
              agentCatalogLoading={controller.agentCatalogLoading}
              onChangeAgent={controller.changeAgent}
              onRefreshAgents={controller.refreshAgentCatalog}
              onAdoptServerAgent={controller.adoptServerAgent}
              onNewSameAgentChat={() => void openSameAgentChat()}
              onOpenPendingQueue={openPendingQueue}
              onBack={() => router.back()}
              onRenameTitle={() => {
                setTitleDraft(controller.session?.title || "");
                setTitleError(null);
                setTitleDialogVisible(true);
              }}
            />
          }
          items={durableTimeline}
          allMessages={controller.messages}
          streamContent={controller.streamContent}
          reduceMotion={reduceMotion}
          agentRuns={controller.agentRuns}
          agentRunErrors={controller.agentRunErrors}
          onLongPressMessage={openMessageActions}
          onPressMessage={dismissMessageActions}
          actionTargetId={actionTargetId}
          editTargetId={editTarget?.id ?? null}
          editContent={editContent}
          editSaving={editSaving}
          editError={editError}
          isAuthenticated={isAuthenticated}
          retryingMessageIds={controller.retryingMessageIds}
          onDismissMessageActions={dismissMessageActions}
          onBeginEdit={openEdit}
          onEditContentChange={setEditContent}
          onCancelEdit={cancelEdit}
          onSubmitEdit={() => void submitEdit()}
          onRetryMessage={retryMessage}
          onRerunMessage={rerunMessage}
          onCopyMessage={(message) => void copyMessage(message)}
          onForkMessage={forkMessage}
          onSwitchBranch={switchMessageBranch}
          onRetryAgentRun={controller.retryAgentRun}
          onOpenWork={openWork}
          onOpenAppLink={openAppLink}
          onRespondPermission={controller.respondPermission}
        />
      </ChatTimelineFrame>

      <ChatComposer
        voiceContext={{
          conversationKey: voiceSessionAliasesRef.current.get(sessionId) ?? sessionId,
          sessionId: controller.session?.id ?? sessionId,
          sessionIsLocalOnly: controller.session?.is_local_only === true,
          projectId: currentProjectId ?? null,
          characterName: controller.session?.character_name ?? "assistant",
          userId: user?.user_id ?? null,
          messages: controller.visibleMessages,
          busy: controller.diagnostics.runState !== "idle",
        }}
        bottomInset={insets.bottom}
        keyboardVisible={keyboardVisible}
        sendEnabled={Boolean(sendEnabled)}
        directResponseActive={Boolean(directResponse)}
        currentResponseModelLabel={currentResponseModelLabel}
        currentLlmModeLabel={currentLlmModeLabel}
        effortEnabled={
          directResponse
            ? directEffortOptions.length > 0
            : controller.llmModeOptions.length > 0
        }
        llmSelectionMessage={controller.llmSelectionMessage}
        llmSyncLabel={llmSyncLabel}
        skillCommands={controller.skillCommands}
        isAuthenticated={isAuthenticated}
        pendingCount={pendingMessages.length}
        onSend={sendComposerMessage}
        onOpenResponseModel={openResponseModel}
        onOpenLlmMode={openLlmMode}
        onOpenContextDiagnostics={openContextSnapshot}
        onOpenPendingQueue={openPendingQueue}
        onStartDeepResearch={controller.startDeepResearch}
      />

      <ChatDialogHost>
        <Dialog
          visible={contextVisible}
          onDismiss={() => setContextVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Context snapshot</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
            {contextLoading ? <Text style={styles.diagnosticsText}>読み込み中…</Text> : null}
            {contextError ? <Text style={styles.dialogError}>{contextError}</Text> : null}
            {!contextLoading && !contextError && contextMain ? (
              <>
                <Text style={styles.contextMetric}>
                  {contextMain.provider ?? "server"} / {contextMain.model ?? "default"}
                </Text>
                <Text style={styles.contextMetric}>
                  Tokens {contextMain.input_tokens ?? "?"} / {contextMain.context_window_tokens ?? "?"}
                </Text>
                <Text style={styles.contextMetric}>
                  Remaining {contextMain.remaining_tokens ?? "?"} · {contextMain.measurement ?? "estimated"}
                </Text>
                {(contextMain.categories ?? contextMain.components ?? []).map((item, index) => (
                  <Text key={`${item.id ?? item.category ?? "category"}-${index}`} style={styles.contextCategory}>
                    {item.label ?? item.category ?? item.id ?? "context"}: {item.tokens ?? item.input_tokens ?? item.chars ?? "?"}
                  </Text>
                ))}
              </>
            ) : null}
            {!contextLoading && !contextError && !contextMain ? (
              <Text style={styles.diagnosticsText}>利用可能なスナップショットはありません。</Text>
            ) : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setContextVisible(false)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={titleDialogVisible}
          onDismiss={() => {
            if (!titleSaving) setTitleDialogVisible(false);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            セッションタイトルを変更
          </Dialog.Title>
          <Dialog.Content>
            <ChatTextInput
              value={titleDraft}
              onChangeText={setTitleDraft}
              mode="outlined"
              label="タイトル"
              maxLength={200}
              autoFocus
              disabled={titleSaving}
              onSubmitEditing={() => void saveSessionTitle()}
            />
            {titleError ? (
              <Text style={styles.dialogError}>{titleError}</Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              onPress={() => setTitleDialogVisible(false)}
              disabled={titleSaving}
            >
              キャンセル
            </Button>
            <Button
              onPress={() => void saveSessionTitle()}
              loading={titleSaving}
              disabled={titleSaving || !titleDraft.trim()}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={pendingQueueVisible}
          onDismiss={() => setPendingQueueVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>未送信キュー</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {pendingMessages.map((message) => (
                <Surface
                  key={message.id}
                  style={styles.pendingRow}
                  elevation={0}
                >
                  <View style={styles.pendingTextWrap}>
                    <Text style={styles.pendingTitle} numberOfLines={1}>
                      {message.content || "未送信メッセージ"}
                    </Text>
                    <Text style={styles.pendingMeta} numberOfLines={2}>
                      {String(message.metadata?.direct_error || message.metadata?.delivery_error || message.metadata?.error || "再送待ち")}
                    </Text>
                  </View>
                  <View>
                    {isAuthenticated &&
                    message.metadata?.delivery_route !== "direct" ? (
                      <Button
                        compact
                        icon="refresh"
                        textColor="#89b4fa"
                        disabled={controller.retryingMessageIds.includes(
                          message.id,
                        )}
                        onPress={() => {
                          setPendingQueueVisible(false);
                          void controller.retryPendingMessage(message, "server");
                        }}
                      >
                        {controller.retryingMessageIds.includes(message.id)
                          ? "再試行中…"
                          : "サーバーへ再送"}
                      </Button>
                    ) : null}
                    {!agentBoundToServer ? <Button
                      compact
                      icon="cloud-outline"
                      textColor="#89b4fa"
                      disabled={controller.retryingMessageIds.includes(message.id)}
                      onPress={() => {
                        setPendingQueueVisible(false);
                        void controller.retryPendingMessage(message, "direct");
                      }}
                    >
                      Directで再試行
                    </Button> : null}
                    {(message.metadata?.pending ||
                      message.metadata?.persistence_error ||
                      message.metadata?.message_state === "direct-failed") ? (
                      <>
                        <Button
                          compact
                          icon="undo"
                          textColor="#f9e2af"
                          disabled={controller.retryingMessageIds.includes(message.id)}
                          onPress={() => {
                            setPendingQueueVisible(false);
                            void controller.cancelPendingMessage(message, "cancel").catch((error) =>
                              Alert.alert("Chat", error instanceof Error ? error.message : "未送信入力を取り消せませんでした."),
                            );
                          }}
                        >
                          送信を取り消す
                        </Button>
                        <Button
                          compact
                          icon="content-save-outline"
                          textColor="#a6adc8"
                          disabled={controller.retryingMessageIds.includes(message.id)}
                          onPress={() => {
                            setPendingQueueVisible(false);
                            void controller.cancelPendingMessage(message, "draft").catch((error) =>
                              Alert.alert("Chat", error instanceof Error ? error.message : "下書きへ戻せませんでした."),
                            );
                          }}
                        >
                          下書きに戻す
                        </Button>
                      </>
                    ) : null}
                  </View>
                </Surface>
              ))}
              {pendingMessages.length === 0 ? (
                <Text style={styles.diagnosticsText}>
                  未送信メッセージはありません。
                </Text>
              ) : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              onPress={() => setPendingQueueVisible(false)}
            >
              Close
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={responseModelVisible}
          onDismiss={closeResponseModelDialog}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {activeResponseGroup ? activeResponseGroup.label : "次の応答モデル"}
          </Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {activeResponseGroup ? (
                <>
                  <Button
                    icon="chevron-left"
                    textColor="#a6adc8"
                    style={styles.modeButton}
                    onPress={() => setModelPickerProvider(null)}
                  >
                    プロバイダー一覧へ戻る
                  </Button>
                  {activeResponseGroup.models.length > 8 ? (
                    <TextInput
                      label="モデルを検索"
                      value={modelPickerFilter}
                      onChangeText={setModelPickerFilter}
                      mode="outlined"
                      dense
                      style={styles.modelPickerSearch}
                      autoCapitalize="none"
                    />
                  ) : null}
                  {activeResponseModels.map((entry) => {
                    const selected =
                      entry.kind === "direct"
                        ? directResponse?.provider === entry.provider &&
                          directResponse?.model === entry.model
                        : responseModel?.provider === entry.provider &&
                          responseModel?.model === entry.model;
                    return (
                      <Button
                        key={`${entry.kind}:${entry.provider}:${entry.model}`}
                        mode={selected ? "contained" : "outlined"}
                        buttonColor={selected ? "#7c3aed" : undefined}
                        textColor={selected ? "#f5e9ff" : "#89b4fa"}
                        style={styles.modeButton}
                        disabled={(appContextSelected || agentBoundToServer) && entry.kind === "direct"}
                        onPress={() => selectResponseModelEntry(entry)}
                      >
                        {entry.routeLabel
                          ? `${entry.label} · ${entry.routeLabel}`
                          : entry.label}
                      </Button>
                    );
                  })}
                </>
              ) : (
                <>
                  <Button
                    mode={
                      !responseModel && !directResponse ? "contained" : "outlined"
                    }
                    buttonColor={
                      !responseModel && !directResponse ? "#7c3aed" : undefined
                    }
                    textColor={
                      !responseModel && !directResponse ? "#f5e9ff" : "#89b4fa"
                    }
                    style={styles.modeButton}
                    onPress={() => {
                      controller.changeResponseTarget({ kind: "server" });
                      closeResponseModelDialog();
                    }}
                  >
                    サーバー自動
                  </Button>
                  {responseModelGroups.map((group) => {
                    const selected =
                      (Boolean(directResponse) &&
                        group.models.some(
                          (entry) =>
                            entry.kind === "direct" &&
                            directResponse?.provider === entry.provider &&
                            directResponse?.model === entry.model,
                        )) ||
                      (Boolean(responseModel) &&
                        group.models.some(
                          (entry) =>
                            entry.kind === "server" &&
                            responseModel?.provider === entry.provider &&
                            responseModel?.model === entry.model,
                        ));
                    return (
                      <Button
                        key={group.key}
                        mode="outlined"
                        textColor={selected ? "#c084fc" : "#89b4fa"}
                        style={styles.modeButton}
                        contentStyle={styles.providerButtonContent}
                        icon="chevron-right"
                        onPress={() => {
                          setModelPickerFilter("");
                          setModelPickerProvider(group.key);
                        }}
                      >
                        {group.label}
                      </Button>
                    );
                  })}
                  {!isAuthenticated ? (
                    <Text style={styles.diagnosticsText}>
                      サーバーモデルを使用するにはログインが必要です。
                    </Text>
                  ) : !controller.responseModelOptionsLoading &&
                    controller.responseModelOptions.length === 0 ? (
                    <Text style={styles.diagnosticsText}>
                      現在利用可能なサーバーモデルがありません。ローカルLLMを使用する場合はサーバー側runtimeを確認して更新してください。
                    </Text>
                  ) : null}
                </>
              )}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button
              icon="refresh"
              textColor="#89b4fa"
              onPress={() => void controller.refreshResponseModelOptions()}
              loading={controller.responseModelOptionsLoading}
              disabled={!isAuthenticated || controller.responseModelOptionsLoading}
            >
              更新
            </Button>
            <Button textColor="#a6adc8" onPress={closeResponseModelDialog}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={llmModeVisible}
          onDismiss={() => setLlmModeVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>LLM mode</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogHelp}>
              {controller.llmModeKind === "reasoning_effort"
                ? "reasoning effort を切り替えます。"
                : "現在の応答モードを切り替えます。"}
            </Text>
            {(directResponse ? directEffortOptions : controller.llmModeOptions).map((mode) => {
              const selected = directResponse
                ? mode === directResponse.reasoningEffort
                : mode === controller.llmMode;
              return (
                <Button
                  key={mode}
                  mode={selected ? "contained" : "outlined"}
                  compact
                  buttonColor={selected ? "#7c3aed" : undefined}
                  textColor={selected ? "#f5e9ff" : "#89b4fa"}
                  style={styles.modeButton}
                  onPress={() => {
                    if (directResponse) {
                      controller.changeResponseTarget({
                        kind: "direct",
                        selection: { ...directResponse, reasoningEffort: mode },
                      });
                      setLlmModeVisible(false);
                    } else {
                      void changeLlmMode(mode);
                    }
                  }}
                >
                  {directResponse ? mode : controller.llmModeLabels[mode] ?? mode}
                </Button>
              );
            })}
            {(directResponse ? directEffortOptions : controller.llmModeOptions).length === 0 ? (
              <Text style={styles.diagnosticsText}>
                サーバー接続中のみ利用できます。
              </Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              onPress={() => setLlmModeVisible(false)}
            >
              Cancel
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={Boolean(modelTarget)}
          onDismiss={closeModelTargetDialog}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {activeRerunGroup
              ? activeRerunGroup.label
              : "別モデルでこの回答を再生成"}
          </Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {controller.responseModelOptionsLoading ? (
                <Text style={styles.diagnosticsText}>
                  モデル一覧を読み込み中です。
                </Text>
              ) : null}
              {!controller.responseModelOptionsLoading &&
              rerunModelGroups.length === 0 ? (
                <Text style={styles.diagnosticsText}>
                  再生成モデルがありません。
                </Text>
              ) : null}
              {!controller.responseModelOptionsLoading && activeRerunGroup ? (
                <>
                  <Button
                    icon="chevron-left"
                    textColor="#a6adc8"
                    style={styles.modeButton}
                    onPress={() => setRerunPickerProvider(null)}
                  >
                    プロバイダー一覧へ戻る
                  </Button>
                  {activeRerunGroup.models.map((entry) => (
                    <Button
                      key={`${entry.provider}:${entry.model}`}
                      textColor="#89b4fa"
                      style={styles.modeButton}
                      onPress={() => {
                        const target = modelTarget;
                        closeModelTargetDialog();
                        if (target)
                          void controller.rerunMessage(target, {
                            provider: entry.provider,
                            model: entry.model,
                          });
                      }}
                    >
                      {entry.routeLabel
                        ? `${entry.label} · ${entry.routeLabel}`
                        : entry.label}
                    </Button>
                  ))}
                </>
              ) : null}
              {!controller.responseModelOptionsLoading && !activeRerunGroup
                ? rerunModelGroups.map((group) => (
                    <Button
                      key={group.key}
                      mode="outlined"
                      textColor="#89b4fa"
                      style={styles.modeButton}
                      contentStyle={styles.providerButtonContent}
                      icon="chevron-right"
                      onPress={() => setRerunPickerProvider(group.key)}
                    >
                      {group.label}
                    </Button>
                  ))
                : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={closeModelTargetDialog}>
              Cancel
            </Button>
          </Dialog.Actions>
        </Dialog>

      </ChatDialogHost>
    </ChatScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  contextMetric: { color: "#cdd6f4", fontSize: 13, marginTop: 8 },
  contextCategory: { color: "#a6adc8", fontSize: 12, marginTop: 4 },
  loadingContainer: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    backgroundColor: "#11111b",
  },
  statusChip: { backgroundColor: "#313244", marginRight: 6 },
  statusChipText: { color: "#cdd6f4", fontSize: 11 },
  connectionBar: {
    minHeight: 30,
    flexDirection: "row",
    flexWrap: "nowrap",
    alignItems: "center",
    paddingHorizontal: 14,
    paddingVertical: 4,
    gap: 7,
    backgroundColor: "#181825",
    borderBottomColor: "#313244",
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  connectionDot: { width: 8, height: 8, borderRadius: 4, flexGrow: 0, flexShrink: 0 },
  connectionLabel: {
    minWidth: 0,
    flexShrink: 1,
    color: "#cdd6f4",
    fontSize: 12,
    fontWeight: "700",
  },
  projectChip: {
    maxWidth: 132,
    backgroundColor: "#313244",
    height: 28,
  },
  projectChipText: { color: "#cdd6f4", fontSize: 11 },
  projectError: {
    color: "#f38ba8",
    backgroundColor: "#2d1822",
    fontSize: 12,
    paddingHorizontal: 14,
    paddingVertical: 6,
  },
  pendingBadge: {
    backgroundColor: "#453a2b",
    borderRadius: 10,
    paddingHorizontal: 8,
    paddingVertical: 3,
  },
  pendingBadgeText: { color: "#f9e2af", fontSize: 11, fontWeight: "700" },
  commandChip: { backgroundColor: "#181825", marginRight: 6 },
  commandChipText: { color: "#cdd6f4", fontSize: 11 },
  pendingRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    backgroundColor: "#181825",
    borderRadius: 8,
    padding: 10,
    marginBottom: 8,
  },
  pendingTextWrap: { flex: 1 },
  pendingTitle: { color: "#cdd6f4", fontSize: 13, fontWeight: "700" },
  pendingMeta: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
  errorBanner: {
    backgroundColor: "#2d1822",
    borderBottomColor: "#f38ba8",
    borderBottomWidth: 1,
    padding: 10,
  },
  errorText: { color: "#f38ba8", fontSize: 13 },
  messageList: { paddingHorizontal: 14, paddingTop: 16, paddingBottom: 12 },
  empty: { alignItems: "center", paddingTop: 60 },
  emptyText: { color: "#a6adc8", fontSize: 14 },
  messageWrap: { marginBottom: 8 },
  messageBubble: {
    marginBottom: 8,
    paddingHorizontal: 13,
    paddingVertical: 10,
    borderRadius: 18,
    maxWidth: "88%",
  },
  userBubble: { alignSelf: "flex-end", backgroundColor: "#2d2d3d" },
  assistantBubble: {
    alignSelf: "stretch",
    backgroundColor: "transparent",
    maxWidth: "100%",
    paddingHorizontal: 2,
  },
  userText: { color: "#cdd6f4", fontSize: 15 },
  cancelledLabel: {
    color: "#f9a8d4",
    fontSize: 12,
    fontWeight: "600",
    marginBottom: 6,
  },
  cancelledLogCard: {
    backgroundColor: "#181825",
    borderColor: "#45475a",
    borderWidth: 1,
    borderRadius: 12,
    marginBottom: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
  },
  cancelledLogTitle: {
    color: "#cba6f7",
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 7,
  },
  cancelledLogRow: {
    borderLeftColor: "#585b70",
    borderLeftWidth: 2,
    marginBottom: 8,
    paddingLeft: 9,
  },
  cancelledLogAction: { color: "#cdd6f4", fontSize: 12 },
  cancelledLogDetail: {
    color: "#a6adc8",
    fontFamily: "monospace",
    fontSize: 11,
    marginTop: 3,
  },
  branchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "flex-end",
    marginTop: -10,
  },
  branchText: { color: "#a6adc8", fontSize: 12 },
  eventCard: {
    backgroundColor: "#181825",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 10,
  },
  eventHeader: { flexDirection: "row", alignItems: "center", gap: 8 },
  eventTitle: { flex: 1, color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  eventDescription: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 18,
    marginTop: 6,
  },
  eventMeta: {
    color: "#a6adc8",
    fontSize: 12,
    lineHeight: 17,
    marginTop: 8,
    backgroundColor: "#11111b",
    padding: 8,
    borderRadius: 6,
  },
  eventChip: { backgroundColor: "#313244" },
  eventChipText: { color: "#cdd6f4", fontSize: 10 },
  dangerText: { color: "#f38ba8" },
  eventActions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: 8,
    marginTop: 10,
  },
  progress: {
    height: 6,
    borderRadius: 6,
    marginTop: 10,
    backgroundColor: "#313244",
  },
  inputBar: {
    backgroundColor: "#11111b",
    paddingHorizontal: 10,
    paddingTop: 6,
  },
  composerStatus: {
    flexDirection: "row",
    alignItems: "center",
    marginHorizontal: 48,
    marginBottom: 2,
  },
  composerStatusText: { flex: 1, color: "#a6adc8", fontSize: 11 },
  clearCommandButton: { margin: 0 },
  composerError: { color: "#f38ba8", fontSize: 12, paddingHorizontal: 6, paddingTop: 4 },
  composerSyncStatus: {
    color: "#a6adc8",
    fontSize: 11,
    paddingHorizontal: 6,
    paddingTop: 3,
  },
  composerSyncWarning: {
    color: "#f9e2af",
    fontSize: 11,
    paddingHorizontal: 6,
    paddingTop: 3,
  },
  slashSuggestions: {
    backgroundColor: "#181825",
    borderColor: "#45475a",
    borderWidth: 1,
    borderRadius: 10,
    marginBottom: 4,
    overflow: "hidden",
  },
  slashSuggestionRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 10,
    paddingVertical: 9,
    borderBottomColor: "#313244",
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  slashCommand: { color: "#c084fc", fontSize: 13, fontWeight: "700" },
  slashDescription: { flex: 1, color: "#a6adc8", fontSize: 12 },
  composerCard: {
    backgroundColor: "#1e1e2e",
    borderColor: "#45475a",
    borderWidth: 1,
    borderRadius: 24,
    overflow: "hidden",
  },
  textInput: {
    backgroundColor: "#1e1e2e",
    fontSize: 15,
    minHeight: 54,
    maxHeight: 120,
    paddingHorizontal: 8,
  },
  composerActions: {
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 4,
    paddingBottom: 4,
  },
  composerIconButton: { margin: 0 },
  composerSelector: {
    minWidth: 0,
    maxWidth: 116,
    flexShrink: 1,
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 7,
    paddingVertical: 7,
  },
  composerSelectorText: {
    minWidth: 0,
    flexShrink: 1,
    color: "#cdd6f4",
    fontSize: 12,
    fontWeight: "600",
  },
  composerSelectorChevron: { color: "#a6adc8", fontSize: 13, marginLeft: 2 },
  composerSelectorDisabled: { color: "#585b70" },
  sendButton: { margin: 2, marginLeft: "auto" },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogError: { color: "#f38ba8", fontSize: 12, marginTop: 8 },
  menuContent: { backgroundColor: "#1e1e2e" },
  dialogHelp: { color: "#a6adc8", fontSize: 13, marginBottom: 8 },
  modeButton: { marginTop: 8 },
  providerButtonContent: { flexDirection: "row-reverse", justifyContent: "space-between" },
  targetButton: { marginTop: 4, marginLeft: 16 },
  dialogScrollArea: { maxHeight: 460, borderColor: "#313244" },
  dialogScrollContent: { paddingVertical: 4 },
  modelPickerSearch: { marginBottom: 8 },
  commandRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    backgroundColor: "transparent",
  },
  commandText: { flex: 1 },
  commandSectionTitle: {
    color: "#c084fc",
    fontSize: 12,
    fontWeight: "700",
    marginTop: 4,
    marginBottom: 2,
  },
  commandTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "600" },
  commandDescription: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  quickActionContent: { justifyContent: "flex-start" },
  diagnosticsText: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  innerDivider: { backgroundColor: "#313244", marginVertical: 8 },
});
