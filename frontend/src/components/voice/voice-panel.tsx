"use client";

import { useState } from "react";
import {
  LoaderCircle,
  Mic,
  MicOff,
  Phone,
  PhoneOff,
  Radio,
  Square,
  Volume2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import {
  useVoiceSession,
  type UseVoiceSessionOptions,
  type VoiceSessionPhase,
} from "./use-voice-session";
import type { VoiceSessionMode } from "@/lib/voice-session-api";

export type VoicePanelProps = UseVoiceSessionOptions & {
  disabled?: boolean;
  className?: string;
  title?: string;
};

function phaseLabel(phase: VoiceSessionPhase): string {
  switch (phase) {
    case "requesting":
      return "準備中";
    case "connecting":
      return "接続中";
    case "connected":
      return "接続済み";
    case "disconnecting":
      return "切断中";
    case "ended":
      return "終了";
    case "error":
      return "エラー";
    default:
      return "待機中";
  }
}

function modeLabel(mode: VoiceSessionMode): string {
  switch (mode) {
    case "realtime_character_tts":
      return "Realtime + キャラクター音声";
    case "pipeline":
      return "Pipeline";
    case "realtime_native":
    default:
      return "Live Voice";
  }
}

/** Unified composer popover for authenticated voice sessions. */
export function VoicePanel({
  disabled = false,
  className,
  title,
  mode = "realtime_native",
  ...options
}: VoicePanelProps) {
  const [open, setOpen] = useState(false);
  // `mode` remains an initial/default value for callers.  The selector is
  // intentionally local so changing it cannot mutate a running session.
  const [selectedMode, setSelectedMode] = useState<VoiceSessionMode>(mode);
  const { state, remoteAudioRef, start, stop, interrupt, toggleMute } =
    useVoiceSession({ ...options, mode: selectedMode });
  const active =
    state.phase === "requesting" ||
    state.phase === "connecting" ||
    state.phase === "connected" ||
    state.phase === "disconnecting";
  const panelTitle = title ?? modeLabel(selectedMode);

  return (
    <>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              type="button"
              variant={active ? "secondary" : "ghost"}
              size="icon"
              className={cn(
                "shrink-0",
                active && "border border-primary/40 text-primary shadow-sm",
                className,
              )}
              disabled={disabled && !active}
              title={panelTitle}
              aria-label={panelTitle}
              aria-pressed={active}
            />
          }
        >
          {active ? <Radio className="size-4" /> : <Mic className="size-4" />}
        </PopoverTrigger>
        <PopoverContent
          side="top"
          align="end"
          sideOffset={8}
          className="w-[min(24rem,calc(100vw-1rem))] p-0"
          data-voice-panel="true"
          data-live-voice-panel="true"
        >
          <div className="flex items-center justify-between border-b px-3 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <div className="grid size-8 shrink-0 place-items-center rounded-full bg-primary/10 text-primary">
                <Volume2 className="size-4" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">{panelTitle}</div>
                <div className="text-xs text-muted-foreground">
                  {phaseLabel(state.phase)} · {state.provider ?? "openai_realtime"}
                </div>
              </div>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7 shrink-0"
              onClick={() => setOpen(false)}
              aria-label={`${panelTitle}パネルを閉じる`}
            >
              <X className="size-3.5" />
            </Button>
          </div>

          <div className="space-y-3 p-3">
            <label className="grid gap-1 text-xs text-muted-foreground" htmlFor="voice-mode-selector">
              <span className="font-medium text-foreground">音声会話モード</span>
              <AppSelect
                id="voice-mode-selector"
                data-testid="voice-mode-selector"
                aria-label="音声会話モード"
                value={selectedMode}
                onChange={(event) =>
                  setSelectedMode(event.target.value as VoiceSessionMode)
                }
                disabled={disabled || active}
                className="h-9 w-full rounded-md border bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="realtime_native">Realtime native（OpenAI音声）</option>
                <option value="realtime_character_tts">
                  Realtime + Character Voice（キャラクター音声）
                </option>
              </AppSelect>
              <span className="sr-only">
                会話開始前に選択できます。接続中は変更できません。
              </span>
            </label>
            <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>{state.error ? "エラーが発生しました" : state.statusMessage}</span>
              {state.model && <span className="max-w-32 truncate">{state.model}</span>}
            </div>
            <div
              className="h-1.5 overflow-hidden rounded-full bg-muted"
              role="progressbar"
              aria-label="音声会話の進行状況"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.max(0, Math.min(100, state.progress))}
            >
              <div
                className={cn(
                  "h-full rounded-full bg-primary transition-[width] duration-300",
                  state.phase === "error" && "bg-destructive",
                )}
                style={{ width: `${Math.max(0, Math.min(100, state.progress))}%` }}
              />
            </div>

            {state.transcripts.length > 0 ? (
              <div
                className="max-h-52 space-y-2 overflow-y-auto rounded-lg border bg-muted/30 p-2"
                aria-label="音声会話文字起こし"
                data-voice-transcript="true"
                data-live-voice-transcript="true"
              >
                {state.transcripts.map((line) => (
                  <div
                    key={line.id}
                    className={cn(
                      "rounded-md px-2 py-1.5 text-xs leading-5",
                      line.role === "assistant"
                        ? "bg-card text-foreground"
                        : "ml-4 bg-primary/10 text-foreground",
                    )}
                  >
                    <span className="mr-1 font-medium text-muted-foreground">
                      {line.role === "assistant" ? "AoiTalk" : "あなた"}
                    </span>
                    {line.text}
                    {!line.final && <span className="ml-1 animate-pulse">…</span>}
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded-lg border border-dashed px-3 py-4 text-center text-xs text-muted-foreground">
                マイクをオンにして話しかけると文字起こしが表示されます。
              </div>
            )}

            {state.error && (
              <div
                role="alert"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-xs text-destructive"
              >
                {state.error}
              </div>
            )}

            <div className="flex items-center justify-between gap-2">
              {state.phase === "requesting" || state.phase === "connecting" ? (
                <>
                  <Button type="button" className="flex-1" disabled>
                    <LoaderCircle className="mr-1.5 size-4 animate-spin" />
                    {state.phase === "requesting" ? "準備中" : "接続中"}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={() => void stop()}
                    aria-label="音声会話を切断"
                    title="切断"
                  >
                    <PhoneOff className="size-4" />
                  </Button>
                </>
              ) : !active || state.phase === "ended" || state.phase === "error" ? (
                <Button
                  type="button"
                  className="flex-1"
                  onClick={() => void start()}
                  disabled={disabled}
                >
                  <Phone className="mr-1.5 size-4" />
                  開始
                </Button>
              ) : (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={toggleMute}
                    aria-label={state.muted ? "マイクのミュートを解除" : "マイクをミュート"}
                    title={state.muted ? "ミュート解除" : "ミュート"}
                    disabled={state.phase === "disconnecting"}
                  >
                    {state.muted ? <MicOff className="size-4" /> : <Mic className="size-4" />}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={interrupt}
                    className="flex-1"
                    disabled={state.phase !== "connected"}
                  >
                    <Square className="mr-1.5 size-3.5" />
                    中断
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="icon"
                    onClick={() => void stop()}
                    aria-label="音声会話を切断"
                    title="切断"
                  >
                    <PhoneOff className="size-4" />
                  </Button>
                </>
              )}
            </div>
          </div>
        </PopoverContent>
      </Popover>
      <audio ref={remoteAudioRef} autoPlay playsInline className="hidden" />
    </>
  );
}
