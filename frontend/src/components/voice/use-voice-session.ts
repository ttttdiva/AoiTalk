"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createLiveVoiceSession,
  endLiveVoiceSession,
  exchangeLiveVoiceSdp,
  getLiveVoiceSession,
  LIVE_VOICE_END_TIMEOUT_MS,
  LiveVoiceApiError,
  postLiveVoiceEvent,
  type LiveVoiceEvent,
} from "@/lib/live-voice-api";
import {
  createVoiceSession,
  endVoiceSession,
  exchangeVoiceSessionSdp,
  getVoiceSession,
  interruptVoiceSession,
  postVoiceSessionEvent,
  type VoiceSessionMode,
} from "@/lib/voice-session-api";
import {
  isBrowserTelemetryWireType,
  useRealtimeWebRTC,
  type RealtimePresentationEvent,
} from "./use-realtime-webrtc";
import { useCharacterAudioTransport } from "./use-character-audio-transport";

export type VoiceSessionPhase =
  | "idle"
  | "requesting"
  | "connecting"
  | "connected"
  | "disconnecting"
  | "ended"
  | "error";

export type VoiceTranscript = {
  id: string;
  role: "user" | "assistant";
  text: string;
  final: boolean;
};

export type VoiceSessionState = {
  phase: VoiceSessionPhase;
  sessionId: string | null;
  mode: VoiceSessionMode;
  provider: string | null;
  model: string | null;
  muted: boolean;
  transcripts: VoiceTranscript[];
  progress: number;
  statusMessage: string;
  error: string | null;
};

export type UseVoiceSessionOptions = {
  mode?: VoiceSessionMode;
  conversationSessionId?: string | null;
  projectId?: string | null;
  includeProjectContext?: boolean | null;
  characterName?: string | null;
  ensureConversationSession?: () => Promise<string>;
};

export type VoiceSessionStopOptions = {
  endSession?: boolean;
};

export type UseVoiceSessionResult = {
  state: VoiceSessionState;
  remoteAudioRef: React.MutableRefObject<HTMLAudioElement | null>;
  start: () => Promise<void>;
  stop: (options?: VoiceSessionStopOptions) => Promise<void>;
  interrupt: () => void;
  toggleMute: () => void;
};

const STATUS_POLL_MS = 1500;
const MAX_ENDED_SESSION_IDS = 128;

type StatusPoll = {
  id: string;
  generation: number;
  controller: AbortController;
  timer: ReturnType<typeof setTimeout> | null;
};

type SessionApi = {
  create: (input: {
    conversationSessionId: string | null;
    projectId: string | null;
    includeProjectContext: boolean | null;
    characterName: string | null;
    mode: VoiceSessionMode;
  }) => Promise<{ id: string; provider?: string; model?: string }>;
  get: (
    id: string,
    signal?: AbortSignal,
  ) => Promise<Record<string, unknown>>;
  exchangeSdp: (sessionId: string, sdp: string) => Promise<string>;
  postEvent: (sessionId: string, event: LiveVoiceEvent) => Promise<unknown>;
  end: (sessionId: string, signal?: AbortSignal) => Promise<unknown>;
};

function liveVoiceApiAdapter(): SessionApi {
  return {
    create: async (input) => {
      const session = await createLiveVoiceSession({
        conversationSessionId: input.conversationSessionId,
        projectId: input.projectId,
        includeProjectContext: input.includeProjectContext,
        characterName: input.characterName,
      });
      return session;
    },
    get: async (id, signal) =>
      (await getLiveVoiceSession(id, { signal })) as Record<string, unknown>,
    exchangeSdp: async (sessionId, sdp) => {
      const answer = await exchangeLiveVoiceSdp({ sessionId, sdp });
      return answer.sdp;
    },
    postEvent: (sessionId, event) => postLiveVoiceEvent(sessionId, event),
    end: (sessionId, signal) => endLiveVoiceSession(sessionId, { signal }),
  };
}

function unifiedVoiceApiAdapter(): SessionApi {
  return {
    create: async (input) => {
      const session = await createVoiceSession({
        mode: input.mode,
        conversationSessionId: input.conversationSessionId,
        projectId: input.projectId,
        includeProjectContext: input.includeProjectContext,
        characterName: input.characterName,
      });
      return session;
    },
    get: async (id, signal) =>
      (await getVoiceSession(id, { signal })) as Record<string, unknown>,
    exchangeSdp: async (sessionId, sdp) => {
      const answer = await exchangeVoiceSessionSdp({ sessionId, sdp });
      return answer.sdp;
    },
    postEvent: (sessionId, event) => postVoiceSessionEvent(sessionId, event),
    end: (sessionId, signal) => endVoiceSession(sessionId, { signal }),
  };
}

function resolveSessionApi(mode: VoiceSessionMode): SessionApi {
  return mode === "realtime_native"
    ? liveVoiceApiAdapter()
    : unifiedVoiceApiAdapter();
}

function initialState(mode: VoiceSessionMode): VoiceSessionState {
  return {
    phase: "idle",
    sessionId: null,
    mode,
    provider: null,
    model: null,
    muted: false,
    transcripts: [],
    progress: 0,
    statusMessage: "音声会話を開始できます",
    error: null,
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "マイクの使用が許可されませんでした";
  }
  if (error instanceof LiveVoiceApiError) return error.message;
  if (error instanceof Error && error.message.trim()) {
    const message = error.message.trim();
    if (
      message.startsWith("Live Voice") ||
      message.startsWith("音声") ||
      message.startsWith("このブラウザ") ||
      message.startsWith("マイク") ||
      message.startsWith("会話セッション") ||
      message.startsWith("音声接続") ||
      message.startsWith("Realtimeデータ接続")
    ) {
      return message;
    }
  }
  return "音声会話に接続できませんでした";
}

function nextTranscriptId(role: "user" | "assistant"): string {
  const suffix =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${role}-${suffix}`;
}

function wireTypeForPresentation(event: RealtimePresentationEvent): string | null {
  switch (event.type) {
    case "session_ready":
      return "session.created";
    case "response_started":
      return "response.created";
    case "response_completed":
      return "response.done";
    case "provider_error":
      return "error";
    default:
      return null;
  }
}

function presentationTelemetryEvent(
  event: RealtimePresentationEvent,
): LiveVoiceEvent | null {
  const wireType = wireTypeForPresentation(event);
  if (!wireType) return null;
  const eventId =
    "eventId" in event && typeof event.eventId === "string" ? event.eventId : undefined;
  return {
    type: wireType,
    source: "browser",
    ...(eventId ? { event_id: eventId } : {}),
  };
}

export function useVoiceSession({
  mode = "realtime_native",
  conversationSessionId = null,
  projectId = null,
  includeProjectContext = null,
  characterName = null,
  ensureConversationSession,
}: UseVoiceSessionOptions = {}): UseVoiceSessionResult {
  const [state, setState] = useState<VoiceSessionState>(() => initialState(mode));
  const mountedRef = useRef(true);
  const sessionIdRef = useRef<string | null>(null);
  const transcriptDraftRef = useRef<{ user: string | null; assistant: string | null }>({
    user: null,
    assistant: null,
  });
  const stopPromiseRef = useRef<Promise<void> | null>(null);
  const stopPromiseGenerationRef = useRef<number | null>(null);
  const lifecycleRef = useRef<"idle" | "starting" | "active" | "stopping">("idle");
  const operationRef = useRef(0);
  const startPromiseRef = useRef<Promise<void> | null>(null);
  const endedSessionPromisesRef = useRef(new Map<string, Promise<void>>());
  const endedSessionIdsRef = useRef(new Set<string>());
  const statusPollRef = useRef<StatusPoll | null>(null);
  const stopRef = useRef<((options?: VoiceSessionStopOptions) => Promise<void>) | null>(null);
  const sessionModeRef = useRef<VoiceSessionMode>(mode);
  const sessionApiRef = useRef(resolveSessionApi(mode));
  // A caller may rerender with a different default while a call is active.
  // Keep the API adapter and mode captured for that call; only an idle session
  // may adopt a new selector value.
  if (lifecycleRef.current === "idle" && sessionModeRef.current !== mode) {
    sessionModeRef.current = mode;
    sessionApiRef.current = resolveSessionApi(mode);
  }

  const setSafeState = useCallback(
    (updater: (previous: VoiceSessionState) => VoiceSessionState) => {
      if (mountedRef.current) setState(updater);
    },
    [],
  );

  const appendTranscript = useCallback(
    (role: "user" | "assistant", text: string, final: boolean) => {
      const value = text.trim();
      if (!value) return;
      setSafeState((previous) => {
        const draftId = transcriptDraftRef.current[role];
        const draftIndex = draftId
          ? previous.transcripts.findIndex((item) => item.id === draftId)
          : -1;
        if (draftIndex >= 0) {
          const transcripts = [...previous.transcripts];
          const current = transcripts[draftIndex];
          transcripts[draftIndex] = {
            ...current,
            text: final ? value : `${current.text}${text}`,
            final,
          };
          if (final) transcriptDraftRef.current[role] = null;
          return { ...previous, transcripts };
        }
        const id = nextTranscriptId(role);
        if (!final) transcriptDraftRef.current[role] = id;
        return {
          ...previous,
          transcripts: [...previous.transcripts, { id, role, text, final }],
        };
      });
    },
    [setSafeState],
  );

  const reportEvent = useCallback((event: LiveVoiceEvent) => {
    const id = sessionIdRef.current;
    if (!id) return;
    const type = typeof event.type === "string" ? event.type : "";
    if (!isBrowserTelemetryWireType(type)) return;
    const eventId =
      typeof event.event_id === "string"
        ? event.event_id
        : typeof event.id === "string"
          ? event.id
          : undefined;
    void sessionApiRef.current
      .postEvent(id, {
        type,
        source: "browser",
        ...(eventId ? { event_id: eventId } : {}),
      })
      .catch(() => {
        // transient reporting failure must not tear down the call
      });
  }, []);

  const endSessionOnce = useCallback((id: string): Promise<void> => {
    if (endedSessionIdsRef.current.has(id)) return Promise.resolve();
    const existing = endedSessionPromisesRef.current.get(id);
    if (existing) return existing;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), LIVE_VOICE_END_TIMEOUT_MS);
    const promise = sessionApiRef.current
      .end(id, controller.signal)
      .then(() => undefined)
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(timeout);
        endedSessionIdsRef.current.add(id);
        while (endedSessionIdsRef.current.size > MAX_ENDED_SESSION_IDS) {
          const oldest = endedSessionIdsRef.current.values().next().value;
          if (typeof oldest !== "string") break;
          endedSessionIdsRef.current.delete(oldest);
        }
        if (endedSessionPromisesRef.current.get(id) === promise) {
          endedSessionPromisesRef.current.delete(id);
        }
      });
    endedSessionPromisesRef.current.set(id, promise);
    return promise;
  }, []);

  const cancelStatusPoll = useCallback(() => {
    const poll = statusPollRef.current;
    statusPollRef.current = null;
    if (!poll) return;
    poll.controller.abort();
    if (poll.timer !== null) clearTimeout(poll.timer);
  }, []);

  const handlePresentationEvent = useCallback(
    (event: RealtimePresentationEvent) => {
      const telemetry = presentationTelemetryEvent(event);
      if (telemetry) reportEvent(telemetry);
      switch (event.type) {
        case "session_ready":
          setSafeState((previous) => ({
            ...previous,
            phase: "connected",
            progress: 5,
            statusMessage: "接続しました。話しかけてください",
          }));
          break;
        case "user_speaking":
          setSafeState((previous) => ({
            ...previous,
            progress: 10,
            statusMessage: "聞き取り中…",
          }));
          break;
        case "response_started":
          setSafeState((previous) => ({
            ...previous,
            progress: 25,
            statusMessage: "応答を生成中…",
          }));
          break;
        case "playback_started":
          setSafeState((previous) => ({
            ...previous,
            progress: Math.max(previous.progress, 45),
            statusMessage: "音声を再生中…",
          }));
          break;
        case "assistant_transcript_delta":
          appendTranscript("assistant", event.delta, false);
          break;
        case "assistant_transcript_done":
          appendTranscript("assistant", event.text, true);
          break;
        case "user_transcript_delta":
          appendTranscript("user", event.delta, false);
          break;
        case "user_transcript_done":
          appendTranscript("user", event.text, true);
          break;
        case "response_completed":
          setSafeState((previous) => ({
            ...previous,
            progress: 100,
            statusMessage: "聞き取り中…",
          }));
          break;
        case "provider_error": {
          const cleanup = stopRef.current;
          if (cleanup) {
            void cleanup().then(() =>
              setSafeState((previous) => ({
                ...previous,
                phase: "error",
                error: event.message,
                statusMessage: "エラーが発生しました",
              })),
            );
          } else {
            setSafeState((previous) => ({
              ...previous,
              phase: "error",
              error: event.message,
              statusMessage: "エラーが発生しました",
            }));
          }
          break;
        }
        default:
          break;
      }
    },
    [appendTranscript, reportEvent, setSafeState],
  );

  const webrtc = useRealtimeWebRTC({
    onPresentationEvent: handlePresentationEvent,
    onConnectionLost: (message) => {
      const cleanup = stopRef.current;
      if (!cleanup) return;
      void cleanup().then(() =>
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: message,
        })),
      );
    },
    onDataChannelClosed: () => {
      if (lifecycleRef.current === "active" || lifecycleRef.current === "starting") {
        void stopRef.current?.();
      }
    },
    onDataChannelError: (message) => {
      if (lifecycleRef.current === "stopping" || lifecycleRef.current === "idle") return;
      const cleanup = stopRef.current;
      if (!cleanup) return;
      void cleanup().then(() =>
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: message,
        })),
      );
    },
  });
  const webrtcRef = useRef(webrtc);
  webrtcRef.current = webrtc;

  const handleCharacterAudioError = useCallback(
    (message: string) => {
      if (
        lifecycleRef.current === "idle" ||
        lifecycleRef.current === "stopping"
      ) {
        return;
      }
      const cleanup = stopRef.current;
      if (!cleanup) {
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: "エラーが発生しました",
        }));
        return;
      }
      void cleanup().then(() =>
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: "エラーが発生しました",
        })),
      );
    },
    [setSafeState],
  );

  const characterAudio = useCharacterAudioTransport({
    sessionId: state.sessionId,
    enabled:
      sessionModeRef.current === "realtime_character_tts" && !!state.sessionId,
    onError: handleCharacterAudioError,
  });
  const characterAudioRef = useRef(characterAudio);
  characterAudioRef.current = characterAudio;

  const stop = useCallback(
    async (options: VoiceSessionStopOptions = {}) => {
      if (
        stopPromiseRef.current &&
        stopPromiseGenerationRef.current === operationRef.current
      ) {
        return stopPromiseRef.current;
      }
      const shouldEndSession = options.endSession !== false;
      const stopGeneration = ++operationRef.current;
      startPromiseRef.current = null;
      lifecycleRef.current = "stopping";
      cancelStatusPoll();
      characterAudioRef.current.flush();
      const id = sessionIdRef.current;
      if (!id && !webrtcRef.current.getMediaStream()) {
        lifecycleRef.current = "idle";
        setSafeState((previous) => ({
          ...previous,
          phase: previous.phase === "idle" ? "idle" : "ended",
          progress: 0,
          statusMessage: "音声会話を開始できます",
        }));
        return;
      }

      const promise = (async () => {
        setSafeState((previous) => ({
          ...previous,
          phase: "disconnecting",
          statusMessage: "切断中…",
        }));
        webrtcRef.current.cleanupActive();
        if (shouldEndSession && id) {
          await endSessionOnce(id);
        }
        sessionIdRef.current = null;
        transcriptDraftRef.current = { user: null, assistant: null };
        lifecycleRef.current = "idle";
        setSafeState((previous) => ({
          ...previous,
          phase: "ended",
          sessionId: null,
          muted: false,
          progress: 0,
          statusMessage: "音声会話を終了しました",
        }));
      })().finally(() => {
        if (stopPromiseRef.current === promise) {
          stopPromiseRef.current = null;
          stopPromiseGenerationRef.current = null;
        }
        if (
          operationRef.current === stopGeneration &&
          lifecycleRef.current === "stopping"
        ) {
          lifecycleRef.current = "idle";
        }
      });
      stopPromiseRef.current = promise;
      stopPromiseGenerationRef.current = stopGeneration;
      return promise;
    },
    [cancelStatusPoll, endSessionOnce, setSafeState],
  );

  stopRef.current = stop;

  const monitorSession = useCallback(
    (id: string, generation: number) => {
      cancelStatusPoll();
      const poll: StatusPoll = {
        id,
        generation,
        controller: new AbortController(),
        timer: null,
      };
      statusPollRef.current = poll;
      const isCurrent = () =>
        mountedRef.current &&
        operationRef.current === generation &&
        lifecycleRef.current === "active" &&
        sessionIdRef.current === id &&
        statusPollRef.current === poll &&
        !poll.controller.signal.aborted;
      const failSession = async (message: string) => {
        if (!isCurrent()) return;
        await stop({ endSession: false });
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: message,
        }));
      };
      const pollOnce = async (): Promise<void> => {
        if (!isCurrent()) return;
        try {
          const session = await sessionApiRef.current.get(id, poll.controller.signal);
          if (!isCurrent()) return;
          const nested =
            session.session && typeof session.session === "object"
              ? (session.session as Record<string, unknown>)
              : null;
          const statusValue =
            typeof session.status === "string"
              ? session.status
              : typeof nested?.status === "string"
                ? (nested.status as string)
                : "";
          const status = statusValue.trim().toLowerCase();
          if (["failed", "expired", "closed", "ended", "error"].includes(status)) {
            await failSession(`音声セッションが${status}になりました`);
            return;
          }
        } catch (error) {
          if (poll.controller.signal.aborted || !isCurrent()) return;
          const status = error instanceof LiveVoiceApiError ? error.status : null;
          if ([401, 403, 404, 410].includes(status ?? 0)) {
            await failSession("音声セッションが終了しました");
          }
        }
        if (!isCurrent()) return;
        poll.timer = setTimeout(() => {
          poll.timer = null;
          void pollOnce();
        }, STATUS_POLL_MS);
      };
      void pollOnce();
    },
    [cancelStatusPoll, setSafeState, stop],
  );

  const start = useCallback(() => {
    if (startPromiseRef.current || lifecycleRef.current !== "idle") {
      return startPromiseRef.current ?? Promise.resolve();
    }
    if (typeof window === "undefined" || typeof RTCPeerConnection === "undefined") {
      setSafeState((previous) => ({
        ...previous,
        phase: "error",
        error: "このブラウザはWebRTCに対応していません",
        statusMessage: "WebRTCを利用できません",
      }));
      return Promise.resolve();
    }

    const generation = ++operationRef.current;
    const startMode = sessionModeRef.current;
    lifecycleRef.current = "starting";
    setSafeState(() => ({
      ...initialState(startMode),
      phase: "requesting",
      statusMessage: "音声セッションを準備中…",
    }));
    transcriptDraftRef.current = { user: null, assistant: null };

    let localSessionId: string | null = null;
    let localStream: MediaStream | null = null;
    let localPeer: RTCPeerConnection | null = null;
    let localChannel: RTCDataChannel | null = null;

    const cleanupLocal = async () => {
      webrtcRef.current.cleanupLocal({
        stream: localStream,
        peer: localPeer,
        channel: localChannel,
      });
      if (localSessionId) void endSessionOnce(localSessionId);
      localStream = null;
      localPeer = null;
      localChannel = null;
    };

    const isCurrent = () =>
      mountedRef.current &&
      operationRef.current === generation &&
      (lifecycleRef.current === "starting" || lifecycleRef.current === "active");

    const trackedPromise = (async () => {
      try {
        let durableConversationSessionId = conversationSessionId?.trim() || null;
        if (!durableConversationSessionId && ensureConversationSession) {
          durableConversationSessionId =
            (await ensureConversationSession()).trim() || null;
          if (!durableConversationSessionId) {
            throw new Error("会話セッションを準備できませんでした");
          }
        }
        const session = await sessionApiRef.current.create({
          conversationSessionId: durableConversationSessionId,
          projectId,
          includeProjectContext,
          characterName,
          mode: startMode,
        });
        localSessionId = session.id;
        if (!isCurrent()) {
          await cleanupLocal();
          return;
        }
        sessionIdRef.current = session.id;
        setSafeState((previous) => ({
          ...previous,
          sessionId: session.id,
          provider: session.provider ?? "openai_realtime",
          model: session.model ?? null,
          phase: "connecting",
          progress: 10,
          statusMessage: "マイクと音声を接続中…",
        }));

        await webrtcRef.current.connect({
          isCurrent,
          exchangeSdp: (sdp) =>
            sessionApiRef.current.exchangeSdp(session.id, sdp),
          onLocalResources: ({ stream, peer, channel }) => {
            localStream = stream;
            localPeer = peer;
            localChannel = channel;
            if (!isCurrent()) {
              void cleanupLocal();
              return;
            }
            webrtcRef.current.setActiveRefs({ stream, peer, channel });
          },
        });
        if (!isCurrent()) {
          await cleanupLocal();
          return;
        }
        lifecycleRef.current = "active";
        setSafeState((previous) => ({
          ...previous,
          phase: "connected",
          progress: 15,
          statusMessage: "接続しました。話しかけてください",
        }));
        monitorSession(session.id, generation);
      } catch (error) {
        if (!isCurrent()) {
          await cleanupLocal();
          return;
        }
        const message = errorMessage(error);
        await stop();
        setSafeState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: message,
        }));
      }
    })().finally(() => {
      if (startPromiseRef.current === trackedPromise) {
        startPromiseRef.current = null;
      }
      if (
        lifecycleRef.current === "starting" &&
        operationRef.current === generation
      ) {
        lifecycleRef.current = "idle";
      }
    });
    startPromiseRef.current = trackedPromise;
    return trackedPromise;
  }, [
    characterName,
    conversationSessionId,
    ensureConversationSession,
    endSessionOnce,
    includeProjectContext,
    monitorSession,
    projectId,
    setSafeState,
    stop,
  ]);

  const interrupt = useCallback(() => {
    const id = sessionIdRef.current;
    if (!id) return;
    // Stop browser-side queued/stale audio before asking the server to cancel
    // the current response; this keeps barge-in fail-closed under latency.
    characterAudioRef.current.flush();
    if (sessionModeRef.current !== "realtime_native") {
      void interruptVoiceSession(id).catch(() => {
        // server interrupt failure must not tear down the call
      });
    } else {
      webrtcRef.current.sendControl({ type: "response.cancel" });
    }
    reportEvent({ type: "interrupt" });
    setSafeState((previous) => ({
      ...previous,
      progress: 10,
      statusMessage: "応答を中断しました。聞き取り中…",
    }));
  }, [reportEvent, setSafeState]);

  const toggleMute = useCallback(() => {
    const muted = webrtcRef.current.toggleMute();
    if (muted === null) return;
    setSafeState((previous) => ({ ...previous, muted }));
  }, [setSafeState]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      void stop();
    };
  }, [stop]);

  return {
    state,
    remoteAudioRef: webrtc.remoteAudioRef,
    start,
    stop,
    interrupt,
    toggleMute,
  };
}
