"use client";

import { useCallback } from "react";
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
import type { GenerationProfile } from "@/lib/generation-profile";

const GENERATION_PROFILES = [
  {
    value: "chat",
    label: "チャットモード",
    mnemonic: "C",
    desc: "単発応答、必要なツールのみ使用",
    icon: MessageSquare,
  },
  {
    value: "assisted_work",
    label: "支援作業",
    mnemonic: "S",
    desc: "検証ループあり、ツールは確認",
    icon: ShieldCheck,
  },
  {
    value: "autonomous_work",
    label: "自律作業",
    mnemonic: "A",
    desc: "検証ループあり、権限を自動承認",
    icon: Code,
  },
  {
    value: "review",
    label: "レビュー",
    mnemonic: "R",
    desc: "変更せず確認中心で実行",
    icon: ClipboardList,
  },
] as const;

type Props = {
  value: GenerationProfile;
  onChange: (mode: GenerationProfile) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onComposerFocusRequest?: () => void;
};

export function GenerationProfileSelector({
  value,
  onChange,
  open,
  onOpenChange,
  onComposerFocusRequest,
}: Props) {
  const current =
    GENERATION_PROFILES.find((m) => m.value === value) ??
    GENERATION_PROFILES[0];
  const CurrentIcon = current.icon;
  const focusComposer = useCallback(() => {
    requestAnimationFrame(() => onComposerFocusRequest?.());
  }, [onComposerFocusRequest]);

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(nextOpen) => {
        onOpenChange(nextOpen);
        if (!nextOpen) focusComposer();
      }}
    >
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0"
            title={`${current.label} (Ctrl+M)`}
            aria-label="動作モード"
          />
        }
      >
        <CurrentIcon className="size-4" />
        <ChevronUp className="ml-0.5 size-3 text-muted-foreground" />
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" sideOffset={8} align="start" className="w-64">
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(v) => {
            onChange(v as GenerationProfile);
            onOpenChange(false);
            focusComposer();
          }}
        >
          {GENERATION_PROFILES.map((mode) => {
            const Icon = mode.icon;
            return (
              <DropdownMenuRadioItem
                key={mode.value}
                value={mode.value}
                mnemonic={mode.mnemonic}
              >
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
