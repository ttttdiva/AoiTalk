"use client";

import type React from "react";

import {
  ArrowRightLeft,
  Copy,
  Filter as FilterIcon,
  HelpCircle,
  MoreHorizontal,
  Trash2,
  X,
} from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  TaskFilterBuilder,
  type FilterConfig,
} from "@/components/tasks/task-filter-builder";
import { TaskStatusMenuItems } from "@/components/tasks/task-status-menu-items";
import type { Project, Tag } from "@/lib/task-api";
import { cn } from "@/lib/utils";
import {
  handleStatusShortcutCapture,
  type FilterTab,
} from "@/lib/tasks-page-utils";

/**
 * タスク一覧のフィルタ・トグル群と、選択中の一括操作バー。
 */
export function TaskListToolbar({
  selectedIds,
  bulkLoading,
  bulkStatusMenuOpen,
  setBulkStatusMenuOpen,
  onBulkStatusChange,
  onBulkDuplicate,
  onBulkMove,
  onBulkDelete,
  clearSelection,
  projects,
  tags,
  filter,
  setFilter,
  showClosed,
  setShowClosed,
  showFuture,
  setShowFuture,
  showPriority,
  setShowPriority,
  showOnlyMine,
  setShowOnlyMine,
  filterOpen,
  setFilterOpen,
  customFilter,
  setCustomFilter,
}: {
  selectedIds: Set<string>;
  bulkLoading: boolean;
  bulkStatusMenuOpen: boolean;
  setBulkStatusMenuOpen: (open: boolean) => void;
  onBulkStatusChange: (status: string) => void;
  onBulkDuplicate: () => void;
  onBulkMove: (projectId: string) => void;
  onBulkDelete: () => void;
  clearSelection: () => void;
  projects: Project[];
  tags: Tag[];
  filter: FilterTab;
  setFilter: (filter: FilterTab) => void;
  showClosed: boolean;
  setShowClosed: (value: boolean) => void;
  showFuture: boolean;
  setShowFuture: (value: boolean) => void;
  showPriority: boolean;
  setShowPriority: (value: boolean) => void;
  showOnlyMine: boolean;
  setShowOnlyMine: (value: boolean) => void;
  filterOpen: boolean;
  setFilterOpen: (open: boolean) => void;
  customFilter: FilterConfig;
  setCustomFilter: React.Dispatch<React.SetStateAction<FilterConfig>>;
}) {
  return (
    <div className="flex items-center gap-4 flex-wrap">
      {selectedIds.size > 0 ? (
        <>
          <span className="text-sm font-medium text-primary">
            {selectedIds.size}件選択
          </span>
          <div className="flex items-center gap-1">
            <DropdownMenu
              open={bulkStatusMenuOpen}
              onOpenChange={setBulkStatusMenuOpen}
            >
              <DropdownMenuTrigger className="inline-flex items-center gap-1 h-7 px-2 text-xs rounded-md hover:bg-muted transition-colors cursor-pointer">
                ステータス変更
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="start"
                className="min-w-36"
                onKeyDownCapture={(e) =>
                  handleStatusShortcutCapture(e, (target) => {
                    setBulkStatusMenuOpen(false);
                    onBulkStatusChange(target);
                  })
                }
              >
                <TaskStatusMenuItems
                  onSelect={(status) => onBulkStatusChange(status)}
                />
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex items-center gap-1 h-7 px-2 text-xs rounded-md hover:bg-muted transition-colors cursor-pointer"
              disabled={bulkLoading}
              onClick={onBulkDuplicate}
            >
              <Copy className="size-3" />
              コピー
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger className="inline-flex items-center gap-1 h-7 px-2 text-xs rounded-md hover:bg-muted transition-colors cursor-pointer">
                <ArrowRightLeft className="size-3" />
                移動
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-36">
                {projects.map((p) => (
                  <DropdownMenuItem
                    key={p.id}
                    className="cursor-pointer"
                    onClick={() => onBulkMove(p.id)}
                  >
                    {p.name}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex items-center gap-1 h-7 px-2 text-xs rounded-md text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors cursor-pointer"
              disabled={bulkLoading}
              onClick={onBulkDelete}
            >
              <Trash2 className="size-3" />
              削除
            </button>
          </div>
          <button
            className="ml-auto p-1 rounded hover:bg-muted transition-colors"
            onClick={clearSelection}
          >
            <X className="size-3.5" />
          </button>
        </>
      ) : (
        <>
          <Tabs value={filter} onValueChange={(v) => setFilter(v as FilterTab)}>
            <TabsList>
              <TabsTrigger value="all">すべて</TabsTrigger>
              <TabsTrigger value="overdue">期限超過</TabsTrigger>
            </TabsList>
          </Tabs>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
            <Checkbox
              checked={showClosed}
              onCheckedChange={(v) => setShowClosed(!!v)}
            />
            完了済みを表示
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
            <Checkbox
              checked={showFuture}
              onCheckedChange={(v) => setShowFuture(!!v)}
            />
            未来のタスクを表示
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
            <Checkbox
              checked={showPriority}
              onCheckedChange={(v) => setShowPriority(!!v)}
            />
            優先度を表示
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
            <Checkbox
              checked={showOnlyMine}
              onCheckedChange={(v) => setShowOnlyMine(!!v)}
            />
            自分が担当のタスクのみ
          </label>

          {/* ClickUp 風カスタムフィルタ（三点リーダー） */}
          <DropdownMenu>
            <DropdownMenuTrigger className="inline-flex h-7 items-center gap-1 rounded-md border bg-background px-2 text-xs shadow-xs transition-colors hover:bg-accent hover:text-accent-foreground">
              <HelpCircle className="size-3.5" />
              Help
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56 min-w-56">
              <DropdownMenuLabel>Keyboard shortcuts</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Ctrl+J: 先頭
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                ↑↓: 移動
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Enter: 開く
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Ctrl+Space: 選択
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Ctrl+F: 検索
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Ctrl+Shift+;: プロジェクト切替
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                /: フォーカス行コマンド
              </div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                Alt+S: フォーカス行のタイマー開始
              </div>
            </DropdownMenuContent>
          </DropdownMenu>

          <Popover open={filterOpen} onOpenChange={setFilterOpen}>
            <PopoverTrigger
              className={cn(
                "ml-auto inline-flex h-7 items-center gap-1 rounded-md border bg-background px-2 text-xs shadow-xs transition-colors hover:bg-accent hover:text-accent-foreground",
                customFilter.rules.length > 0 &&
                  "border-primary text-primary hover:text-primary",
              )}
              title="カスタムフィルタ"
            >
              {customFilter.rules.length > 0 ? (
                <>
                  <FilterIcon className="size-3" />
                  <span>フィルタ {customFilter.rules.length}</span>
                </>
              ) : (
                <MoreHorizontal className="size-3.5" />
              )}
            </PopoverTrigger>
            <PopoverContent align="end" className="w-auto p-2">
              <TaskFilterBuilder
                config={customFilter}
                onChange={setCustomFilter}
                tags={tags}
                projects={projects}
                onClose={() => setFilterOpen(false)}
              />
            </PopoverContent>
          </Popover>
        </>
      )}
    </div>
  );
}
