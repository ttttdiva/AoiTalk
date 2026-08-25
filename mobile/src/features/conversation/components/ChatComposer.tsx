import React, { useCallback, useMemo, useRef, useState } from "react";
import { Keyboard, Pressable, ScrollView, View } from "react-native";
import { Button, Dialog, Divider, IconButton, Portal, Surface, Text } from "react-native-paper";
import {
  filterSlashCommands,
  MOBILE_CHAT_COMMANDS,
  resolveMobileCommandSubmission,
  type ChatCommandCapability,
  type MobileChatCommand,
  type SkillSlashCommand,
} from "../chat-commands";
import { ChatTextInput } from "../chat-input-theme";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { chatScreenStyles as styles } from "./chat-screen.styles";

function compactLabel(value: string): string {
  return value.length > 18 ? `${value.slice(0, 17)}...` : value;
}

export type ChatComposerSubmission = {
  content: string;
  capabilities?: ChatCommandCapability[];
};

export type ChatComposerProps = {
  bottomInset: number;
  keyboardVisible: boolean;
  sendEnabled: boolean;
  serverGenerationActive: boolean;
  directResponseActive: boolean;
  currentResponseModelLabel: string;
  currentLlmModeLabel: string;
  effortEnabled: boolean;
  llmSelectionMessage: string | null;
  llmSyncLabel: string | null;
  skillCommands: SkillSlashCommand[];
  isAuthenticated: boolean;
  pendingCount: number;
  onSend: (submission: ChatComposerSubmission) => Promise<void>;
  onStopGeneration: () => Promise<void>;
  onSteerGeneration?: (message: string) => Promise<unknown>;
  onOpenResponseModel: () => void;
  onOpenLlmMode: () => void;
  onOpenPendingQueue: () => void;
  onStartDeepResearch: (query: string) => Promise<void>;
};

export const ChatComposer = React.memo(function ChatComposer({
  bottomInset,
  keyboardVisible,
  sendEnabled,
  serverGenerationActive,
  directResponseActive,
  currentResponseModelLabel,
  currentLlmModeLabel,
  effortEnabled,
  llmSelectionMessage,
  llmSyncLabel,
  skillCommands,
  isAuthenticated,
  pendingCount,
  onSend,
  onStopGeneration,
  onSteerGeneration,
  onOpenResponseModel,
  onOpenLlmMode,
  onOpenPendingQueue,
  onStartDeepResearch,
}: ChatComposerProps) {
  conversationPerformanceDiagnostics.recordRender("ChatComposer");
  const submissionRef = useRef(false);
  const [input, setInput] = useState("");
  const [activeCommand, setActiveCommand] = useState<MobileChatCommand | null>(null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [commandSheetVisible, setCommandSheetVisible] = useState(false);
  const [deepResearchVisible, setDeepResearchVisible] = useState(false);
  const [deepResearchQuery, setDeepResearchQuery] = useState("");
  const [steerVisible, setSteerVisible] = useState(false);
  const [steerDraft, setSteerDraft] = useState("");
  const [steerError, setSteerError] = useState<string | null>(null);
  const [steering, setSteering] = useState(false);

  const slashQuery = /^\/[^\s\n]*$/.test(input) ? input : null;
  const slashSuggestions = useMemo(() => {
    if (!slashQuery) return [];
    const builtIns = filterSlashCommands(MOBILE_CHAT_COMMANDS, slashQuery).map((command) => ({
      kind: "command" as const,
      command,
    }));
    const skills = filterSlashCommands(skillCommands, slashQuery).map((command) => ({
      kind: "skill" as const,
      command,
    }));
    return [...builtIns, ...skills].slice(0, 6);
  }, [skillCommands, slashQuery]);

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
    if (submissionRef.current) return;
    const submission = resolveMobileCommandSubmission(input, activeCommand);
    if (submission.error) {
      setComposerError(submission.error);
      return;
    }
    if (!submission.content || !sendEnabled) return;
    if (
      directResponseActive &&
      (Boolean(submission.capabilities?.length) || submission.content.startsWith("/"))
    ) {
      setComposerError("組み込みコマンドとSkillsはServerモデルで実行してください。");
      return;
    }
    setInput("");
    setActiveCommand(null);
    setComposerError(null);
    submissionRef.current = true;
    void onSend({
      content: submission.content,
      capabilities: submission.capabilities,
    }).finally(() => {
      submissionRef.current = false;
    });
  }, [activeCommand, directResponseActive, input, onSend, sendEnabled]);
  const submitDeepResearch = useCallback(() => {
    const query = deepResearchQuery.trim() || input.trim();
    if (!query) return;
    setDeepResearchVisible(false);
    setDeepResearchQuery("");
    void onStartDeepResearch(query);
  }, [deepResearchQuery, input, onStartDeepResearch]);
  const submitSteer = useCallback(async () => {
    const value = steerDraft.trim();
    if (!value || !onSteerGeneration || steering) return;
    setSteering(true);
    setSteerError(null);
    try {
      await onSteerGeneration(value);
      setSteerDraft("");
      setSteerVisible(false);
    } catch (error) {
      // Keep the draft so a transient failure never loses the user's instruction.
      setSteerError(error instanceof Error ? error.message : "追加指示に失敗しました。");
    } finally {
      setSteering(false);
    }
  }, [onSteerGeneration, steerDraft, steering]);

  return (
    <>
      <View
        style={[
          styles.inputBar,
          { paddingBottom: keyboardVisible ? 4 : Math.max(4, bottomInset) },
        ]}
      >
          {slashSuggestions.length > 0 ? (
            <Surface style={styles.slashSuggestions} elevation={3}>
              {slashSuggestions.map((item) => (
                <Pressable
                  key={`${item.kind}-${item.command.command}`}
                  style={styles.slashSuggestionRow}
                  onPress={() =>
                    conversationPerformanceDiagnostics.measureInteraction(
                      "ChatComposer.select-suggestion",
                      () =>
                        item.kind === "command"
                          ? selectBuiltInCommand(item.command)
                          : selectSkillCommand(item.command),
                    )
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
                onPress={() =>
                  conversationPerformanceDiagnostics.measureInteraction(
                    "ChatComposer.clear-command",
                    () => setActiveCommand(null),
                  )
                }
                accessibilityLabel="選択中のコマンドを解除"
              />
            </View>
          ) : null}
          {composerError ? <Text style={styles.composerError}>{composerError}</Text> : null}
          {llmSelectionMessage ? (
            <Text style={styles.composerSyncWarning} accessibilityLiveRegion="polite">
              {llmSelectionMessage}
            </Text>
          ) : llmSyncLabel ? (
            <Text style={styles.composerSyncStatus} accessibilityLiveRegion="polite">
              {llmSyncLabel}
            </Text>
          ) : null}
          <Surface style={styles.composerCard} elevation={1}>
            <ChatTextInput
              value={input}
              onChangeText={setInput}
              placeholder="メッセージを入力..."
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
                  conversationPerformanceDiagnostics.measureInteraction(
                    "ChatComposer.open-command-sheet",
                    () => {
                      Keyboard.dismiss();
                      setCommandSheetVisible(true);
                    },
                  );
                }}
                accessibilityLabel="ツールとコマンドを開く"
              />
              <Pressable
                style={styles.composerSelector}
                onPress={() => {
                  conversationPerformanceDiagnostics.measureInteraction(
                    "ChatComposer.open-response-model",
                    () => {
                      Keyboard.dismiss();
                      onOpenResponseModel();
                    },
                  );
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
                disabled={!effortEnabled}
                onPress={() => {
                  conversationPerformanceDiagnostics.measureInteraction(
                    "ChatComposer.open-llm-mode",
                    () => {
                      Keyboard.dismiss();
                      onOpenLlmMode();
                    },
                  );
                }}
                accessibilityRole="button"
                accessibilityLabel={`Effortを選択。現在は${currentLlmModeLabel}`}
              >
                <Text
                  style={[
                    styles.composerSelectorText,
                    !effortEnabled ? styles.composerSelectorDisabled : null,
                  ]}
                  numberOfLines={1}
                >
                  {compactLabel(currentLlmModeLabel)}
                </Text>
                <Text style={styles.composerSelectorChevron}>⌄</Text>
              </Pressable>
              {serverGenerationActive ? (
                <>
                  {onSteerGeneration ? (
                    <IconButton
                      icon="message-processing-outline"
                      iconColor="#11111b"
                      containerColor="#f9e2af"
                      size={22}
                      style={styles.sendButton}
                      onPress={() => {
                        setSteerError(null);
                        setSteerVisible(true);
                      }}
                      accessibilityLabel="生成中に追加指示"
                    />
                  ) : null}
                  <IconButton
                    icon="stop"
                    iconColor="#11111b"
                    containerColor="#f38ba8"
                    size={22}
                    style={styles.sendButton}
                    onPress={() =>
                      conversationPerformanceDiagnostics.measureInteraction(
                        "ChatComposer.stop-generation",
                        () => void onStopGeneration(),
                      )
                    }
                    accessibilityLabel="応答生成を停止"
                  />
                </>
              ) : (
                <IconButton
                  icon="arrow-up"
                  iconColor={input.trim() && sendEnabled ? "#11111b" : "#6c7086"}
                  containerColor={input.trim() && sendEnabled ? "#89b4fa" : "#313244"}
                  size={22}
                  style={styles.sendButton}
                  onPress={() =>
                    conversationPerformanceDiagnostics.measureInteraction(
                      "ChatComposer.send",
                      handleSend,
                    )
                  }
                  disabled={!input.trim() || !sendEnabled}
                  accessibilityLabel="送信"
                />
              )}
            </View>
          </Surface>
      </View>

      <Portal>
        {deepResearchVisible ? (
          <Dialog
            visible
            onDismiss={() => setDeepResearchVisible(false)}
            style={styles.dialog}
          >
            <Dialog.Title style={styles.dialogTitle}>Deep Research</Dialog.Title>
            <Dialog.Content>
              <Text style={styles.dialogHelp}>
                長時間ジョブとして開始し、進捗はタイムラインに表示します。
              </Text>
              <ChatTextInput
                value={deepResearchQuery}
                onChangeText={setDeepResearchQuery}
                mode="outlined"
                multiline
                placeholder={input.trim() || "調査したい内容"}
                style={styles.editInput}
              />
            </Dialog.Content>
            <Dialog.Actions>
              <Button textColor="#a6adc8" onPress={() => setDeepResearchVisible(false)}>
                Cancel
              </Button>
              <Button textColor="#7c3aed" onPress={submitDeepResearch}>
                Start
              </Button>
            </Dialog.Actions>
          </Dialog>
        ) : null}

        {steerVisible ? (
          <Dialog
            visible
            onDismiss={() => {
              if (!steering) setSteerVisible(false);
            }}
            style={styles.dialog}
          >
            <Dialog.Title style={styles.dialogTitle}>生成中に追加指示</Dialog.Title>
            <Dialog.Content>
              <ChatTextInput
                value={steerDraft}
                onChangeText={setSteerDraft}
                mode="outlined"
                multiline
                autoFocus
                placeholder="この生成に追加する指示"
                style={styles.editInput}
                disabled={steering}
              />
              {steerError ? <Text style={styles.composerError}>{steerError}</Text> : null}
            </Dialog.Content>
            <Dialog.Actions>
              <Button textColor="#a6adc8" onPress={() => setSteerVisible(false)} disabled={steering}>閉じる</Button>
              <Button textColor="#7c3aed" onPress={() => void submitSteer()} loading={steering} disabled={steering || !steerDraft.trim()}>送信</Button>
            </Dialog.Actions>
          </Dialog>
        ) : null}

        {commandSheetVisible ? (
          <Dialog
            visible
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
                  onPress={() =>
                    conversationPerformanceDiagnostics.measureInteraction(
                      "ChatComposer.open-deep-research",
                      () => {
                        setCommandSheetVisible(false);
                        setDeepResearchVisible(true);
                      },
                    )
                  }
                >
                  Deep Research
                </Button>
                {pendingCount > 0 ? (
                  <Button
                    icon="cloud-upload-outline"
                    textColor="#f9e2af"
                    contentStyle={styles.quickActionContent}
                    onPress={() =>
                      conversationPerformanceDiagnostics.measureInteraction(
                        "ChatComposer.open-pending-queue",
                        () => {
                          setCommandSheetVisible(false);
                          onOpenPendingQueue();
                        },
                      )
                    }
                  >
                    未送信メッセージ {pendingCount}件
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
                      <Text style={styles.commandTitle}>
                        {command.command} · {command.label}
                      </Text>
                      <Text style={styles.commandDescription}>{command.description}</Text>
                    </View>
                  </Pressable>
                ))}
                <Divider style={styles.innerDivider} />
                <Text style={styles.commandSectionTitle}>Skills</Text>
                {skillCommands.map((command) => (
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
                {skillCommands.length === 0 ? (
                  <Text style={styles.diagnosticsText}>利用可能な手動Skillはありません。</Text>
                ) : null}
              </ScrollView>
            </Dialog.ScrollArea>
            <Dialog.Actions>
              <Button textColor="#a6adc8" onPress={() => setCommandSheetVisible(false)}>
                Close
              </Button>
            </Dialog.Actions>
          </Dialog>
        ) : null}
      </Portal>
    </>
  );
});
