import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import type {
  FlatList,
  LayoutChangeEvent,
  NativeScrollEvent,
  NativeSyntheticEvent,
} from "react-native";
import type { TimelineItem } from "../models";

export const CHAT_TIMELINE_NEAR_BOTTOM_THRESHOLD = 96;

type ScrollMetrics = {
  contentHeight: number;
  viewportHeight: number;
  offsetY: number;
};

export function isNearChatTimelineBottom(
  { contentHeight, viewportHeight, offsetY }: ScrollMetrics,
  threshold = CHAT_TIMELINE_NEAR_BOTTOM_THRESHOLD,
): boolean {
  return Math.max(0, contentHeight - viewportHeight - offsetY) <= threshold;
}

type Options = {
  listRef: RefObject<FlatList<TimelineItem> | null>;
  sessionKey: string;
  itemCount: number;
  streamContent: string;
  reduceMotion: boolean;
};

export function useChatTimelineFollow({
  listRef,
  sessionKey,
  itemCount,
  streamContent,
  reduceMotion,
}: Options) {
  const followingLatestRef = useRef(true);
  const lastSessionKeyRef = useRef(sessionKey);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const metricsRef = useRef({ contentHeight: 0, viewportHeight: 0 });
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  const clearScheduledScroll = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const scheduleScrollToLatest = useCallback((delay = 48) => {
    if (!followingLatestRef.current || timerRef.current !== null) return;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (followingLatestRef.current) {
        listRef.current?.scrollToEnd({ animated: !reduceMotion });
      }
    }, delay);
  }, [listRef, reduceMotion]);

  const syncJumpButton = useCallback(() => {
    const { contentHeight, viewportHeight } = metricsRef.current;
    setShowJumpToLatest(
      !followingLatestRef.current && contentHeight > viewportHeight + CHAT_TIMELINE_NEAR_BOTTOM_THRESHOLD,
    );
  }, []);

  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    const contentHeight = Math.max(0, Number(contentSize.height) || 0);
    const viewportHeight = Math.max(0, Number(layoutMeasurement.height) || 0);
    metricsRef.current = { contentHeight, viewportHeight };
    followingLatestRef.current = isNearChatTimelineBottom({
      contentHeight,
      viewportHeight,
      offsetY: Math.max(0, Number(contentOffset.y) || 0),
    });
    syncJumpButton();
  }, [syncJumpButton]);

  const onLayout = useCallback((event: LayoutChangeEvent) => {
    const viewportHeight = Math.max(0, Number(event.nativeEvent.layout.height) || 0);
    metricsRef.current.viewportHeight = viewportHeight;
    if (metricsRef.current.contentHeight <= viewportHeight + CHAT_TIMELINE_NEAR_BOTTOM_THRESHOLD) {
      followingLatestRef.current = true;
      setShowJumpToLatest(false);
      return;
    }
    syncJumpButton();
    scheduleScrollToLatest();
  }, [scheduleScrollToLatest, syncJumpButton]);

  const onContentSizeChange = useCallback((_width: number, height: number) => {
    metricsRef.current.contentHeight = Math.max(0, Number(height) || 0);
    if (metricsRef.current.contentHeight <= metricsRef.current.viewportHeight + CHAT_TIMELINE_NEAR_BOTTOM_THRESHOLD) {
      followingLatestRef.current = true;
      setShowJumpToLatest(false);
      return;
    }
    syncJumpButton();
    scheduleScrollToLatest();
  }, [scheduleScrollToLatest, syncJumpButton]);

  const scrollToLatest = useCallback(() => {
    clearScheduledScroll();
    followingLatestRef.current = true;
    setShowJumpToLatest(false);
    listRef.current?.scrollToEnd({ animated: !reduceMotion });
  }, [clearScheduledScroll, listRef, reduceMotion]);

  useLayoutEffect(() => {
    if (lastSessionKeyRef.current === sessionKey) return;
    clearScheduledScroll();
    lastSessionKeyRef.current = sessionKey;
    followingLatestRef.current = true;
    setShowJumpToLatest(false);
    metricsRef.current = { contentHeight: 0, viewportHeight: 0 };
  }, [clearScheduledScroll, sessionKey]);

  useEffect(() => {
    scheduleScrollToLatest();
  }, [itemCount, scheduleScrollToLatest, sessionKey, streamContent]);

  useEffect(() => clearScheduledScroll, [clearScheduledScroll]);

  return { showJumpToLatest, onScroll, onLayout, onContentSizeChange, scrollToLatest };
}
