"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { Sliders, ChevronDown, ChevronUp } from "lucide-react";
import { Label } from "@/components/ui/label";
import { chatApi } from "@/lib/chat-api";

interface SteeringPanelProps {
  sessionId: string;
  isVisible: boolean;
}

const SLIDER_DEFS = [
  { key: "creativity", label: "創造性", desc: "プロンプトに忠実 ↔ 自由に即興" },
  { key: "detail", label: "詳細度", desc: "簡潔 ↔ 詳細な描写" },
  { key: "tempo", label: "テンポ", desc: "ゆっくり ↔ 速い展開" },
  { key: "emotion", label: "感情", desc: "淡々 ↔ ドラマチック" },
] as const;

const DEFAULT_VALUES: Record<string, number> = {
  creativity: 50,
  detail: 50,
  tempo: 50,
  emotion: 50,
};

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, value));
}

function toDisplaySettings(
  settings: Record<string, number> | undefined,
): Record<string, number> {
  if (!settings) return {};

  return Object.fromEntries(
    Object.entries(settings).flatMap(([key, value]) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return [];
      return [[key, clampPercent(value * 100)]];
    }),
  );
}

function toApiSettings(
  settings: Record<string, number>,
): Record<string, number> {
  return Object.fromEntries(
    Object.entries(settings).flatMap(([key, value]) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return [];
      return [[key, clampPercent(value) / 100]];
    }),
  );
}

export function SteeringPanel({ sessionId, isVisible }: SteeringPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [values, setValues] = useState<Record<string, number>>({
    ...DEFAULT_VALUES,
  });
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // セッション変更時にRP設定を取得
  useEffect(() => {
    if (!sessionId) return;
    chatApi
      .getRpSettings(sessionId)
      .then((data) => {
        if (data.rp_settings && Object.keys(data.rp_settings).length > 0) {
          setValues({
            ...DEFAULT_VALUES,
            ...toDisplaySettings(data.rp_settings),
          });
        } else {
          setValues({ ...DEFAULT_VALUES });
        }
      })
      .catch(() => setValues({ ...DEFAULT_VALUES }));
  }, [sessionId]);

  const handleChange = useCallback(
    (key: string, newValue: number) => {
      setValues((prev) => {
        const updated = { ...prev, [key]: newValue };

        // デバウンスでAPIに保存
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
          chatApi
            .updateRpSettings(sessionId, toApiSettings(updated))
            .catch(() => {});
        }, 500);

        return updated;
      });
    },
    [sessionId],
  );

  if (!isVisible) return null;

  return (
    <div className="border-t border-border-subtle bg-surface-container-low">
      <button
        className="flex w-full items-center justify-between px-4 py-2.5 text-[11px] uppercase tracking-[0.08em] text-text-secondary transition-colors hover:bg-surface-slate hover:text-on-surface"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="flex items-center gap-1.5">
          <Sliders className="size-3" />
          ステアリング
        </span>
        {expanded ? (
          <ChevronDown className="size-3" />
        ) : (
          <ChevronUp className="size-3" />
        )}
      </button>

      {expanded && (
        <div className="space-y-3 border-t border-border-subtle px-4 pb-4 pt-3">
          {SLIDER_DEFS.map((def) => (
            <div key={def.key} className="space-y-1">
              <div className="flex items-center justify-between">
                <Label className="text-xs">{def.label}</Label>
                <span className="text-[10px] text-primary tabular-nums">
                  {values[def.key] ?? 50}
                </span>
              </div>
              <input
                type="range"
                min={0}
                max={100}
                value={values[def.key] ?? 50}
                onChange={(e) => handleChange(def.key, Number(e.target.value))}
                className="h-1 w-full cursor-pointer appearance-none rounded-full bg-surface-container-highest accent-primary"
              />
              <p className="text-[10px] text-text-secondary">{def.desc}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
