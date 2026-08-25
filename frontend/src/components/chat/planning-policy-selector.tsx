"use client";

import { useCallback } from "react";
import { ChevronUp, Compass, ListChecks, Zap } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import type { PlanningPolicy } from "@/lib/planning-policy";

const PLANNING_POLICIES = [
  {
    value: "auto",
    label: "自動",
    mnemonic: "A",
    desc: "必要なときだけ計画フェーズへ",
    icon: Compass,
  },
  {
    value: "plan_first",
    label: "計画優先",
    mnemonic: "P",
    desc: "新作業は必ず計画から開始",
    icon: ListChecks,
  },
  {
    value: "direct",
    label: "直接実行",
    mnemonic: "D",
    desc: "自発計画なし（質問・許可は可）",
    icon: Zap,
  },
] as const;

type Props = {
  value: PlanningPolicy;
  onChange: (mode: PlanningPolicy) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onComposerFocusRequest?: () => void;
};

export function PlanningPolicySelector({
  value,
  onChange,
  open,
  onOpenChange,
  onComposerFocusRequest,
}: Props) {
  const current =
    PLANNING_POLICIES.find((item) => item.value === value) ??
    PLANNING_POLICIES[0];
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
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 gap-1 px-2 text-xs"
            title={`計画ポリシー: ${current.label}`}
          >
            <CurrentIcon className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{current.label}</span>
            <ChevronUp className="h-3 w-3 opacity-60" />
          </Button>
        }
      />
      <DropdownMenuContent align="start" className="w-72">
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(next) => onChange(next as PlanningPolicy)}
        >
          {PLANNING_POLICIES.map((item) => {
            const Icon = item.icon;
            return (
              <DropdownMenuRadioItem
                key={item.value}
                value={item.value}
                className="items-start gap-2 py-2"
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="min-w-0">
                  <div className="font-medium">{item.label}</div>
                  <div className="text-xs text-muted-foreground">{item.desc}</div>
                </div>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
