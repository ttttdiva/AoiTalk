"use client";

import {
  useCallback,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { explorerBookmarks, explorerSearch } from "@/lib/explorer-api";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import {
  getBgmAutoEnabled,
  getBgmState,
  isDirectAudioUrl,
  isVideoAudioUrl,
  py,
  type PlayLog,
  type Room,
} from "@/lib/trpg-room-utils";

// TRPG ルームの BGM 再生・AI自動切替を管理するカスタムフック
export function useTrpgBgm({
  room,
  setRoom,
}: {
  room: Room | null;
  setRoom: Dispatch<SetStateAction<Room | null>>;
}) {
  const [bgmBusy, setBgmBusy] = useState(false);
  const lastHandledBgmRef = useRef<string | null>(null);
  const { play, stop: stopAudio, setVolume } = useAudioPlayer();

  const bgmAutoEnabled = room ? getBgmAutoEnabled(room.shared_state || {}) : true;
  const currentBgm = room ? getBgmState(room.shared_state || {}) : null;

  const playBgmTrack = useCallback(
    async (track: string, volume = 0.45) => {
      if (!room?.id) return;
      const normalized = track.trim();
      if (!normalized) return;
      const key = `${normalized}:${volume}`;
      if (lastHandledBgmRef.current === key) return;

      if (normalized.toLowerCase() === "stop") {
        stopAudio();
        lastHandledBgmRef.current = key;
        return;
      }

      setBgmBusy(true);
      try {
        if (isVideoAudioUrl(normalized)) {
          await py(`/api/trpg/rooms/${room.id}/bgm/video`, {
            method: "POST",
            body: JSON.stringify({ track: normalized }),
          });
          lastHandledBgmRef.current = key;
          return;
        }

        if (isDirectAudioUrl(normalized)) {
          const name =
            normalized.split(/[\\/]/).pop()?.replace(/[?#].*$/, "") ||
            "BGM";
          play({ name, path: normalized, type: "audio" });
          setVolume(volume);
          lastHandledBgmRef.current = key;
          return;
        }

        const bookmarkData = await explorerBookmarks();
        const bgmBookmark = bookmarkData.success
          ? bookmarkData.bookmarks.find(
              (b) => b.name === "BGM" || b.path.toLowerCase().includes("bgm"),
            )
          : undefined;
        const searchRoot = bgmBookmark ? bgmBookmark.path : "";
        const searchRes = await explorerSearch(normalized, searchRoot, 1);
        if (searchRes.success && searchRes.results.length > 0) {
          const file = searchRes.results[0];
          play({ name: file.name, path: file.path, type: "audio" });
          setVolume(volume);
          lastHandledBgmRef.current = key;
        } else {
          console.warn(`TRPG BGM not found: ${normalized}`);
        }
      } catch (e) {
        console.error("TRPG BGM playback failed:", e);
      } finally {
        setBgmBusy(false);
      }
    },
    [play, room?.id, setVolume, stopAudio],
  );

  const handleBgmLog = useCallback(
    (log: PlayLog) => {
      if (!bgmAutoEnabled) return;
      const metadata = log.metadata || {};
      const action = typeof metadata.action === "string" ? metadata.action : "";
      const track = typeof metadata.track === "string" ? metadata.track : "";
      const volume =
        typeof metadata.volume === "number" ? metadata.volume : undefined;
      if (action === "stop" || track.toLowerCase() === "stop") {
        void playBgmTrack("stop", 0);
      } else if (track) {
        void playBgmTrack(track, volume ?? 0.45);
      }
    },
    [bgmAutoEnabled, playBgmTrack],
  );

  const handleBgmAutoToggle = useCallback(
    async (enabled: boolean) => {
      if (!room) return;
      const updates: Record<string, unknown> = {
        bgm_auto_enabled: enabled,
      };
      if (!enabled) {
        updates.bgm = null;
      }
      try {
        const sharedState = await py<Record<string, unknown>>(
          `/api/trpg/rooms/${room.id}/shared_state`,
          {
            method: "PUT",
            body: JSON.stringify({ updates }),
          },
        );
        setRoom((prev) =>
          prev ? { ...prev, shared_state: sharedState } : prev,
        );
        if (!enabled) {
          stopAudio();
          lastHandledBgmRef.current = "stop:0";
        } else if (currentBgm?.track) {
          await playBgmTrack(currentBgm.track, currentBgm.volume ?? 0.45);
        }
      } catch (e) {
        console.error(e);
        alert("BGM自動再生設定の更新に失敗しました");
      }
    },
    [currentBgm, playBgmTrack, room, setRoom, stopAudio],
  );

  // BGM を停止し、直前の再生キーを stop に固定する
  const stopBgm = useCallback(() => {
    stopAudio();
    lastHandledBgmRef.current = "stop:0";
  }, [stopAudio]);

  return {
    bgmAutoEnabled,
    currentBgm,
    bgmBusy,
    playBgmTrack,
    handleBgmLog,
    handleBgmAutoToggle,
    stopBgm,
  };
}
