export type AudioPlaybackScope = "folder_loop" | "global_next";

export type AudioPlayerSettings = {
  playbackScope: AudioPlaybackScope;
  shuffle: boolean;
  repeatOne: boolean;
};

export const DEFAULT_AUDIO_PLAYER_SETTINGS: AudioPlayerSettings = {
  playbackScope: "folder_loop",
  shuffle: false,
  repeatOne: false,
};

export function normalizeAudioPlayerSettings(
  value: unknown,
): AudioPlayerSettings {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return DEFAULT_AUDIO_PLAYER_SETTINGS;
  }
  const raw = value as Record<string, unknown>;
  return {
    playbackScope:
      raw.playback_scope === "global_next" ? "global_next" : "folder_loop",
    shuffle: raw.shuffle === true,
    repeatOne: raw.repeat_one === true,
  };
}

export function serializeAudioPlayerSettings(settings: AudioPlayerSettings) {
  return {
    playback_scope: settings.playbackScope,
    shuffle: settings.shuffle,
    repeat_one: settings.repeatOne,
  };
}
