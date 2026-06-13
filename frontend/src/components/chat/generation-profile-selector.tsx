"use client";

import {
  MessageSquare,
  ShieldCheck,
  Code,
  ClipboardList,
  ChevronUp,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";

export type GenerationProfile =
  | "chat"
  | "assisted_work"
  | "autonomous_work"
  | "review";

const GENERATION_PROFILES = [
  {
    value: "chat",
    label: "チャットモード",
    desc: "単発応答、必要なツールのみ使用",
    icon: MessageSquare,
  },
  {
    value: "assisted_work",
    label: "支援作業",
    desc: "検証ループあり、ツールは確認",
    icon: ShieldCheck,
  },
  {
    value: "autonomous_work",
    label: "自律作業",
    desc: "検証ループあり、権限を自動承認",
    icon: Code,
  },
  {
    value: "review",
    label: "レビュー",
    desc: "変更せず確認中心で実行",
    icon: ClipboardList,
  },
] as const;

type Props = {
  value: GenerationProfile;
  onChange: (mode: GenerationProfile) => void;
};

export function GenerationProfileSelector({ value, onChange }: Props) {
  const current =
    GENERATION_PROFILES.find((m) => m.value === value) ??
    GENERATION_PROFILES[0];
  const CurrentIcon = current.icon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button variant="ghost" size="icon" className="shrink-0" title={current.label} />
        }
      >
        <CurrentIcon className="size-4" />
        <ChevronUp className="ml-0.5 size-3 text-muted-foreground" />
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" sideOffset={8} align="start" className="w-64">
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(v) => onChange(v as GenerationProfile)}
        >
          {GENERATION_PROFILES.map((mode) => {
            const Icon = mode.icon;
            return (
              <DropdownMenuRadioItem key={mode.value} value={mode.value}>
                <Icon className="size-4 shrink-0" />
                <div className="flex flex-col">
                  <span className="text-sm font-medium">{mode.label}</span>
                  <span className="text-xs text-muted-foreground">
                    {mode.desc}
                  </span>
                </div>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
