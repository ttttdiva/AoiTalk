import React, { useCallback, useMemo, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { Keyboard, Pressable, ScrollView, View } from "react-native";
import { Button, Dialog, Divider, IconButton, Portal, Surface, Switch, Text } from "react-native-paper";
import {
  filterSlashCommands,
  MOBILE_CHAT_COMMANDS,
  resolveMobileCommandSubmission,
  visibleSkillSlashCommands,
  type ChatCommandCapability,
  type MobileChatCommand,
  type SkillSlashCommand,
} from "../chat-commands";
import { useChatIntent } from "../use-chat-intent";
import { useChatIntentPreference } from "../use-chat-intent-preference";
import { appendChatIntent, type ChatIntentCandidate } from "../../../lib/chat-intent-api";
import { ChatIntentDialog } from "./ChatIntentDialog";
import { ChatTextInput } from "../chat-input-theme";
import { useChatComposerDraft } from "../use-chat-composer-draft";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { chatScreenStyles as styles } from "./chat-screen.styles";

import { ChatVoiceControls, type ChatVoiceContext, type ChatVoiceHandle } from "./ChatVoiceControls";

function compactLabel(value: string): string {
  return value.length > 18 ? `${value.slice(0, 17)}...` : value;
}

export type ChatComposerSubmission = {
  content: string;
  submissionId?: string;
  capabilities?: ChatCommandCapability[];
};

export type ChatComposerProps = {
  voiceContext?: ChatVoiceContext;
  bottomInset: number;
  keyboardVisible: boolean;
  sendEnabled: boolean;
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
  onOpenResponseModel: () => void;
  onOpenLlmMode: () => void;
  onOpenContextDiagnostics?: () => void;
  onOpenPendingQueue: () => void;
  onStartDeepResearch: (query: string) => Promise<void>;
  /** Embedded fixed-Agent surfaces keep model/effort under Agent settings. */
  hideModelSelector?: boolean;
  hideEffortSelector?: boolean;
};

export const ChatComposer = React.memo(function ChatComposer({
  voiceContext,
  bottomInset,
  keyboardVisible,
  sendEnabled,
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
  onOpenResponseModel,
  onOpenLlmMode,
  onOpenContextDiagnostics,
  onOpenPendingQueue,
  onStartDeepResearch,
  hideModelSelector = false,
  hideEffortSelector = false,
}: ChatComposerProps) {
  conversationPerformanceDiagnostics.recordRender("ChatComposer");
  const voiceRef = useRef<ChatVoiceHandle>(null);
  const submissionRef = useRef(false);
  const intentSubmissionRef = useRef(false);
  const intentScope = JSON.stringify([voiceContext?.userId, voiceContext?.conversationKey, voiceContext?.projectId]);
  const intentScopeRef = useRef(intentScope);
  useLayoutEffect(() => {
    if (intentScopeRef.current !== intentScope) retrySubmissionRef.current = null;
    intentScopeRef.current = intentScope;
  }, [intentScope]);
  const submissionSequenceRef = useRef(0);
  const retrySubmissionRef = useRef<{ key: string; id: string } | null>(null);
  const intentDraftRevisionRef = useRef(0);
  const {
    input,
    activeCommand,
    setInput: setDraftInput,
    setActiveCommand: setDraftActiveCommand,
    beginSubmission,
    acceptSubmission,
    rejectSubmission,
    hydrated,
  } = useChatComposerDraft({
    authUser: isAuthenticated ? voiceContext?.userId ?? null : null,
    conversationKey: voiceContext?.conversationKey ?? null,
  });
  const setInput = useCallback<Dispatch<SetStateAction<string>>>((next) => {
    intentDraftRevisionRef.current += 1;
    setDraftInput(next);
  }, [setDraftInput]);
  const setActiveCommand = useCallback<Dispatch<SetStateAction<MobileChatCommand | null>>>((next) => {
    intentDraftRevisionRef.current += 1;
    setDraftActiveCommand(next);
  }, [setDraftActiveCommand]);
  const intentPreference = useChatIntentPreference(isAuthenticated ? voiceContext?.userId ?? null : null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [commandSheetVisible, setCommandSheetVisible] = useState(false);
  const [deepResearchVisible, setDeepResearchVisible] = useState(false);
  const [deepResearchQuery, setDeepResearchQuery] = useState("");

  const visibleSkillCommands = useMemo(
    () => visibleSkillSlashCommands(skillCommands),
    [skillCommands],
  );
  const slashQuery = /^\/[^\s\n]*$/.test(input) ? input : null;
  const slashSuggestions = useMemo(() => {
    if (!slashQuery) return [];
    const builtIns = filterSlashCommands(MOBILE_CHAT_COMMANDS, slashQuery).map((command) => ({
      kind: "command" as const,
      command,
    }));
    const skills = filterSlashCommands(visibleSkillCommands, slashQuery).map((command) => ({
      kind: "skill" as const,
      command,
    }));
    return [...builtIns, ...skills].slice(0, 6);
  }, [slashQuery, visibleSkillCommands]);

  const selectBuiltInCommand = useCallback((command: MobileChatCommand) => {
    // /masking is a trusted server operation.  Keep its literal slash token
    // in the payload just like a manual Skill so direct typing and menu
    // selection cannot diverge into a local/direct-model request.
    if (command.serverOnly) {
      setActiveCommand(null);
      setInput((value) => {
        const existing = value.trim();
        return /^\/[^\s\n]*$/.test(value) || !existing
          ? `${command.command} `
          : `${command.command} ${existing}`;
      });
      setComposerError(null);
      setCommandSheetVisible(false);
      return;
    }
    setActiveCommand(command);
    setComposerError(null);
    setInput((value) => (/^\/[^\s\n]*$/.test(value) ? "" : value));
    setCommandSheetVisible(false);
  }, [setActiveCommand, setInput]);
  const selectSkillCommand = useCallback((command: SkillSlashCommand) => {
    setActiveCommand(null);
    setComposerError(null);
    setInput(`${command.command} `);
    setCommandSheetVisible(false);
  }, [setActiveCommand, setInput]);
  const handleIntentSend = useCallback(async (candidate: ChatIntentCandidate): Promise<boolean> => {
    if (!hydrated || submissionRef.current || intentSubmissionRef.current || !sendEnabled || /^\/masking(?:\s|$)/i.test(input.trim())) return false;
    const submission = resolveMobileCommandSubmission(input, activeCommand);
    if (submission.error) { setComposerError(submission.error); return false; }
    if (!submission.content) return false;
    if (directResponseActive && (Boolean(submission.capabilities?.length) || submission.content.startsWith("/"))) {
      setComposerError("組み込みコマンドとSkillsはServerモデルで実行してください。");
      return false;
    }
    intentSubmissionRef.current = true;
    const sequence = ++submissionSequenceRef.current;
    const content = appendChatIntent(submission.content, candidate);
    const key = JSON.stringify([content, submission.capabilities]);
    const id = retrySubmissionRef.current?.key === key ? retrySubmissionRef.current.id
      : `mobile-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    retrySubmissionRef.current = null;
    voiceRef.current?.beforeTextSend();
    const draftRevision = intentDraftRevisionRef.current;
    const draftSnapshot = beginSubmission();
    if (!draftSnapshot) {
      intentSubmissionRef.current = false;
      return false;
    }
    const clearAccepted = () => {
      if (intentScopeRef.current !== intentScope || intentDraftRevisionRef.current !== draftRevision) return;
      acceptSubmission(draftSnapshot);
      setComposerError(null);
    };
    try {
      await onSend({ content, capabilities: submission.capabilities, submissionId: id });
      clearAccepted();
      return true;
    } catch (error) {
      if (error && typeof error === "object" && "retainedInTimeline" in error && error.retainedInTimeline === true) {
        clearAccepted();
        return true;
      }
      if (
        intentScopeRef.current === intentScope &&
        intentDraftRevisionRef.current === draftRevision
      ) {
        if (sequence === submissionSequenceRef.current) retrySubmissionRef.current = { key, id };
        voiceRef.current?.submissionFailed();
        rejectSubmission(draftSnapshot);
      }
      return false;
    } finally { intentSubmissionRef.current = false; }
  }, [acceptSubmission, activeCommand, beginSubmission, directResponseActive, hydrated, input, intentScope, onSend, rejectSubmission, sendEnabled]);
  const intent = useChatIntent({
    text: input,
    scopeKey: JSON.stringify([intentScope, activeCommand, isAuthenticated, intentPreference.scope]),
    sessionId: voiceContext?.sessionIsLocalOnly || voiceContext?.sessionId.startsWith("local-") || voiceContext?.sessionId === "new"
      ? null : voiceContext?.sessionId,
    projectId: voiceContext?.projectId,
    enabled: isAuthenticated && hydrated && sendEnabled && !/^\/masking(?:\s|$)/i.test(input.trim()),
    onSend: handleIntentSend,
  });

  const handleSend = useCallback(() => {
    if (!hydrated || submissionRef.current || intentSubmissionRef.current || !intentPreference.loaded || intentPreference.saving) return;
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
    if (intentPreference.enabled && isAuthenticated && !/^\/masking(?:\s|$)/i.test(input.trim())) {
      setComposerError(null);
      intent.open();
      return;
    }
    voiceRef.current?.beforeTextSend();
    const draftSnapshot = beginSubmission({ clearUi: true });
    if (!draftSnapshot) return;
    setComposerError(null);
    submissionRef.current = true;
    const key = JSON.stringify([submission.content, submission.capabilities]);
    const sequence = ++submissionSequenceRef.current;
    // Reuse an ID only for a rejected acceptance, never for a second deliberate
    // press with identical text while the previous request is still pending.
    const id = retrySubmissionRef.current?.key === key
      ? retrySubmissionRef.current.id
      : `mobile-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    retrySubmissionRef.current = null;
    const restoreRejectedDraft = (error: unknown) => {
      voiceRef.current?.submissionFailed();
      // Once accepted, the bubble owns retry/error state; never copy it back
      // into the input and create a second submission on the next press.
      if (error && typeof error === "object" && "retainedInTimeline" in error && error.retainedInTimeline === true) {
        acceptSubmission(draftSnapshot);
        return;
      }
      if (submissionSequenceRef.current !== sequence) return;
      retrySubmissionRef.current = { key, id };
      // Delivery failures after acceptance belong to the history. This callback
      // handles only validation/acceptance rejection from the controller.
      rejectSubmission(draftSnapshot);
      setComposerError(
        error instanceof Error ? error.message : "送信を開始できませんでした。入力を保持しました。",
      );
    };
    try {
      void onSend({
        content: submission.content,
        capabilities: submission.capabilities,
        submissionId: id,
      }).then(() => acceptSubmission(draftSnapshot)).catch(restoreRejectedDraft);
    } catch (error) {
      restoreRejectedDraft(error);
    }
    queueMicrotask(() => { submissionRef.current = false; });
  }, [acceptSubmission, activeCommand, beginSubmission, directResponseActive, hydrated, input, intent.open, intentPreference.enabled, intentPreference.loaded, intentPreference.saving, isAuthenticated, onSend, rejectSubmission, sendEnabled]);

  const submitDeepResearch = useCallback(() => {
    const query = deepResearchQuery.trim() || input.trim();
    if (!query) return;
    setDeepResearchVisible(false);
    setDeepResearchQuery("");
    void onStartDeepResearch(query);
  }, [deepResearchQuery, input, onStartDeepResearch]);

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
              {voiceContext ? (
                <ChatVoiceControls
                  ref={voiceRef}
                  context={voiceContext}
                  isAuthenticated={isAuthenticated}
                  onSend={onSend}
                />
              ) : null}
              {!hideModelSelector ? <Pressable
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
              </Pressable> : null}
              {effortEnabled && !hideEffortSelector ? (
              <Pressable
                style={styles.composerSelector}
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
              ) : null}
                <IconButton
                  icon="arrow-up"
                  iconColor={input.trim() && sendEnabled && hydrated ? "#11111b" : "#6c7086"}
                  containerColor={input.trim() && sendEnabled && hydrated ? "#89b4fa" : "#313244"}
                  size={22}
                  style={styles.sendButton}
                  onPress={() =>
                    conversationPerformanceDiagnostics.measureInteraction(
                      "ChatComposer.send",
                      handleSend,
                    )
                  }
                  disabled={!input.trim() || !hydrated || !sendEnabled || !intentPreference.loaded || intentPreference.saving || intent.sending}
                  accessibilityLabel="送信"
                />
            </View>
          </Surface>
      </View>

      <Portal>
        <ChatIntentDialog intent={intent} />
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
                <View style={styles.commandRow}>
                  <View style={styles.commandText}>
                    <Text style={styles.commandTitle}>意図確認</Text>
                    <Text style={styles.commandDescription}>送信前に言葉の意図を確認</Text>
                  </View>
                  <Text style={styles.commandDescription}>{intentPreference.enabled ? "ON" : "OFF"}</Text>
                  <Switch
                    accessibilityLabel="意図確認"
                    value={intentPreference.enabled}
                    disabled={!intentPreference.loaded || intentPreference.saving || intent.sending}
                    onValueChange={(value) => { intent.close(); void intentPreference.update(value); }}
                  />
                </View>
                {intentPreference.error && <Text accessibilityRole="alert" style={styles.composerError}>{intentPreference.error}</Text>}
                {onOpenContextDiagnostics ? (
                  <Button
                    icon="chart-box-outline"
                    textColor="#cdd6f4"
                    contentStyle={styles.quickActionContent}
                    disabled={!isAuthenticated}
                    onPress={() => {
                      setCommandSheetVisible(false);
                      onOpenContextDiagnostics();
                    }}
                  >
                    コンテキスト診断
                  </Button>
                ) : null}
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
                {visibleSkillCommands.map((command) => (
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
                {visibleSkillCommands.length === 0 ? (
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

