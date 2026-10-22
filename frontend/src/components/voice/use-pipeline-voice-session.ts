"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { decodeLipAudio, observeMediaPlayback } from "@/lib/aoirig/lip-sync";
import { clearRigConversation, setRigConversation } from "@/lib/aoirig/conversation";
import {
  createVoiceSession,
  endVoiceSession,
  interruptVoiceSession,
  synthesizeVoicePipeline,
  transcribeVoicePipeline,
} from "@/lib/voice-session-api";
import type {
  UseVoiceSessionOptions,
  UseVoiceSessionResult,
  VoiceSessionState,
  VoiceTranscript,
} from "./use-voice-session";

export type PipelineAssistantMessage = {
  id: string;
  text: string;
} | null;

export type PipelineSendResult =
  | void
  | boolean
  | "accepted"
  | "pending"
  | "failed";

export type UsePipelineVoiceSessionOptions = Omit<UseVoiceSessionOptions, "mode"> & {
  assistantMessage?: PipelineAssistantMessage;
  chatBusy?: boolean;
  onSendTranscript?: (
    text: string,
  ) => PipelineSendResult | Promise<PipelineSendResult>;
  onStopGeneration?: () => void;
};

export type UsePipelineVoiceSessionResult = UseVoiceSessionResult & {
  canSubmitUtterance: boolean;
  submitUtterance: () => Promise<void>;
};

const MAX_SPEECH_CHARS = 500;
const RECORDING_MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
] as const;

type PipelineMime =
  | "audio/mp4"
  | "audio/m4a"
  | "audio/x-m4a"
  | "audio/wav"
  | "audio/webm";

function initialState(): VoiceSessionState {
  return {
    phase: "idle",
    sessionId: null,
    mode: "pipeline",
    provider: null,
    model: null,
    muted: false,
    transcripts: [],
    progress: 0,
    statusMessage: "ローカル音声会話を開始できます",
    error: null,
  };
}

function transcriptId(role: "user" | "assistant"): string {
  const suffix =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${role}-${suffix}`;
}

function publicError(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "マイクの使用が許可されませんでした";
  }
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "ローカル音声会話の処理に失敗しました";
}

function supportedRecorderMime(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  if (typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return RECORDING_MIME_CANDIDATES.find((candidate) =>
    MediaRecorder.isTypeSupported(candidate),
  );
}

function normalizeRecorderMime(value: string): PipelineMime {
  const mime = value.split(";", 1)[0]?.trim().toLowerCase();
  if (
    mime === "audio/mp4" ||
    mime === "audio/m4a" ||
    mime === "audio/x-m4a" ||
    mime === "audio/wav" ||
    mime === "audio/webm"
  ) {
    return mime;
  }
  return "audio/webm";
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const parts: string[] = [];
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const chunk = bytes.subarray(offset, Math.min(bytes.length, offset + chunkSize));
    parts.push(String.fromCharCode(...chunk));
  }
  return btoa(parts.join(""));
}

function base64AudioBlob(encoded: string, mimeType: string): Blob {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mimeType });
}

export function splitPipelineSpeech(text: string): string[] {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return [];
  const sentences = normalized.split(/(?<=[。！？!?])\s*/u).filter(Boolean);
  const segments: string[] = [];
  let current = "";
  const flush = () => {
    const value = current.trim();
    if (value) segments.push(value);
    current = "";
  };
  for (const sentence of sentences) {
    if (sentence.length > MAX_SPEECH_CHARS) {
      flush();
      for (let offset = 0; offset < sentence.length; offset += MAX_SPEECH_CHARS) {
        segments.push(sentence.slice(offset, offset + MAX_SPEECH_CHARS));
      }
      continue;
    }
    const joined = current ? `${current} ${sentence}` : sentence;
    if (joined.length > MAX_SPEECH_CHARS) flush();
    current = current ? `${current} ${sentence}` : sentence;
  }
  flush();
  return segments;
}

function assistantFingerprint(message: PipelineAssistantMessage | undefined): string | null {
  if (!message?.id || !message.text.trim()) return null;
  return `${message.id}:${message.text}`;
}

export function usePipelineVoiceSession({
  conversationSessionId = null,
  projectId = null,
  includeProjectContext = null,
  characterName = null,
  ensureConversationSession,
  assistantMessage = null,
  chatBusy = false,
  onSendTranscript,
  onStopGeneration,
}: UsePipelineVoiceSessionOptions = {}): UsePipelineVoiceSessionResult {
  const [state, setState] = useState<VoiceSessionState>(initialState);
  const [canSubmitUtterance, setCanSubmitUtterance] = useState(false);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const mountedRef = useRef(true);
  const activeRef = useRef(false);
  const operationRef = useRef(0);
  const sessionIdRef = useRef<string | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recorderChunksRef = useRef<BlobPart[]>([]);
  const awaitingAssistantRef = useRef(false);
  const assistantBaselineRef = useRef<string | null>(null);
  const playbackResolveRef = useRef<(() => void) | null>(null);
  const playbackUrlRef = useRef<string | null>(null);
  const stopLipRef = useRef<(() => void) | null>(null);
  const startPromiseRef = useRef<Promise<void> | null>(null);
  const stopPromiseRef = useRef<Promise<void> | null>(null);
  const assistantMessageRef = useRef(assistantMessage);
  assistantMessageRef.current = assistantMessage;

  const safeSetState = useCallback(
    (updater: (previous: VoiceSessionState) => VoiceSessionState) => {
      if (mountedRef.current) setState(updater);
    },
    [],
  );

  const appendTranscript = useCallback(
    (role: VoiceTranscript["role"], text: string) => {
      const value = text.trim();
      if (!value) return;
      safeSetState((previous) => ({
        ...previous,
        transcripts: [
          ...previous.transcripts,
          { id: transcriptId(role), role, text: value, final: true },
        ],
      }));
    },
    [safeSetState],
  );

  const stopPlayback = useCallback(() => {
    stopLipRef.current?.();
    stopLipRef.current = null;
    clearRigConversation(characterName, sessionIdRef);
    const audio = remoteAudioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    const url = playbackUrlRef.current;
    playbackUrlRef.current = null;
    if (url) URL.revokeObjectURL(url);
    const resolve = playbackResolveRef.current;
    playbackResolveRef.current = null;
    resolve?.();
  }, [characterName]);

  const beginRecording = useCallback(() => {
    const stream = streamRef.current;
    if (!activeRef.current || !stream || recorderRef.current) return;
    if (stream.getAudioTracks().every((track) => track.readyState === "ended")) {
      throw new Error("マイク接続が終了しました。音声会話を再開してください");
    }
    const requestedMime = supportedRecorderMime();
    const recorder = requestedMime
      ? new MediaRecorder(stream, { mimeType: requestedMime })
      : new MediaRecorder(stream);
    recorderChunksRef.current = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) recorderChunksRef.current.push(event.data);
    };
    recorder.onerror = () => {
      safeSetState((previous) => ({
        ...previous,
        error: "マイク録音に失敗しました",
        statusMessage: "マイク録音に失敗しました",
      }));
    };
    recorder.start(250);
    recorderRef.current = recorder;
    setCanSubmitUtterance(true);
    setRigConversation(characterName, sessionIdRef, "listening");
    safeSetState((previous) => ({
      ...previous,
      phase: "connected",
      progress: 10,
      statusMessage: "聞き取り中… 話し終えたら「発話を送信」を押してください",
    }));
  }, [characterName, safeSetState]);

  const finishRecording = useCallback(async (): Promise<Blob> => {
    const recorder = recorderRef.current;
    if (!recorder) throw new Error("録音が開始されていません");
    recorderRef.current = null;
    setCanSubmitUtterance(false);
    setRigConversation(characterName, sessionIdRef, "thinking");
    return new Promise<Blob>((resolve, reject) => {
      recorder.onerror = () => reject(new Error("マイク録音に失敗しました"));
      recorder.onstop = () => {
        const mimeType = recorder.mimeType || "audio/webm";
        resolve(new Blob(recorderChunksRef.current, { type: mimeType }));
        recorderChunksRef.current = [];
      };
      if (recorder.state === "inactive") {
        recorder.onstop?.(new Event("stop"));
      } else {
        recorder.stop();
      }
    });
  }, [characterName]);

  const playSpeech = useCallback(
    async (audioBase64: string, mimeType: string, generation: number) => {
      const audio = remoteAudioRef.current;
      if (!audio || !activeRef.current || operationRef.current !== generation) return;
      stopPlayback();
      const blob = base64AudioBlob(audioBase64, mimeType);
      const buffer = characterName ? await decodeLipAudio(blob) : null;
      if (!activeRef.current || operationRef.current !== generation || remoteAudioRef.current !== audio) return;
      const url = URL.createObjectURL(blob);
      playbackUrlRef.current = url;
      audio.src = url;
      if (characterName && buffer) stopLipRef.current = observeMediaPlayback(characterName, {
        media: audio, buffer, session: sessionIdRef.current ?? "pipeline", generation: String(generation), segment: url, visemes: [],
      });
      await new Promise<void>((resolve, reject) => {
        playbackResolveRef.current = resolve;
        audio.onended = () => {
          playbackResolveRef.current = null;
          stopPlayback();
          resolve();
        };
        audio.onerror = () => {
          playbackResolveRef.current = null;
          stopPlayback();
          reject(new Error("生成音声を再生できませんでした"));
        };
        void audio.play().catch((error: unknown) => {
          playbackResolveRef.current = null;
          stopPlayback();
          reject(error);
        });
      });
    },
    [characterName, stopPlayback],
  );

  const speakAssistant = useCallback(
    async (message: NonNullable<PipelineAssistantMessage>) => {
      const id = sessionIdRef.current;
      if (!id || !activeRef.current) return;
      const generation = operationRef.current;
      appendTranscript("assistant", message.text);
      safeSetState((previous) => ({
        ...previous,
        progress: 70,
        statusMessage: "キャラクター音声を生成中…",
      }));
      try {
        const segments = splitPipelineSpeech(message.text);
        for (let index = 0; index < segments.length; index += 1) {
          if (!activeRef.current || operationRef.current !== generation) return;
          const speech = await synthesizeVoicePipeline({
            sessionId: id,
            text: segments[index],
          });
          if (!activeRef.current || operationRef.current !== generation) return;
          safeSetState((previous) => ({
            ...previous,
            progress: 80 + Math.round(((index + 1) / segments.length) * 20),
            statusMessage: "キャラクター音声を再生中…",
          }));
          await playSpeech(speech.audioBase64, speech.mimeType, generation);
        }
        if (activeRef.current && operationRef.current === generation) beginRecording();
      } catch (error) {
        if (!activeRef.current || operationRef.current !== generation) return;
        safeSetState((previous) => ({
          ...previous,
          error: publicError(error),
          statusMessage: "読み上げに失敗しました。聞き取りを再開します",
        }));
        beginRecording();
      }
    },
    [appendTranscript, beginRecording, playSpeech, safeSetState],
  );

  const stop = useCallback(async () => {
    if (stopPromiseRef.current) return stopPromiseRef.current;
    const promise = (async () => {
      ++operationRef.current;
      activeRef.current = false;
      awaitingAssistantRef.current = false;
      setCanSubmitUtterance(false);
      safeSetState((previous) => ({
        ...previous,
        phase: "disconnecting",
        statusMessage: "ローカル音声会話を終了中…",
      }));
      stopPlayback();
      const recorder = recorderRef.current;
      recorderRef.current = null;
      if (recorder && recorder.state !== "inactive") {
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.onerror = null;
        recorder.stop();
      }
      recorderChunksRef.current = [];
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      const id = sessionIdRef.current;
      sessionIdRef.current = null;
      if (id) {
        await endVoiceSession(id).catch(() => undefined);
      }
      safeSetState((previous) => ({
        ...previous,
        phase: "ended",
        sessionId: null,
        muted: false,
        progress: 0,
        statusMessage: "ローカル音声会話を終了しました",
      }));
    })().finally(() => {
      if (stopPromiseRef.current === promise) stopPromiseRef.current = null;
    });
    stopPromiseRef.current = promise;
    return promise;
  }, [safeSetState, stopPlayback]);

  const start = useCallback(() => {
    if (startPromiseRef.current || activeRef.current) {
      return startPromiseRef.current ?? Promise.resolve();
    }
    const generation = ++operationRef.current;
    const promise = (async () => {
      safeSetState(() => ({
        ...initialState(),
        phase: "requesting",
        statusMessage: "マイクとローカル音声処理を準備中…",
      }));
      if (
        typeof navigator === "undefined" ||
        !navigator.mediaDevices?.getUserMedia ||
        typeof MediaRecorder === "undefined"
      ) {
        throw new Error("このブラウザはマイク録音に対応していません");
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      streamRef.current = stream;
      let durableSessionId = conversationSessionId?.trim() || null;
      if (!durableSessionId && ensureConversationSession) {
        durableSessionId = (await ensureConversationSession()).trim() || null;
      }
      if (!durableSessionId) throw new Error("会話セッションを準備できませんでした");
      const session = await createVoiceSession({
        mode: "pipeline",
        conversationSessionId: durableSessionId,
        projectId,
        includeProjectContext,
        characterName,
      });
      if (!mountedRef.current || generation !== operationRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        await endVoiceSession(session.id).catch(() => undefined);
        return;
      }
      sessionIdRef.current = session.id;
      activeRef.current = true;
      safeSetState((previous) => ({
        ...previous,
        phase: "connected",
        sessionId: session.id,
        provider: session.provider ?? "local_pipeline",
        model: session.model ?? null,
        progress: 5,
        statusMessage: "ローカル音声会話を開始しました",
      }));
      beginRecording();
    })()
      .catch(async (error) => {
        streamRef.current?.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
        const id = sessionIdRef.current;
        sessionIdRef.current = null;
        activeRef.current = false;
        if (id) await endVoiceSession(id).catch(() => undefined);
        const message = publicError(error);
        safeSetState((previous) => ({
          ...previous,
          phase: "error",
          error: message,
          statusMessage: message,
        }));
      })
      .finally(() => {
        if (startPromiseRef.current === promise) startPromiseRef.current = null;
      });
    startPromiseRef.current = promise;
    return promise;
  }, [
    beginRecording,
    characterName,
    conversationSessionId,
    ensureConversationSession,
    includeProjectContext,
    projectId,
    safeSetState,
  ]);

  const submitUtterance = useCallback(async () => {
    const id = sessionIdRef.current;
    if (!activeRef.current || !id || !recorderRef.current) return;
    const generation = operationRef.current;
    safeSetState((previous) => ({
      ...previous,
      progress: 25,
      error: null,
      statusMessage: "Whisperで文字起こし中…",
    }));
    try {
      const recording = await finishRecording();
      if (!activeRef.current || generation !== operationRef.current) return;
      if (!recording.size) throw new Error("録音データが空です。もう一度話してください");
      const transcription = await transcribeVoicePipeline({
        sessionId: id,
        audioBase64: await blobToBase64(recording),
        mimeType: normalizeRecorderMime(recording.type),
      });
      if (!activeRef.current || generation !== operationRef.current) return;
      if (!transcription.text) {
        safeSetState((previous) => ({
          ...previous,
          progress: 10,
          statusMessage: "音声を認識できませんでした。もう一度話してください",
        }));
        beginRecording();
        return;
      }
      appendTranscript("user", transcription.text);
      assistantBaselineRef.current = assistantFingerprint(assistantMessageRef.current);
      awaitingAssistantRef.current = true;
      safeSetState((previous) => ({
        ...previous,
        progress: 50,
        statusMessage: "通常の会話モデルで応答を生成中…",
      }));
      if (!onSendTranscript) throw new Error("会話送信経路を利用できません");
      const result = await onSendTranscript(transcription.text);
      if (result === false || result === "failed") {
        throw new Error("音声入力を会話へ送信できませんでした");
      }
    } catch (error) {
      awaitingAssistantRef.current = false;
      if (!activeRef.current || generation !== operationRef.current) return;
      const message = publicError(error);
      safeSetState((previous) => ({
        ...previous,
        error: message,
        statusMessage: `${message}。聞き取りを再開します`,
      }));
      beginRecording();
    }
  }, [
    appendTranscript,
    beginRecording,
    finishRecording,
    onSendTranscript,
    safeSetState,
  ]);

  const interrupt = useCallback(() => {
    const id = sessionIdRef.current;
    if (!activeRef.current || !id) return;
    ++operationRef.current;
    awaitingAssistantRef.current = false;
    onStopGeneration?.();
    stopPlayback();
    void interruptVoiceSession(id).catch(() => undefined);
    safeSetState((previous) => ({
      ...previous,
      progress: 10,
      statusMessage: "応答を中断しました。聞き取りを再開します",
    }));
    if (!recorderRef.current) beginRecording();
  }, [beginRecording, onStopGeneration, safeSetState, stopPlayback]);

  const toggleMute = useCallback(() => {
    const tracks = streamRef.current?.getAudioTracks() ?? [];
    if (!tracks.length) return;
    const muted = tracks.some((track) => track.enabled);
    tracks.forEach((track) => {
      track.enabled = !muted;
    });
    safeSetState((previous) => ({ ...previous, muted }));
  }, [safeSetState]);

  useEffect(() => {
    if (!awaitingAssistantRef.current || chatBusy) return;
    const fingerprint = assistantFingerprint(assistantMessage);
    if (!fingerprint || fingerprint === assistantBaselineRef.current || !assistantMessage) return;
    awaitingAssistantRef.current = false;
    assistantBaselineRef.current = fingerprint;
    void speakAssistant(assistantMessage);
  }, [assistantMessage, chatBusy, speakAssistant]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      void stop();
    };
  }, [stop]);

  return {
    state,
    remoteAudioRef,
    start,
    stop,
    interrupt,
    toggleMute,
    canSubmitUtterance,
    submitUtterance,
  };
}
