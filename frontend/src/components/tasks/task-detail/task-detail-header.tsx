"use client";

import type { RefObject } from "react";

import {
  Send,
  Trash2,
  Plus,
  MoreHorizontal,
  CircleDot,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  SlashCommandInput,
  type CommandCandidateSelection,
} from "@/components/tasks/slash-command-input";
import {
  normalizeTaskTitle,
  taskValueCompletion,
  taskValuePreview,
} from "@/components/tasks/task-form-utils";
import type { TimeEntry, Task } from "@/lib/task-api";
import {
  formatDateTime,
  formatDuration,
  formatTimeRange,
} from "@/components/tasks/task-detail/task-detail-utils";

type SlashCandidate = ReturnType<
  typeof import("@/components/tasks/task-form-utils").buildTaskCommandCandidates
>;

export function TaskDetailHeader({
  task,
  effectiveTaskId,
  editTitle,
  editingTitle,
  setEditingTitle,
  titleInputRef,
  slashCandidates,
  entryFocus,
  allProjects,
  spaces,
  launchingAgent,
  triagingAgent,
  onTitleChange,
  onTitleBlur,
  onTitleSubmitIntent,
  onParseSlashCommands,
  focusDescriptionEditor,
  immediateUpdate,
  handleRunWithAgent,
  handleRunAgentTriage,
  handleDuplicate,
  handleDelete,
  handleDialogOpenChange,
}: {
  task: Task;
  effectiveTaskId: string | null;
  editTitle: string;
  editingTitle: boolean;
  setEditingTitle: (value: boolean) => void;
  titleInputRef: RefObject<HTMLInputElement | null>;
  slashCandidates: SlashCandidate;
  entryFocus?: TimeEntry | null;
  allProjects: { id: string; name: string; space_id?: string | null }[];
  spaces: { id: string; name: string }[];
  launchingAgent: boolean;
  triagingAgent: boolean;
  onTitleChange: (val: string) => void;
  onTitleBlur: () => void;
  onTitleSubmitIntent: () => void;
  onParseSlashCommands: (
    text: string,
    selection?: CommandCandidateSelection,
  ) => string;
  focusDescriptionEditor: () => void;
  immediateUpdate: (
    data: Record<string, unknown>,
  ) => Promise<Task | null> | void;
  handleRunWithAgent: () => void;
  handleRunAgentTriage: () => void;
  handleDuplicate: () => void;
  handleDelete: () => void;
  handleDialogOpenChange: (open: boolean) => void;
}) {
  return (
    <div className="pt-2">
      {editingTitle ? (
        <SlashCommandInput
          inputRef={titleInputRef}
          value={editTitle}
          getValuePreview={taskValuePreview}
          getValueCompletion={taskValueCompletion}
          commandCandidates={slashCandidates}
          onChange={onTitleChange}
          onBlur={onTitleBlur}
          onSubmitIntent={onTitleSubmitIntent}
          submitOnEnter={!effectiveTaskId}
          onParseSlashCommands={onParseSlashCommands}
          className="text-2xl md:text-2xl font-bold h-auto border-none shadow-none px-0 py-0 focus-visible:ring-0"
          autoFocus
          onNavigateDown={focusDescriptionEditor}
        />
      ) : (
        <h1
          className="text-2xl md:text-2xl font-bold cursor-pointer hover:text-primary/80 transition-colors"
          onClick={() => setEditingTitle(true)}
        >
          {editTitle || task.title}
        </h1>
      )}
      {entryFocus && (
        <div className="mt-3 rounded-xl border border-border/60 bg-muted/30 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                Time Entry
              </p>
              <p className="mt-1 truncate text-sm text-muted-foreground">
                {entryFocus.project_name || "プロジェクト未設定"}
              </p>
              <p className="truncate text-base font-semibold">
                {entryFocus.task_title || task.title}
              </p>
            </div>
            <div className="shrink-0 text-right">
              <p className="text-lg font-semibold tabular-nums">
                {formatDuration(entryFocus.duration_seconds || 0)}
              </p>
              <p className="text-xs text-muted-foreground tabular-nums">
                {formatTimeRange(entryFocus.started_at, entryFocus.ended_at)}
              </p>
            </div>
          </div>
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {/* プロジェクト表示 */}
        {task.project_id && allProjects.length > 1 && (
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-xs text-muted-foreground">📁</span>
            <Select
              value={task.project_id}
              onValueChange={(v) => v && immediateUpdate({ project_id: v })}
            >
              <SelectTrigger className="h-6 w-auto max-w-full border-none px-1 text-xs text-muted-foreground shadow-none hover:text-foreground">
                <span className="truncate">
                  {allProjects.find((p) => p.id === task.project_id)?.name ||
                    "不明"}
                </span>
              </SelectTrigger>
              <SelectContent>
                {spaces.map((s) => {
                  const group = allProjects.filter((p) => p.space_id === s.id);
                  if (group.length === 0) return null;
                  return (
                    <SelectGroup key={s.id}>
                      <SelectLabel>{s.name}</SelectLabel>
                      {group.map((p) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  );
                })}
                {allProjects.some((p) => !p.space_id) && (
                  <SelectGroup>
                    <SelectLabel>(スペースなし)</SelectLabel>
                    {allProjects
                      .filter((p) => !p.space_id)
                      .map((p) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.name}
                        </SelectItem>
                      ))}
                  </SelectGroup>
                )}
              </SelectContent>
            </Select>
          </div>
        )}

        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 gap-2"
            onClick={() => void handleRunWithAgent()}
            disabled={
              launchingAgent || !normalizeTaskTitle(editTitle || task.title)
            }
          >
            <Send className="size-3.5" />
            {launchingAgent ? "Starting..." : "Run with agent"}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger className="inline-flex size-8 items-center justify-center rounded-md border bg-background text-muted-foreground shadow-xs transition-colors hover:bg-accent hover:text-accent-foreground">
              <MoreHorizontal className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56 min-w-56">
              <DropdownMenuLabel>Task</DropdownMenuLabel>
              {effectiveTaskId ? (
                <>
                  <DropdownMenuItem
                    mnemonic="A"
                    disabled={triagingAgent}
                    onClick={() => void handleRunAgentTriage()}
                  >
                    <CircleDot className="size-4" />
                    {triagingAgent ? "Preparing..." : "Prepare for Agent"}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    作成: {formatDateTime(task.created_at)}
                  </div>
                  <div className="px-2 py-1.5 text-xs text-muted-foreground">
                    更新: {formatDateTime(task.updated_at)}
                  </div>
                  {task.completed_at && (
                    <div className="px-2 py-1.5 text-xs text-muted-foreground">
                      完了: {formatDateTime(task.completed_at)}
                    </div>
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    mnemonic="C"
                    onClick={() => void handleDuplicate()}
                  >
                    <Plus className="size-4" />
                    複製
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    mnemonic="D"
                    variant="destructive"
                    onClick={() => void handleDelete()}
                  >
                    <Trash2 className="size-4" />
                    削除
                  </DropdownMenuItem>
                </>
              ) : (
                <DropdownMenuItem
                  mnemonic="D"
                  variant="destructive"
                  onClick={() => handleDialogOpenChange(false)}
                >
                  <Trash2 className="size-4" />
                  下書きを破棄
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>
  );
}
