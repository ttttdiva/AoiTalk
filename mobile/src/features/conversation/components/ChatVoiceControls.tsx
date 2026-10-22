import {setRigConversation,clearRigState} from '../../characters/rig-playback';
import React, {
  forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState,
} from "react";
import { AppState, StyleSheet, View } from "react-native";
import { IconButton, Text, useTheme } from "react-native-paper";
import { useFocusEffect } from "expo-router";
import { getConfiguredApiServerFingerprint } from "../../../lib/api-client";
import { mobileVoiceApi, type VoiceCapabilities, type VoiceScope } from "../../../lib/mobile-voice-api";
import { useNetworkStore } from "../../../stores/network";
import type { ConversationMessage } from "../../../types/api";
import { INITIAL_VOICE_STATE, VoiceSession } from "../voice-session";

export type ChatVoiceContext = {
  /** Stable across promotion of a local conversation to its server ID. */
  conversationKey: string;
  sessionId: string;
  sessionIsLocalOnly?: boolean;
  projectId: string | null;
  characterName: string;
  userId: string | null;
  messages: ConversationMessage[];
  busy: boolean;
};
export type ChatVoiceHandle = {
  beforeTextSend: () => void;
  submissionFailed: () => void;
};
type Props = {
  context: ChatVoiceContext;
  isAuthenticated: boolean;
  onSend: (submission: { content: string; submissionId?: string }) => Promise<void>;
};
// Load native AV only after the user opts into voice (not during chat import).
function voiceAudio(): typeof import("../voice-audio") {
  return require("../voice-audio");
}
// Android permission dialogs also emit "background". Allow the native dialog
// to finish its foreground transition, but never retain a request for a later
// return to the app. Recording/playback still require an active foreground.
function waitForPermissionForeground(signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false);
  if (AppState.currentState === "active") return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let subscription: ReturnType<typeof AppState.addEventListener> | undefined;
    const finish = (active: boolean) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      subscription?.remove();
      signal.removeEventListener("abort", onAbort);
      resolve(active);
    };
    const onAbort = () => finish(false);
    timer = setTimeout(() => finish(false), 1000);
    subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") finish(true);
    });
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) finish(false);
    else if (AppState.currentState === "active") finish(true);
  });
}

function requestScope(context: ChatVoiceContext): VoiceScope {
  return {
    conversation_session_id: context.sessionIsLocalOnly || context.sessionId.startsWith("local-") || context.sessionId === "new"
      ? null : context.sessionId,
    project_id: context.projectId,
  };
}

export const ChatVoiceControls = forwardRef<ChatVoiceHandle, Props>(function ChatVoiceControls(
  { context, isAuthenticated, onSend }, ref,
) {
  const theme = useTheme();
  const [state, setState] = useState(INITIAL_VOICE_STATE);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<VoiceCapabilities | null>(null);
  const reachable = useNetworkStore((value) => value.serverReachable);
  const connected = useNetworkStore((value) => value.connected);
  const alive = useRef(true);
  const startAbort = useRef<AbortController | null>(null);
  const micAbort = useRef<AbortController | null>(null);
  const permissionAbort = useRef<AbortController | null>(null);
  const rigOwner = useRef({});
  const live = useRef({ context, onSend });
  live.current = { context, onSend };
  const serverRef = useRef<string | null>(null);
  const sessionRef = useRef<VoiceSession | null>(null);
  if (!sessionRef.current) {
    sessionRef.current = new VoiceSession({
      onState: (next) => {
        if (alive.current) setState(next);
        const character=live.current.context.characterName;
        if(next.enabled)setRigConversation(character,rigOwner.current,next.phase==='listening'?'listening':next.phase==='waiting'||next.phase==='transcribing'?'thinking':'idle');
        else clearRigState(character,rigOwner.current);
      },
      record: (signal) => voiceAudio().recordUtterance(signal),
      transcribe: (uri, signal) => {
        const server = serverRef.current;
        if (!server) throw new Error("音声接続がありません。");
        return mobileVoiceApi.transcribe(server, requestScope(live.current.context), uri, signal);
      },
      speak: async (text, character, signal) => {
        const server = serverRef.current;
        if (!server) throw new Error("音声接続がありません。");
        const audio = await mobileVoiceApi.speech(server, requestScope(live.current.context), text, character, signal);
        if (!signal.aborted) await voiceAudio().playVoiceBase64(audio, signal, character);
      },
      send: (text) => live.current.onSend({
        content: text,
        submissionId: `voice-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      }),
    });
  }
  useEffect(()=>()=>clearRigState(context.characterName,rigOwner.current),[context.characterName]);
  const session = sessionRef.current;
  const stop = useCallback(() => {
    startAbort.current?.abort();
    startAbort.current = null;
    micAbort.current?.abort();
    micAbort.current = null;
    session.disable();
    serverRef.current = null;
    if (alive.current) setStarting(false);
  }, [session]);
  useImperativeHandle(ref, () => ({
    beforeTextSend: () => session.beforeTextSend(),
    submissionFailed: () => session.submissionFailed(),
  }), [session]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; stop(); };
  }, [stop]);
  useFocusEffect(useCallback(() => () => stop(), [stop]));
  useEffect(() => {
    stop();
    setError(null);
  }, [context.conversationKey, context.projectId, context.characterName, context.userId, isAuthenticated, stop]);
  useEffect(() => {
    if (connected === false || (!reachable && session.state.enabled)) {
      stop();
      setError("サーバーとの接続が切れたため、音声を停止しました。文字入力は引き続き使えます。");
    }
  }, [connected, reachable, session, stop]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "background") {
        // A known native permission dialog is not an abandoned start. Its
        // result must still pass the bounded foreground check below.
        if (startAbort.current && permissionAbort.current !== startAbort.current) {
          startAbort.current.abort();
          startAbort.current = null;
        }
        if (micAbort.current && permissionAbort.current !== micAbort.current) {
          micAbort.current.abort();
          micAbort.current = null;
        }
        if (!startAbort.current && alive.current) setStarting(false);
      }
      // Stop active audio even while the permission dialog owns the foreground.
      if (next !== "active") session.pauseForBackground();
    });
    return () => subscription.remove();
  }, [session]);
  useEffect(() => session.observe(context.messages, context.busy, context.characterName),
    [context.messages, context.busy, context.characterName, session]);

  const requestMic = async (abort: AbortController) => {
    if (abort.signal.aborted) return;
    const speakerWasMuted = session.state.speakerMuted;
    permissionAbort.current = abort;
    try {
      await voiceAudio().requestMicrophone();
    } finally {
      const foreground = await waitForPermissionForeground(abort.signal);
      if (permissionAbort.current === abort) permissionAbort.current = null;
      if (!foreground) abort.abort();
      // Asking for microphone permission must not silently disable an already
      // enabled speaker. Never resume the interrupted utterance or a late start.
      if (foreground && !abort.signal.aborted && alive.current && session.state.enabled && !speakerWasMuted) {
        session.setSpeakerMuted(false);
      }
    }
  };

  const start = async () => {
    // Use a ref: rapid taps can arrive before React renders the loading state.
    if (startAbort.current) { stop(); return; }
    setError(null);
    if (!isAuthenticated || connected === false) {
      setError("音声会話にはサーバーへの接続とログインが必要です。");
      return;
    }
    const abort = new AbortController();
    startAbort.current = abort;
    setStarting(true);
    const cancelled = () => abort.signal.aborted || !alive.current || AppState.currentState === "background";
    try {
      const server = await getConfiguredApiServerFingerprint();
      if (cancelled()) return;
      const available = await mobileVoiceApi.capabilities(server, abort.signal);
      if (cancelled()) return;
      if (!available.input_enabled && !available.output_enabled) {
        throw new Error("このサーバーでは音声機能が無効です。");
      }
      setCapabilities(available);
      // Entering a chat screen alone never requests microphone permission.
      let input = available.input_enabled;
      if (input) {
        try { await requestMic(abort); } catch (permissionError) {
          input = false;
          if (!cancelled()) setError(permissionError instanceof Error ? permissionError.message : "マイクを利用できません。");
        }
      }
      if (cancelled() || (!input && !available.output_enabled)) return;
      serverRef.current = server;
      const current = live.current.context;
      session.observe(current.messages, current.busy, current.characterName);
      session.enable(current.messages, input, available.output_enabled);
    } catch (failure) {
      if (!cancelled()) setError(failure instanceof Error ? failure.message : "音声会話を開始できませんでした。");
    } finally {
      if (startAbort.current === abort) {
        startAbort.current = null;
        if (alive.current) setStarting(false);
      }
    }
  };
  const toggleMic = async () => {
    setError(null);
    if (micAbort.current) {
      micAbort.current.abort();
      micAbort.current = null;
      return;
    }
    if (!session.state.micMuted) { session.setMicMuted(true); return; }
    const abort = new AbortController();
    micAbort.current = abort;
    try {
      await requestMic(abort);
      if (!abort.signal.aborted && session.state.enabled && alive.current && AppState.currentState !== "background") {
        session.setMicMuted(false);
      }
    } catch (failure) {
      if (!abort.signal.aborted && alive.current) setError(failure instanceof Error ? failure.message : "マイクを利用できません。");
    } finally {
      if (micAbort.current === abort) micAbort.current = null;
    }
  };
  const visibleError = error || state.error;
  return (
    <View style={styles.container}>
      <View style={styles.row}>
        {state.enabled ? (
          <>
            <IconButton
              icon={state.micMuted ? "microphone-off" : "microphone"}
              size={19} style={styles.icon} disabled={!capabilities?.input_enabled}
              onPress={() => void toggleMic()}
              accessibilityLabel={state.micMuted ? "マイクをオン" : "マイクをミュート"}
              iconColor={state.micMuted ? theme.colors.onSurfaceVariant : theme.colors.primary}
            />
            <IconButton
              icon={state.speakerMuted ? "volume-off" : "volume-high"}
              size={19} style={styles.icon} disabled={!capabilities?.output_enabled}
              onPress={() => session.setSpeakerMuted(!session.state.speakerMuted)}
              accessibilityLabel={state.speakerMuted ? "読み上げをオン" : "読み上げをミュート"}
              iconColor={state.speakerMuted ? theme.colors.onSurfaceVariant : theme.colors.primary}
            />
            <IconButton
              icon="close"
              size={18}
              style={styles.icon}
              onPress={stop}
              accessibilityLabel="音声会話を終了"
            />
          </>
        ) : (
          <IconButton
            icon={starting ? "progress-clock" : "headphones"}
            size={21}
            style={styles.icon}
            onPress={() => void start()}
            accessibilityLabel={starting ? "音声会話の接続を中止" : "音声会話を開始"}
            iconColor={theme.colors.onSurfaceVariant}
          />
        )}
      </View>
      {visibleError ? (
        <Text
          pointerEvents="none"
          style={[styles.error, { color: theme.colors.error }]}
          accessibilityLiveRegion="polite"
        >
          {visibleError}
        </Text>
      ) : null}
    </View>
  );
});
const styles = StyleSheet.create({
  container: { position: "relative", flexShrink: 0 },
  row: { flexDirection: "row", alignItems: "center", minHeight: 36 },
  icon: { margin: 0, width: 34, height: 36 },
  error: {
    position: "absolute",
    left: 0,
    bottom: 42,
    width: 280,
    zIndex: 10,
    fontSize: 12,
    paddingHorizontal: 8,
    paddingVertical: 6,
    borderRadius: 8,
    backgroundColor: "#2d1822",
  },
});
