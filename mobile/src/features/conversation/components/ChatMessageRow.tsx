import React from "react";
import { Pressable, View } from "react-native";
import { Button, IconButton, Surface, Text } from "react-native-paper";
import type { AgentRun, ConversationMessage } from "../../../types/api";
import {
  agentRunTimelineText,
  isPublicAgentRunTimelineItem,
} from "../cancelled-generation";
import { sameConversationMessageDisplayRevision } from "../timeline";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { ChatMarkdown } from "./ChatMarkdown";
import { chatScreenStyles as styles } from "./chat-screen.styles";
import { sanitizeChatAttachmentMetadata } from "../../../lib/chat-api";

export type ChatMessageRowProps = {
  message: ConversationMessage;
  branchIndex: number;
  branchCount: number;
  agentRun: AgentRun | null;
  agentRunFailed: boolean;
  onLongPress: (message: ConversationMessage) => void;
  onSwitchBranch: (message: ConversationMessage, nextIndex: number) => void;
  onRetryAgentRun: (runId: string) => void;
  onRender?: (messageId: string) => void;
};

export function chatMessageRowPropsEqual(
  previous: ChatMessageRowProps,
  next: ChatMessageRowProps,
): boolean {
  return (
    sameConversationMessageDisplayRevision(previous.message, next.message) &&
    previous.branchIndex === next.branchIndex &&
    previous.branchCount === next.branchCount &&
    previous.agentRun === next.agentRun &&
    previous.agentRunFailed === next.agentRunFailed &&
    previous.onLongPress === next.onLongPress &&
    previous.onSwitchBranch === next.onSwitchBranch &&
    previous.onRetryAgentRun === next.onRetryAgentRun &&
    previous.onRender === next.onRender
  );
}

export const ChatMessageRow = React.memo(function ChatMessageRow({
  message,
  branchIndex,
  branchCount,
  agentRun,
  agentRunFailed,
  onLongPress,
  onSwitchBranch,
  onRetryAgentRun,
  onRender,
}: ChatMessageRowProps) {
  conversationPerformanceDiagnostics.recordRender("ChatMessageRow", message.id);
  onRender?.(message.id);
  const generationCancelled =
    message.role === "assistant" &&
    message.metadata?.generation_status === "cancelled";
  const agentRunId = String(message.metadata?.agent_run_id ?? "").trim();
  const agentRunItems =
    agentRun?.timeline?.filter(isPublicAgentRunTimelineItem) ?? [];
  const persistedToolResults = Array.isArray(message.metadata?.tool_results)
    ? message.metadata.tool_results
    : [];
  const attachments = sanitizeChatAttachmentMetadata(message.metadata?.attachments);
  const hasBranch = branchIndex >= 0 && branchCount > 1;

  return (
    <View style={styles.messageWrap}>
      <Pressable
        onLongPress={() =>
          conversationPerformanceDiagnostics.measureInteraction(
            "ChatMessageRow.long-press",
            () => onLongPress(message),
          )
        }
        delayLongPress={280}
      >
        <Surface
          style={[
            styles.messageBubble,
            message.role === "user" ? styles.userBubble : styles.assistantBubble,
          ]}
          elevation={0}
        >
          {message.role === "user" ? (
            <Text style={styles.userText}>{message.content}</Text>
          ) : (
            <>
              {generationCancelled ? (
                <Text style={styles.cancelledLabel}>応答生成を停止しました</Text>
              ) : null}
              {message.content ? <ChatMarkdown content={message.content} /> : null}
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
      {generationCancelled && agentRunId ? (
        <Surface style={styles.cancelledLogCard} elevation={0}>
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
        <Surface style={styles.cancelledLogCard} elevation={0}>
          <Text style={styles.cancelledLogTitle}>停止時点のツール出力</Text>
          {persistedToolResults.map((result, index) => (
            <Text key={index} style={styles.cancelledLogDetail}>
              {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
            </Text>
          ))}
        </Surface>
      ) : null}
      {hasBranch ? (
        <View style={styles.branchRow}>
          <IconButton
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
