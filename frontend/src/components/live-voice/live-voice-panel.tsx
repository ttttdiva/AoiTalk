"use client";

import { VoicePanel, type VoicePanelProps } from "@/components/voice/voice-panel";

export type LiveVoicePanelProps = Omit<VoicePanelProps, "mode" | "title">;

/** Backward-compatible Live Voice panel. Delegates to unified VoicePanel. */
export function LiveVoicePanel(props: LiveVoicePanelProps) {
  return <VoicePanel {...props} mode="realtime_native" title="Live Voice" />;
}

export const LiveVoiceControl = LiveVoicePanel;
