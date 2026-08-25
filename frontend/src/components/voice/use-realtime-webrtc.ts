"use client";

import { useCallback, useRef } from "react";

/** Provider-neutral presentation events for voice UI layers. */
export type RealtimePresentationEvent =
  | { type: "session_ready"; eventId?: string }
  | { type: "user_speaking" }
  | { type: "response_started"; eventId?: string }
  | { type: "playback_started" }
  | { type: "assistant_transcript_delta"; delta: string }
  | { type: "assistant_transcript_done"; text: string }
  | { type: "user_transcript_delta"; delta: string }
  | { type: "user_transcript_done"; text: string }
  | { type: "response_completed"; eventId?: string }
  | { type: "provider_error"; message: string; raw?: Record<string, unknown>; eventId?: string };

export type RealtimeControlEvent = {
  type: "response.cancel";
};

export type UseRealtimeWebRTCOptions = {
  onPresentationEvent: (event: RealtimePresentationEvent) => void;
  onConnectionLost?: (message: string) => void;
  onDataChannelClosed?: () => void;
  onDataChannelError?: (message: string) => void;
};

export type RealtimeWebRTCConnectParams = {
  exchangeSdp: (sdp: string) => Promise<string>;
  isCurrent: () => boolean;
  onLocalResources?: (resources: {
    stream: MediaStream;
    peer: RTCPeerConnection;
    channel: RTCDataChannel;
  }) => void;
};

export type UseRealtimeWebRTCResult = {
  remoteAudioRef: React.MutableRefObject<HTMLAudioElement | null>;
  connect: (params: RealtimeWebRTCConnectParams) => Promise<void>;
  cleanupLocal: (resources: {
    stream: MediaStream | null;
    peer: RTCPeerConnection | null;
    channel: RTCDataChannel | null;
  }) => void;
  cleanupActive: () => void;
  sendControl: (event: RealtimeControlEvent) => boolean;
  toggleMute: () => boolean | null;
  getMediaStream: () => MediaStream | null;
  setActiveRefs: (refs: {
    stream: MediaStream | null;
    peer: RTCPeerConnection | null;
    channel: RTCDataChannel | null;
  }) => void;
};

function realtimeErrorMessage(event: Record<string, unknown>): string {
  const payload =
    event.error && typeof event.error === "object"
      ? (event.error as Record<string, unknown>)
      : event;
  const code = String(payload.code ?? payload.type ?? "").toLowerCase();
  if (code.includes("quota") || code.includes("rate_limit")) {
    return "音声サービスの利用上限に達しました。しばらく待って再試行してください。";
  }
  if (code.includes("auth") || code.includes("api_key")) {
    return "音声サービスの認証設定を確認してください。";
  }
  if (code.includes("permission") || code.includes("forbidden")) {
    return "この音声サービスを利用する権限がありません。";
  }
  if (code.includes("model") && code.includes("not")) {
    return "指定された音声モデルを利用できません。管理者に設定を確認してもらってください。";
  }
  return "Realtime音声サービスでエラーが発生しました。もう一度お試しください。";
}

function wireEventId(event: Record<string, unknown>): string | undefined {
  if (typeof event.event_id === "string") return event.event_id;
  if (typeof event.id === "string") return event.id;
  return undefined;
}

/** Map provider wire events to presentation-neutral events. */
export function mapProviderEventToPresentation(
  event: Record<string, unknown>,
  assistantSource: "audio" | "text" | null,
): {
  events: RealtimePresentationEvent[];
  assistantSource: "audio" | "text" | null;
} {
  const type = typeof event.type === "string" ? event.type : "";
  if (!type) return { events: [], assistantSource };
  const eventId = wireEventId(event);

  if (type === "session.created") {
    return { events: [{ type: "session_ready", eventId }], assistantSource };
  }
  if (type === "input_audio_buffer.speech_started") {
    return { events: [{ type: "user_speaking" }], assistantSource };
  }
  if (type === "response.created") {
    return { events: [{ type: "response_started", eventId }], assistantSource: null };
  }
  if (type === "response.output_item.added") {
    return { events: [{ type: "playback_started" }], assistantSource };
  }
  if (
    type === "response.output_audio_transcript.delta" ||
    type === "response.audio_transcript.delta"
  ) {
    if (assistantSource === "text") return { events: [], assistantSource };
    const delta = typeof event.delta === "string" ? event.delta : "";
    return {
      events: delta ? [{ type: "assistant_transcript_delta", delta }] : [],
      assistantSource: "audio",
    };
  }
  if (
    type === "response.output_audio_transcript.done" ||
    type === "response.audio_transcript.done"
  ) {
    if (assistantSource === "text") return { events: [], assistantSource };
    const text = typeof event.transcript === "string" ? event.transcript : "";
    return {
      events: text ? [{ type: "assistant_transcript_done", text }] : [],
      assistantSource: "audio",
    };
  }
  if (type === "response.output_text.delta") {
    if (assistantSource === "audio") return { events: [], assistantSource };
    const delta = typeof event.delta === "string" ? event.delta : "";
    return {
      events: delta ? [{ type: "assistant_transcript_delta", delta }] : [],
      assistantSource: "text",
    };
  }
  if (type === "response.output_text.done") {
    if (assistantSource === "audio") return { events: [], assistantSource };
    const text =
      typeof event.text === "string"
        ? event.text
        : typeof event.transcript === "string"
          ? event.transcript
          : "";
    return {
      events: text ? [{ type: "assistant_transcript_done", text }] : [],
      assistantSource: "text",
    };
  }
  if (type === "conversation.item.input_audio_transcription.delta") {
    const delta = typeof event.delta === "string" ? event.delta : "";
    return {
      events: delta ? [{ type: "user_transcript_delta", delta }] : [],
      assistantSource,
    };
  }
  if (
    type === "conversation.item.input_audio_transcription.completed" ||
    type === "conversation.item.input_audio_transcription.done"
  ) {
    const text = typeof event.transcript === "string" ? event.transcript : "";
    return {
      events: text ? [{ type: "user_transcript_done", text }] : [],
      assistantSource,
    };
  }
  if (type === "response.done") {
    return { events: [{ type: "response_completed", eventId }], assistantSource };
  }
  if (type === "error") {
    return {
      events: [
        {
          type: "provider_error",
          message: realtimeErrorMessage(event),
          raw: event,
          eventId,
        },
      ],
      assistantSource,
    };
  }
  return { events: [], assistantSource };
}

/** Telemetry event types allowed to cross the browser reporting boundary. */
export function isBrowserTelemetryWireType(type: string): boolean {
  return (
    type === "session.created" ||
    type === "response.created" ||
    type === "response.done" ||
    type === "error" ||
    type === "interrupt"
  );
}

/**
 * Low-level WebRTC lifecycle for Realtime voice sessions.
 * Provider wire events are mapped to presentation-neutral events before
 * reaching UI layers. Session policy is owned by the server sideband.
 */
export function useRealtimeWebRTC({
  onPresentationEvent,
  onConnectionLost,
  onDataChannelClosed,
  onDataChannelError,
}: UseRealtimeWebRTCOptions): UseRealtimeWebRTCResult {
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const peerConnectionRef = useRef<RTCPeerConnection | null>(null);
  const dataChannelRef = useRef<RTCDataChannel | null>(null);
  const assistantTranscriptSourceRef = useRef<"audio" | "text" | null>(null);

  const cleanupLocal = useCallback(
    (resources: {
      stream: MediaStream | null;
      peer: RTCPeerConnection | null;
      channel: RTCDataChannel | null;
    }) => {
      const channel = resources.channel;
      try {
        if (channel?.readyState === "open") {
          channel.send(JSON.stringify({ type: "response.cancel" }));
        }
        channel?.close();
      } catch {
        // best effort
      }
      for (const track of resources.stream?.getTracks() ?? []) track.stop();
      try {
        resources.peer?.close();
      } catch {
        // no-op
      }
    },
    [],
  );

  const cleanupActive = useCallback(() => {
    cleanupLocal({
      stream: mediaStreamRef.current,
      peer: peerConnectionRef.current,
      channel: dataChannelRef.current,
    });
    mediaStreamRef.current = null;
    peerConnectionRef.current = null;
    dataChannelRef.current = null;
    assistantTranscriptSourceRef.current = null;
    const audio = remoteAudioRef.current;
    if (audio) {
      audio.pause();
      audio.srcObject = null;
    }
  }, [cleanupLocal]);

  const setActiveRefs = useCallback(
    (refs: {
      stream: MediaStream | null;
      peer: RTCPeerConnection | null;
      channel: RTCDataChannel | null;
    }) => {
      mediaStreamRef.current = refs.stream;
      peerConnectionRef.current = refs.peer;
      dataChannelRef.current = refs.channel;
    },
    [],
  );

  const sendControl = useCallback((event: RealtimeControlEvent): boolean => {
    const channel = dataChannelRef.current;
    if (!channel || channel.readyState !== "open") return false;
    channel.send(JSON.stringify(event));
    return true;
  }, []);

  const toggleMute = useCallback((): boolean | null => {
    const tracks = mediaStreamRef.current?.getAudioTracks() ?? [];
    if (tracks.length === 0) return null;
    const muted = tracks.some((track) => track.enabled);
    for (const track of tracks) track.enabled = !muted;
    return muted;
  }, []);

  const getMediaStream = useCallback(() => mediaStreamRef.current, []);

  const connect = useCallback(
    async ({ exchangeSdp, isCurrent, onLocalResources }: RealtimeWebRTCConnectParams) => {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("マイクを利用できるブラウザ環境ではありません");
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      let localStream: MediaStream | null = stream;
      let localPeer: RTCPeerConnection | null = null;
      let localChannel: RTCDataChannel | null = null;

      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: null, channel: null });
        return;
      }

      const peer = new RTCPeerConnection();
      localPeer = peer;
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: null });
        return;
      }

      peer.ontrack = (event) => {
        if (!isCurrent()) return;
        const audio = remoteAudioRef.current;
        if (audio && event.streams[0]) audio.srcObject = event.streams[0];
      };
      peer.onconnectionstatechange = () => {
        if (!isCurrent()) return;
        if (!["disconnected", "failed", "closed"].includes(peer.connectionState)) {
          return;
        }
        const message =
          peer.connectionState === "failed"
            ? "音声接続が失敗しました"
            : "音声接続が切断されました";
        onConnectionLost?.(message);
      };
      for (const track of stream.getTracks()) peer.addTrack(track, stream);

      const channel = peer.createDataChannel("oai-events");
      localChannel = channel;
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: localChannel });
        return;
      }

      channel.onopen = () => {
        if (!isCurrent()) return;
        // Session policy (output modalities, transcription, tools) is owned
        // by the server sideband. The browser must not send session.update.
      };
      channel.onmessage = (message) => {
        if (!isCurrent()) return;
        try {
          const wire = JSON.parse(String(message.data)) as Record<string, unknown>;
          const mapped = mapProviderEventToPresentation(
            wire,
            assistantTranscriptSourceRef.current,
          );
          assistantTranscriptSourceRef.current = mapped.assistantSource;
          for (const presentationEvent of mapped.events) {
            onPresentationEvent(presentationEvent);
          }
        } catch {
          // Ignore malformed provider events.
        }
      };
      channel.onclose = () => {
        if (!isCurrent()) return;
        onDataChannelClosed?.();
      };
      channel.onerror = () => {
        if (!isCurrent()) return;
        onDataChannelError?.("Realtimeデータ接続でエラーが発生しました");
      };

      onLocalResources?.({ stream, peer, channel });

      const offer = await peer.createOffer();
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: localChannel });
        return;
      }
      await peer.setLocalDescription(offer);
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: localChannel });
        return;
      }
      const answerSdp = await exchangeSdp(offer.sdp ?? "");
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: localChannel });
        return;
      }
      await peer.setRemoteDescription({ type: "answer", sdp: answerSdp });
      if (!isCurrent()) {
        cleanupLocal({ stream: localStream, peer: localPeer, channel: localChannel });
        return;
      }

      localStream = null;
      localPeer = null;
      localChannel = null;
    },
    [cleanupLocal, onConnectionLost, onDataChannelClosed, onDataChannelError, onPresentationEvent],
  );

  return {
    remoteAudioRef,
    connect,
    cleanupLocal,
    cleanupActive,
    sendControl,
    toggleMute,
    getMediaStream,
    setActiveRefs,
  };
}
