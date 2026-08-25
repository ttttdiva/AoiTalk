"use client";

/**
 * Backward-compatible Live Voice hook.
 * Delegates to the unified `useVoiceSession` with `realtime_native` mode,
 * which continues to use `/api/live-voice/*` routes.
 */
import {
  useVoiceSession,
  type UseVoiceSessionOptions,
  type UseVoiceSessionResult,
  type VoiceSessionPhase,
  type VoiceSessionState,
  type VoiceSessionStopOptions,
  type VoiceTranscript,
} from "@/components/voice/use-voice-session";

export type LiveVoicePhase = VoiceSessionPhase;
export type LiveVoiceTranscript = VoiceTranscript;
export type LiveVoiceState = Omit<VoiceSessionState, "sessionId" | "mode"> & {
  liveSessionId: string | null;
};
export type UseLiveVoiceOptions = Omit<UseVoiceSessionOptions, "mode">;
export type LiveVoiceStopOptions = VoiceSessionStopOptions;

export type UseLiveVoiceResult = Omit<UseVoiceSessionResult, "state"> & {
  state: LiveVoiceState;
};

function toLiveVoiceState(state: VoiceSessionState): LiveVoiceState {
  const { sessionId, mode: _mode, ...rest } = state;
  return {
    ...rest,
    liveSessionId: sessionId,
  };
}

export function useLiveVoice(options: UseLiveVoiceOptions = {}): UseLiveVoiceResult {
  const result = useVoiceSession({ ...options, mode: "realtime_native" });
  return {
    ...result,
    state: toLiveVoiceState(result.state),
  };
}

// Re-export presentation helpers for tests and gradual migration.
export {
  isBrowserTelemetryWireType,
  mapProviderEventToPresentation,
} from "@/components/voice/use-realtime-webrtc";
