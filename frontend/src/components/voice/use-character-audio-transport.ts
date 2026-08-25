"use client";

import { useCallback, useEffect, useRef } from "react";
import { voiceSessionAudioWebSocketUrl } from "@/lib/voice-session-api";

export type CharacterAudioTransportState =
  | "idle"
  | "connecting"
  | "connected"
  | "disconnecting"
  | "error";

export type CharacterAudioAck = {
  generationId: string;
  sequence: number;
};

export type UseCharacterAudioTransportOptions = {
  sessionId: string | null;
  enabled: boolean;
  onStateChange?: (state: CharacterAudioTransportState) => void;
  onError?: (message: string) => void;
};

export type UseCharacterAudioTransportResult = {
  state: CharacterAudioTransportState;
  flush: () => void;
  sendAck: (ack: CharacterAudioAck) => void;
};

type PendingSegment = {
  generationId: string;
  sequence: number;
  segmentId: string;
};

type PendingFrame = PendingSegment & {
  data: ArrayBuffer;
};

/**
 * WebSocket transport for `/api/voice-sessions/{id}/audio`.
 * Handles binary TTS frames, bounded queueing, and playback acks.
 */
export function useCharacterAudioTransport({
  sessionId,
  enabled,
  onStateChange,
  onError,
}: UseCharacterAudioTransportOptions): UseCharacterAudioTransportResult {
  const stateRef = useRef<CharacterAudioTransportState>("idle");
  const socketRef = useRef<WebSocket | null>(null);
  const queueRef = useRef<PendingFrame[]>([]);
  const pendingSegmentRef = useRef<PendingSegment | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const currentGenerationRef = useRef<string | null>(null);
  const activeSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const ackEnabledRef = useRef(true);
  const playbackEpochRef = useRef(0);
  const playingRef = useRef(false);
  const mountedRef = useRef(true);
  const closingRef = useRef(false);
  const failedRef = useRef(false);

  const setState = useCallback(
    (next: CharacterAudioTransportState) => {
      if (!mountedRef.current || stateRef.current === next) return;
      stateRef.current = next;
      onStateChange?.(next);
    },
    [onStateChange],
  );

  const closeAudioContext = useCallback(() => {
    const context = audioContextRef.current;
    audioContextRef.current = null;
    if (!context) return;
    try {
      const result = context.close?.();
      if (result && typeof result.catch === "function") {
        void result.catch(() => undefined);
      }
    } catch {
      // best effort cleanup
    }
  }, []);

  const stopActivePlayback = useCallback(() => {
    ackEnabledRef.current = false;
    playbackEpochRef.current += 1;
    const source = activeSourceRef.current;
    activeSourceRef.current = null;
    if (!source) return;
    try {
      source.stop(0);
    } catch {
      // already stopped
    }
    // Some browser/test implementations do not dispatch `ended` after stop.
    // Resolve the drain promise without acknowledging the interrupted frame.
    try {
      source.onended?.(new Event("ended"));
    } catch {
      // no-op
    }
  }, []);

  const ensureAudioContext = useCallback(async (): Promise<AudioContext> => {
    if (typeof window === "undefined") {
      throw new Error("キャラクター音声を再生できるブラウザ環境ではありません");
    }
    if (!audioContextRef.current) {
      const Ctor =
        window.AudioContext ??
        (window as typeof window & { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!Ctor) {
        throw new Error("キャラクター音声を再生できるブラウザ環境ではありません");
      }
      audioContextRef.current = new Ctor();
    }
    const context = audioContextRef.current;
    if (context.state === "suspended") {
      await context.resume();
    }
    return context;
  }, []);

  const sendAck = useCallback((ack: CharacterAudioAck) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(
        JSON.stringify({
          type: "audio.ack",
          generation_id: ack.generationId,
          sequence: ack.sequence,
        }),
      );
    } catch {
      // A close/error handler will fail the transport and clear playback.
    }
  }, []);

  const failTransport = useCallback(
    (message: string) => {
      if (failedRef.current) return;
      failedRef.current = true;
      closingRef.current = true;
      stopActivePlayback();
      queueRef.current = [];
      pendingSegmentRef.current = null;
      currentGenerationRef.current = null;
      closeAudioContext();
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        try {
          socket.close();
        } catch {
          // best effort cleanup
        }
      }
      setState("error");
      onError?.(message);
    },
    [closeAudioContext, onError, setState, stopActivePlayback],
  );

  const playFrame = useCallback(
    async (frame: PendingFrame, playbackEpoch: number) => {
      try {
        const context = await ensureAudioContext();
        if (playbackEpochRef.current !== playbackEpoch) return;
        const buffer = await context.decodeAudioData(frame.data.slice(0));
        if (playbackEpochRef.current !== playbackEpoch) return;
        await new Promise<void>((resolve) => {
          const source = context.createBufferSource();
          source.buffer = buffer;
          source.connect(context.destination);
          activeSourceRef.current = source;
          ackEnabledRef.current = true;
          let settled = false;
          const finish = () => {
            if (settled) return;
            settled = true;
            if (activeSourceRef.current === source) {
              activeSourceRef.current = null;
            }
            if (
              ackEnabledRef.current &&
              playbackEpochRef.current === playbackEpoch
            ) {
              sendAck({ generationId: frame.generationId, sequence: frame.sequence });
            }
            resolve();
          };
          source.onended = finish;
          source.start();
        });
      } catch {
        failTransport("キャラクター音声の再生に失敗しました");
      }
    },
    [ensureAudioContext, failTransport, sendAck],
  );

  const drainQueue = useCallback(async () => {
    if (playingRef.current || failedRef.current) return;
    playingRef.current = true;
    const playbackEpoch = playbackEpochRef.current;
    try {
      while (queueRef.current.length > 0) {
        if (playbackEpochRef.current !== playbackEpoch || failedRef.current) break;
        const frame = queueRef.current.shift();
        if (!frame) break;
        if (
          currentGenerationRef.current &&
          frame.generationId !== currentGenerationRef.current
        ) {
          continue;
        }
        await playFrame(frame, playbackEpoch);
      }
    } finally {
      playingRef.current = false;
    }
  }, [playFrame]);

  const flush = useCallback(() => {
    stopActivePlayback();
    queueRef.current = [];
    pendingSegmentRef.current = null;
    currentGenerationRef.current = null;
  }, [stopActivePlayback]);

  const disconnect = useCallback(() => {
    closingRef.current = true;
    stopActivePlayback();
    const socket = socketRef.current;
    socketRef.current = null;
    queueRef.current = [];
    pendingSegmentRef.current = null;
    currentGenerationRef.current = null;
    closeAudioContext();
    if (socket) {
      setState("disconnecting");
      try {
        socket.close();
      } catch {
        // no-op
      }
    }
    setState("idle");
  }, [closeAudioContext, setState, stopActivePlayback]);

  useEffect(() => {
    mountedRef.current = true;
    failedRef.current = false;
    closingRef.current = false;
    if (!enabled || !sessionId) {
      disconnect();
      return () => {
        mountedRef.current = false;
        disconnect();
      };
    }

    setState("connecting");
    const url = voiceSessionAudioWebSocketUrl(sessionId);
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      failTransport("キャラクター音声チャネルに接続できませんでした");
      return () => {
        mountedRef.current = false;
        disconnect();
      };
    }
    socket.binaryType = "arraybuffer";
    socketRef.current = socket;

    socket.onopen = () => {
      if (!mountedRef.current || socketRef.current !== socket) return;
      setState("connected");
    };
    socket.onmessage = (event) => {
      if (!mountedRef.current || socketRef.current !== socket) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data) as Record<string, unknown>;
          const messageType = String(payload.type ?? "");
          if (
            messageType === "audio.generation" &&
            typeof payload.generation_id === "string"
          ) {
            currentGenerationRef.current = payload.generation_id;
            return;
          }
          if (messageType === "audio.clear") {
            if (
              !currentGenerationRef.current ||
              payload.generation_id === currentGenerationRef.current
            ) {
              flush();
            }
            return;
          }
          if (messageType === "audio.segment") {
            const generationId = String(payload.generation_id ?? "");
            const sequence = Number(payload.sequence);
            const segmentId = String(payload.segment_id ?? "");
            if (!generationId || Number.isNaN(sequence)) return;
            if (
              currentGenerationRef.current &&
              generationId !== currentGenerationRef.current
            ) {
              return;
            }
            if (!currentGenerationRef.current) return;
            pendingSegmentRef.current = { generationId, sequence, segmentId };
          }
        } catch {
          // ignore control parse errors
        }
        return;
      }
      if (!(event.data instanceof ArrayBuffer)) return;
      const pending = pendingSegmentRef.current;
      if (!pending) return;
      if (
        currentGenerationRef.current &&
        pending.generationId !== currentGenerationRef.current
      ) {
        pendingSegmentRef.current = null;
        return;
      }
      pendingSegmentRef.current = null;
      queueRef.current.push({ ...pending, data: event.data });
      if (queueRef.current.length > 8) queueRef.current.shift();
      void drainQueue();
    };
    socket.onerror = () => {
      if (!mountedRef.current || socketRef.current !== socket || closingRef.current) {
        return;
      }
      failTransport("キャラクター音声チャネルでエラーが発生しました");
    };
    socket.onclose = () => {
      if (!mountedRef.current || socketRef.current !== socket || closingRef.current) {
        return;
      }
      failTransport("キャラクター音声チャネルが切断されました");
    };

    return () => {
      mountedRef.current = false;
      disconnect();
    };
  }, [disconnect, drainQueue, enabled, failTransport, flush, sessionId, setState]);

  return {
    state: stateRef.current,
    flush,
    sendAck,
  };
}
