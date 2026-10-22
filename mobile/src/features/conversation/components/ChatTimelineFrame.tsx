import React, { useCallback, useEffect, useRef, useState } from "react";
import { AppState, StyleSheet, View } from "react-native";
import { useFocusEffect } from "expo-router";
import { useTheme } from "react-native-paper";
import { NativeCharacterAnimation } from "../../characters/character-animation";
import { useChatAvatarPreference } from "../../characters/use-chat-avatar-preference";
import { useChatWavePreference } from "../use-chat-wave-preference";
import { ChatWaveGLView, type ChatWaveGLHandle } from "./ChatWaveGLView";
import { ChatWaveTouchContext } from "./chat-wave-touch-context";
import { useReducedMotion } from "../../ui/use-reduced-motion";

type Props = React.ComponentProps<typeof NativeCharacterAnimation> & {
  topInset: number;
  children: React.ReactNode;
  /** Route/runtime focus gate for embedded surfaces. */
  active?: boolean;
};

/** Decorative background only: never consumes timeline or composer height. */
export function ChatTimelineFrame({ topInset, children, active = true, ...avatar }: Props) {
  const { enabled, loaded } = useChatAvatarPreference();
  const wavePreference = useChatWavePreference();
  const reduceMotion = useReducedMotion();
  // Mounted conversation surfaces are active by default; useFocusEffect flips
  // this off as soon as the owning route loses focus.
  const [focused, setFocused] = useState(true);
  const [foreground, setForeground] = useState(
    AppState.currentState !== "background" && AppState.currentState !== "inactive",
  );
  const waveRef = useRef<ChatWaveGLHandle>(null);
  const viewportRef = useRef({ width: 1, height: 1 });
  const viewportNodeRef = useRef<any>(null);
  const viewportOriginRef = useRef({ x: 0, y: 0 });
  const ignoredEventsRef = useRef(new WeakSet<object>());
  const dark = useTheme().dark !== false;
  const touchRef = useRef<{
    x: number;
    y: number;
    startedAt: number;
    moved: boolean;
    multiTouch: boolean;
    ignored: boolean;
  } | null>(null);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (nextState) => {
      setForeground(nextState === "active");
      if (nextState !== "active") touchRef.current = null;
    });
    return () => subscription.remove();
  }, []);
  const touchEnabled = Boolean(
    wavePreference.loaded &&
      wavePreference.enabled &&
      active &&
      focused &&
      foreground &&
      !reduceMotion,
  );

  const readLocation = useCallback((event: any) => {
    const native = event?.nativeEvent ?? {};
    const x = Number.isFinite(native.pageX)
      ? native.pageX - viewportOriginRef.current.x
      : native.locationX;
    const y = Number.isFinite(native.pageY)
      ? native.pageY - viewportOriginRef.current.y
      : native.locationY;
    return { x: Number.isFinite(x) ? x : 0, y: Number.isFinite(y) ? y : 0 };
  }, []);
  const markIgnoredTouch = useCallback((event: unknown) => {
    const native = (event as any)?.nativeEvent;
    if (!native || typeof native !== "object") return;
    ignoredEventsRef.current.add(native);
  }, []);
  const measureViewport = useCallback(() => {
    viewportNodeRef.current?.measureInWindow?.((x: number, y: number) => {
      viewportOriginRef.current = { x, y };
    });
  }, []);
  const onTouchStart = useCallback((event: any) => {
    const native = event?.nativeEvent ?? {};
    measureViewport();
    const location = readLocation(event);
    const touches = Number(native.numberActiveTouches ?? 1);
    const ignored = native && typeof native === "object" && ignoredEventsRef.current.has(native);
    if (native && typeof native === "object") ignoredEventsRef.current.delete(native);
    touchRef.current = touches === 1 ? {
      ...location,
      startedAt: Date.now(),
      moved: false,
      multiTouch: false,
      ignored,
    } : null;
  }, [measureViewport, readLocation]);
  const onTouchMove = useCallback((event: any) => {
    const current = touchRef.current;
    if (!current) return;
    const native = event?.nativeEvent ?? {};
    if (Number(native.numberActiveTouches ?? 1) > 1) {
      current.multiTouch = true;
      return;
    }
    const location = readLocation(event);
    if (Math.hypot(location.x - current.x, location.y - current.y) > 8) {
      current.moved = true;
    }
  }, [readLocation]);
  const onTouchEnd = useCallback((event: any) => {
    const current = touchRef.current;
    touchRef.current = null;
    measureViewport();
    if (!current || current.ignored || current.moved || current.multiTouch) return;
    if (Date.now() - current.startedAt > 460) return;
    const location = readLocation(event);
    const { width, height } = viewportRef.current;
    if (location.x < 0 || location.y < 0 || location.x > width || location.y > height) return;
    // React Native's touch handlers bubble through children.  We deliberately
    // never request the responder, so FlatList scrolling, pressables, links,
    // selection and long-press menus retain ownership of the gesture.
    waveRef.current?.addRipple(location.x, location.y);
  }, [measureViewport, readLocation]);
  const onTouchCancel = useCallback(() => {
    touchRef.current = null;
  }, []);
  const onLayout = useCallback((event: any) => {
    const layout = event?.nativeEvent?.layout;
    if (!layout) return;
    viewportRef.current = {
      width: Math.max(1, Number(layout.width) || 1),
      height: Math.max(1, Number(layout.height) || 1),
    };
    measureViewport();
  }, [measureViewport]);
  return (
    <View
      testID="chat-timeline-frame"
      style={[styles.frame, { paddingTop: topInset }]}
    >
      <ChatWaveTouchContext.Provider value={markIgnoredTouch}>
        <View
          ref={viewportNodeRef}
          testID="chat-timeline-viewport"
          style={styles.viewport}
          onLayout={onLayout}
          onTouchStart={touchEnabled ? onTouchStart : undefined}
          onTouchMove={touchEnabled ? onTouchMove : undefined}
          onTouchEnd={touchEnabled ? onTouchEnd : undefined}
          onTouchCancel={touchEnabled ? onTouchCancel : undefined}
          onStartShouldSetResponder={touchEnabled ? () => false : undefined}
          onMoveShouldSetResponder={touchEnabled ? () => false : undefined}
          onResponderTerminationRequest={touchEnabled ? () => true : undefined}
        >
          <ChatWaveGLView
            ref={waveRef}
            enabled={wavePreference.enabled}
            loaded={wavePreference.loaded}
            active={active}
            dark={dark}
          />
          {loaded && enabled ? (
            <View
              testID="chat-avatar-background"
              style={styles.background}
              pointerEvents="none"
              accessible={false}
              accessibilityElementsHidden
              importantForAccessibility="no-hide-descendants"
            >
              <NativeCharacterAnimation {...avatar} />
            </View>
          ) : null}
          {children}
        </View>
      </ChatWaveTouchContext.Provider>
    </View>
  );
}

const styles = StyleSheet.create({
  frame: { flex: 1, minHeight: 0 },
  viewport: { flex: 1, minHeight: 0, position: "relative", overflow: "hidden" },
  background: {
    ...StyleSheet.absoluteFillObject,
    justifyContent: "center",
    opacity: 0.14,
  },
});
