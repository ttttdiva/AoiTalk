"use client";

import type React from "react";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import {
  STATUS_DOT_COLORS,
  STATUS_KEY_HINTS,
  STATUS_LABELS,
} from "@/lib/tasks-page-utils";

export const TASK_STATUS_OPTIONS = [
  "open",
  "in_progress",
  "on_hold",
  "review",
  "closed",
] as const;

export type TaskStatusOption = (typeof TASK_STATUS_OPTIONS)[number];

/**
 * ステータス変更ドロップダウンの共通メニュー項目。
 * currentStatus を渡すと該当ステータスを太字表示する。
 */
export function TaskStatusMenuItems({
  currentStatus,
  onSelect,
}: {
  currentStatus?: string;
  onSelect: (status: TaskStatusOption, e: React.MouseEvent) => void;
}) {
  return (
    <>
      {TASK_STATUS_OPTIONS.map((status) => (
        <DropdownMenuItem
          key={status}
          className={cn(
            "flex items-center justify-between gap-2 cursor-pointer",
            currentStatus !== undefined &&
              currentStatus === status &&
              "font-bold",
          )}
          onClick={(e) => onSelect(status, e)}
        >
          <span className="flex items-center gap-2">
            <span
              className={cn(
                "inline-block size-2.5 rounded-full border-2",
                STATUS_DOT_COLORS[status],
              )}
            />
            {STATUS_LABELS[status]}
          </span>
          {STATUS_KEY_HINTS[status] && (
            <kbd className="ml-auto text-[10px] text-muted-foreground opacity-60">
              {STATUS_KEY_HINTS[status]}
            </kbd>
          )}
        </DropdownMenuItem>
      ))}
    </>
  );
}
