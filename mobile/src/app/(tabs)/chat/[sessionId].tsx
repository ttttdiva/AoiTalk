import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import * as Clipboard from "expo-clipboard";
import { useLocalSearchParams, useNavigation } from "expo-router";
import Markdown from "react-native-markdown-display";
import {
  ActivityIndicator,
  Badge,
  Button,
  Chip,
  Dialog,
  Divider,
  IconButton,
  Portal,
  ProgressBar,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { useConversationController } from "../../../features/conversation/useConversationController";
import { groupMessageKey } from "../../../features/conversation/timeline";
import type { TimelineItem } from "../../../features/conversation/models";
import type { ConversationMessage } from "../../../types/api";

function compactLabel(value: string): string {
  return value.length > 18 ? `${value.slice(0, 17)}...` : value;
}

export default function ChatScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const navigation = useNavigation();
  const { user, isAuthenticated } = useAuth();
  const { selectedProjectId } = useProject();
  const flatListRef = useRef<FlatList<TimelineItem>>(null);
  const [input, setInput] = useState("");
  const includeProjectContext = Boolean(selectedProjectId);
  const [editTarget, setEditTarget] = useState<ConversationMessage | null>(
    null,
  );
  const [editContent, setEditContent] = useState("");
  const [modelTarget, setModelTarget] = useState<ConversationMessage | null>(
    null,
  );
  const [deepResearchVisible, setDeepResearchVisible] = useState(false);
  const [deepResearchQuery, setDeepResearchQuery] = useState("");
  const [commandSheetVisible, setCommandSheetVisible] = useState(false);
  const [llmModeVisible, setLlmModeVisible] = useState(false);
  const [llmModeSaving, setLlmModeSaving] = useState(false);
  const [pendingQueueVisible, setPendingQueueVisible] = useState(false);
  const [actionTarget, setActionTarget] = useState<ConversationMessage | null>(
    null,
  );

  const controller = useConversationController({
    sessionId,
    isAuthenticated,
    userRole: user?.role,
    selectedProjectId,
  });

  const messageGroups = useMemo(() => {
    const groups: Record<string, ConversationMessage[]> = {};
    for (const message of controller.messages) {
      const key = groupMessageKey(message);
      if (!groups[key]) groups[key] = [];
      groups[key].push(message);
    }
    for (const group of Object.values(groups)) {
      group.sort((a, b) => (a.branch_index ?? 0) - (b.branch_index ?? 0));
    }
    return groups;
  }, [controller.messages]);

  const sendEnabled = controller.commands.find(
    (command) => command.id === "send-message",
  )?.enabled;
  const pendingMessages = useMemo(
    () =>
      controller.messages.filter((message) =>
        Boolean(message.metadata?.pending),
      ),
    [controller.messages],
  );
  const currentLlmModeLabel = controller.llmMode
    ? controller.llmModeLabels[controller.llmMode] ?? controller.llmMode
    : "LLM";

  useEffect(() => {
    navigation.setOptions({
      title: controller.session?.title || "チャット",
    });
  }, [controller.session?.title, navigation]);

  useEffect(() => {
    setTimeout(() => flatListRef.current?.scrollToEnd({ animated: true }), 80);
  }, [controller.timeline.length]);

  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text || !sendEnabled) return;
    setInput("");
    void controller.sendConversationCommand({
      message: text,
      projectId: selectedProjectId,
      includeProjectContext,
      agentMode: "confirm",
    });
  }, [
    controller,
    includeProjectContext,
    input,
    selectedProjectId,
    sendEnabled,
  ]);

  const openEdit = useCallback((message: ConversationMessage) => {
    setEditTarget(message);
    setEditContent(message.content);
  }, []);

  const submitEdit = useCallback(() => {
    if (!editTarget || !editContent.trim()) return;
    void controller.editMessage(editTarget, editContent.trim());
    setEditTarget(null);
    setEditContent("");
  }, [controller, editContent, editTarget]);

  const copyMessage = useCallback(async (message: ConversationMessage) => {
    await Clipboard.setStringAsync(message.content);
  }, []);

  const submitDeepResearch = useCallback(() => {
    const query = deepResearchQuery.trim() || input.trim();
    if (!query) return;
    setDeepResearchVisible(false);
    setDeepResearchQuery("");
    void controller.startDeepResearch(query);
  }, [controller, deepResearchQuery, input]);

  const changeLlmMode = useCallback(
    async (mode: string) => {
      setLlmModeSaving(true);
      try {
        await controller.changeLlmMode(mode);
        setLlmModeVisible(false);
      } finally {
        setLlmModeSaving(false);
      }
    },
    [controller],
  );

  const renderMessage = (message: ConversationMessage) => {
    const key = groupMessageKey(message);
    const siblings = messageGroups[key] || [];
    const hasBranch = key !== "__root__" && siblings.length > 1;
    const currentBranchIdx = hasBranch
      ? siblings.findIndex((entry) => entry.id === message.id)
      : -1;
    return (
      <View style={styles.messageWrap}>
        <Pressable
          onLongPress={() => setActionTarget(message)}
          delayLongPress={280}
        >
          <Surface
            style={[
              styles.messageBubble,
              message.role === "user"
                ? styles.userBubble
                : styles.assistantBubble,
            ]}
            elevation={0}
          >
            {message.role === "user" ? (
              <Text style={styles.userText}>{message.content}</Text>
            ) : (
              <Markdown style={markdownStyles}>{message.content}</Markdown>
            )}
          </Surface>
        </Pressable>
        {hasBranch ? (
          <View style={styles.branchRow}>
            <IconButton
              icon="chevron-left"
              iconColor={currentBranchIdx > 0 ? "#cdd6f4" : "#585b70"}
              size={18}
              onPress={() =>
                void controller.switchBranch(message, currentBranchIdx - 1)
              }
              disabled={currentBranchIdx <= 0}
            />
            <Text style={styles.branchText}>
              {currentBranchIdx + 1}/{siblings.length}
            </Text>
            <IconButton
              icon="chevron-right"
              iconColor={
                currentBranchIdx < siblings.length - 1 ? "#cdd6f4" : "#585b70"
              }
              size={18}
              onPress={() =>
                void controller.switchBranch(message, currentBranchIdx + 1)
              }
              disabled={currentBranchIdx >= siblings.length - 1}
            />
          </View>
        ) : null}
      </View>
    );
  };

  const renderEvent = (item: TimelineItem) => {
    if (item.type === "message") return renderMessage(item.message);
    if (item.type === "stream") {
      return (
        <Surface
          style={[styles.messageBubble, styles.assistantBubble]}
          elevation={0}
        >
          <Markdown style={markdownStyles}>{item.content}</Markdown>
        </Surface>
      );
    }
    const event = item.event;
    const danger = event.severity === "danger";
    return (
      <Surface style={styles.eventCard} elevation={0}>
        <View style={styles.eventHeader}>
          <Text style={[styles.eventTitle, danger ? styles.dangerText : null]}>
            {event.title}
          </Text>
          <Chip
            compact
            style={styles.eventChip}
            textStyle={styles.eventChipText}
          >
            {event.kind}
          </Chip>
        </View>
        <Text style={styles.eventDescription}>{event.description}</Text>
        {event.kind === "permission" ? (
          <>
            <Text style={styles.eventMeta} numberOfLines={5}>
              {JSON.stringify(event.request.toolArgs, null, 2)}
            </Text>
            <View style={styles.eventActions}>
              <Button
                mode="outlined"
                compact
                textColor="#f38ba8"
                onPress={() =>
                  controller.respondPermission(event.request.requestId, false)
                }
              >
                Deny
              </Button>
              <Button
                mode="contained"
                compact
                buttonColor="#7c3aed"
                onPress={() =>
                  controller.respondPermission(event.request.requestId, true)
                }
              >
                Allow
              </Button>
            </View>
          </>
        ) : null}
        {event.kind === "job" ? (
          <>
            <ProgressBar
              progress={Math.max(0, Math.min(1, event.job.progress / 100))}
              color={danger ? "#f38ba8" : "#7c3aed"}
              style={styles.progress}
            />
            {event.job.resultText ? (
              <Text style={styles.eventMeta} numberOfLines={8}>
                {event.job.resultText}
              </Text>
            ) : null}
          </>
        ) : null}
      </Surface>
    );
  };

  if (controller.loading) {
    return (
      <View style={styles.loadingContainer}>
        <ActivityIndicator size="large" color="#7c3aed" />
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : "height"}
      keyboardVerticalOffset={Platform.OS === "ios" ? 90 : 0}
    >
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <View style={styles.headerText}>
            <Text style={styles.sessionTitle} numberOfLines={1}>
              {controller.session?.title || "チャット"}
            </Text>
            <Text style={styles.sessionSubtitle} numberOfLines={1}>
              {controller.diagnostics.connectionCapability}
            </Text>
          </View>
          {pendingMessages.length > 0 ? (
            <View style={styles.pendingBadgeWrap}>
              <IconButton
                icon="cloud-upload-outline"
                iconColor="#f9e2af"
                size={20}
                onPress={() => setPendingQueueVisible(true)}
                accessibilityLabel="未送信キューを開く"
              />
              <Badge style={styles.pendingBadge}>{pendingMessages.length}</Badge>
            </View>
          ) : null}
          <Chip
            compact
            icon="tune-variant"
            disabled={controller.llmModeOptions.length === 0}
            style={styles.llmModeChip}
            textStyle={styles.llmModeChipText}
            onPress={() => setLlmModeVisible(true)}
          >
            {compactLabel(currentLlmModeLabel)}
          </Chip>
        </View>
      </Surface>

      {controller.error ? (
        <Surface style={styles.errorBanner} elevation={0}>
          <Text style={styles.errorText}>{controller.error}</Text>
          <Button
            compact
            textColor="#89b4fa"
            onPress={() => void controller.load()}
          >
            再読み込み
          </Button>
        </Surface>
      ) : null}

      <FlatList
        ref={flatListRef}
        data={controller.timeline}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => renderEvent(item)}
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={styles.messageList}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyText}>
              メッセージを送信して会話を開始します。
            </Text>
          </View>
        }
      />

      <Surface style={styles.inputBar} elevation={2}>
        <View style={styles.inputRow}>
          <IconButton
            icon="plus"
            iconColor="#a6adc8"
            onPress={() => setCommandSheetVisible(true)}
          />
          <TextInput
            value={input}
            onChangeText={setInput}
            placeholder="メッセージを入力..."
            placeholderTextColor="#585b70"
            style={styles.textInput}
            mode="outlined"
            outlineStyle={styles.textInputOutline}
            multiline
            maxLength={4000}
            disabled={!sendEnabled}
            onSubmitEditing={handleSend}
            blurOnSubmit={false}
          />
          <IconButton
            icon="send"
            iconColor={input.trim() && sendEnabled ? "#7c3aed" : "#585b70"}
            size={24}
            onPress={handleSend}
            disabled={!input.trim() || !sendEnabled}
          />
        </View>
      </Surface>

      <Portal>
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
                      {String(message.metadata?.error || "再送待ち")}
                    </Text>
                  </View>
                  <Button
                    compact
                    icon="refresh"
                    textColor="#89b4fa"
                    onPress={() => {
                      setPendingQueueVisible(false);
                      void controller.retryPendingMessage(message);
                    }}
                  >
                    Retry
                  </Button>
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
            {controller.llmModeOptions.map((mode) => {
              const selected = mode === controller.llmMode;
              return (
                <Button
                  key={mode}
                  mode={selected ? "contained" : "outlined"}
                  compact
                  buttonColor={selected ? "#7c3aed" : undefined}
                  textColor={selected ? "#f5e9ff" : "#89b4fa"}
                  disabled={llmModeSaving}
                  style={styles.modeButton}
                  onPress={() => void changeLlmMode(mode)}
                >
                  {controller.llmModeLabels[mode] ?? mode}
                </Button>
              );
            })}
            {controller.llmModeOptions.length === 0 ? (
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
          visible={Boolean(actionTarget)}
          onDismiss={() => setActionTarget(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>メッセージ操作</Dialog.Title>
          <Dialog.Content>
            {(() => {
              if (!actionTarget) return null;
              const pending = Boolean(actionTarget.metadata?.pending);
              const canServerAction =
                isAuthenticated &&
                !String(actionTarget.id).startsWith("temp-") &&
                !pending;
              const canEdit = canServerAction && actionTarget.role === "user";
              const canModelRerun =
                canServerAction && actionTarget.role === "assistant";
              return (
                <>
                  <Button
                    icon="content-copy"
                    textColor="#cdd6f4"
                    onPress={() => {
                      const target = actionTarget;
                      setActionTarget(null);
                      if (target) void copyMessage(target);
                    }}
                  >
                    コピー
                  </Button>
                  {pending && isAuthenticated ? (
                    <Button
                      icon="refresh"
                      textColor="#89b4fa"
                      onPress={() => {
                        const target = actionTarget;
                        setActionTarget(null);
                        if (target) void controller.retryPendingMessage(target);
                      }}
                    >
                      リトライ
                    </Button>
                  ) : null}
                  {canEdit ? (
                    <Button
                      icon="pencil"
                      textColor="#cdd6f4"
                      onPress={() => {
                        const target = actionTarget;
                        setActionTarget(null);
                        if (target) openEdit(target);
                      }}
                    >
                      編集
                    </Button>
                  ) : null}
                  {canServerAction ? (
                    <>
                      <Button
                        icon="replay"
                        textColor="#89b4fa"
                      onPress={() => {
                        const target = actionTarget;
                        setActionTarget(null);
                        if (target) void controller.rerunMessage(target);
                      }}
                    >
                        {actionTarget.role === "assistant"
                          ? "この回答を再生成"
                          : "再実行"}
                      </Button>
                      {canModelRerun ? (
                        <Button
                          icon="tune"
                          textColor="#89b4fa"
                          onPress={() => {
                            const target = actionTarget;
                            setActionTarget(null);
                            setModelTarget(target);
                          }}
                        >
                          別モデルでこの回答を再生成
                        </Button>
                      ) : null}
                      <Button
                        icon="source-branch"
                        textColor="#89b4fa"
                        onPress={() => {
                          const target = actionTarget;
                          setActionTarget(null);
                          if (target) void controller.loadBranches(target.id);
                        }}
                      >
                        分岐を読み込み
                      </Button>
                    </>
                  ) : null}
                </>
              );
            })()}
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setActionTarget(null)}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={Boolean(editTarget)}
          onDismiss={() => setEditTarget(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Edit Message</Dialog.Title>
          <Dialog.Content>
            <TextInput
              value={editContent}
              onChangeText={setEditContent}
              mode="outlined"
              multiline
              style={styles.editInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setEditTarget(null)}>
              Cancel
            </Button>
            <Button
              textColor="#7c3aed"
              onPress={submitEdit}
              disabled={!editContent.trim()}
            >
              Save
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={Boolean(modelTarget)}
          onDismiss={() => setModelTarget(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            別モデルでこの回答を再生成
          </Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {controller.responseModelOptionsLoading ? (
                <Text style={styles.diagnosticsText}>
                  モデル一覧を読み込み中です。
                </Text>
              ) : null}
              {!controller.responseModelOptionsLoading &&
              controller.responseModelOptions.length === 0 ? (
                <Text style={styles.diagnosticsText}>
                  再生成モデルがありません。
                </Text>
              ) : null}
              {!controller.responseModelOptionsLoading
                ? controller.responseModelOptions.map((option) => (
                    <Button
                      key={`${option.provider}:${option.model}`}
                      textColor="#89b4fa"
                      onPress={() => {
                        const target = modelTarget;
                        setModelTarget(null);
                        if (target) void controller.rerunMessage(target, option);
                      }}
                    >
                      {option.label}
                    </Button>
                  ))
                : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setModelTarget(null)}>
              Cancel
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={deepResearchVisible}
          onDismiss={() => setDeepResearchVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Deep Research</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogHelp}>
              長時間ジョブとして開始し、進捗はタイムラインに表示します。
            </Text>
            <TextInput
              value={deepResearchQuery}
              onChangeText={setDeepResearchQuery}
              mode="outlined"
              multiline
              placeholder={input.trim() || "調査したい内容"}
              style={styles.editInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              onPress={() => setDeepResearchVisible(false)}
            >
              Cancel
            </Button>
            <Button textColor="#7c3aed" onPress={submitDeepResearch}>
              Start
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={commandSheetVisible}
          onDismiss={() => setCommandSheetVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Commands</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              {controller.commands.map((command) => (
                <Surface
                  key={command.id}
                  style={styles.commandRow}
                  elevation={0}
                >
                  <View style={styles.commandText}>
                    <Text style={styles.commandTitle}>{command.label}</Text>
                    <Text style={styles.commandDescription}>
                      {command.enabled ? command.description : command.reason}
                    </Text>
                  </View>
                  <Chip
                    compact
                    disabled={!command.enabled}
                    style={styles.commandChip}
                  >
                    {command.category}
                  </Chip>
                </Surface>
              ))}
              <Divider style={styles.innerDivider} />
              <Text style={styles.diagnosticsText}>
                user={controller.diagnostics.userCapability} / connection=
                {controller.diagnostics.connectionCapability} / pending=
                {controller.diagnostics.pendingMessages}
              </Text>
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button
              textColor="#a6adc8"
              onPress={() => setCommandSheetVisible(false)}
            >
              Close
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  loadingContainer: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    backgroundColor: "#11111b",
  },
  header: {
    backgroundColor: "#1e1e2e",
    paddingHorizontal: 12,
    paddingTop: 8,
    paddingBottom: 8,
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerText: { flex: 1 },
  sessionTitle: { color: "#cdd6f4", fontSize: 17, fontWeight: "700" },
  sessionSubtitle: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  pendingBadgeWrap: { position: "relative", marginRight: 2 },
  pendingBadge: {
    position: "absolute",
    right: 2,
    top: 2,
    backgroundColor: "#f38ba8",
  },
  llmModeChip: {
    maxWidth: 132,
    backgroundColor: "#181825",
    marginRight: 2,
  },
  llmModeChipText: { color: "#cdd6f4", fontSize: 11 },
  statusChip: { backgroundColor: "#313244", marginRight: 6 },
  statusChipText: { color: "#cdd6f4", fontSize: 11 },
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
  messageList: { padding: 12, paddingBottom: 8 },
  empty: { alignItems: "center", paddingTop: 60 },
  emptyText: { color: "#a6adc8", fontSize: 14 },
  messageWrap: { marginBottom: 8 },
  messageBubble: {
    marginBottom: 8,
    padding: 12,
    borderRadius: 8,
    maxWidth: "88%",
  },
  userBubble: { alignSelf: "flex-end", backgroundColor: "#3b236a" },
  assistantBubble: { alignSelf: "flex-start", backgroundColor: "#1e1e2e" },
  userText: { color: "#cdd6f4", fontSize: 15 },
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
    backgroundColor: "#1e1e2e",
    paddingHorizontal: 8,
    paddingVertical: 4,
    paddingBottom: Platform.OS === "ios" ? 24 : 4,
  },
  inputRow: { flexDirection: "row", alignItems: "flex-end" },
  textInput: {
    flex: 1,
    backgroundColor: "#313244",
    fontSize: 15,
    maxHeight: 120,
  },
  textInputOutline: { borderColor: "#585b70", borderRadius: 20 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogHelp: { color: "#a6adc8", fontSize: 13, marginBottom: 8 },
  modeButton: { marginTop: 8 },
  editInput: { backgroundColor: "#313244", marginTop: 8 },
  dialogScrollArea: { maxHeight: 460, borderColor: "#313244" },
  dialogScrollContent: { paddingVertical: 4 },
  commandRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingVertical: 10,
    backgroundColor: "transparent",
  },
  commandText: { flex: 1 },
  commandTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "600" },
  commandDescription: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  diagnosticsText: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  innerDivider: { backgroundColor: "#313244", marginVertical: 8 },
});

const markdownStyles = {
  body: { color: "#cdd6f4", fontSize: 15 },
  heading1: { color: "#cdd6f4", fontWeight: "bold" as const },
  heading2: { color: "#cdd6f4", fontWeight: "bold" as const },
  heading3: { color: "#cdd6f4", fontWeight: "bold" as const },
  code_inline: { backgroundColor: "#313244", color: "#a6e3a1", padding: 2 },
  code_block: {
    backgroundColor: "#313244",
    color: "#a6e3a1",
    padding: 8,
    borderRadius: 6,
  },
  fence: {
    backgroundColor: "#313244",
    color: "#a6e3a1",
    padding: 8,
    borderRadius: 6,
  },
  link: { color: "#89b4fa" },
  blockquote: {
    borderLeftColor: "#7c3aed",
    borderLeftWidth: 3,
    paddingLeft: 8,
  },
  bullet_list_icon: { color: "#7c3aed" },
  ordered_list_icon: { color: "#7c3aed" },
};
