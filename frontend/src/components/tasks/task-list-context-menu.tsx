"use client";

import type React from "react";
import { createPortal } from "react-dom";

import {
  ChevronRight,
  Copy,
  Flag,
  Hash,
  Play,
  RefreshCw,
  Square,
  Trash2,
} from "lucide-react";

import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import type { Task } from "@/lib/task-api";
import { cn } from "@/lib/utils";
import {
  PRIORITY_COLORS,
  PRIORITY_LABELS,
  STATUS_DOT_COLORS,
  STATUS_KEY_HINTS,
  STATUS_LABELS,
} from "@/lib/tasks-page-utils";

/**
 * タスク一覧の右クリックコンテキストメニュー（ポータル描画）。
 */
export function TaskListContextMenu({
  contextMenu,
  contextMenuRef,
  contextMenuStyle,
  contextSubmenuClassName,
  statusSubmenuOpen,
  setStatusSubmenuOpen,
  prioritySubmenuOpen,
  setPrioritySubmenuOpen,
  onStatusChange,
  onPriorityChange,
  onTimer,
  onDuplicate,
  onCopyTaskId,
  onDelete,
}: {
  contextMenu: { x: number; y: number; task: Task } | null;
  contextMenuRef: React.RefObject<HTMLDivElement | null>;
  contextMenuStyle: React.CSSProperties;
  contextSubmenuClassName: string;
  statusSubmenuOpen: boolean;
  setStatusSubmenuOpen: (open: boolean) => void;
  prioritySubmenuOpen: boolean;
  setPrioritySubmenuOpen: (open: boolean) => void;
  onStatusChange: (status: string) => void;
  onPriorityChange: (priority: string) => void;
  onTimer: () => void;
  onDuplicate: () => void;
  onCopyTaskId: () => void;
  onDelete: () => void;
}) {
  if (!contextMenu || typeof document === "undefined") return null;

  return createPortal(
    <MenuMnemonicSurface
      ref={contextMenuRef}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={contextMenuStyle}
      onContextMenu={(e) => e.preventDefault()}
    >
      {/* ステータス変更 */}
      <div
        className="relative"
        onMouseEnter={() => setStatusSubmenuOpen(true)}
        onMouseLeave={() => setStatusSubmenuOpen(false)}
      >
        <MenuMnemonicButton
          className="flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
          onClick={() => setStatusSubmenuOpen(true)}
        >
          <span className="flex items-center gap-2">
            <RefreshCw className="size-4" />
            ステータス変更
          </span>
          <ChevronRight className="size-4" />
        </MenuMnemonicButton>
        {statusSubmenuOpen && (
          <MenuMnemonicSurface className={contextSubmenuClassName}>
            {(
              ["open", "in_progress", "on_hold", "review", "closed"] as const
            ).map((status) => (
              <MenuMnemonicButton
                key={status}
                mnemonic={STATUS_KEY_HINTS[status]}
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default",
                  contextMenu.task.status === status && "font-bold",
                )}
                onClick={() => onStatusChange(status)}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={cn(
                      "inline-block size-2 rounded-full border-2",
                      STATUS_DOT_COLORS[status],
                    )}
                  />
                  {STATUS_LABELS[status]}
                </span>
              </MenuMnemonicButton>
            ))}
          </MenuMnemonicSurface>
        )}
      </div>

      {/* 優先度変更 */}
      <div
        className="relative"
        onMouseEnter={() => setPrioritySubmenuOpen(true)}
        onMouseLeave={() => setPrioritySubmenuOpen(false)}
      >
        <MenuMnemonicButton
          className="flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
          mnemonic="P"
          onClick={() => setPrioritySubmenuOpen(true)}
        >
          <span className="flex items-center gap-2">
            <Flag className="size-4" />
            優先度変更
          </span>
          <ChevronRight className="size-4" />
        </MenuMnemonicButton>
        {prioritySubmenuOpen && (
          <MenuMnemonicSurface className={contextSubmenuClassName}>
            {(["urgent", "high", "medium", "low", "none"] as const).map(
              (priority) => (
                <MenuMnemonicButton
                  key={priority}
                  mnemonic={
                    {
                      urgent: "U",
                      high: "H",
                      medium: "M",
                      low: "L",
                      none: "N",
                    }[priority]
                  }
                  className={cn(
                    "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default",
                    contextMenu.task.priority === priority && "font-bold",
                  )}
                  onClick={() => onPriorityChange(priority)}
                >
                  <span
                    className={cn(
                      "inline-block size-2 rounded-full",
                      PRIORITY_COLORS[priority].split(" ")[0],
                    )}
                  />
                  {PRIORITY_LABELS[priority]}
                </MenuMnemonicButton>
              ),
            )}
          </MenuMnemonicSurface>
        )}
      </div>

      {/* 区切り線 */}
      <div className="my-1 h-px bg-border" />

      {/* タイマー開始/停止 */}
      <MenuMnemonicButton
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        mnemonic="T"
        onClick={onTimer}
      >
        {contextMenu.task.active_time_entry ? (
          <>
            <Square className="size-4" />
            タイマー停止
          </>
        ) : (
          <>
            <Play className="size-4" />
            タイマー開始
          </>
        )}
      </MenuMnemonicButton>

      {/* 複製 */}
      <MenuMnemonicButton
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        mnemonic="C"
        onClick={onDuplicate}
      >
        <Copy className="size-4" />
        複製
      </MenuMnemonicButton>

      {/* タスクIDコピー */}
      <MenuMnemonicButton
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        mnemonic="I"
        onClick={onCopyTaskId}
      >
        <Hash className="size-4" />
        タスクIDをコピー
      </MenuMnemonicButton>

      {/* 区切り線 */}
      <div className="my-1 h-px bg-border" />

      {/* 削除 */}
      <MenuMnemonicButton
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-red-600 hover:bg-red-100 dark:text-red-400 dark:hover:bg-red-900/30 cursor-default"
        mnemonic="D"
        onClick={onDelete}
      >
        <Trash2 className="size-4" />
        削除
      </MenuMnemonicButton>
    </MenuMnemonicSurface>,
    document.body,
  );
}
