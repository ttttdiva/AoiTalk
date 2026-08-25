"use client";

import {
  Bot as BotIcon,
  Check,
  Mic,
  Radio,
  Volume2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type {
  RuntimeFeatureState,
  RuntimeVoiceStatus,
} from "@/contexts/runtime-context";
import { normalizeVoiceRms } from "@/lib/voice-level";
export type { RuntimeLlmEngine } from "@/contexts/runtime-context";

export type RuntimeUtilityPanelProps = {
  open: boolean;
  onClose: () => void;
  pythonConnected: boolean;
  runtimeFeatures: RuntimeFeatureState | null;
  changeRuntimeFeature: (
    feature: string,
    enabled: boolean,
  ) => boolean | Promise<boolean>;
  changeRuntimeFeatures: (
    features: Record<string, boolean>,
  ) => boolean | Promise<boolean>;
  voiceStatus: RuntimeVoiceStatus | null;
};

export function RuntimeUtilityPanel({
  open,
  onClose,
  pythonConnected,
  runtimeFeatures,
  changeRuntimeFeature,
  changeRuntimeFeatures,
  voiceStatus,
}: RuntimeUtilityPanelProps) {
  const runtimeFeatureFlags = runtimeFeatures?.features ?? {};
  const [featureMutationPending, setFeatureMutationPending] = useState(false);
  const discordBotService = runtimeFeatures?.discord_bot_service;
  const discordBotState = discordBotService?.state ?? "stopped";
  const discordBotTitle =
    discordBotState === "running"
      ? `Discord Bot: 稼働中${discordBotService?.user ? ` (${discordBotService.user})` : ""}`
      : discordBotState === "starting"
        ? "Discord Bot: 起動中"
        : discordBotState === "stopping"
          ? "Discord Bot: 停止中"
          : discordBotState === "failed"
            ? `Discord Bot: 起動失敗${discordBotService?.last_error ? ` - ${discordBotService.last_error}` : ""}`
            : "Discord Bot/VC";

  const featureEntries = [
    { key: "local_mic", icon: Mic, title: "ローカルマイク入力" },
    { key: "tts", icon: Volume2, title: "読み上げ" },
    { key: "discord", icon: BotIcon, title: discordBotTitle },
  ] as const;

  const panelRef = useRef<HTMLElement | null>(null);
  const runFeatureMutation = async (
    mutation: () => boolean | Promise<boolean>,
  ) => {
    if (featureMutationPending) return;
    setFeatureMutationPending(true);
    try {
      await mutation();
    } finally {
      setFeatureMutationPending(false);
    }
  };
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (!target || panelRef.current?.contains(target)) return;
      // The trigger is outside the panel but owns the toggle state. Let its
      // click handler decide whether to close/open instead of closing here.
      if (target.closest("[data-runtime-panel-trigger='true']")) return;
      onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [onClose, open]);

  if (!open) return null;

  return (
    <aside
      ref={panelRef}
      className="ao-runtime-panel fixed z-[70] flex flex-col overflow-hidden border text-card-foreground"
      role="dialog"
      aria-label="ランタイム設定"
      data-shell-region="runtime-panel"
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Radio className="size-4 shrink-0 text-primary" />
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">Runtime</h2>
            <p className="truncate text-[11px] text-muted-foreground">
              Connection・音声・API
            </p>
          </div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          aria-label="ランタイム設定を閉じる"
          title="閉じる"
        >
          <X className="size-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        <section className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold">Connection</h3>
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <span
                className={`size-2 rounded-full ${pythonConnected ? "bg-emerald-500" : "bg-muted-foreground/40"}`}
              />
              {pythonConnected ? "Python API 接続中" : "Python API 未接続"}
            </span>
          </div>
          {voiceStatus && (
            <div className="rounded-md border border-border/70 bg-background/50 p-2">
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <Mic className="size-3.5" />
                  Voice
                </span>
                <span className="text-muted-foreground">
                  {voiceStatus.recording
                    ? "録音中"
                    : voiceStatus.ready
                      ? "待機"
                      : "停止"}
                </span>
              </div>
              <div
                className="mt-2 h-1 overflow-hidden rounded-full bg-muted"
                role="meter"
                aria-label="Voice入力レベル"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(normalizeVoiceRms(voiceStatus.rms) * 100)}
              >
                <div
                  className={`h-full transition-all duration-200 ${voiceStatus.recording ? "bg-red-500" : "bg-primary"}`}
                  style={{ width: `${Math.round(normalizeVoiceRms(voiceStatus.rms) * 100)}%` }}
                />
              </div>
            </div>
          )}
        </section>

        <section className="space-y-2">
          <h3 className="text-xs font-semibold">Voice & services</h3>
          <div className="grid grid-cols-3 gap-2">
            {featureEntries.map(({ key, icon: Icon, title }) => {
              const enabled =
                key === "discord"
                  ? !!(
                      runtimeFeatureFlags.discord_bot &&
                      runtimeFeatureFlags.discord_text &&
                      runtimeFeatureFlags.discord_vc_input &&
                      runtimeFeatureFlags.discord_vc_output
                    )
                  : !!runtimeFeatureFlags[key];
              const discordFailed =
                key === "discord" && enabled && discordBotState === "failed";
              const discordChanging =
                key === "discord" &&
                enabled &&
                (discordBotState === "starting" || discordBotState === "stopping");
              const discordRunning =
                key === "discord" && enabled && discordBotState === "running";
              return (
                <button
                  key={key}
                  type="button"
                  disabled={
                    !pythonConnected || !runtimeFeatures || featureMutationPending
                  }
                  onClick={() => {
                    if (key === "discord") {
                      const nextEnabled = !enabled;
                      void runFeatureMutation(() =>
                        changeRuntimeFeatures({
                          discord_bot: nextEnabled,
                          discord_text: nextEnabled,
                          discord_vc_input: nextEnabled,
                          discord_vc_output: nextEnabled,
                          tts:
                            nextEnabled || !!runtimeFeatureFlags.local_speaker,
                        }),
                      );
                      return;
                    }
                    void runFeatureMutation(() =>
                      changeRuntimeFeature(key, !enabled),
                    );
                  }}
                  title={title}
                  aria-label={title}
                  className={`relative flex min-h-12 flex-col items-center justify-center gap-1 rounded-md border text-[11px] transition-colors ${
                    enabled
                      ? discordFailed
                        ? "border-destructive/60 bg-destructive text-destructive-foreground"
                        : discordChanging
                          ? "border-amber-500/70 bg-amber-500 text-white"
                          : discordRunning
                            ? "border-emerald-500/70 bg-emerald-500 text-white"
                            : "border-primary/50 bg-primary text-primary-foreground"
                      : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-foreground"
                  }`}
                >
                  <Icon className="size-4" />
                  <span>{key === "local_mic" ? "Mic" : key === "tts" ? "TTS" : "Discord"}</span>
                  {enabled && <Check className="absolute right-1 top-1 size-3" />}
                </button>
              );
            })}
          </div>
        </section>
      </div>
    </aside>
  );
}
