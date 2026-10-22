import React, {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { AppState, PixelRatio, StyleSheet, View } from "react-native";
import { useFocusEffect } from "expo-router";
import type { ExpoWebGLRenderingContext, GLView as GLViewType } from "expo-gl";
import {
  createChatWaveRenderer,
  type ChatWaveRenderer,
} from "../../../../../resources/chat-background";
import { useReducedMotion } from "../../ui/use-reduced-motion";

const MAX_RIPPLES = 8;
const MAX_DRAWING_BUFFER_WIDTH = 1440;
const MAX_DRAWING_BUFFER_HEIGHT = 2400;
const FRAME_INTERVAL_MS = 1000 / 30;

export type ChatWaveGLHandle = {
  addRipple: (x: number, y: number, timeSeconds?: number) => boolean;
};

type Props = {
  enabled: boolean;
  loaded: boolean;
  active?: boolean;
  dark?: boolean;
  onFailure?: (message: string) => void;
};

type PendingRipple = { x: number; y: number; timeSeconds: number };

function nowMs(): number {
  return typeof globalThis.performance?.now === "function"
    ? globalThis.performance.now()
    : Date.now();
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * Native expo-gl host for the shared chat-wave renderer.  The view is fully
 * decorative and does not participate in the responder chain.  Its loop is
 * owned here instead of React state, so streaming message updates never cause
 * a frame render or an animation restart.
 */
export const ChatWaveGLView = forwardRef<ChatWaveGLHandle, Props>(function ChatWaveGLView(
  { enabled, loaded, active = true, dark = true, onFailure },
  ref,
) {
  const [NativeGLView] = useState<typeof GLViewType | null>(() => {
    try {
      return (require("expo-gl") as typeof import("expo-gl")).GLView;
    } catch {
      return null;
    }
  });
  const reduceMotion = useReducedMotion();
  const [focused, setFocused] = useState(false);
  const [foreground, setForeground] = useState(
    AppState.currentState !== "background" && AppState.currentState !== "inactive",
  );
  const [gl, setGL] = useState<ExpoWebGLRenderingContext | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [failed, setFailed] = useState(false);
  const rendererRef = useRef<ChatWaveRenderer | null>(null);
  const pendingRipplesRef = useRef<PendingRipple[]>([]);
  const elapsedRef = useRef(0);
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const pixelRatio = Math.max(1, PixelRatio.get());
  const capWidth = Math.max(1, Math.min(size.width || 1, MAX_DRAWING_BUFFER_WIDTH / pixelRatio));
  const capHeight = Math.max(1, Math.min(size.height || 1, MAX_DRAWING_BUFFER_HEIGHT / pixelRatio));
  const renderSizeRef = useRef({
    logicalWidth: 1,
    logicalHeight: 1,
    physicalWidth: Math.max(1, Math.round(capWidth * pixelRatio)),
    physicalHeight: Math.max(1, Math.round(capHeight * pixelRatio)),
  });
  renderSizeRef.current = {
    logicalWidth: Math.max(1, size.width),
    logicalHeight: Math.max(1, size.height),
    physicalWidth: Math.max(1, Math.round(capWidth * pixelRatio)),
    physicalHeight: Math.max(1, Math.round(capHeight * pixelRatio)),
  };
  const activeRef = useRef(false);
  const visibilityRef = useRef<boolean | null>(null);
  const glEpochRef = useRef(0);
  const glContextEpochRef = useRef<number | null>(null);
  const onFailureRef = useRef(onFailure);
  onFailureRef.current = onFailure;

  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => {
        setFocused(false);
        setGL(null);
      };
    }, []),
  );

  useEffect(() => {
    const subscription = AppState.addEventListener("change", (nextState) => {
      setForeground(nextState === "active");
      if (nextState !== "active") setGL(null);
    });
    return () => subscription.remove();
  }, []);

  const visible = Boolean(
    NativeGLView &&
      loaded &&
      enabled &&
      active &&
      focused &&
      foreground &&
      !reduceMotion &&
      size.width > 1 &&
      size.height > 1 &&
      !failed,
  );
  activeRef.current = visible;
  if (visibilityRef.current !== visible) {
    visibilityRef.current = visible;
    glEpochRef.current += 1;
  }
  const glViewEpoch = glEpochRef.current;
  const contextReady = gl !== null && glContextEpochRef.current === glViewEpoch;

  const onContextCreate = useCallback((context: ExpoWebGLRenderingContext) => {
    if (!visible || !activeRef.current || glEpochRef.current !== glViewEpoch) return;
    glContextEpochRef.current = glViewEpoch;
    setGL(context);
  }, [glViewEpoch, visible]);

  useEffect(() => {
    if (!loaded || !enabled || !active || !focused || !foreground || !reduceMotion) {
      setFailed(false);
    }
  }, [active, enabled, focused, foreground, loaded, reduceMotion]);

  useEffect(() => {
    if (!visible && gl !== null) {
      glContextEpochRef.current = null;
      setGL(null);
    }
  }, [gl, visible]);

  useEffect(() => {
    const current = rendererRef.current;
    rendererRef.current = null;
    current?.dispose();
    elapsedRef.current = 0;
    if (!visible || !contextReady || !gl) {
      if (!visible) pendingRipplesRef.current = [];
      return undefined;
    }

    let renderer: ChatWaveRenderer;
    try {
      renderer = createChatWaveRenderer(gl as unknown as Record<string, any>, {
        width: renderSizeRef.current.logicalWidth,
        height: renderSizeRef.current.logicalHeight,
        drawingBufferWidth: renderSizeRef.current.physicalWidth,
        drawingBufferHeight: renderSizeRef.current.physicalHeight,
        dark,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "チャット背景を初期化できませんでした。";
      setFailed(true);
      setGL(null);
      onFailureRef.current?.(message);
      return undefined;
    }
    rendererRef.current = renderer;
    const queued = pendingRipplesRef.current.splice(0);
    queued.forEach((ripple) => {
      try {
        renderer.addRipple(ripple.x, ripple.y, ripple.timeSeconds);
      } catch {
        // A context can be lost while the GL view is being remounted.
      }
    });

    let disposed = false;
    let frame: number | ReturnType<typeof setTimeout> | null = null;
    let startedAt = nowMs();
    let previousFrameAt = -Infinity;
    const raf = (callback: FrameRequestCallback) => {
      if (typeof globalThis.requestAnimationFrame === "function") {
        return globalThis.requestAnimationFrame(callback);
      }
      return setTimeout(() => callback(nowMs()), FRAME_INTERVAL_MS);
    };
    const cancel = (handle: number | ReturnType<typeof setTimeout>) => {
      if (typeof globalThis.cancelAnimationFrame === "function" && typeof handle === "number") {
        globalThis.cancelAnimationFrame(handle);
      } else {
        clearTimeout(handle);
      }
    };
    const draw = (timestamp: number) => {
      const elapsed = Math.max(0, (timestamp - startedAt) / 1000);
      elapsedRef.current = elapsed;
      renderer.resize(
        renderSizeRef.current.logicalWidth,
        renderSizeRef.current.logicalHeight,
        renderSizeRef.current.physicalWidth,
        renderSizeRef.current.physicalHeight,
      );
      renderer.render(elapsed);
    };
    const tick = (timestamp: number) => {
      if (disposed || !activeRef.current) return;
      if (previousFrameAt === -Infinity || timestamp - previousFrameAt >= FRAME_INTERVAL_MS) {
        previousFrameAt = timestamp;
        try {
          draw(timestamp);
        } catch (error) {
          disposed = true;
          renderer.dispose();
          rendererRef.current = null;
          setFailed(true);
          setGL(null);
          onFailureRef.current?.(
            error instanceof Error ? error.message : "チャット背景の描画に失敗しました。",
          );
          return;
        }
      }
      frame = raf(tick);
    };
    try {
      draw(startedAt);
      frame = raf(tick);
    } catch (error) {
      renderer.dispose();
      rendererRef.current = null;
      setFailed(true);
      setGL(null);
      onFailureRef.current?.(
        error instanceof Error ? error.message : "チャット背景の描画に失敗しました。",
      );
    }
    return () => {
      disposed = true;
      if (frame !== null) cancel(frame);
      if (rendererRef.current === renderer) rendererRef.current = null;
      renderer.dispose();
    };
  }, [contextReady, dark, gl, visible]);

  useEffect(() => {
    if (!visible || !contextReady || !gl || !rendererRef.current) return;
    rendererRef.current.resize(
      renderSizeRef.current.logicalWidth,
      renderSizeRef.current.logicalHeight,
      renderSizeRef.current.physicalWidth,
      renderSizeRef.current.physicalHeight,
    );
  }, [contextReady, gl, size.height, size.width, visible]);

  const addRipple = useCallback((x: number, y: number, timeSeconds = elapsedRef.current): boolean => {
    if (!activeRef.current || !Number.isFinite(x) || !Number.isFinite(y)) return false;
    const next = {
      x: clamp(x, 0, Math.max(1, sizeRef.current.width)),
      y: clamp(y, 0, Math.max(1, sizeRef.current.height)),
      timeSeconds: Math.max(0, Number.isFinite(timeSeconds) ? timeSeconds : elapsedRef.current),
    };
    const renderer = rendererRef.current;
    if (!renderer) {
      if (pendingRipplesRef.current.length >= MAX_RIPPLES) pendingRipplesRef.current.shift();
      pendingRipplesRef.current.push(next);
      return true;
    }
    try {
      return renderer.addRipple(next.x, next.y, next.timeSeconds);
    } catch {
      return false;
    }
  }, []);

  useImperativeHandle(ref, () => ({ addRipple }), [addRipple]);

  const onLayout = useCallback((event: { nativeEvent: { layout: { width: number; height: number } } }) => {
    const width = Math.max(1, event.nativeEvent.layout.width);
    const height = Math.max(1, event.nativeEvent.layout.height);
    setSize((current) => (current.width === width && current.height === height ? current : { width, height }));
  }, []);

  if (!loaded || !enabled) return null;
  const scaleX = size.width > 1 ? size.width / capWidth : 1;
  const scaleY = size.height > 1 ? size.height / capHeight : 1;
  return (
    <View
      testID="chat-wave-background"
      pointerEvents="none"
      accessible={false}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={styles.container}
      onLayout={onLayout}
    >
      {visible && NativeGLView ? (
        <NativeGLView
          key={`chat-wave-gl-${glViewEpoch}`}
          testID="chat-wave-gl-view"
          msaaSamples={0}
          style={[
            styles.glView,
            {
              width: capWidth,
              height: capHeight,
              transformOrigin: "top left",
              transform: [{ scaleX }, { scaleY }],
            },
          ]}
          onContextCreate={(context: ExpoWebGLRenderingContext) => {
            if (!visible || !activeRef.current || glEpochRef.current !== glViewEpoch) return;
            setFailed(false);
            onContextCreate(context);
          }}
        />
      ) : null}
    </View>
  );
});

const styles = StyleSheet.create({
  container: {
    ...StyleSheet.absoluteFillObject,
    opacity: 0.96,
  },
  glView: {
    position: "absolute",
    left: 0,
    top: 0,
  },
});
