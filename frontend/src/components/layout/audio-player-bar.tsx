"use client";

import { useAudioPlayer } from "@/contexts/audio-player-context";
import { Button } from "@/components/ui/button";
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  X,
  Volume2,
  VolumeX,
  Music,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";

function formatTime(seconds: number): string {
  if (!seconds || !isFinite(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Base UIのscriptタグ問題を回避するカスタムスライダー */
function AudioSlider({
  value,
  max,
  onChange,
  className,
}: {
  value: number;
  max: number;
  onChange: (value: number) => void;
  className?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [isDragging, setIsDragging] = useState(false);

  const calcValue = useCallback(
    (clientX: number) => {
      const track = trackRef.current;
      if (!track) return 0;
      const rect = track.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return ratio * max;
    },
    [max]
  );

  const handlePointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      setIsDragging(true);
      const newVal = calcValue(e.clientX);
      onChange(newVal);

      const onMove = (ev: PointerEvent) => {
        onChange(calcValue(ev.clientX));
      };
      const onUp = () => {
        setIsDragging(false);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [calcValue, onChange]
  );

  const percent = max > 0 ? (value / max) * 100 : 0;

  return (
    <div
      ref={trackRef}
      className={`relative flex items-center select-none touch-none cursor-pointer ${className ?? ""}`}
      style={{ height: 16 }}
      onPointerDown={handlePointerDown}
    >
      {/* トラック背景 */}
      <div className="absolute left-0 right-0 h-1 rounded-full bg-muted" />
      {/* プログレス */}
      <div
        className="absolute left-0 h-1 rounded-full bg-primary"
        style={{ width: `${percent}%` }}
      />
      {/* つまみ */}
      <div
        className={`absolute size-3 rounded-full border border-ring bg-white transition-shadow ${
          isDragging ? "ring-3 ring-ring/50" : "hover:ring-3 hover:ring-ring/50"
        }`}
        style={{ left: `calc(${percent}% - 6px)` }}
      />
    </div>
  );
}

export function AudioPlayerBar() {
  const {
    track,
    playlist,
    isPlaying,
    currentTime,
    duration,
    volume,
    pause,
    resume,
    stop,
    next,
    prev,
    seek,
    setVolume,
  } = useAudioPlayer();

  const [prevVolume, setPrevVolume] = useState(1);

  const toggleMute = useCallback(() => {
    if (volume > 0) {
      setPrevVolume(volume);
      setVolume(0);
    } else {
      setVolume(prevVolume || 1);
    }
  }, [volume, prevVolume, setVolume]);

  const togglePlay = useCallback(() => {
    if (isPlaying) pause();
    else resume();
  }, [isPlaying, pause, resume]);

  // 現在のトラック位置
  const currentIndex = track
    ? playlist.findIndex((t) => t.path === track.path)
    : -1;
  const hasPrev = currentIndex > 0;
  const hasNext = currentIndex >= 0 && currentIndex < playlist.length - 1;

  if (!track) return null;

  return (
    <div className="fixed bottom-0 left-0 right-0 z-40 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="flex items-center h-16 px-4 gap-3">
        {/* トラック情報 */}
        <div className="flex items-center gap-3 min-w-0 w-56 shrink-0">
          <div className="size-10 rounded bg-green-900/30 flex items-center justify-center shrink-0">
            <Music className="size-5 text-green-500" />
          </div>
          <div className="min-w-0">
            <p className="text-sm font-medium truncate">{track.name}</p>
          </div>
        </div>

        {/* 再生コントロール */}
        <div className="flex flex-col items-center gap-0.5 flex-1 max-w-xl mx-auto">
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={prev}
              disabled={!hasPrev}
              className="text-muted-foreground hover:text-foreground"
            >
              <SkipBack className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={togglePlay}
              className="text-foreground"
            >
              {isPlaying ? (
                <Pause className="size-5" fill="currentColor" />
              ) : (
                <Play className="size-5" fill="currentColor" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={next}
              disabled={!hasNext}
              className="text-muted-foreground hover:text-foreground"
            >
              <SkipForward className="size-4" />
            </Button>
          </div>
          {/* シークバー */}
          <div className="flex items-center gap-2 w-full">
            <span className="text-[10px] text-muted-foreground tabular-nums w-8 text-right">
              {formatTime(currentTime)}
            </span>
            <AudioSlider
              value={currentTime}
              max={duration || 1}
              onChange={seek}
              className="flex-1"
            />
            <span className="text-[10px] text-muted-foreground tabular-nums w-8">
              {formatTime(duration)}
            </span>
          </div>
        </div>

        {/* ボリューム + 閉じる */}
        <div className="flex items-center gap-2 w-40 shrink-0 justify-end">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={toggleMute}
            className="text-muted-foreground hover:text-foreground"
          >
            {volume === 0 ? (
              <VolumeX className="size-4" />
            ) : (
              <Volume2 className="size-4" />
            )}
          </Button>
          <AudioSlider
            value={volume}
            max={1}
            onChange={setVolume}
            className="w-20"
          />
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={stop}
            className="text-muted-foreground hover:text-foreground ml-1"
            title="閉じる"
          >
            <X className="size-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
