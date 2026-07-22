import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  FlatList,
  Keyboard,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import * as Clipboard from "expo-clipboard";
import { useLocalSearchParams, useNavigation, useRouter } from "expo-router";
import { KeyboardStickyView } from "react-native-keyboard-controller";
import Markdown from "react-native-markdown-display";
import {
  ActivityIndicator,
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
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { useConversationController } from "../../../features/conversation/useConversationController";
import { groupMessageKey } from "../../../features/conversation/timeline";
import type { TimelineItem } from "../../../features/conversation/models";
import type { ConversationMessage } from "../../../types/api";
import type { ChatResponseModelSelection } from "../../../types/api";
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
  filterSlashCommands,
  MOBILE_CHAT_COMMANDS,
  resolveMobileCommandSubmission,
  type MobileChatCommand,
  type SkillSlashCommand,
} from "../../../features/conversation/chat-commands";

function compactLabel(value: string): string {
  return value.length > 18 ? `${value.slice(0, 17)}...` : value;
}

type DirectModelOption = DirectMobileLlmSelection & { label: string };

export default function ChatScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const navigation = useNavigation();
  const router = useRouter();
  const insets = useSafeAreaInsets();
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
  const [responseModelVisible, setResponseModelVisible] = useState(false);
  const [responseModel, setResponseModel] =
    useState<ChatResponseModelSelection | null>(null);
  const [directResponse, setDirectResponse] =
    useState<DirectMobileLlmSelection | null>(null);
  const [directModelOptions, setDirectModelOptions] = useState<DirectModelOption[]>([]);
  const [activeCommand, setActiveCommand] = useState<MobileChatCommand | null>(null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [pendingQueueVisible, setPendingQueueVisible] = useState(false);
  const [keyboardVisible, setKeyboardVisible] = useState(false);
  const [actionTarget, setActionTarget] = useState<ConversationMessage | null>(
    null,
  );

  const handleSessionPromoted = useCallback(
    (remoteSessionId: string) => {
      router.replace({
        pathname: "/(tabs)/chat/[sessionId]",
        params: { sessionId: remoteSessionId },
      });
    },
    [router],
  );

  const controller = useConversationController({
    sessionId,
    isAuthenticated,
    userRole: user?.role,
    selectedProjectId,
    onSessionPromoted: handleSessionPromoted,
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
  const directEffortOptions = directResponse
    ? getDirectReasoningEffortOptions(directResponse.provider, directResponse.model)
    : [];
  const currentLlmModeLabel = directResponse
    ? directResponse.reasoningEffort || directEffortOptions[0] || "標準"
    : controller.llmMode
      ? controller.llmModeLabels[controller.llmMode] ?? controller.llmMode
      : "LLM";
  const currentResponseModelLabel = useMemo(() => {
    if (directResponse) {
      return `Direct · ${getProviderLabel(directResponse.provider)} / ${directResponse.model}`;
    }
    const option = responseModel
      ? controller.responseModelOptions.find(
          (item) =>
            item.provider === responseModel.provider &&
            item.model === responseModel.model,
        )
      : controller.responseModelOptions.find((item) => item.isCurrent);
    return option?.modelLabel ?? responseModel?.model ?? "自動";
  }, [controller.responseModelOptions, directResponse, responseModel]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [main, fallback] = await Promise.all([getMainSlot(), getFallbackConfig()]);
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
          models.slice(0, 30).map((model) => ({
            provider,
            model,
            label: `Direct · ${getProviderLabel(provider)} / ${model}`,
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
  const slashQuery = /^\/[^\s\n]*$/.test(input) ? input : null;
  const slashSuggestions = useMemo(() => {
    if (!slashQuery) return [];
    const builtIns = filterSlashCommands(MOBILE_CHAT_COMMANDS, slashQuery).map((command) => ({
      kind: "command" as const,
      command,
    }));
    const skills = filterSlashCommands(controller.skillCommands, slashQuery).map((command) => ({
      kind: "skill" as const,
      command,
    }));
    return [...builtIns, ...skills].slice(0, 6);
  }, [controller.skillCommands, slashQuery]);

  useEffect(() => {
    navigation.setOptions({
      title: controller.session?.title || "チャット",
    });
  }, [controller.session?.title, navigation]);

  useEffect(() => {
    setTimeout(() => flatListRef.current?.scrollToEnd({ animated: true }), 80);
  }, [controller.timeline.length]);

  useEffect(() => {
    const showSubscription = Keyboard.addListener("keyboardDidShow", () => {
      setKeyboardVisible(true);
      setTimeout(() => flatListRef.current?.scrollToEnd({ animated: true }), 80);
    });
    const hideSubscription = Keyboard.addListener("keyboardDidHide", () => {
      setKeyboardVisible(false);
    });
    return () => {
      showSubscription.remove();
      hideSubscription.remove();
    };
  }, []);

  const selectBuiltInCommand = useCallback((command: MobileChatCommand) => {
    setActiveCommand(command);
    setComposerError(null);
    setInput((value) => (/^\/[^\s\n]*$/.test(value) ? "" : value));
    setCommandSheetVisible(false);
  }, []);

  const selectSkillCommand = useCallback((command: SkillSlashCommand) => {
    setActiveCommand(null);
    setComposerError(null);
    setInput(`${command.command} `);
    setCommandSheetVisible(false);
  }, []);

  const handleSend = useCallback(() => {
    const submission = resolveMobileCommandSubmission(input, activeCommand);
    if (submission.error) {
      setComposerError(submission.error);
      return;
    }
    const text = submission.content;
    if (!text || !sendEnabled) return;
    if (
      directResponse &&
      (Boolean(submission.capabilities?.length) || text.startsWith("/"))
    ) {
      setComposerError("組み込みコマンドとSkillsはServerモデルで実行してください。");
      return;
    }
    setInput("");
    setActiveCommand(null);
    setComposerError(null);
    void controller.sendConversationCommand({
      message: text,
      projectId: selectedProjectId,
      includeProjectContext,
      agentMode: "confirm",
      target: directResponse
        ? { kind: "direct", selection: directResponse }
        : { kind: "server", responseModel: responseModel ?? undefined },
      commandCapabilities: submission.capabilities,
    });
  }, [
    activeCommand,
    controller,
    includeProjectContext,
    input,
    selectedProjectId,
    sendEnabled,
    responseModel,
    directResponse,
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
      setComposerError(null);
      try {
        await controller.changeLlmMode(mode);
        setLlmModeVisible(false);
      } catch (error) {
        setComposerError(
          error instanceof Error ? error.message : "Effortの変更に失敗しました。",
        );
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
    <View style={styles.container}>
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

      <KeyboardStickyView offset={{ closed: 0, opened: 0 }}>
        <View
          style={[
            styles.inputBar,
            {
              paddingBottom: keyboardVisible ? 4 : Math.max(4, insets.bottom),
            },
          ]}
        >
        {slashSuggestions.length > 0 ? (
          <Surface style={styles.slashSuggestions} elevation={3}>
            {slashSuggestions.map((item) => (
              <Pressable
                key={`${item.kind}-${item.command.command}`}
                style={styles.slashSuggestionRow}
                onPress={() =>
                  item.kind === "command"
                    ? selectBuiltInCommand(item.command)
                    : selectSkillCommand(item.command)
                }
              >
                <Text style={styles.slashCommand}>{item.command.command}</Text>
                <Text style={styles.slashDescription} numberOfLines={1}>
                  {item.command.description}
                </Text>
              </Pressable>
            ))}
          </Surface>
        ) : null}
        {activeCommand ? (
          <View style={styles.composerStatus}>
            <Text style={styles.composerStatusText} numberOfLines={1}>
              {activeCommand.command} · {activeCommand.label}
            </Text>
            <IconButton
              icon="close"
              size={16}
              iconColor="#a6adc8"
              style={styles.clearCommandButton}
              onPress={() => setActiveCommand(null)}
              accessibilityLabel="選択中のコマンドを解除"
            />
          </View>
        ) : null}
        {composerError ? <Text style={styles.composerError}>{composerError}</Text> : null}
        <Surface style={styles.composerCard} elevation={1}>
          <TextInput
            value={input}
            onChangeText={setInput}
            placeholder="メッセージを入力..."
            placeholderTextColor="#585b70"
            style={styles.textInput}
            mode="flat"
            underlineColor="transparent"
            activeUnderlineColor="transparent"
            multiline
            maxLength={4000}
            disabled={!sendEnabled}
            onSubmitEditing={handleSend}
            blurOnSubmit={false}
          />
          <View style={styles.composerActions}>
            <IconButton
              icon="plus"
              iconColor="#cdd6f4"
              size={24}
              style={styles.composerIconButton}
              onPress={() => {
                Keyboard.dismiss();
                setCommandSheetVisible(true);
              }}
              accessibilityLabel="ツールとコマンドを開く"
            />
            <Pressable
              style={styles.composerSelector}
              onPress={() => {
                Keyboard.dismiss();
                setResponseModelVisible(true);
              }}
              accessibilityRole="button"
              accessibilityLabel={`モデルを選択。現在は${currentResponseModelLabel}`}
            >
              <Text style={styles.composerSelectorText} numberOfLines={1}>
                {compactLabel(currentResponseModelLabel)}
              </Text>
              <Text style={styles.composerSelectorChevron}>⌄</Text>
            </Pressable>
            <Pressable
              style={styles.composerSelector}
              disabled={
                directResponse
                  ? directEffortOptions.length === 0
                  : controller.llmModeOptions.length === 0
              }
              onPress={() => {
                Keyboard.dismiss();
                setLlmModeVisible(true);
              }}
              accessibilityRole="button"
              accessibilityLabel={`Effortを選択。現在は${currentLlmModeLabel}`}
            >
              <Text
                style={[
                  styles.composerSelectorText,
                  (directResponse
                    ? directEffortOptions.length === 0
                    : controller.llmModeOptions.length === 0)
                    ? styles.composerSelectorDisabled
                    : null,
                ]}
                numberOfLines={1}
              >
                {compactLabel(currentLlmModeLabel)}
              </Text>
              <Text style={styles.composerSelectorChevron}>⌄</Text>
            </Pressable>
            <IconButton
              icon="arrow-up"
              iconColor={input.trim() && sendEnabled ? "#11111b" : "#6c7086"}
              containerColor={
                input.trim() && sendEnabled ? "#89b4fa" : "#313244"
              }
              size={22}
              style={styles.sendButton}
              onPress={handleSend}
              disabled={!input.trim() || !sendEnabled}
              accessibilityLabel="送信"
            />
          </View>
        </Surface>
        </View>
      </KeyboardStickyView>

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
          visible={responseModelVisible}
          onDismiss={() => setResponseModelVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>次の応答モデル</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <Button
                mode={!responseModel ? "contained" : "outlined"}
                buttonColor={!responseModel ? "#7c3aed" : undefined}
                textColor={!responseModel ? "#f5e9ff" : "#89b4fa"}
                style={styles.modeButton}
                onPress={() => {
                  setResponseModel(null);
                  setDirectResponse(null);
                  setResponseModelVisible(false);
                }}
              >
                サーバー既定（自動）
              </Button>
              {directModelOptions.length > 0 ? (
                <>
                  <Divider style={styles.innerDivider} />
                  <Text style={styles.commandSectionTitle}>Direct</Text>
                  {directModelOptions.map((option) => {
                    const selected =
                      directResponse?.provider === option.provider &&
                      directResponse?.model === option.model;
                    return (
                      <Button
                        key={`direct:${option.provider}:${option.model}`}
                        mode={selected ? "contained" : "outlined"}
                        buttonColor={selected ? "#7c3aed" : undefined}
                        textColor={selected ? "#f5e9ff" : "#89b4fa"}
                        style={styles.modeButton}
                        onPress={() => {
                          const efforts = getDirectReasoningEffortOptions(
                            option.provider,
                            option.model,
                          );
                          setDirectResponse({
                            provider: option.provider,
                            model: option.model,
                            reasoningEffort: efforts.includes("medium")
                              ? "medium"
                              : efforts[0],
                          });
                          setResponseModel(null);
                          setResponseModelVisible(false);
                        }}
                      >
                        {option.label}
                      </Button>
                    );
                  })}
                  <Divider style={styles.innerDivider} />
                  <Text style={styles.commandSectionTitle}>Server</Text>
                </>
              ) : null}
              {controller.responseModelOptions.map((option) => {
                const selected =
                  responseModel?.provider === option.provider &&
                  responseModel?.model === option.model;
                return (
                  <Button
                    key={`${option.provider}:${option.model}`}
                    mode={selected ? "contained" : "outlined"}
                    buttonColor={selected ? "#7c3aed" : undefined}
                    textColor={selected ? "#f5e9ff" : "#89b4fa"}
                    style={styles.modeButton}
                    onPress={() => {
                      setResponseModel({ provider: option.provider, model: option.model });
                      setDirectResponse(null);
                      setResponseModelVisible(false);
                    }}
                  >
                    {option.label}
                  </Button>
                );
              })}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setResponseModelVisible(false)}>
              Cancel
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
                  disabled={!directResponse && llmModeSaving}
                  style={styles.modeButton}
                  onPress={() => {
                    if (directResponse) {
                      setDirectResponse({ ...directResponse, reasoningEffort: mode });
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
          <Dialog.Title style={styles.dialogTitle}>追加と設定</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.dialogScrollContent}>
              <Text style={styles.commandSectionTitle}>ツール</Text>
              <Button
                icon="magnify"
                textColor="#cdd6f4"
                contentStyle={styles.quickActionContent}
                disabled={!isAuthenticated}
                onPress={() => {
                  setCommandSheetVisible(false);
                  setDeepResearchVisible(true);
                }}
              >
                Deep Research
              </Button>
              {pendingMessages.length > 0 ? (
                <Button
                  icon="cloud-upload-outline"
                  textColor="#f9e2af"
                  contentStyle={styles.quickActionContent}
                  onPress={() => {
                    setCommandSheetVisible(false);
                    setPendingQueueVisible(true);
                  }}
                >
                  未送信メッセージ {pendingMessages.length}件
                </Button>
              ) : null}
              <Divider style={styles.innerDivider} />
              <Text style={styles.commandSectionTitle}>組み込みコマンド</Text>
              {MOBILE_CHAT_COMMANDS.map((command) => (
                <Pressable
                  key={command.command}
                  style={styles.commandRow}
                  onPress={() => selectBuiltInCommand(command)}
                >
                  <View style={styles.commandText}>
                    <Text style={styles.commandTitle}>{command.command} · {command.label}</Text>
                    <Text style={styles.commandDescription}>
                      {command.description}
                    </Text>
                  </View>
                </Pressable>
              ))}
              <Divider style={styles.innerDivider} />
              <Text style={styles.commandSectionTitle}>Skills</Text>
              {controller.skillCommands.map((command) => (
                <Pressable
                  key={command.command}
                  style={styles.commandRow}
                  onPress={() => selectSkillCommand(command)}
                >
                  <View style={styles.commandText}>
                    <Text style={styles.commandTitle}>{command.command}</Text>
                    <Text style={styles.commandDescription}>{command.description}</Text>
                  </View>
                </Pressable>
              ))}
              {controller.skillCommands.length === 0 ? (
                <Text style={styles.diagnosticsText}>利用可能な手動Skillはありません。</Text>
              ) : null}
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
    </View>
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
