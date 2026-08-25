"use client";

import { useState } from "react";
import type React from "react";

import {
  ArrowRightLeft,
  Check,
  ChevronDown,
  Columns3,
  Copy,
  Filter as FilterIcon,
  HelpCircle,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
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
import type {
  Project,
  Tag,
} from "@/lib/task-api";
import { cn } from "@/lib/utils";
import type { FilterTab } from "@/lib/tasks-page-utils";
import type {
  TaskListColumn,
  TaskListColumnVisibility,
} from "@/components/tasks/hooks/use-task-view-preferences";

/**
 * Desktop Task List toolbar. The compact controls deliberately keep the
 * existing filter/bulk handlers; this component only changes their placement
 * and skin so keyboard, selection, and mutation behaviour remain untouched.
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
  readOnly = false,
  projects,
  filterProjects = projects,
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
  searchInputRef,
  search,
  setSearch,
  onCreateTask,
  createDisabled = false,
  columnVisibility,
  onColumnVisibilityChange,
  projectScopeAll = false,
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
  readOnly?: boolean;
  /** Writable projects used by Bulk Move. */
  projects: Project[];
  /** All readable projects used by the Advanced Filter picker. */
  filterProjects?: Project[];
  tags: Tag[];
  filter: FilterTab;
  setFilter: (filter: FilterTab) => void;
  showClosed: boolean;
  setShowClosed: (value: boolean) => void;
  showFuture: boolean;
  setShowFuture: (value: boolean) => void;
  /** Kept for mobile/legacy callers; desktop uses the Columns menu. */
  showPriority: boolean;
  setShowPriority: (value: boolean) => void;
  showOnlyMine: boolean;
  setShowOnlyMine: (value: boolean) => void;
  filterOpen: boolean;
  setFilterOpen: (open: boolean) => void;
  customFilter: FilterConfig;
  setCustomFilter: React.Dispatch<React.SetStateAction<FilterConfig>>;
  searchInputRef?: React.RefObject<HTMLInputElement | null>;
  search?: string;
  setSearch?: (value: string) => void;
  onCreateTask?: () => void;
  createDisabled?: boolean;
  columnVisibility?: TaskListColumnVisibility;
  onColumnVisibilityChange?: (
    column: TaskListColumn,
    visible: boolean,
  ) => void;
  projectScopeAll?: boolean;
}) {
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const columns: Array<{ id: TaskListColumn; label: string }> = [
    { id: "start", label: "Start Date" },
    { id: "due", label: "Due Date" },
    { id: "time", label: "Time Tracked" },
    { id: "project", label: "Project" },
    { id: "priority", label: "Priority" },
    { id: "assignee", label: "Assignee" },
  ];
  const visibleColumnCount = columnVisibility
    ? columns.filter((column) => columnVisibility[column.id]).length
    : 3;
  const filterCount =
    customFilter.rules.length +
    (filter === "overdue" ? 1 : 0) +
    (showClosed ? 1 : 0) +
    (showFuture ? 1 : 0) +
    (showOnlyMine ? 1 : 0);

  return (
    <>
    <div
      className={cn(
        "ao-task-list-toolbar hidden min-h-16 shrink-0 flex-wrap items-center gap-2 border-b border-border bg-card dark:bg-background/95 px-6 py-3 backdrop-blur md:flex",
        selectedIds.size > 0 &&
          !readOnly &&
          "md:absolute md:bottom-8 md:left-1/2 md:z-30 md:min-h-0 md:-translate-x-1/2 md:rounded-lg md:border md:border-border md:bg-card md:px-4 md:py-2 md:shadow-[0_8px_32px_rgba(0,0,0,0.35)]",
      )}
      data-testid="task-list-toolbar"
    >
      {selectedIds.size > 0 && !readOnly ? (
        <>
          <span className="inline-flex items-center gap-2 text-[13px] leading-5 font-medium text-foreground">
            <span className="grid size-6 place-items-center rounded bg-primary/20 font-mono text-[11px] font-bold text-primary">
              {selectedIds.size}
            </span>
            <span className="hidden sm:inline">tasks selected</span>
            <span className="sm:hidden">選択中</span>
          </span>
          <span className="hidden h-5 w-px bg-border md:block" />
          <div className="flex items-center gap-1">
            <DropdownMenu
              open={bulkStatusMenuOpen}
              onOpenChange={setBulkStatusMenuOpen}
            >
              <DropdownMenuTrigger className="inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-[13px] leading-5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                <Check className="size-3.5" />
                <span>Status</span>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-36">
                <TaskStatusMenuItems
                  onSelect={(status) => {
                    setBulkStatusMenuOpen(false);
                    onBulkStatusChange(status);
                  }}
                />
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-[13px] leading-5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              disabled={bulkLoading}
              onClick={onBulkDuplicate}
            >
              <Copy className="size-3.5" />
              <span>Duplicate</span>
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger className="inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-[13px] leading-5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                <ArrowRightLeft className="size-3.5" />
                <span>Move</span>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-36">
                {projects.map((project) => (
                  <DropdownMenuItem
                    key={project.id}
                    className="cursor-pointer"
                    onClick={() => onBulkMove(project.id)}
                  >
                    {project.name}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex h-8 items-center gap-1.5 rounded px-2.5 text-[13px] leading-5 text-destructive transition-colors hover:bg-destructive/10 disabled:opacity-50"
              disabled={bulkLoading}
              onClick={onBulkDelete}
            >
              <Trash2 className="size-3.5" />
              <span>Delete</span>
            </button>
          </div>
          <button
            className="ml-auto grid size-7 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            onClick={clearSelection}
            aria-label="選択を解除"
          >
            <X className="size-3.5" />
          </button>
        </>
      ) : (
        <>
          <div className="relative hidden min-w-48 flex-1 md:block md:max-w-xs">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <input
              ref={searchInputRef}
              value={search ?? ""}
              onChange={(event) => setSearch?.(event.target.value)}
              placeholder="Search tasks..."
              aria-label="タスク・タグ・プロジェクトを検索"
              className="ao-task-toolbar-control ao-task-search-input h-9 w-full rounded border border-border bg-card/40 pl-9 pr-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus:border-primary focus:ring-1 focus:ring-primary/30"
            />
          </div>
          <span className="hidden h-5 w-px bg-border md:block" />

          <Popover open={filterOpen} onOpenChange={setFilterOpen}>
            <PopoverTrigger
              className={cn(
                "ao-task-toolbar-control ao-task-filter-trigger inline-flex h-9 items-center gap-1.5 rounded px-2.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                filterCount > 0 && "text-primary",
              )}
              title="フィルタ"
            >
              <FilterIcon className="size-4" />
              <span>Filter</span>
              {filterCount > 0 && (
                <span className="grid size-4 place-items-center rounded-full bg-primary/20 text-[10px] text-primary">
                  {filterCount}
                </span>
              )}
              <ChevronDown className="size-3" />
            </PopoverTrigger>
            <PopoverContent
              align="start"
              className="w-[min(42rem,calc(100vw-1rem))] max-w-[calc(100vw-1rem)] p-4"
            >
              <h3 className="mb-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                Quick Filters
              </h3>
              <div className="grid gap-2">
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Checkbox
                    checked={filter === "overdue"}
                    onCheckedChange={(checked) =>
                      setFilter(checked ? "overdue" : "all")
                    }
                  />
                  期限超過のみ
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Checkbox
                    checked={showClosed}
                    onCheckedChange={(checked) => setShowClosed(!!checked)}
                  />
                  完了済みを表示
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Checkbox
                    checked={showFuture}
                    onCheckedChange={(checked) => setShowFuture(!!checked)}
                  />
                  未来のタスクを表示
                </label>
                <label className="flex cursor-pointer items-center gap-2 text-sm">
                  <Checkbox
                    checked={showOnlyMine}
                    onCheckedChange={(checked) => setShowOnlyMine(!!checked)}
                  />
                  自分が担当のタスクのみ
                </label>
              </div>
              <div className="mt-4 border-t border-border pt-3">
                <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                  Advanced
                </p>
                <TaskFilterBuilder
                  config={customFilter}
                  onChange={setCustomFilter}
                  tags={tags}
                  projects={filterProjects}
                  onClose={() => setFilterOpen(false)}
                />
              </div>
            </PopoverContent>
          </Popover>

          <Popover>
            <PopoverTrigger
              className={cn(
                "ao-task-toolbar-control ao-task-columns-trigger inline-flex h-9 items-center gap-1.5 rounded px-2.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                visibleColumnCount !== 3 && "text-primary",
              )}
              title="表示列"
            >
              <Columns3 className="size-4" />
              <span className="hidden sm:inline">Columns</span>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-52 p-3">
              <h3 className="mb-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                Visible Columns
              </h3>
              <label className="mb-2 flex items-center gap-2 text-sm text-muted-foreground">
                <Checkbox checked disabled />
                Task Name
              </label>
              <div className="grid gap-2">
                {columns.map((column) => {
                  const checked =
                    (column.id === "project" && projectScopeAll) ||
                    (columnVisibility?.[column.id] ??
                      ["start", "due", "time"].includes(column.id));
                  const requiredInAllScope =
                    column.id === "project" && projectScopeAll;
                  return (
                    <label
                      key={column.id}
                      className="flex cursor-pointer items-center gap-2 text-sm"
                    >
                      <Checkbox
                        checked={checked}
                        disabled={requiredInAllScope}
                        onCheckedChange={(value) =>
                          onColumnVisibilityChange?.(column.id, value === true)
                        }
                      />
                      {column.label}
                    </label>
                  );
                })}
              </div>
            </PopoverContent>
          </Popover>

          <DropdownMenu>
            <DropdownMenuTrigger
              className="ao-task-toolbar-control ao-task-help-trigger inline-flex h-9 items-center justify-center rounded px-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              title="その他"
            >
              <HelpCircle className="size-4" />
              <span className="sr-only">Keyboard shortcuts</span>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56 min-w-56">
              <DropdownMenuLabel>Keyboard shortcuts</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <div className="px-2 py-1.5 text-xs text-muted-foreground">↑↓: 移動</div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">Enter: 開く</div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">Ctrl+Space: 選択</div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">/: フォーカス行コマンド</div>
              <div className="px-2 py-1.5 text-xs text-muted-foreground">Alt+S: タイマー開始</div>
            </DropdownMenuContent>
          </DropdownMenu>

          {onCreateTask && (
            <Button
              type="button"
              onClick={onCreateTask}
              disabled={createDisabled}
              className="ml-auto h-9 gap-1.5 rounded bg-primary px-3 text-sm font-medium text-primary-foreground shadow-none hover:bg-primary/90"
            >
              <Plus className="size-4" />
              <span>New Task</span>
            </Button>
          )}
        </>
      )}
    </div>
    <div className="ao-task-list-toolbar ao-task-list-toolbar-mobile flex min-h-12 shrink-0 flex-wrap items-center gap-2 border-b border-border bg-card dark:bg-background/95 px-3 py-2 md:hidden">
      {selectedIds.size > 0 && !readOnly ? (
        <>
          <span className="text-xs font-medium text-primary">{selectedIds.size}件選択</span>
          <div className="flex items-center gap-1">
            <DropdownMenu
              open={bulkStatusMenuOpen}
              onOpenChange={setBulkStatusMenuOpen}
            >
              <DropdownMenuTrigger className="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                ステータス変更
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-36">
                <TaskStatusMenuItems
                  onSelect={(status) => {
                    setBulkStatusMenuOpen(false);
                    onBulkStatusChange(status);
                  }}
                />
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              disabled={bulkLoading}
              onClick={onBulkDuplicate}
            >
              <Copy className="size-3" />
              コピー
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger className="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
                <ArrowRightLeft className="size-3" />
                移動
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-36">
                {projects.map((project) => (
                  <DropdownMenuItem
                    key={project.id}
                    className="cursor-pointer"
                    onClick={() => onBulkMove(project.id)}
                  >
                    {project.name}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="inline-flex h-7 items-center gap-1 rounded border border-destructive/40 px-2 text-xs text-destructive transition-colors hover:bg-destructive/10"
              disabled={bulkLoading}
              onClick={onBulkDelete}
            >
              <Trash2 className="size-3" />
              削除
            </button>
          </div>
          <button
            className="ml-auto rounded p-1 transition-colors hover:bg-muted"
            onClick={clearSelection}
            aria-label="選択を解除"
          >
            <X className="size-3.5" />
          </button>
        </>
      ) : (
        <>
          <Tabs value={filter} onValueChange={(value) => setFilter(value as FilterTab)}>
            <TabsList className="h-7 gap-0 rounded-none border-0 bg-transparent p-0">
              <TabsTrigger
                value="all"
                className="h-7 rounded-none border-b-2 border-transparent px-2 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground"
              >
                すべて
              </TabsTrigger>
              <TabsTrigger
                value="overdue"
                className="h-7 rounded-none border-b-2 border-transparent px-2 text-xs data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground"
              >
                期限超過
              </TabsTrigger>
            </TabsList>
          </Tabs>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
            <Checkbox checked={showClosed} onCheckedChange={(value) => setShowClosed(!!value)} />
            完了済みを表示
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
            <Checkbox checked={showFuture} onCheckedChange={(value) => setShowFuture(!!value)} />
            未来のタスクを表示
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
            <Checkbox checked={showPriority} onCheckedChange={(value) => setShowPriority(!!value)} />
            優先度を表示
          </label>
          <label className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
            <Checkbox checked={showOnlyMine} onCheckedChange={(value) => setShowOnlyMine(!!value)} />
            自分が担当のタスクのみ
          </label>
          <Popover open={mobileFilterOpen} onOpenChange={setMobileFilterOpen}>
            <PopoverTrigger
              className={cn(
                "ao-task-toolbar-control ml-auto inline-flex h-7 items-center gap-1 rounded border border-border bg-transparent px-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                customFilter.rules.length > 0 && "border-primary text-primary",
              )}
              title="カスタムフィルタ"
            >
              <FilterIcon className="size-3" />
              <span>フィルタ{customFilter.rules.length ? ` ${customFilter.rules.length}` : ""}</span>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              className="w-[min(42rem,calc(100vw-1rem))] max-w-[calc(100vw-1rem)] p-2"
            >
              <TaskFilterBuilder
                config={customFilter}
                onChange={setCustomFilter}
                tags={tags}
                projects={filterProjects}
                onClose={() => setMobileFilterOpen(false)}
              />
            </PopoverContent>
          </Popover>
        </>
      )}
    </div>
    </>
  );
}
