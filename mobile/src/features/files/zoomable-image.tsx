import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Image,
  type LayoutChangeEvent,
  PanResponder,
  Pressable,
  StyleSheet,
} from "react-native";
import { filesApi } from "../../lib/files-api";

type MediaSource = Awaited<ReturnType<typeof filesApi.getMediaSource>>;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function touchDistance(
  touches: Array<{ pageX: number; pageY: number }>,
): number {
  if (touches.length < 2) return 0;
  const [first, second] = touches;
  return Math.hypot(second.pageX - first.pageX, second.pageY - first.pageY);
}

export function ZoomableImage({
  source,
  onError,
  onSwipeLeft,
  onSwipeRight,
}: {
  source: MediaSource;
  onError: () => void;
  onSwipeLeft: () => void;
  onSwipeRight: () => void;
}) {
  const [scale, setScale] = useState(1);
  const [translate, setTranslate] = useState({ x: 0, y: 0 });
  const [viewport, setViewport] = useState({ width: 1, height: 1 });
  const scaleRef = useRef(1);
  const translateRef = useRef({ x: 0, y: 0 });
  const gestureRef = useRef({
    startDistance: 0,
    startScale: 1,
    startTranslateX: 0,
    startTranslateY: 0,
  });

  const clampTranslate = useCallback(
    (nextScale: number, x: number, y: number) => {
      if (nextScale <= 1) return { x: 0, y: 0 };
      const maxX = (viewport.width * (nextScale - 1)) / 2;
      const maxY = (viewport.height * (nextScale - 1)) / 2;
      return {
        x: clamp(x, -maxX, maxX),
        y: clamp(y, -maxY, maxY),
      };
    },
    [viewport.height, viewport.width],
  );

  const applyTransform = useCallback(
    (nextScale: number, x: number, y: number) => {
      const boundedScale = clamp(nextScale, 1, 5);
      const boundedTranslate = clampTranslate(boundedScale, x, y);
      scaleRef.current = boundedScale;
      translateRef.current = boundedTranslate;
      setScale(boundedScale);
      setTranslate(boundedTranslate);
    },
    [clampTranslate],
  );

  const resetZoom = useCallback(() => {
    applyTransform(1, 0, 0);
  }, [applyTransform]);

  useEffect(() => {
    resetZoom();
  }, [resetZoom, source.uri]);

  const panResponder = useMemo(
    () =>
      PanResponder.create({
        onStartShouldSetPanResponder: (event) =>
          event.nativeEvent.touches.length >= 2 || scaleRef.current > 1,
        onMoveShouldSetPanResponder: (event, gesture) =>
          event.nativeEvent.touches.length >= 2 ||
          (scaleRef.current > 1 &&
            (Math.abs(gesture.dx) > 2 || Math.abs(gesture.dy) > 2)) ||
          (scaleRef.current <= 1 &&
            Math.abs(gesture.dx) > 12 &&
            Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4),
        onPanResponderGrant: (event) => {
          const touches = event.nativeEvent.touches;
          gestureRef.current = {
            startDistance: touchDistance(touches),
            startScale: scaleRef.current,
            startTranslateX: translateRef.current.x,
            startTranslateY: translateRef.current.y,
          };
        },
        onPanResponderMove: (event, gesture) => {
          const touches = event.nativeEvent.touches;
          const gestureStart = gestureRef.current;

          if (touches.length >= 2 && gestureStart.startDistance > 0) {
            const ratio = touchDistance(touches) / gestureStart.startDistance;
            applyTransform(
              gestureStart.startScale * ratio,
              gestureStart.startTranslateX,
              gestureStart.startTranslateY,
            );
            return;
          }

          if (scaleRef.current > 1) {
            applyTransform(
              scaleRef.current,
              gestureStart.startTranslateX + gesture.dx,
              gestureStart.startTranslateY + gesture.dy,
            );
          }
        },
        onPanResponderRelease: (_, gesture) => {
          if (scaleRef.current <= 1.03) {
            if (
              Math.abs(gesture.dx) > 50 &&
              Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4
            ) {
              if (gesture.dx < 0) onSwipeLeft();
              else onSwipeRight();
              return;
            }
            resetZoom();
            return;
          }
          if (Math.abs(gesture.dx) < 6 && Math.abs(gesture.dy) < 6) {
            return;
          }
          const currentScale = scaleRef.current;
          const currentTranslate = translateRef.current;
          applyTransform(currentScale, currentTranslate.x, currentTranslate.y);
        },
        onPanResponderTerminationRequest: () => false,
      }),
    [applyTransform, onSwipeLeft, onSwipeRight, resetZoom],
  );

  const handleLayout = (event: LayoutChangeEvent) => {
    const { width, height } = event.nativeEvent.layout;
    setViewport({ width: Math.max(1, width), height: Math.max(1, height) });
  };

  return (
    <Pressable
      style={styles.zoomSurface}
      onLayout={handleLayout}
      onLongPress={resetZoom}
      {...panResponder.panHandlers}
    >
      <Image
        source={source}
        style={[
          styles.viewerImage,
          {
            transform: [
              { translateX: translate.x },
              { translateY: translate.y },
              { scale },
            ],
          },
        ]}
        resizeMode="contain"
        onError={onError}
      />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  zoomSurface: {
    flex: 1,
    alignSelf: "stretch",
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
  },
  viewerImage: { width: "100%", height: "100%" },
});
