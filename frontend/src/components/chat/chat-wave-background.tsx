"use client";

import {
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  createChatWaveRenderer,
  type ChatWaveGL,
  type ChatWaveRenderer,
  type ChatWaveRendererOptions,
} from "../../../../resources/chat-background";

const MAX_FPS = 30;
const FRAME_INTERVAL_MS = 1000 / MAX_FPS;
const MAX_DPR = 1.5;
const MAX_CANVAS_PIXELS = 2_000_000;
const SHORT_TAP_MAX_MS = 650;
const SHORT_TAP_MAX_DISTANCE_PX = 14;

type ChatWaveBackgroundProps = {
  enabled: boolean | null;
  dark: boolean;
  /** Memory visualization owns its own motion and pauses the wave layer. */
  active?: boolean;
  viewportRef: RefObject<HTMLElement | null>;
  /** Optional injection keeps renderer lifecycle tests deterministic. */
  rendererFactory?:
    | ((gl: ChatWaveGL, options: ChatWaveRendererOptions) => ChatWaveRenderer)
    | null;
};

type PointerStart = {
  x: number;
  y: number;
  startedAt: number;
  excluded: boolean;
};

type RendererState =
  | "initializing"
  | "unsupported"
  | "ready"
  | "paused"
  | "context-lost"
  | "init-failed"
  | "render-failed"
  | "disposed";

type RendererReason =
  | "webgl-api-missing"
  | "context-unavailable"
  | "context-threw"
  | "factory-threw"
  | "factory-returned-null"
  | "resize-threw"
  | "render-threw";

function usePrefersReducedMotion(): boolean | null {
  const [reducedMotion, setReducedMotion] = useState<boolean | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- capability snapshot is only known in the browser effect.
      setReducedMotion(false);
      return;
    }

    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handleChange = () => setReducedMotion(query.matches);
    handleChange();
    query.addEventListener?.("change", handleChange);
    return () => query.removeEventListener?.("change", handleChange);
  }, []);

  return reducedMotion;
}

function getWebglContext(canvas: HTMLCanvasElement): {
  gl: WebGLRenderingContext | WebGL2RenderingContext | null;
  reason?: RendererReason;
} {
  // Some DOM test hosts expose HTMLCanvasElement but no WebGL constructors;
  // avoid calling their not-implemented getContext shim and retain the CSS
  // background fallback quietly.
  if (
    typeof WebGLRenderingContext === "undefined" &&
    typeof WebGL2RenderingContext === "undefined"
  ) {
    return { gl: null, reason: "webgl-api-missing" };
  }
  const options: WebGLContextAttributes = {
    alpha: true,
    antialias: false,
    depth: false,
    desynchronized: true,
    premultipliedAlpha: true,
    preserveDrawingBuffer: false,
    stencil: false,
  };

  try {
    const gl = (
      canvas.getContext("webgl2", options) ||
      canvas.getContext("webgl", options) ||
      canvas.getContext("experimental-webgl", options)
    ) as WebGLRenderingContext | WebGL2RenderingContext | null;
    return gl ? { gl } : { gl: null, reason: "context-unavailable" };
  } catch {
    // A missing/blocked context is a normal capability fallback for chat.
    return { gl: null, reason: "context-threw" };
  }
}

function getHostSize(host: HTMLElement): { width: number; height: number } {
  const rect = host.getBoundingClientRect();
  return {
    width: Math.max(1, rect.width || host.clientWidth || 1),
    height: Math.max(1, rect.height || host.clientHeight || 1),
  };
}

function getCanvasSize(
  width: number,
  height: number,
): { width: number; height: number } {
  const dpr = Math.min(
    MAX_DPR,
    Math.max(1, typeof window === "undefined" ? 1 : window.devicePixelRatio || 1),
  );
  const rawWidth = Math.max(1, Math.ceil(width * dpr));
  const rawHeight = Math.max(1, Math.ceil(height * dpr));
  const rawPixels = rawWidth * rawHeight;
  const capScale = rawPixels > MAX_CANVAS_PIXELS
    ? Math.sqrt(MAX_CANVAS_PIXELS / rawPixels)
    : 1;
  return {
    width: Math.max(1, Math.floor(rawWidth * capScale)),
    height: Math.max(1, Math.floor(rawHeight * capScale)),
  };
}

function isExcludedPointerTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return true;
  return Boolean(
    target.closest(
      [
        "a",
        "button",
        "input",
        "textarea",
        "select",
        "option",
        "summary",
        "[contenteditable=\"true\"]",
        "[role=\"button\"]",
        "[role=\"link\"]",
        "[role=\"menuitem\"]",
        "[data-chat-wave-ignore]",
        "[data-radix-collection-item]",
        "[draggable=\"true\"]",
      ].join(","),
    ),
  );
}

function disposeRenderer(renderer: ChatWaveRenderer | null) {
  if (!renderer) return;
  try {
    renderer.dispose();
  } catch {
    // Renderer cleanup must never take down the chat page.
  }
}

export function ChatWaveBackground({
  enabled,
  dark,
  active = true,
  viewportRef,
  rendererFactory,
}: ChatWaveBackgroundProps) {
  const reducedMotion = usePrefersReducedMotion();
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<ChatWaveRenderer | null>(null);
  const renderStartedAtRef = useRef(0);

  useEffect(() => {
    if (enabled !== true || !active || reducedMotion !== false) return;

    const host = hostRef.current;
    const canvas = canvasRef.current;
    const viewport = viewportRef.current;
    if (!host || !canvas || !viewport) return;

    const factory = rendererFactory ?? createChatWaveRenderer;

    let disposed = false;
    let animationFrame: number | null = null;
    let lastFrameAt = 0;
    let documentVisible = document.visibilityState !== "hidden";
    let intersectionVisible = true;
    let contextLost = false;
    let hasRendered = false;
    const pointerStarts = new Map<number, PointerStart>();
    const elapsedOffsetRef = { current: 0 };

    const setRendererStatus = (
      state: RendererState,
      reason?: RendererReason,
    ) => {
      if (host.dataset.waveRendererState !== state) {
        host.dataset.waveRendererState = state;
      }
      if (reason) {
        if (host.dataset.waveRendererReason !== reason) {
          host.dataset.waveRendererReason = reason;
        }
      } else if (host.hasAttribute("data-wave-renderer-reason")) {
        host.removeAttribute("data-wave-renderer-reason");
      }
    };

    setRendererStatus("initializing");

    const now = () =>
      typeof performance === "undefined" ? Date.now() : performance.now();

    const getElapsed = (timestamp = now()) => {
      if (renderStartedAtRef.current <= 0) return elapsedOffsetRef.current;
      return elapsedOffsetRef.current +
        Math.max(0, (timestamp - renderStartedAtRef.current) / 1000);
    };

    const stopLoop = () => {
      if (renderStartedAtRef.current > 0) {
        elapsedOffsetRef.current = Math.max(
          elapsedOffsetRef.current,
          getElapsed(),
        );
      }
      if (animationFrame !== null) {
        window.cancelAnimationFrame(animationFrame);
        animationFrame = null;
      }
      lastFrameAt = 0;
      renderStartedAtRef.current = 0;
    };

    const disposeCurrentRenderer = () => {
      const current = rendererRef.current;
      rendererRef.current = null;
      disposeRenderer(current);
    };

    const resizeCanvas = () => {
      const size = getHostSize(host);
      const canvasSize = getCanvasSize(size.width, size.height);
      if (canvas.width === canvasSize.width && canvas.height === canvasSize.height) {
        rendererRef.current?.resize(
          size.width,
          size.height,
          canvasSize.width,
          canvasSize.height,
        );
        return;
      }
      canvas.width = canvasSize.width;
      canvas.height = canvasSize.height;
      rendererRef.current?.resize(
        size.width,
        size.height,
        canvasSize.width,
        canvasSize.height,
      );
    };

    const initializeRenderer = () => {
      if (disposed || contextLost) return;
      hasRendered = false;
      setRendererStatus("initializing");
      disposeCurrentRenderer();
      const { gl, reason } = getWebglContext(canvas);
      if (!gl) {
        setRendererStatus("unsupported", reason);
        return;
      }
      const logicalSize = getHostSize(host);
      resizeCanvas();
      let nextRenderer: ChatWaveRenderer;
      try {
        nextRenderer = factory(gl as unknown as ChatWaveGL, {
          width: logicalSize.width,
          height: logicalSize.height,
          drawingBufferWidth: canvas.width,
          drawingBufferHeight: canvas.height,
          dark,
        });
      } catch {
        setRendererStatus("init-failed", "factory-threw");
        return;
      }
      if (!nextRenderer) {
        setRendererStatus("init-failed", "factory-returned-null");
        return;
      }
      rendererRef.current = nextRenderer;
      try {
        nextRenderer.resize(
          logicalSize.width,
          logicalSize.height,
          canvas.width,
          canvas.height,
        );
      } catch {
        disposeCurrentRenderer();
        setRendererStatus("init-failed", "resize-threw");
      }
    };

    const shouldRun = () =>
      !disposed &&
      documentVisible &&
      intersectionVisible &&
      !contextLost &&
      rendererRef.current !== null;

    const renderFrame = (timestamp: number) => {
      animationFrame = null;
      if (!shouldRun()) return;
      if (renderStartedAtRef.current <= 0) renderStartedAtRef.current = timestamp;
      if (
        lastFrameAt <= 0 ||
        timestamp - lastFrameAt >= FRAME_INTERVAL_MS
      ) {
        const renderer = rendererRef.current;
        if (renderer) {
          try {
            renderer.render(getElapsed(timestamp));
            lastFrameAt = timestamp;
            if (!hasRendered) {
              hasRendered = true;
              setRendererStatus("ready");
            }
          } catch {
            stopLoop();
            disposeCurrentRenderer();
            setRendererStatus("render-failed", "render-threw");
            return;
          }
        }
      }
      animationFrame = window.requestAnimationFrame(renderFrame);
    };

    const startLoop = () => {
      if (disposed || contextLost || !rendererRef.current) return;
      if (!documentVisible || !intersectionVisible) {
        setRendererStatus("paused");
        return;
      }
      if (animationFrame !== null) return;
      setRendererStatus(hasRendered ? "ready" : "initializing");
      // Keep the renderer clock monotonic while skipping the wall-clock time
      // spent hidden/invisible. Existing ripples therefore keep their age.
      renderStartedAtRef.current = now();
      lastFrameAt = 0;
      animationFrame = window.requestAnimationFrame(renderFrame);
    };

    const handleVisibilityChange = () => {
      documentVisible = document.visibilityState !== "hidden";
      if (documentVisible) startLoop();
      else {
        stopLoop();
        if (rendererRef.current && !contextLost) setRendererStatus("paused");
      }
    };

    const handleIntersection = (entries: IntersectionObserverEntry[]) => {
      intersectionVisible = entries.some((entry) => entry.isIntersecting);
      if (intersectionVisible) startLoop();
      else {
        stopLoop();
        if (rendererRef.current && !contextLost) setRendererStatus("paused");
      }
    };

    const handleResize = () => {
      resizeCanvas();
      if (shouldRun()) startLoop();
    };

    const handleContextLost = (event: Event) => {
      // Keep the browser eligible to restore the context while making the
      // failure invisible to chat controls.
      event.preventDefault();
      contextLost = true;
      stopLoop();
      disposeCurrentRenderer();
      hasRendered = false;
      setRendererStatus("context-lost");
    };
    const handleContextRestored = () => {
      contextLost = false;
      initializeRenderer();
      startLoop();
    };

    const handlePointerDown = (event: PointerEvent) => {
      if (!event.isPrimary || event.button !== 0 || !shouldRun()) return;
      pointerStarts.set(event.pointerId, {
        x: event.clientX,
        y: event.clientY,
        startedAt: now(),
        excluded: isExcludedPointerTarget(event.target),
      });
    };

    const handlePointerMove = (event: PointerEvent) => {
      const started = pointerStarts.get(event.pointerId);
      if (!started) return;
      if (
        Math.hypot(event.clientX - started.x, event.clientY - started.y) >
        SHORT_TAP_MAX_DISTANCE_PX
      ) {
        started.excluded = true;
      }
    };

    const handleScroll = () => {
      for (const started of pointerStarts.values()) started.excluded = true;
    };

    const handlePointerUp = (event: PointerEvent) => {
      if (!event.isPrimary || event.button !== 0 || !shouldRun()) return;
      const started = pointerStarts.get(event.pointerId);
      pointerStarts.delete(event.pointerId);
      if (!started || started.excluded || isExcludedPointerTarget(event.target)) return;
      if (now() - started.startedAt > SHORT_TAP_MAX_MS) return;
      const distance = Math.hypot(event.clientX - started.x, event.clientY - started.y);
      if (distance > SHORT_TAP_MAX_DISTANCE_PX) return;
      if (typeof window.getSelection === "function" && !window.getSelection()?.isCollapsed) return;

      const rect = host.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      ) return;
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      try {
        rendererRef.current?.addRipple(x, y, getElapsed());
      } catch {
        // A lost/failed renderer should never interfere with pointer handling.
      }
    };
    const handlePointerCancel = (event: PointerEvent) => {
      pointerStarts.delete(event.pointerId);
    };
    const clearPointerStarts = () => pointerStarts.clear();

    initializeRenderer();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    viewport.addEventListener("pointerdown", handlePointerDown, true);
    viewport.addEventListener("pointermove", handlePointerMove, true);
    viewport.addEventListener("pointerup", handlePointerUp, true);
    viewport.addEventListener("pointercancel", handlePointerCancel, true);
    viewport.addEventListener("scroll", handleScroll, true);
    window.addEventListener("blur", clearPointerStarts);
    canvas.addEventListener("webglcontextlost", handleContextLost);
    canvas.addEventListener("webglcontextrestored", handleContextRestored);

    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(handleResize);
    resizeObserver?.observe(host);
    const intersectionObserver = typeof IntersectionObserver === "undefined"
      ? null
      : new IntersectionObserver(handleIntersection, { threshold: 0.01 });
    intersectionObserver?.observe(host);
    startLoop();

    return () => {
      disposed = true;
      pointerStarts.clear();
      stopLoop();
      resizeObserver?.disconnect();
      intersectionObserver?.disconnect();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      viewport.removeEventListener("pointerdown", handlePointerDown, true);
      viewport.removeEventListener("pointermove", handlePointerMove, true);
      viewport.removeEventListener("pointerup", handlePointerUp, true);
      viewport.removeEventListener("pointercancel", handlePointerCancel, true);
      viewport.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("blur", clearPointerStarts);
      canvas.removeEventListener("webglcontextlost", handleContextLost);
      canvas.removeEventListener("webglcontextrestored", handleContextRestored);
      disposeCurrentRenderer();
      setRendererStatus("disposed");
    };
  }, [active, dark, enabled, reducedMotion, rendererFactory, viewportRef]);

  if (enabled !== true || !active || reducedMotion !== false) return null;

  return (
    <div
      ref={hostRef}
      aria-hidden="true"
      data-testid="chat-wave-background"
      data-wave-renderer-state="initializing"
      className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
    >
      <canvas
        ref={canvasRef}
        data-testid="chat-wave-background-canvas"
        className="block size-full"
        aria-hidden="true"
      />
    </div>
  );
}
