import React, { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import { Animated, FlatList, View } from "react-native";
import { Button, Chip, ProgressBar, Surface, Text } from "react-native-paper";
import type { AgentRun, ConversationMessage } from "../../../types/api";
import { findAddedTimelineItemIds, timelineItemIds } from "../animations";
import type { ConversationEvent, TimelineItem } from "../models";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { buildConversationBranchPresentations } from "../timeline";
import { ChatMessageRow } from "./ChatMessageRow";
import { StreamingMessageRow } from "./StreamingMessageRow";
import { chatScreenStyles as styles } from "./chat-screen.styles";

const EmptyTimeline = React.memo(function EmptyTimeline() {
  return (
    <View style={styles.empty}>
      <Text style={styles.emptyText}>メッセージを送信して会話を開始します。</Text>
    </View>
  );
});

function timelineItemKey(item: TimelineItem): string {
  return item.id;
}

const TimelineItemTransition = React.memo(function TimelineItemTransition({
  animate,
  reduceMotion,
  children,
}: {
  animate: boolean;
  reduceMotion: boolean;
  children: React.ReactNode;
}) {
  const animateOnMountRef = useRef(animate);
  const progress = useRef(
    new Animated.Value(animateOnMountRef.current && !reduceMotion ? 0 : 1),
  ).current;

  React.useEffect(() => {
    if (!animateOnMountRef.current || reduceMotion) {
      progress.setValue(1);
      return;
    }
    const animation = Animated.timing(progress, {
      toValue: 1,
      duration: 170,
      useNativeDriver: true,
    });
    animation.start();
    return () => animation.stop();
  }, [progress, reduceMotion]);

  return (
    <Animated.View
      style={{
        opacity: progress,
        transform: [
          {
            translateY: progress.interpolate({
              inputRange: [0, 1],
              outputRange: [6, 0],
            }),
          },
        ],
      }}
    >
      {children}
    </Animated.View>
  );
});

const ConversationEventRow = React.memo(function ConversationEventRow({
  event,
  onRespondPermission,
}: {
  event: ConversationEvent;
  onRespondPermission: (requestId: string, approved: boolean) => void;
}) {
  conversationPerformanceDiagnostics.recordRender(
    "ConversationEventRow",
    event.id,
  );
  const danger = event.severity === "danger";
  return (
    <Surface style={styles.eventCard} elevation={0}>
      <View style={styles.eventHeader}>
        <Text style={[styles.eventTitle, danger ? styles.dangerText : null]}>
          {event.title}
        </Text>
        <Chip compact style={styles.eventChip} textStyle={styles.eventChipText}>
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
                conversationPerformanceDiagnostics.measureInteraction(
                  "ConversationEventRow.permission-deny",
                  () => onRespondPermission(event.request.requestId, false),
                )
              }
            >
              Deny
            </Button>
            <Button
              mode="contained"
              compact
              buttonColor="#7c3aed"
              onPress={() =>
                conversationPerformanceDiagnostics.measureInteraction(
                  "ConversationEventRow.permission-allow",
                  () => onRespondPermission(event.request.requestId, true),
                )
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
});

export type ChatTimelineProps = {
  items: TimelineItem[];
  allMessages: ConversationMessage[];
  streamContent: string;
  reduceMotion: boolean;
  agentRuns: Record<string, AgentRun | null>;
  agentRunErrors: Record<string, boolean>;
  onLongPressMessage: (message: ConversationMessage) => void;
  onSwitchBranch: (message: ConversationMessage, nextIndex: number) => void;
  onRetryAgentRun: (runId: string) => void;
  onRespondPermission: (requestId: string, approved: boolean) => void;
  onMessageRender?: (messageId: string) => void;
  listRef?: React.RefObject<FlatList<TimelineItem> | null>;
};

export const ChatTimeline = React.memo(function ChatTimeline({
  items,
  allMessages,
  streamContent,
  reduceMotion,
  agentRuns,
  agentRunErrors,
  onLongPressMessage,
  onSwitchBranch,
  onRetryAgentRun,
  onRespondPermission,
  onMessageRender,
  listRef,
}: ChatTimelineProps) {
  conversationPerformanceDiagnostics.recordRender("ChatTimeline");
  const previousIdsRef = useRef<Set<string> | null>(null);
  const addedIds = useMemo(
    () => findAddedTimelineItemIds(previousIdsRef.current, items),
    [items],
  );
  useLayoutEffect(() => {
    previousIdsRef.current = timelineItemIds(items);
  }, [items]);

  const branchPresentations = useMemo(() => {
    return buildConversationBranchPresentations(allMessages);
  }, [allMessages]);

  const renderItem = useCallback(
    ({ item }: { item: TimelineItem }) => {
      if (item.type === "stream") return null;
      if (item.type === "event") {
        return (
          <TimelineItemTransition
            animate={addedIds.has(item.id)}
            reduceMotion={reduceMotion}
          >
            <ConversationEventRow
              event={item.event}
              onRespondPermission={onRespondPermission}
            />
          </TimelineItemTransition>
        );
      }
      const branch = branchPresentations.get(item.message.id) ?? {
        index: -1,
        count: 1,
      };
      const agentRunId = String(item.message.metadata?.agent_run_id ?? "").trim();
      return (
        <TimelineItemTransition
          animate={addedIds.has(item.id)}
          reduceMotion={reduceMotion}
        >
          <ChatMessageRow
            message={item.message}
            branchIndex={branch.index}
            branchCount={branch.count}
            agentRun={agentRunId ? agentRuns[agentRunId] ?? null : null}
            agentRunFailed={agentRunId ? agentRunErrors[agentRunId] === true : false}
            onLongPress={onLongPressMessage}
            onSwitchBranch={onSwitchBranch}
            onRetryAgentRun={onRetryAgentRun}
            onRender={onMessageRender}
          />
        </TimelineItemTransition>
      );
    },
    [
      addedIds,
      agentRunErrors,
      agentRuns,
      branchPresentations,
      onLongPressMessage,
      onMessageRender,
      onRespondPermission,
      onRetryAgentRun,
      onSwitchBranch,
      reduceMotion,
    ],
  );

  const footer = useMemo(
    () => <StreamingMessageRow content={streamContent} />,
    [streamContent],
  );

  return (
    <FlatList
      ref={listRef}
      data={items}
      keyExtractor={timelineItemKey}
      renderItem={renderItem}
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={styles.messageList}
      ListEmptyComponent={EmptyTimeline}
      ListFooterComponent={footer}
    />
  );
});
