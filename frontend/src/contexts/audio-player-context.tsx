"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { explorerList, filerBrowse } from "@/lib/explorer-api";
import { useUserSettings } from "@/contexts/user-settings-context";
import { getFileExt } from "@/lib/utils";

interface AudioTrack {
  name: string;
  path: string;
  type: string;
  rootPath?: string;
  sourceKind?: "explorer" | "filer";
}

interface AudioPlayerState {
  track: AudioTrack | null;
  playlist: AudioTrack[];
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  volume: number;
}

interface AudioPlayerContextType extends AudioPlayerState {
  play: (track: AudioTrack, playlist?: AudioTrack[]) => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
  next: () => void;
  prev: () => void;
  seek: (time: number) => void;
  setVolume: (vol: number) => void;
  audioRef: React.RefObject<HTMLAudioElement | null>;
}

const AudioPlayerContext = createContext<AudioPlayerContextType | null>(null);
const VOLUME_STORAGE_KEY = "aoitalk-audio-volume";
const AUDIO_EXTENSIONS = new Set(["mp3", "wav", "ogg", "flac", "m4a", "aac"]);
const MAX_GLOBAL_AUDIO_TRACKS = 1000;
const MAX_GLOBAL_AUDIO_DIRECTORIES = 240;

export function useAudioPlayer() {
  const ctx = useContext(AudioPlayerContext);
  if (!ctx) throw new Error("useAudioPlayer must be used within AudioPlayerProvider");
  return ctx;
}

function loadSavedVolume(): number {
  if (typeof window === "undefined") return 1;
  try {
    const saved = localStorage.getItem(VOLUME_STORAGE_KEY);
    if (saved !== null) {
      const value = parseFloat(saved);
      if (isFinite(value) && value >= 0 && value <= 1) return value;
    }
  } catch {}
  return 1;
}

function isAudioTrackLike(track: Pick<AudioTrack, "name" | "type">): boolean {
  return track.type.startsWith("audio") || AUDIO_EXTENSIONS.has(getFileExt(track.name));
}

function sortedUniqueTracks(tracks: AudioTrack[]): AudioTrack[] {
  const seen = new Set<string>();
  return tracks
    .filter((track) => {
      if (seen.has(track.path)) return false;
      seen.add(track.path);
      return true;
    })
    .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
}

export function AudioPlayerProvider({ children }: { children: React.ReactNode }) {
  const { audioPlayerSettings } = useUserSettings();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [track, setTrack] = useState<AudioTrack | null>(null);
  const [playlist, setPlaylist] = useState<AudioTrack[]>([]);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolumeState] = useState(loadSavedVolume);
  const seekingRef = useRef(false);
  const settingsRef = useRef(audioPlayerSettings);
  const playbackGenerationRef = useRef(0);

  useEffect(() => {
    settingsRef.current = audioPlayerSettings;
  }, [audioPlayerSettings]);

  const loadGlobalPlaylist = useCallback(async (baseTrack: AudioTrack) => {
    if (!baseTrack.rootPath || !baseTrack.sourceKind) return null;
    const rootPath = baseTrack.rootPath;
    const sourceKind = baseTrack.sourceKind;
    const pending = [rootPath];
    const tracks: AudioTrack[] = [];
    let scanned = 0;

    while (
      pending.length > 0 &&
      tracks.length < MAX_GLOBAL_AUDIO_TRACKS &&
      scanned < MAX_GLOBAL_AUDIO_DIRECTORIES
    ) {
      const path = pending.shift() ?? "";
      scanned += 1;
      const data =
        sourceKind === "filer" ? await filerBrowse(path) : await explorerList(path);
      const directories =
        sourceKind === "filer"
          ? (data as Awaited<ReturnType<typeof filerBrowse>>).folders.map(
              (folder) => folder.path,
            )
          : (data as Awaited<ReturnType<typeof explorerList>>).directories.map(
              (directory) => directory.path,
            );
      pending.push(...directories);
      tracks.push(
        ...data.files
          .filter((file) => isAudioTrackLike({ name: file.name, type: file.type || "audio" }))
          .map((file) => ({
            name: file.name,
            path: file.path,
            type: file.type || "audio",
            rootPath,
            sourceKind,
          })),
      );
    }

    return sortedUniqueTracks(tracks);
  }, []);

  const playTrack = useCallback((nextTrack: AudioTrack) => {
    const audio = audioRef.current;
    if (!audio) return;
    setTrack(nextTrack);
    setCurrentTime(0);
    setDuration(0);
    audio.src = getFilerFileUrl(nextTrack.path);
    audio.volume = loadSavedVolume();
    audio.play().catch(() => {});
  }, []);

  const pickAdjacentTrack = useCallback(
    async (
      direction: 1 | -1,
      wrap: boolean,
      playbackGeneration: number,
    ): Promise<AudioTrack | null> => {
      if (!track) return null;
      const audio = audioRef.current;
      if (direction === -1 && audio && audio.currentTime > 3) {
        audio.currentTime = 0;
        return null;
      }

      const settings = settingsRef.current;
      if (direction === 1 && settings.repeatOne) return track;

      let candidates = playlist;
      if (direction === 1 && settings.playbackScope === "global_next") {
        const globalPlaylist = await loadGlobalPlaylist(track).catch(() => null);
        if (playbackGeneration !== playbackGenerationRef.current) return null;
        if (globalPlaylist?.length) {
          candidates = globalPlaylist;
          setPlaylist(globalPlaylist);
        }
      }
      if (candidates.length === 0) return null;

      if (direction === 1 && settings.shuffle && candidates.length > 1) {
        const pool = candidates.filter((item) => item.path !== track.path);
        return pool[Math.floor(Math.random() * pool.length)] ?? null;
      }

      const index = candidates.findIndex((item) => item.path === track.path);
      const nextIndex = index + direction;
      if (nextIndex >= 0 && nextIndex < candidates.length) return candidates[nextIndex];
      if (wrap && direction === 1) return candidates[0] ?? null;
      return null;
    },
    [loadGlobalPlaylist, playlist, track],
  );

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTimeUpdate = () => {
      if (!seekingRef.current) setCurrentTime(audio.currentTime);
    };
    const onDurationChange = () => setDuration(audio.duration || 0);
    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    const onEnded = () => {
      const playbackGeneration = ++playbackGenerationRef.current;
      void pickAdjacentTrack(1, true, playbackGeneration).then((nextTrack) => {
        if (playbackGeneration !== playbackGenerationRef.current) return;
        if (nextTrack) playTrack(nextTrack);
        else setIsPlaying(false);
      });
    };

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("durationchange", onDurationChange);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);

    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
    };
  }, [pickAdjacentTrack, playTrack]);

  const play = useCallback((newTrack: AudioTrack, newPlaylist?: AudioTrack[]) => {
    playbackGenerationRef.current += 1;
    const first = newPlaylist?.[0];
    const normalizedTrack = {
      ...newTrack,
      rootPath: newTrack.rootPath ?? first?.rootPath,
      sourceKind: newTrack.sourceKind ?? first?.sourceKind,
    };
    setPlaylist(newPlaylist ?? [normalizedTrack]);
    playTrack(normalizedTrack);
  }, [playTrack]);

  const pause = useCallback(() => {
    playbackGenerationRef.current += 1;
    audioRef.current?.pause();
  }, []);

  const resume = useCallback(() => {
    playbackGenerationRef.current += 1;
    audioRef.current?.play().catch(() => {});
  }, []);

  const stop = useCallback(() => {
    playbackGenerationRef.current += 1;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.src = "";
    }
    setTrack(null);
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
  }, []);

  const next = useCallback(() => {
    const playbackGeneration = ++playbackGenerationRef.current;
    void pickAdjacentTrack(1, true, playbackGeneration).then((nextTrack) => {
      if (playbackGeneration !== playbackGenerationRef.current) return;
      if (nextTrack) playTrack(nextTrack);
    });
  }, [pickAdjacentTrack, playTrack]);

  const prev = useCallback(() => {
    const playbackGeneration = ++playbackGenerationRef.current;
    void pickAdjacentTrack(-1, false, playbackGeneration).then((prevTrack) => {
      if (playbackGeneration !== playbackGenerationRef.current) return;
      if (prevTrack) playTrack(prevTrack);
    });
  }, [pickAdjacentTrack, playTrack]);

  const seek = useCallback((time: number) => {
    const audio = audioRef.current;
    if (audio) {
      seekingRef.current = true;
      audio.currentTime = time;
      setCurrentTime(time);
      const onSeeked = () => {
        seekingRef.current = false;
        audio.removeEventListener("seeked", onSeeked);
      };
      audio.addEventListener("seeked", onSeeked);
    }
  }, []);

  const setVolume = useCallback((vol: number) => {
    const audio = audioRef.current;
    if (audio) audio.volume = vol;
    setVolumeState(vol);
    try {
      localStorage.setItem(VOLUME_STORAGE_KEY, String(vol));
    } catch {}
  }, []);

  return (
    <AudioPlayerContext.Provider
      value={{
        track,
        playlist,
        isPlaying,
        currentTime,
        duration,
        volume,
        play,
        pause,
        resume,
        stop,
        next,
        prev,
        seek,
        setVolume,
        audioRef,
      }}
    >
      {children}
      <audio ref={audioRef} preload="auto" />
    </AudioPlayerContext.Provider>
  );
}

function isAbsoluteFilePath(path: string): boolean {
  return Boolean(path) && (/^[A-Za-z]:[\\/]/.test(path) || path.startsWith("/"));
}

function getFilerFileUrl(filePath: string) {
  if (/^https?:\/\//i.test(filePath)) {
    return filePath;
  }
  if (isAbsoluteFilePath(filePath)) {
    return `/api/python-proxy/filer/file?path=${encodeURIComponent(filePath)}`;
  }
  return `/api/python-proxy/explorer/serve?path=${encodeURIComponent(filePath)}`;
}
