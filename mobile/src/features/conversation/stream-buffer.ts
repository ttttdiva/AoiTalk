export const DEFAULT_STREAM_PUBLISH_INTERVAL_MS = 50;
export const MIN_STREAM_PUBLISH_INTERVAL_MS = 50;
export const MAX_STREAM_PUBLISH_INTERVAL_MS = 100;

export type StreamLifecycleId = string | number;

export interface StreamBufferIdentity {
  sessionId: string;
  lifecycleId: StreamLifecycleId;
}

export type StreamFlushReason =
  | "interval"
  | "manual"
  | "terminal"
  | "cancel"
  | "error"
  | "session-switch"
  | "unmount";

export interface StreamBufferPublication extends StreamBufferIdentity {
  text: string;
  delta: string;
  chunkCount: number;
  totalChunkCount: number;
  reason: StreamFlushReason;
}

export interface StreamBufferSnapshot extends StreamBufferIdentity {
  text: string;
  pendingChunkCount: number;
  totalChunkCount: number;
  acceptingChunks: boolean;
  disposed: boolean;
}

export interface StreamBufferOptions {
  identity: StreamBufferIdentity;
  onPublish: (publication: StreamBufferPublication) => void;
  publishIntervalMs?: number;
}

export interface StreamBuffer {
  append(identity: StreamBufferIdentity, chunk: string): boolean;
  flush(reason?: StreamFlushReason): string;
  finalize(
    identity: StreamBufferIdentity,
    reason: "terminal" | "cancel" | "error",
  ): string | null;
  switchIdentity(identity: StreamBufferIdentity): string;
  dispose(): string;
  snapshot(): StreamBufferSnapshot;
}

function sameIdentity(
  left: StreamBufferIdentity,
  right: StreamBufferIdentity,
): boolean {
  return (
    left.sessionId === right.sessionId &&
    left.lifecycleId === right.lifecycleId
  );
}

function normalizePublishInterval(intervalMs: number | undefined): number {
  const requested = intervalMs ?? DEFAULT_STREAM_PUBLISH_INTERVAL_MS;
  if (!Number.isFinite(requested)) return DEFAULT_STREAM_PUBLISH_INTERVAL_MS;
  return Math.min(
    MAX_STREAM_PUBLISH_INTERVAL_MS,
    Math.max(MIN_STREAM_PUBLISH_INTERVAL_MS, requested),
  );
}

/**
 * WebSocket chunks are kept outside React state and published at a bounded rate.
 * Every mutating call carries the session/lifecycle identity so late events from
 * a previous generation cannot enter the active buffer.
 */
export function createStreamBuffer(options: StreamBufferOptions): StreamBuffer {
  const publishIntervalMs = normalizePublishInterval(options.publishIntervalMs);
  let identity = { ...options.identity };
  let pendingChunks: string[] = [];
  let publishedText = "";
  let totalChunkCount = 0;
  let acceptingChunks = true;
  let disposed = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const cancelTimer = () => {
    if (timer === null) return;
    clearTimeout(timer);
    timer = null;
  };

  const flush = (reason: StreamFlushReason = "manual"): string => {
    cancelTimer();
    if (pendingChunks.length === 0) return publishedText;

    const chunks = pendingChunks;
    pendingChunks = [];
    const delta = chunks.join("");
    publishedText += delta;
    options.onPublish({
      ...identity,
      text: publishedText,
      delta,
      chunkCount: chunks.length,
      totalChunkCount,
      reason,
    });
    return publishedText;
  };

  const schedulePublish = () => {
    if (timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      flush("interval");
    }, publishIntervalMs);
  };

  return {
    append(candidateIdentity, chunk) {
      if (
        disposed ||
        !acceptingChunks ||
        !sameIdentity(identity, candidateIdentity)
      ) {
        return false;
      }
      pendingChunks.push(chunk);
      totalChunkCount += 1;
      schedulePublish();
      return true;
    },

    flush,

    finalize(candidateIdentity, reason) {
      if (
        disposed ||
        !acceptingChunks ||
        !sameIdentity(identity, candidateIdentity)
      ) {
        return null;
      }
      acceptingChunks = false;
      return flush(reason);
    },

    switchIdentity(nextIdentity) {
      if (disposed) return publishedText;
      if (sameIdentity(identity, nextIdentity)) return publishedText;

      const previousText = flush("session-switch");
      identity = { ...nextIdentity };
      pendingChunks = [];
      publishedText = "";
      totalChunkCount = 0;
      acceptingChunks = true;
      return previousText;
    },

    dispose() {
      if (disposed) return publishedText;
      acceptingChunks = false;
      const finalText = flush("unmount");
      disposed = true;
      return finalText;
    },

    snapshot() {
      return {
        ...identity,
        text: publishedText + pendingChunks.join(""),
        pendingChunkCount: pendingChunks.length,
        totalChunkCount,
        acceptingChunks,
        disposed,
      };
    },
  };
}
