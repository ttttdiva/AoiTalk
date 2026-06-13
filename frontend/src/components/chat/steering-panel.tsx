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
          setValues({ ...DEFAULT_VALUES, ...data.rp_settings });
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
          chatApi.updateRpSettings(sessionId, updated).catch(() => {});
        }, 500);

        return updated;
      });
    },
    [sessionId],
  );

  if (!isVisible) return null;

  return (
    <div className="border-t bg-muted/30">
      <button
        className="flex w-full items-center justify-between px-4 py-2 text-xs text-muted-foreground hover:bg-muted/50 transition-colors"
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
        <div className="space-y-3 px-4 pb-3">
          {SLIDER_DEFS.map((def) => (
            <div key={def.key} className="space-y-1">
              <div className="flex items-center justify-between">
                <Label className="text-xs">{def.label}</Label>
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  {values[def.key] ?? 50}
                </span>
              </div>
              <input
                type="range"
                min={0}
                max={100}
                value={values[def.key] ?? 50}
                onChange={(e) => handleChange(def.key, Number(e.target.value))}
                className="w-full h-1 rounded-full appearance-none bg-muted accent-primary cursor-pointer"
              />
              <p className="text-[10px] text-muted-foreground">{def.desc}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
