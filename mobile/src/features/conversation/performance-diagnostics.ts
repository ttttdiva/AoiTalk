export const PERFORMANCE_DIAGNOSTIC_CATEGORIES = [
  "render",
  "interaction",
  "frame",
  "stream",
  "controller",
  "socket",
  "timer",
  "http",
  "sqlite",
  "merge",
] as const;

export const PERFORMANCE_DIAGNOSTICS_ENV_KEY =
  "EXPO_PUBLIC_AOITALK_PERFORMANCE_DIAGNOSTICS";
export const PERFORMANCE_DIAGNOSTICS_LOG_PREFIX = "AOITALK_PERF_SNAPSHOT";
export const PERFORMANCE_DIAGNOSTICS_GLOBAL_KEY =
  "__AOITALK_PERFORMANCE_DIAGNOSTICS__";

export type PerformanceDiagnosticCategory =
  (typeof PERFORMANCE_DIAGNOSTIC_CATEGORIES)[number];

export type PerformanceMetricKey =
  `${PerformanceDiagnosticCategory}.${string}`;

export interface PerformanceTimingSnapshot {
  count: number;
  totalMs: number;
  averageMs: number;
  lastMs: number;
  maxMs: number;
  p95Ms: number;
}

export interface PerformanceActiveSnapshot {
  current: number;
  max: number;
  totalStarted: number;
}

export interface PerformanceDiagnosticsSnapshot {
  enabled: boolean;
  capturedAt: string;
  counts: Record<string, number>;
  timings: Record<string, PerformanceTimingSnapshot>;
  active: Record<string, PerformanceActiveSnapshot>;
}

type FrameHandle = ReturnType<typeof setTimeout> | number;
type ScheduleFrame = (callback: () => void) => FrameHandle;
type CancelFrame = (handle: FrameHandle) => void;

export interface PerformanceDiagnosticsOptions {
  enabled?: boolean;
  now?: () => number;
  wallClockNow?: () => Date;
  scheduleFrame?: ScheduleFrame;
  cancelFrame?: CancelFrame;
  sampleLimit?: number;
}

export interface PerformanceDiagnostics {
  readonly enabled: boolean;
  increment(
    category: PerformanceDiagnosticCategory,
    metric: string,
    amount?: number,
  ): void;
  recordDuration(
    category: PerformanceDiagnosticCategory,
    metric: string,
    durationMs: number,
  ): void;
  recordRender(component: string, instanceId?: string): void;
  startTimer(
    category: PerformanceDiagnosticCategory,
    metric: string,
  ): () => number;
  trackActive(
    category: PerformanceDiagnosticCategory,
    metric: string,
  ): () => void;
  measure<T>(
    category: PerformanceDiagnosticCategory,
    metric: string,
    operation: () => T,
  ): T;
  measureAsync<T>(
    category: PerformanceDiagnosticCategory,
    metric: string,
    operation: () => Promise<T>,
  ): Promise<T>;
  measureInteraction<T>(metric: string, handler: () => T): T;
  startFrameObserver(metric?: string): () => void;
  snapshot(): PerformanceDiagnosticsSnapshot;
  exportSnapshot(): string;
  reset(): void;
}

export interface ConversationPerformanceDiagnosticsBridge {
  snapshot(): PerformanceDiagnosticsSnapshot;
  reset(): void;
  exportSnapshot(): string;
  logSnapshot(): string | null;
}

type PerformanceDiagnosticsGlobalTarget = Record<
  string,
  ConversationPerformanceDiagnosticsBridge | undefined
>;

interface MutableTiming {
  count: number;
  totalMs: number;
  lastMs: number;
  maxMs: number;
  samples: number[];
}

export function isPerformanceDiagnosticsFlagEnabled(
  value = process.env.EXPO_PUBLIC_AOITALK_PERFORMANCE_DIAGNOSTICS,
): boolean {
  return /^(1|true|yes|on)$/i.test(value?.trim() ?? "");
}

function defaultNow(): number {
  return typeof performance !== "undefined" && performance.now
    ? performance.now()
    : Date.now();
}

function defaultScheduleFrame(callback: () => void): FrameHandle {
  if (typeof requestAnimationFrame === "function") {
    return requestAnimationFrame(callback);
  }
  return setTimeout(callback, 16);
}

function defaultCancelFrame(handle: FrameHandle): void {
  if (typeof cancelAnimationFrame === "function" && typeof handle === "number") {
    cancelAnimationFrame(handle);
    return;
  }
  clearTimeout(handle as ReturnType<typeof setTimeout>);
}

function metricKey(
  category: PerformanceDiagnosticCategory,
  metric: string,
): PerformanceMetricKey {
  return `${category}.${metric}`;
}

function percentile95(samples: number[]): number {
  if (samples.length === 0) return 0;
  const sorted = [...samples].sort((left, right) => left - right);
  return sorted[Math.ceil(sorted.length * 0.95) - 1];
}

export function createPerformanceDiagnostics(
  options: PerformanceDiagnosticsOptions = {},
): PerformanceDiagnostics {
  const enabled = options.enabled ?? isPerformanceDiagnosticsFlagEnabled();
  const now = options.now ?? defaultNow;
  const wallClockNow = options.wallClockNow ?? (() => new Date());
  const scheduleFrame = options.scheduleFrame ?? defaultScheduleFrame;
  const cancelFrame = options.cancelFrame ?? defaultCancelFrame;
  const sampleLimit = Math.max(1, options.sampleLimit ?? 256);
  const counts = new Map<PerformanceMetricKey, number>();
  const timings = new Map<PerformanceMetricKey, MutableTiming>();
  const active = new Map<PerformanceMetricKey, PerformanceActiveSnapshot>();

  const recordDuration = (
    category: PerformanceDiagnosticCategory,
    metric: string,
    durationMs: number,
  ) => {
    if (!enabled) return;
    const key = metricKey(category, metric);
    const duration = Math.max(0, durationMs);
    const previous = timings.get(key);
    const samples = previous?.samples ?? [];
    samples.push(duration);
    if (samples.length > sampleLimit) samples.shift();
    timings.set(key, {
      count: (previous?.count ?? 0) + 1,
      totalMs: (previous?.totalMs ?? 0) + duration,
      lastMs: duration,
      maxMs: Math.max(previous?.maxMs ?? 0, duration),
      samples,
    });
  };

  const startTimer = (
    category: PerformanceDiagnosticCategory,
    metric: string,
  ) => {
    if (!enabled) return () => 0;
    const startedAt = now();
    let finished = false;
    let duration = 0;
    return () => {
      if (finished) return duration;
      finished = true;
      duration = Math.max(0, now() - startedAt);
      recordDuration(category, metric, duration);
      return duration;
    };
  };

  const diagnostics: PerformanceDiagnostics = {
    enabled,

    increment(category, metric, amount = 1) {
      if (!enabled) return;
      const key = metricKey(category, metric);
      counts.set(key, (counts.get(key) ?? 0) + amount);
    },

    recordDuration,

    recordRender(component, instanceId) {
      if (!enabled) return;
      diagnostics.increment("render", component);
      if (instanceId) diagnostics.increment("render", `${component}.${instanceId}`);
    },

    startTimer,

    trackActive(category, metric) {
      if (!enabled) return () => undefined;
      const key = metricKey(category, metric);
      const previous = active.get(key);
      const current = (previous?.current ?? 0) + 1;
      active.set(key, {
        current,
        max: Math.max(previous?.max ?? 0, current),
        totalStarted: (previous?.totalStarted ?? 0) + 1,
      });
      let stopped = false;
      return () => {
        if (stopped) return;
        stopped = true;
        const latest = active.get(key);
        if (!latest) return;
        active.set(key, {
          ...latest,
          current: Math.max(0, latest.current - 1),
        });
      };
    },

    measure(category, metric, operation) {
      if (!enabled) return operation();
      const stop = startTimer(category, metric);
      try {
        return operation();
      } finally {
        stop();
      }
    },

    async measureAsync(category, metric, operation) {
      if (!enabled) return operation();
      const stop = startTimer(category, metric);
      try {
        return await operation();
      } finally {
        stop();
      }
    },

    measureInteraction(metric, handler) {
      if (!enabled) return handler();
      const stopVisual = startTimer("interaction", `${metric}.visual`);
      scheduleFrame(stopVisual);
      return diagnostics.measure("interaction", `${metric}.handler`, handler);
    },

    startFrameObserver(metric = "ChatScreen") {
      if (!enabled) return () => undefined;
      let stopped = false;
      let handle: FrameHandle | null = null;
      let previousFrameAt: number | null = null;
      const observe = () => {
        if (stopped) return;
        const frameAt = now();
        if (previousFrameAt !== null) {
          const interval = Math.max(0, frameAt - previousFrameAt);
          recordDuration("frame", `${metric}.interval`, interval);
          diagnostics.increment("frame", `${metric}.sample`);
          if (interval > 34) {
            const droppedFrames = Math.max(1, Math.round(interval / 16.667) - 1);
            diagnostics.increment("frame", `${metric}.dropped-frame`, droppedFrames);
            recordDuration("frame", `${metric}.dropped-frame`, interval);
          }
          if (interval >= 50) {
            diagnostics.increment("frame", `${metric}.long-task`);
            recordDuration("frame", `${metric}.long-task`, interval);
          }
        }
        previousFrameAt = frameAt;
        handle = scheduleFrame(observe);
      };
      handle = scheduleFrame(observe);
      return () => {
        if (stopped) return;
        stopped = true;
        if (handle !== null) cancelFrame(handle);
      };
    },

    snapshot() {
      return {
        enabled,
        capturedAt: wallClockNow().toISOString(),
        counts: Object.fromEntries(counts),
        timings: Object.fromEntries(
          [...timings].map(([key, value]) => [
            key,
            {
              count: value.count,
              totalMs: value.totalMs,
              averageMs: value.count > 0 ? value.totalMs / value.count : 0,
              lastMs: value.lastMs,
              maxMs: value.maxMs,
              p95Ms: percentile95(value.samples),
            },
          ]),
        ),
        active: Object.fromEntries(
          [...active].map(([key, value]) => [key, { ...value }]),
        ),
      };
    },

    exportSnapshot() {
      return JSON.stringify(diagnostics.snapshot(), null, 2);
    },

    reset() {
      counts.clear();
      timings.clear();
      active.clear();
    },
  };

  return diagnostics;
}

export const conversationPerformanceDiagnostics =
  createPerformanceDiagnostics();

export function getConversationPerformanceSnapshot(): PerformanceDiagnosticsSnapshot {
  return conversationPerformanceDiagnostics.snapshot();
}

export function resetConversationPerformanceDiagnostics(): void {
  conversationPerformanceDiagnostics.reset();
}

export function exportConversationPerformanceDiagnostics(): string {
  return conversationPerformanceDiagnostics.exportSnapshot();
}

export function logConversationPerformanceSnapshot(
  log: (message: string) => void = console.info,
  diagnostics: PerformanceDiagnostics = conversationPerformanceDiagnostics,
): string | null {
  return logPerformanceSnapshot(diagnostics, log);
}

function logPerformanceSnapshot(
  diagnostics: PerformanceDiagnostics,
  log: (message: string) => void,
): string | null {
  if (!diagnostics.enabled) return null;
  const message = `${PERFORMANCE_DIAGNOSTICS_LOG_PREFIX} ${JSON.stringify(
    diagnostics.snapshot(),
  )}`;
  log(message);
  return message;
}

export function installConversationPerformanceDiagnosticsBridge(
  target: PerformanceDiagnosticsGlobalTarget = globalThis as unknown as PerformanceDiagnosticsGlobalTarget,
  diagnostics: PerformanceDiagnostics = conversationPerformanceDiagnostics,
  log: (message: string) => void = console.info,
): () => void {
  if (!diagnostics.enabled) {
    delete target[PERFORMANCE_DIAGNOSTICS_GLOBAL_KEY];
    return () => undefined;
  }
  const bridge: ConversationPerformanceDiagnosticsBridge = {
    snapshot: () => diagnostics.snapshot(),
    reset: () => diagnostics.reset(),
    exportSnapshot: () => diagnostics.exportSnapshot(),
    logSnapshot: () => logPerformanceSnapshot(diagnostics, log),
  };
  target[PERFORMANCE_DIAGNOSTICS_GLOBAL_KEY] = bridge;
  return () => {
    if (target[PERFORMANCE_DIAGNOSTICS_GLOBAL_KEY] === bridge) {
      delete target[PERFORMANCE_DIAGNOSTICS_GLOBAL_KEY];
    }
  };
}

installConversationPerformanceDiagnosticsBridge();
