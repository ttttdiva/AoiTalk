import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, FlatList, Keyboard, ScrollView, StyleSheet, View } from "react-native";
import * as Clipboard from "expo-clipboard";
import { Button, Dialog, IconButton, Surface, Text, TextInput } from "react-native-paper";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useConversationController } from "./useConversationController";
import type { ConversationRuntimeFocus } from "./useConversationRuntime";
import { useConversationDurableTimeline } from "./useConversationTimeline";
import type { TimelineItem } from "./models";
import { ChatComposer, type ChatComposerSubmission } from "./components/ChatComposer";
import { ChatHeaderStatus } from "./components/ChatHeaderStatus";
import { ChatScreenShell } from "./components/ChatScreenShell";
import { ChatTimeline } from "./components/ChatTimeline";
import { ChatTimelineFrame } from "./components/ChatTimelineFrame";
import { ChatDialogHost } from "./components/ChatDialogHost";
import type { OperationsWorkReference } from "../../lib/operations-api";
import { useReducedMotion } from "../ui/use-reduced-motion";
import { useProject } from "../../contexts/ProjectContext";
import { ScopeSwitcher } from "../../components/scope-switcher";
import type { ConversationMessage } from "../../types/api";
import { getPendingMessageRetryRoute } from "./message-actions";
import type { ChatResponseModelSelection } from "../../types/api";

export type ConversationSurfaceProps = {
  sessionId: string;
  isAuthenticated: boolean;
  userId?: string | null;
  userRole?: string | null;
  selectedProjectId?: string | null;
  /** AoiDots project context hint; the root session remains projectless. */
  contextProjectHintId?: string | null;
  runtimeFocus?: ConversationRuntimeFocus | null;
  /** Kept explicit so root overlays and route wrappers cannot infer focus from mount state. */
  active?: boolean;
  embedded?: boolean;
  onClose?: () => void;
  onOpenWork?: (reference: OperationsWorkReference) => void;
  onSessionPromoted?: (sessionId: string) => void;
  onProjectHintChange?: (projectId: string | null) => void;
  /** Optional content rendered immediately below an embedded surface header. */
  embeddedHeaderAccessory?: React.ReactNode;
};

/**
 * Shared conversation body for root-mounted AoiDots and explicit route wrappers.
 * Controller state is deliberately created inside each surface instance, so
 * two mounted conversations never share a socket, generation gate or queue.
 */
export function ConversationSurface({
  sessionId,
  isAuthenticated,
  userId,
  userRole,
  selectedProjectId = null,
  contextProjectHintId = null,
  runtimeFocus,
  active = true,
  embedded = false,
  onClose,
  onOpenWork,
  onSessionPromoted,
  onProjectHintChange,
  embeddedHeaderAccessory,
}: ConversationSurfaceProps) {
  const insets = useSafeAreaInsets();
  const { projects } = useProject();
  const router = useRouter();
  const reduceMotion = useReducedMotion();
  const flatListRef = useRef<FlatList<TimelineItem>>(null);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [keyboardVisible, setKeyboardVisible] = useState(false);
  const [actionTargetId, setActionTargetId] = useState<string | null>(null);
  const [editTarget, setEditTarget] = useState<ConversationMessage | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [pendingQueueVisible, setPendingQueueVisible] = useState(false);
  const [responseModelVisible, setResponseModelVisible] = useState(false);
  const [llmModeVisible, setLlmModeVisible] = useState(false);
  const controller = useConversationController({
    sessionId,
    isAuthenticated,
    userId,
    userRole,
    selectedProjectId,
    runtimeFocus,
    onSessionPromoted,
    disableLlmPreferencePersistence: embedded,
  });

  const durableTimeline = useConversationDurableTimeline({
    messages: controller.visibleMessages,
    permissions: controller.pendingPermissions,
    jobs: controller.jobs,
    activeTool: controller.diagnostics.activeTool,
    activityMessage: controller.diagnostics.activityMessage,
  });

  // An embedded AoiDots root treats null as an explicit projectless hint;
  // never fall through to the page's global project in that mode.
  const currentProjectId = embedded
    ? contextProjectHintId
    : controller.session?.project_id ?? selectedProjectId;
  const pendingCount = controller.messages.filter(
    (message) =>
      message.role === "user" &&
      Boolean(message.metadata?.local_only) &&
      (Boolean(message.metadata?.pending) ||
        Boolean(message.metadata?.persistence_error) ||
        message.metadata?.message_state === "direct-failed"),
  ).length;
  const sendEnabled = controller.commands.find((command) => command.id === "send-message")?.enabled;
  const pendingMessages = controller.messages.filter(
    (message) =>
      message.role === "user" &&
      Boolean(message.metadata?.local_only) &&
      (Boolean(message.metadata?.pending) ||
        Boolean(message.metadata?.persistence_error) ||
        message.metadata?.message_state === "direct-failed"),
  );
  const currentResponseModel = controller.responseTarget.kind === "server"
    ? controller.responseTarget.responseModel
    : null;
  const currentResponseModelLabel = currentResponseModel
    ? `${currentResponseModel.provider} / ${currentResponseModel.model}`
    : "サーバー自動";
  const currentLlmModeLabel = controller.llmMode
    ? controller.llmModeLabels[controller.llmMode] ?? controller.llmMode
    : "標準";

  const scheduleScrollToEnd = useCallback((delay = 48) => {
    if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    scrollTimerRef.current = setTimeout(() => {
      scrollTimerRef.current = null;
      flatListRef.current?.scrollToEnd({ animated: !reduceMotion });
    }, delay);
  }, [reduceMotion]);

  useEffect(() => {
    scheduleScrollToEnd();
    return () => {
      if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    };
  }, [durableTimeline.length, Boolean(controller.streamContent), scheduleScrollToEnd]);

  useEffect(() => {
    const show = Keyboard.addListener("keyboardDidShow", () => {
      setKeyboardVisible(true);
      scheduleScrollToEnd(64);
    });
    const hide = Keyboard.addListener("keyboardDidHide", () => setKeyboardVisible(false));
    return () => {
      show.remove();
      hide.remove();
    };
  }, [scheduleScrollToEnd]);

  const sendMessage = useCallback(
    (submission: ChatComposerSubmission) =>
      controller.sendConversationCommand({
        message: submission.content,
        submissionId: submission.submissionId,
        // AoiDots root is always projectless. This field is only a context
        // hint and is persisted with the pending dispatch for reconnect.
        projectId: controller.session?.project_id ?? null,
        contextProjectHintId: contextProjectHintId ?? null,
        includeProjectContext: Boolean(contextProjectHintId),
        agentMode: "confirm",
        target: { kind: "server" },
        commandCapabilities: submission.capabilities,
      }),
    [contextProjectHintId, controller, controller.session?.project_id],
  );

  const changeProject = useCallback(
    async (projectId: string | null) => {
      if (onProjectHintChange) {
        onProjectHintChange(projectId);
        return;
      }
      await controller.changeProject(projectId);
    },
    [controller.changeProject, onProjectHintChange],
  );

  const openWork = useCallback(
    (reference: OperationsWorkReference) => onOpenWork?.(reference),
    [onOpenWork],
  );
  const beginEdit = useCallback((message: ConversationMessage) => {
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
  }, [editSaving]);
  const submitEdit = useCallback(async () => {
    if (!editTarget || !editContent.trim() || editSaving) return;
    setEditSaving(true);
    setEditError(null);
    try {
      await controller.editMessage(editTarget, editContent.trim());
      cancelEdit();
    } catch (error) {
      setEditError(error instanceof Error ? error.message : "編集に失敗しました。");
    } finally {
      setEditSaving(false);
    }
  }, [cancelEdit, controller, editContent, editSaving, editTarget]);
  const retryMessage = useCallback((message: ConversationMessage) => {
    void controller.retryPendingMessage(message, getPendingMessageRetryRoute(message));
  }, [controller]);
  const copyMessage = useCallback((message: ConversationMessage) => {
    void Clipboard.setStringAsync(message.content);
  }, []);
  const forkMessage = useCallback((message: ConversationMessage) => {
    void controller.forkConversation(message.id)
      .then((sessionId) => {
        onClose?.();
        router.push({ pathname: "/(tabs)/chat/[sessionId]", params: { sessionId } });
      })
      .catch((error) => Alert.alert("Chat", error instanceof Error ? error.message : "会話のフォークに失敗しました。"));
  }, [controller, onClose, router]);
  const saveResponseModel = useCallback((selection: ChatResponseModelSelection | undefined) => {
    controller.changeResponseTarget({ kind: "server", ...(selection ? { responseModel: selection } : {}) });
    setResponseModelVisible(false);
  }, [controller]);

  const header = embedded ? (
    <>
      <Surface testID="aoidots-embedded-header" style={styles.embeddedHeader} elevation={1}>
        <IconButton
          icon="close"
          iconColor="#cdd6f4"
          size={20}
          style={styles.embeddedClose}
          onPress={onClose}
          accessibilityLabel="AoiDotsを閉じる"
        />
        <View style={styles.embeddedHeaderCopy}>
          <Text style={styles.embeddedTitle}>AoiDots</Text>
          <View style={styles.embeddedProject}>
            <ScopeSwitcher
              variant="inline"
              projectNameOnly
              projects={projects}
              projectId={contextProjectHintId}
              onSelectProject={changeProject}
              allLabel="プロジェクト未選択"
              accessibilityLabel="AoiDotsのプロジェクト文脈を選択"
              disabled={controller.diagnostics.runState !== "idle" || pendingCount > 0}
            />
          </View>
        </View>
        <Text style={styles.embeddedStatus}>
          {controller.diagnostics.runState === "idle" ? "待機中" : "実行中"}
        </Text>
      </Surface>
      {embeddedHeaderAccessory}
    </>
  ) : (
    <ChatHeaderStatus
      diagnostics={controller.diagnostics}
      session={controller.session}
      projects={projects}
      currentProjectId={currentProjectId}
      pendingCount={pendingCount}
      onChangeProject={changeProject}
      onChangeCharacter={controller.changeCharacter}
      onOpenPendingQueue={() => setPendingQueueVisible(true)}
      onBack={onClose}
    />
  );

  const loading = controller.loading && controller.messages.length === 0 && !controller.session;
  return (
    <ChatScreenShell loading={loading} error={controller.error} onReload={() => void controller.load()} header={header}>
      <ChatTimelineFrame
        topInset={embedded ? 0 : insets.top}
        active={active}
        character={controller.session?.character_name ?? "aoidots"}
        state={controller.diagnostics.runState !== "idle" ? "thinking" : "idle"}
        messages={controller.messages}
        historyReady={!controller.loading}
        sessionKey={sessionId}
      >
        <ChatTimeline
          listRef={flatListRef}
          items={durableTimeline}
          allMessages={controller.messages}
          streamContent={controller.streamContent}
          reduceMotion={reduceMotion}
          agentRuns={controller.agentRuns}
          agentRunErrors={controller.agentRunErrors}
          onLongPressMessage={(message) => setActionTargetId(message.id)}
          onPressMessage={() => setActionTargetId(null)}
          onSwitchBranch={(message, nextIndex) => void controller.switchBranch(message, nextIndex)}
          onRetryAgentRun={controller.retryAgentRun}
          onOpenWork={openWork}
          onRespondPermission={controller.respondPermission}
          actionTargetId={actionTargetId}
          editTargetId={editTarget?.id ?? null}
          editContent={editContent}
          editSaving={editSaving}
          editError={editError}
          isAuthenticated={isAuthenticated}
          retryingMessageIds={controller.retryingMessageIds}
          onDismissMessageActions={() => setActionTargetId(null)}
          onBeginEdit={beginEdit}
          onEditContentChange={setEditContent}
          onCancelEdit={cancelEdit}
          onSubmitEdit={() => void submitEdit()}
          onRetryMessage={retryMessage}
          onRerunMessage={(message) => void controller.rerunMessage(message)}
          onCopyMessage={copyMessage}
          onForkMessage={forkMessage}
        />
      </ChatTimelineFrame>
      <ChatComposer
        voiceContext={{
          conversationKey: sessionId,
          sessionId,
          sessionIsLocalOnly: controller.session?.is_local_only === true,
          projectId: currentProjectId,
          characterName: controller.session?.character_name ?? "aoidots",
          userId: userId ?? null,
          messages: controller.visibleMessages,
          busy: controller.diagnostics.runState !== "idle",
        }}
        bottomInset={insets.bottom}
        keyboardVisible={keyboardVisible}
        sendEnabled={Boolean(active && sendEnabled)}
        directResponseActive={false}
        currentResponseModelLabel={currentResponseModelLabel}
        currentLlmModeLabel={currentLlmModeLabel}
        effortEnabled={controller.llmModeOptions.length > 0}
        llmSelectionMessage={controller.llmSelectionMessage}
        llmSyncLabel={null}
        skillCommands={controller.skillCommands}
        isAuthenticated={isAuthenticated}
        pendingCount={pendingMessages.length}
        onSend={sendMessage}
        onOpenResponseModel={() => {
          setResponseModelVisible(true);
          void controller.refreshResponseModelOptions();
        }}
        onOpenLlmMode={() => setLlmModeVisible(true)}
        onOpenPendingQueue={() => setPendingQueueVisible(true)}
        onStartDeepResearch={controller.startDeepResearch}
        hideModelSelector={embedded}
        hideEffortSelector={embedded}
      />
      <ChatDialogHost>
        <Dialog visible={pendingQueueVisible} onDismiss={() => setPendingQueueVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>未送信メッセージ</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {pendingMessages.length === 0 ? <Text style={styles.dialogText}>未送信メッセージはありません。</Text> : pendingMessages.map((message) => (
                <Surface key={message.id} style={styles.pendingRow} elevation={0}>
                  <Text style={styles.pendingText} numberOfLines={2}>{message.content}</Text>
                  <Button compact textColor="#89b4fa" onPress={() => retryMessage(message)}>再送</Button>
                  <Button compact textColor="#f9e2af" onPress={() => void controller.cancelPendingMessage(message, "cancel")}>取消</Button>
                  <Button compact textColor="#cdd6f4" onPress={() => copyMessage(message)}>コピー</Button>
                </Surface>
              ))}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions><Button onPress={() => setPendingQueueVisible(false)}>閉じる</Button></Dialog.Actions>
        </Dialog>
        <Dialog visible={responseModelVisible} onDismiss={() => setResponseModelVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>応答モデル</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <Button mode={!currentResponseModel ? "contained" : "outlined"} onPress={() => saveResponseModel(undefined)}>サーバー自動</Button>
              {controller.responseModelOptions.map((option) => (
                <Button
                  key={`${option.provider}:${option.model}`}
                  mode={currentResponseModel?.provider === option.provider && currentResponseModel?.model === option.model ? "contained" : "outlined"}
                  onPress={() => saveResponseModel({ provider: option.provider, model: option.model })}
                >
                  {option.modelLabel ?? `${option.provider} / ${option.model}`}
                </Button>
              ))}
              {controller.responseModelOptions.length === 0 ? <Text style={styles.dialogText}>利用可能なServerモデルがありません。</Text> : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions><Button onPress={() => setResponseModelVisible(false)}>閉じる</Button></Dialog.Actions>
        </Dialog>
        <Dialog visible={llmModeVisible} onDismiss={() => setLlmModeVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>Effort</Dialog.Title>
          <Dialog.Content>
            {controller.llmModeOptions.map((mode) => (
              <Button key={mode} mode={controller.llmMode === mode ? "contained" : "outlined"} onPress={() => { controller.changeLlmMode(mode); setLlmModeVisible(false); }}>
                {controller.llmModeLabels[mode] ?? mode}
              </Button>
            ))}
          </Dialog.Content>
        </Dialog>
        <Dialog visible={Boolean(editTarget)} onDismiss={cancelEdit} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>メッセージを編集</Dialog.Title>
          <Dialog.Content>
            <TextInput mode="outlined" multiline value={editContent} onChangeText={setEditContent} disabled={editSaving} />
            {editError ? <Text style={styles.dialogError}>{editError}</Text> : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={cancelEdit} disabled={editSaving}>キャンセル</Button>
            <Button onPress={() => void submitEdit()} loading={editSaving} disabled={editSaving || !editContent.trim()}>保存</Button>
          </Dialog.Actions>
        </Dialog>
      </ChatDialogHost>
    </ChatScreenShell>
  );
}

const styles = StyleSheet.create({
  embeddedHeader: {
    minHeight: 50,
    flexDirection: "row",
    alignItems: "flex-start",
    paddingHorizontal: 4,
    paddingVertical: 3,
    backgroundColor: "#1e1e2e",
    borderBottomColor: "#313244",
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  embeddedClose: { width: 44, height: 44, margin: 0 },
  embeddedHeaderCopy: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "flex-start", flexWrap: "wrap", gap: 4, paddingHorizontal: 2 },
  embeddedTitle: { color: "#cdd6f4", fontSize: 15, fontWeight: "700", flexShrink: 1 },
  embeddedProject: { flex: 1, minWidth: 0, flexShrink: 1 },
  embeddedStatus: { color: "#89b4fa", fontSize: 10, flexShrink: 1, minWidth: 0, paddingHorizontal: 4, paddingVertical: 6 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogText: { color: "#a6adc8", paddingVertical: 8 },
  dialogError: { color: "#f38ba8", marginTop: 8 },
  dialogScrollArea: { maxHeight: 460, borderColor: "#313244" },
  dialogScrollContent: { paddingVertical: 4, gap: 6 },
  pendingRow: { backgroundColor: "#181825", padding: 8, borderRadius: 8 },
  pendingText: { color: "#cdd6f4", marginBottom: 4 },
});
