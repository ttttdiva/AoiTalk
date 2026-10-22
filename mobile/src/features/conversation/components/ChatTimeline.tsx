import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
} from "react";
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
import { useChatWaveTouchGuard } from "./chat-wave-touch-context";
import type { OperationsWorkReference } from "../../../lib/operations-api";

const noop = () => undefined;
const noopMessage = (_message: ConversationMessage) => undefined;
const noopMessageId = (_messageId: string) => undefined;

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
  const markWaveTouchIgnored = useChatWaveTouchGuard();
  const danger = event.severity === "danger";
  return (
    <Surface
      onTouchStart={markWaveTouchIgnored}
      style={styles.eventCard}
      elevation={0}
    >
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
          <Text style={styles.eventMeta} numberOfLines={event.request.materialApproval ? undefined : 5}>
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
  onPressMessage?: (message: ConversationMessage) => void;
  onSwitchBranch: (message: ConversationMessage, nextIndex: number) => void;
  onRetryAgentRun: (runId: string) => void;
  onOpenWork?: (reference: OperationsWorkReference) => void;
  onOpenAppLink?: (href: string) => boolean;
  onRespondPermission: (requestId: string, approved: boolean) => void;
  actionTargetId?: string | null;
  editTargetId?: string | null;
  editContent?: string;
  editSaving?: boolean;
  editError?: string | null;
  isAuthenticated?: boolean;
  retryingMessageIds?: string[];
  onDismissMessageActions?: () => void;
  onBeginEdit?: (message: ConversationMessage) => void;
  onEditContentChange?: (content: string) => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: () => void;
  onRetryMessage?: (message: ConversationMessage) => void;
  onRerunMessage?: (message: ConversationMessage) => void;
  onCopyMessage?: (message: ConversationMessage) => void;
  onForkMessage?: (message: ConversationMessage) => void;
  onMessageRender?: (messageId: string) => void;
  listRef?: React.RefObject<FlatList<TimelineItem> | null>;
  header?: React.ReactElement | null;
};

export const ChatTimeline = React.memo(function ChatTimeline({
  items,
  allMessages,
  streamContent,
  reduceMotion,
  agentRuns,
  agentRunErrors,
  onLongPressMessage,
  onPressMessage = noopMessage,
  onSwitchBranch,
  onRetryAgentRun,
  onOpenWork,
  onOpenAppLink,
  onRespondPermission,
  actionTargetId = null,
  editTargetId = null,
  editContent = "",
  editSaving = false,
  editError = null,
  isAuthenticated = false,
  retryingMessageIds = [],
  onDismissMessageActions = noop,
  onBeginEdit = noopMessage,
  onEditContentChange = noopMessageId,
  onCancelEdit = noop,
  onSubmitEdit = noop,
  onRetryMessage = noopMessage,
  onRerunMessage = noopMessage,
  onCopyMessage = noopMessage,
  onForkMessage = noopMessage,
  onMessageRender,
  listRef,
  header = null,
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
  const retryingIds = useMemo(
    () => new Set(retryingMessageIds),
    [retryingMessageIds],
  );
  const scheduleMessageScroll = useCallback(
    (messageId: string, viewPosition: number) => {
      const index = items.findIndex(
        (item) => item.type === "message" && item.message.id === messageId,
      );
      if (index < 0 || !listRef?.current) return null;
      return setTimeout(() => {
        listRef.current?.scrollToIndex({
          index,
          animated: true,
          viewPosition,
        });
      }, 64);
    },
    [items, listRef],
  );
  const scrollMessageIntoView = useCallback(
    (messageId: string) => {
      // Keep the entire editor (including Cancel / Send) above the keyboard
      // and sticky composer instead of centering only the text field.
      scheduleMessageScroll(messageId, 0.08);
    },
    [scheduleMessageScroll],
  );
  useEffect(() => {
    if (!actionTargetId) return undefined;
    // The inline action row increases the item's height after long-press. Move
    // the selected row away from the bottom edge so every icon remains tappable.
    const timeoutId = scheduleMessageScroll(actionTargetId, 0.4);
    return () => {
      if (timeoutId !== null) clearTimeout(timeoutId);
    };
  }, [actionTargetId, scheduleMessageScroll]);

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
            actionsVisible={actionTargetId === item.message.id}
            editing={editTargetId === item.message.id}
            editContent={editTargetId === item.message.id ? editContent : ""}
            editSaving={editTargetId === item.message.id && editSaving}
            editError={editTargetId === item.message.id ? editError : null}
            isAuthenticated={isAuthenticated}
            retrying={retryingIds.has(item.message.id)}
            onLongPress={onLongPressMessage}
            onPressMessage={onPressMessage}
            onDismissActions={onDismissMessageActions}
            onBeginEdit={onBeginEdit}
            onEditContentChange={onEditContentChange}
            onCancelEdit={onCancelEdit}
            onSubmitEdit={onSubmitEdit}
            onRetryMessage={onRetryMessage}
            onRerunMessage={onRerunMessage}
            onCopyMessage={onCopyMessage}
            onForkMessage={onForkMessage}
            onInlineEditFocus={scrollMessageIntoView}
            onSwitchBranch={onSwitchBranch}
            onRetryAgentRun={onRetryAgentRun}
            onOpenWork={onOpenWork}
            onOpenAppLink={onOpenAppLink}
            onRender={onMessageRender}
          />
        </TimelineItemTransition>
      );
    },
    [
      actionTargetId,
      addedIds,
      agentRunErrors,
      agentRuns,
      branchPresentations,
      editContent,
      editError,
      editSaving,
      editTargetId,
      isAuthenticated,
      onBeginEdit,
      onCancelEdit,
      onCopyMessage,
      onDismissMessageActions,
      onEditContentChange,
      onForkMessage,
      onLongPressMessage,
      onPressMessage,
      onMessageRender,
      onOpenWork,
      onOpenAppLink,
      onRespondPermission,
      onRetryAgentRun,
      onRetryMessage,
      onRerunMessage,
      onSubmitEdit,
      onSwitchBranch,
      reduceMotion,
      retryingIds,
      scrollMessageIntoView,
    ],
  );

  const footer = useMemo(
    () => <StreamingMessageRow content={streamContent} />,
    [streamContent],
  );

  return (
    <FlatList
      testID="chat-timeline"
      ref={listRef}
      data={items}
      keyExtractor={timelineItemKey}
      renderItem={renderItem}
      keyboardShouldPersistTaps="handled"
      keyboardDismissMode="interactive"
      contentContainerStyle={styles.messageList}
      ListHeaderComponent={header}
      ListHeaderComponentStyle={header ? styles.timelineHeader : undefined}
      ListEmptyComponent={EmptyTimeline}
      ListFooterComponent={footer}
      onScrollBeginDrag={onDismissMessageActions}
      onTouchStart={onDismissMessageActions}
      onScrollToIndexFailed={({ index, averageItemLength }) => {
        listRef?.current?.scrollToOffset({
          animated: true,
          offset: Math.max(0, averageItemLength * Math.max(0, index - 1)),
        });
      }}
    />
  );
});
