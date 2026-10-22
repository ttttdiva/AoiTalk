import React, { useMemo, useRef } from "react";
import { Pressable, View } from "react-native";
import { Button, IconButton, Surface, Text } from "react-native-paper";
import type { AgentRun, ConversationMessage } from "../../../types/api";
import {
  agentRunTimelineText,
  isPublicAgentRunTimelineItem,
} from "../cancelled-generation";
import { getMessageActionAvailability } from "../message-actions";
import { sameConversationMessageDisplayRevision } from "../timeline";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { ChatTextInput } from "../chat-input-theme";
import { ChatMarkdown } from "./ChatMarkdown";
import { useChatWaveTouchGuard } from "./chat-wave-touch-context";
import { chatScreenStyles as styles } from "./chat-screen.styles";
import { sanitizeChatAttachmentMetadata } from "../../../lib/chat-api";
import {
  workReferenceForConversationMessage,
  type OperationsWorkReference,
} from "../../../lib/operations-api";

export type ChatMessageRowProps = {
  message: ConversationMessage;
  branchIndex: number;
  branchCount: number;
  agentRun: AgentRun | null;
  agentRunFailed: boolean;
  actionsVisible: boolean;
  editing: boolean;
  editContent: string;
  editSaving: boolean;
  editError: string | null;
  isAuthenticated: boolean;
  retrying: boolean;
  onLongPress: (message: ConversationMessage) => void;
  onPressMessage: (message: ConversationMessage) => void;
  onDismissActions: () => void;
  onBeginEdit: (message: ConversationMessage) => void;
  onEditContentChange: (content: string) => void;
  onCancelEdit: () => void;
  onSubmitEdit: () => void;
  onRetryMessage: (message: ConversationMessage) => void;
  onRerunMessage: (message: ConversationMessage) => void;
  onCopyMessage: (message: ConversationMessage) => void;
  onForkMessage: (message: ConversationMessage) => void;
  onInlineEditFocus: (messageId: string) => void;
  onSwitchBranch: (message: ConversationMessage, nextIndex: number) => void;
  onRetryAgentRun: (runId: string) => void;
  /** Open the authoritative Operations Work detail for this completed reply. */
  onOpenWork?: (reference: OperationsWorkReference) => void;
  /** Handle canonical internal App links; return false when consumed. */
  onOpenAppLink?: (href: string) => boolean;
  onRender?: (messageId: string) => void;
};

export function chatMessageRowPropsEqual(
  previous: ChatMessageRowProps,
  next: ChatMessageRowProps,
): boolean {
  const editStateEqual =
    !previous.editing && !next.editing
      ? true
      : previous.editContent === next.editContent &&
        previous.editSaving === next.editSaving &&
        previous.editError === next.editError;
  return (
    sameConversationMessageDisplayRevision(previous.message, next.message) &&
    previous.branchIndex === next.branchIndex &&
    previous.branchCount === next.branchCount &&
    previous.agentRun === next.agentRun &&
    previous.agentRunFailed === next.agentRunFailed &&
    previous.actionsVisible === next.actionsVisible &&
    previous.editing === next.editing &&
    editStateEqual &&
    previous.isAuthenticated === next.isAuthenticated &&
    previous.retrying === next.retrying &&
    previous.onLongPress === next.onLongPress &&
    previous.onPressMessage === next.onPressMessage &&
    previous.onDismissActions === next.onDismissActions &&
    previous.onBeginEdit === next.onBeginEdit &&
    previous.onEditContentChange === next.onEditContentChange &&
    previous.onCancelEdit === next.onCancelEdit &&
    previous.onSubmitEdit === next.onSubmitEdit &&
    previous.onRetryMessage === next.onRetryMessage &&
    previous.onRerunMessage === next.onRerunMessage &&
    previous.onCopyMessage === next.onCopyMessage &&
    previous.onForkMessage === next.onForkMessage &&
    ((!previous.editing && !next.editing) ||
      previous.onInlineEditFocus === next.onInlineEditFocus) &&
    previous.onSwitchBranch === next.onSwitchBranch &&
    previous.onRetryAgentRun === next.onRetryAgentRun &&
    previous.onOpenWork === next.onOpenWork &&
    previous.onOpenAppLink === next.onOpenAppLink &&
    previous.onRender === next.onRender
  );
}

export const ChatMessageRow = React.memo(function ChatMessageRow({
  message,
  branchIndex,
  branchCount,
  agentRun,
  agentRunFailed,
  actionsVisible,
  editing,
  editContent,
  editSaving,
  editError,
  isAuthenticated,
  retrying,
  onLongPress,
  onPressMessage,
  onDismissActions,
  onBeginEdit,
  onEditContentChange,
  onCancelEdit,
  onSubmitEdit,
  onRetryMessage,
  onRerunMessage,
  onCopyMessage,
  onForkMessage,
  onInlineEditFocus,
  onSwitchBranch,
  onRetryAgentRun,
  onOpenWork,
  onOpenAppLink,
  onRender,
}: ChatMessageRowProps) {
  conversationPerformanceDiagnostics.recordRender("ChatMessageRow", message.id);
  onRender?.(message.id);
  const markWaveTouchIgnored = useChatWaveTouchGuard();
  const longPressHandledRef = useRef(false);

  const availability = useMemo(
    () => getMessageActionAvailability(message, isAuthenticated),
    [isAuthenticated, message],
  );
  const generationCancelled =
    message.role === "assistant" &&
    message.metadata?.generation_status === "cancelled";
  const agentRunId = String(message.metadata?.agent_run_id ?? "").trim();
  const workReference = workReferenceForConversationMessage(message, agentRun);
  const agentRunItems =
    agentRun?.timeline?.filter(isPublicAgentRunTimelineItem) ?? [];
  const persistedToolResults = Array.isArray(message.metadata?.tool_results)
    ? message.metadata.tool_results
    : [];
  const attachments = sanitizeChatAttachmentMetadata(message.metadata?.attachments);
  const agentSender = message.sender_type === "agent";
  const humanSender = message.sender_type === "user" || message.sender_type === "human" || (message.role === "user" && !message.sender_type);
  const hasBranch = branchIndex >= 0 && branchCount > 1;
  const rerunLabel =
    message.role === "assistant" ? "回答を再生成" : "メッセージを再送信";

  const dismissThen = (action: () => void) => {
    onDismissActions();
    action();
  };

  return (
    <View style={styles.messageWrap}>
      {editing ? (
        <Surface
          onTouchStart={markWaveTouchIgnored}
          style={[styles.messageBubble, styles.userBubble, styles.inlineEditBubble]}
          elevation={0}
        >
          <ChatTextInput
            accessibilityLabel="メッセージ本文を編集"
            value={editContent}
            onChangeText={onEditContentChange}
            onFocus={() => onInlineEditFocus(message.id)}
            mode="outlined"
            multiline
            autoFocus
            scrollEnabled
            disabled={editSaving}
            style={styles.inlineEditInput}
          />
          {editError ? (
            <Text accessibilityLiveRegion="polite" style={styles.inlineEditError}>
              {editError}
            </Text>
          ) : null}
          <View style={styles.inlineEditActions}>
            <Button
              accessibilityLabel="メッセージ編集をキャンセル"
              compact
              disabled={editSaving}
              textColor="#a6adc8"
              onPress={onCancelEdit}
            >
              キャンセル
            </Button>
            <Button
              accessibilityLabel={
                availability.isPendingLocal ? "編集内容を保存" : "編集して送信"
              }
              compact
              mode="contained"
              buttonColor="#7c3aed"
              loading={editSaving}
              disabled={editSaving || !editContent.trim()}
              onPress={onSubmitEdit}
            >
              {availability.isPendingLocal ? "保存" : "送信"}
            </Button>
          </View>
        </Surface>
      ) : (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={
            humanSender
              ? "ユーザーメッセージ。長押しで操作"
              : agentSender
                ? "Agentメッセージ。長押しで操作"
                : "アシスタントメッセージ。長押しで操作"
          }
          onPressIn={() => {
            longPressHandledRef.current = false;
          }}
          onPress={() => {
            if (longPressHandledRef.current) {
              longPressHandledRef.current = false;
              return;
            }
            onPressMessage(message);
          }}
          onLongPress={() => {
            longPressHandledRef.current = true;
            conversationPerformanceDiagnostics.measureInteraction(
              "ChatMessageRow.long-press",
              () => onLongPress(message),
            );
          }}
          delayLongPress={280}
        >
          <Surface
            style={[
              styles.messageBubble,
              humanSender ? styles.userBubble : styles.assistantBubble,
            ]}
            elevation={0}
          >
            {humanSender ? (
              <Text style={styles.userText}>{message.content}</Text>
            ) : (
              <>
                {message.sender_type === "agent" ? (
                  <Text style={styles.senderLabel}>
                    {message.sender_display_name || "Agent"}
                  </Text>
                ) : null}
                {generationCancelled ? (
                  <Text style={styles.cancelledLabel}>応答生成を停止しました</Text>
                ) : null}
                {message.content ? <ChatMarkdown content={message.content} onLinkPress={onOpenAppLink} /> : null}
              </>
            )}
            {attachments.length > 0 ? (
              <View style={styles.attachmentMetadataList}>
                {attachments.map((attachment, index) => (
                  <View key={`${attachment.name}-${index}`} style={styles.attachmentMetadataRow}>
                    <Text style={styles.attachmentMetadataName} numberOfLines={1}>
                      {attachment.name}
                    </Text>
                    <Text style={styles.attachmentMetadataDetail} numberOfLines={1}>
                      {[attachment.kind, attachment.mime_type, attachment.size != null ? `${attachment.size} bytes` : null]
                        .filter(Boolean)
                        .join(" · ") || "metadata only"}
                    </Text>
                  </View>
                ))}
              </View>
            ) : null}
          </Surface>
        </Pressable>
      )}

      {actionsVisible && !editing ? (
        <Surface
          accessibilityRole="toolbar"
          onTouchStart={markWaveTouchIgnored}
          style={[
            styles.messageActionBar,
            humanSender
              ? styles.userMessageActionBar
              : styles.assistantMessageActionBar,
          ]}
          elevation={2}
        >
          {availability.canEdit ? (
            <IconButton
              accessibilityLabel="メッセージを編集"
              icon="pencil-outline"
              iconColor="#cdd6f4"
              size={20}
              style={styles.messageActionButton}
              onPress={() => dismissThen(() => onBeginEdit(message))}
            />
          ) : null}
          {availability.canRetry ? (
            <IconButton
              accessibilityLabel="メッセージを再送信"
              accessibilityState={{ disabled: retrying, busy: retrying }}
              icon="refresh"
              iconColor="#89b4fa"
              size={20}
              disabled={retrying}
              style={styles.messageActionButton}
              onPress={() => dismissThen(() => onRetryMessage(message))}
            />
          ) : availability.canRerun ? (
            <IconButton
              accessibilityLabel={rerunLabel}
              icon="replay"
              iconColor="#89b4fa"
              size={20}
              style={styles.messageActionButton}
              onPress={() => dismissThen(() => onRerunMessage(message))}
            />
          ) : null}
          {availability.canCopy ? (
            <IconButton
              accessibilityLabel="メッセージをコピー"
              icon="content-copy"
              iconColor="#cdd6f4"
              size={20}
              style={styles.messageActionButton}
              onPress={() => dismissThen(() => onCopyMessage(message))}
            />
          ) : null}
          {availability.canFork ? (
            <IconButton
              accessibilityLabel="ここから会話を分岐"
              icon="source-fork"
              iconColor="#cdd6f4"
              size={20}
              style={styles.messageActionButton}
              onPress={() => dismissThen(() => onForkMessage(message))}
            />
          ) : null}
        </Surface>
      ) : null}

      {generationCancelled && agentRunId ? (
        <Surface
          onTouchStart={markWaveTouchIgnored}
          style={styles.cancelledLogCard}
          elevation={0}
        >
          <Text style={styles.cancelledLogTitle}>停止時点の作業ログ</Text>
          {agentRun && agentRunItems.length > 0 ? (
            agentRunItems.map((item) => {
              const row = agentRunTimelineText(item);
              return (
                <View key={item.id} style={styles.cancelledLogRow}>
                  <Text style={styles.cancelledLogAction}>{row.title}</Text>
                  {row.detail ? (
                    <Text style={styles.cancelledLogDetail}>{row.detail}</Text>
                  ) : null}
                </View>
              );
            })
          ) : agentRunFailed || agentRun ? (
            <>
              {persistedToolResults.length > 0 ? (
                persistedToolResults.map((result, index) => (
                  <Text key={index} style={styles.cancelledLogDetail}>
                    {typeof result === "string"
                      ? result
                      : JSON.stringify(result, null, 2)}
                  </Text>
                ))
              ) : (
                <Text style={styles.cancelledLogDetail}>
                  公開済みの作業ログはありません。
                </Text>
              )}
              {agentRunFailed ? (
                <Button
                  compact
                  mode="text"
                  onPress={() =>
                    conversationPerformanceDiagnostics.measureInteraction(
                      "ChatMessageRow.retry-agent-run",
                      () => onRetryAgentRun(agentRunId),
                    )
                  }
                >
                  作業ログを再読み込み
                </Button>
              ) : null}
            </>
          ) : (
            <Text style={styles.cancelledLogDetail}>
              作業ログを読み込んでいます…
            </Text>
          )}
        </Surface>
      ) : generationCancelled && persistedToolResults.length > 0 ? (
        <Surface
          onTouchStart={markWaveTouchIgnored}
          style={styles.cancelledLogCard}
          elevation={0}
        >
          <Text style={styles.cancelledLogTitle}>停止時点のツール出力</Text>
          {persistedToolResults.map((result, index) => (
            <Text key={index} style={styles.cancelledLogDetail}>
              {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
            </Text>
          ))}
        </Surface>
      ) : null}
      {workReference && onOpenWork ? (
        <Surface
          onTouchStart={markWaveTouchIgnored}
          style={styles.cancelledLogCard}
          elevation={0}
        >
          <Button
            accessibilityLabel="関連Workを開く"
            compact
            icon="briefcase-outline"
            onPress={() => onOpenWork(workReference)}
          >
            関連Workを開く
          </Button>
        </Surface>
      ) : null}
      {hasBranch && !editing ? (
        <View onTouchStart={markWaveTouchIgnored} style={styles.branchRow}>
          <IconButton
            accessibilityLabel="前の分岐"
            icon="chevron-left"
            iconColor={branchIndex > 0 ? "#cdd6f4" : "#585b70"}
            size={18}
            onPress={() =>
              conversationPerformanceDiagnostics.measureInteraction(
                "ChatMessageRow.previous-branch",
                () => onSwitchBranch(message, branchIndex - 1),
              )
            }
            disabled={branchIndex <= 0}
          />
          <Text style={styles.branchText}>
            {branchIndex + 1}/{branchCount}
          </Text>
          <IconButton
            accessibilityLabel="次の分岐"
            icon="chevron-right"
            iconColor={branchIndex < branchCount - 1 ? "#cdd6f4" : "#585b70"}
            size={18}
            onPress={() =>
              conversationPerformanceDiagnostics.measureInteraction(
                "ChatMessageRow.next-branch",
                () => onSwitchBranch(message, branchIndex + 1),
              )
            }
            disabled={branchIndex >= branchCount - 1}
          />
        </View>
      ) : null}
    </View>
  );
}, chatMessageRowPropsEqual);
