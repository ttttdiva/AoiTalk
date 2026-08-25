"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  ChevronDown,
  Loader2,
  Network,
  Plus,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useTaskDependencies } from "@/components/tasks/hooks/use-task-dependencies";
import { taskApi, type Task } from "@/lib/task-api";
import { cn } from "@/lib/utils";

type DependencyDirection = "prerequisite" | "blocking";

function isRemoteTask(task: Task): boolean {
  return (
    task.source === "remote" ||
    Boolean(task.remote_server_id) ||
    task.id.startsWith("remote:")
  );
}

function fallbackTaskLabel(taskId: string): string {
  return `タスク ${taskId.slice(0, 8)}`;
}

export function TaskDependencySection({
  task,
  readOnly = false,
  onOpenTask,
}: {
  task: Task;
  readOnly?: boolean;
  onOpenTask?: (taskId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [pickerDirection, setPickerDirection] =
    useState<DependencyDirection | null>(null);
  const [taskCandidates, setTaskCandidates] = useState<Task[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [candidatesError, setCandidatesError] = useState<string | null>(null);
  const [loadedCandidatesTaskId, setLoadedCandidatesTaskId] = useState<
    string | null
  >(null);
  const activeTaskIdRef = useRef(task.id);
  const remote = isRemoteTask(task);
  const {
    dependencies,
    hasLoadedData,
    isLoading,
    error,
    adding,
    deletingIds,
    addDependency,
    removeDependency,
    retry,
  } = useTaskDependencies({
    taskId: task.id,
    projectId: task.project_id,
    enabled: expanded && !remote,
  });
  activeTaskIdRef.current = task.id;

  const loadTaskCandidates = useCallback(
    async (force = false) => {
      if (
        remote ||
        candidatesLoading ||
        (!force && loadedCandidatesTaskId === task.id)
      ) {
        return;
      }
      const activeTaskId = task.id;
      setLoadedCandidatesTaskId(activeTaskId);
      setCandidatesLoading(true);
      setCandidatesError(null);
      try {
        const candidates = await taskApi.listTasks(task.project_id);
        if (activeTaskIdRef.current !== activeTaskId) return;
        setTaskCandidates(candidates);
      } catch (loadError) {
        if (activeTaskIdRef.current !== activeTaskId) return;
        setTaskCandidates([]);
        setCandidatesError(
          loadError instanceof Error
            ? loadError.message
            : "タスク候補の取得に失敗しました",
        );
      } finally {
        if (activeTaskIdRef.current === activeTaskId) {
          setCandidatesLoading(false);
        }
      }
    },
    [
      candidatesLoading,
      loadedCandidatesTaskId,
      remote,
      task.id,
      task.project_id,
    ],
  );

  const prerequisites = useMemo(
    () => dependencies.filter((item) => item.task_id === task.id),
    [dependencies, task.id],
  );
  const blocking = useMemo(
    () => dependencies.filter((item) => item.depends_on_task_id === task.id),
    [dependencies, task.id],
  );
  const relatedTaskIds = useMemo(
    () =>
      new Set(
        dependencies.flatMap((item) => [item.task_id, item.depends_on_task_id]),
      ),
    [dependencies],
  );
  const candidates = useMemo(
    () =>
      taskCandidates.filter(
        (candidate) =>
          candidate.id !== task.id &&
          candidate.project_id === task.project_id &&
          candidate.source !== "remote" &&
          !candidate.remote_server_id &&
          !relatedTaskIds.has(candidate.id),
      ),
    [relatedTaskIds, task.id, task.project_id, taskCandidates],
  );
  const taskById = useMemo(
    () => new Map([task, ...taskCandidates].map((item) => [item.id, item])),
    [task, taskCandidates],
  );
  const taskLabel = (taskId: string) =>
    taskById.get(taskId)?.title ?? fallbackTaskLabel(taskId);

  if (remote) return null;

  const errorAlert = error ? (
    <div role="alert" className="rounded-md bg-destructive/10 p-3">
      <p className="text-sm text-destructive">{error}</p>
      <Button
        type="button"
        variant="outline"
        size="xs"
        className="mt-2"
        onClick={() => void retry()}
      >
        再読み込み
      </Button>
    </div>
  ) : null;

  const addSelectedTask = async (
    candidate: Task,
    direction: DependencyDirection,
  ) => {
    const activeTaskId = task.id;
    const success = await addDependency(
      direction === "prerequisite"
        ? {
            task_id: activeTaskId,
            depends_on_task_id: candidate.id,
          }
        : {
            task_id: candidate.id,
            depends_on_task_id: activeTaskId,
          },
    );
    if (success && activeTaskIdRef.current === activeTaskId) {
      setPickerDirection(null);
    }
  };

  const renderPicker = (direction: DependencyDirection, label: string) => (
    <Popover
      open={pickerDirection === direction}
      onOpenChange={(open) => setPickerDirection(open ? direction : null)}
    >
      <PopoverTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="xs"
            disabled={adding || candidatesLoading}
            aria-label={label}
          >
            {adding && pickerDirection === direction ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Plus />
            )}
            {label}
          </Button>
        }
      />
      <PopoverContent className="w-80 p-0" align="start">
        <Command>
          <CommandInput placeholder="タスク名・プロジェクト名で検索" />
          <CommandList>
            {candidatesLoading ? (
              <div
                role="status"
                className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground"
              >
                <Loader2 className="size-4 animate-spin" />
                タスク候補を読み込み中
              </div>
            ) : candidatesError ? (
              <div className="space-y-2 p-3 text-sm text-destructive">
                <p>{candidatesError}</p>
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  onClick={() => void loadTaskCandidates(true)}
                >
                  再読み込み
                </Button>
              </div>
            ) : (
              <>
                <CommandEmpty>追加できるタスクがありません</CommandEmpty>
                <CommandGroup heading="同じプロジェクトのタスク">
                  {candidates.map((candidate) => (
                    <CommandItem
                      key={candidate.id}
                      value={`${candidate.title} ${candidate.project_name ?? ""} ${candidate.id}`}
                      disabled={adding}
                      onSelect={() =>
                        void addSelectedTask(candidate, direction)
                      }
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {candidate.title}
                      </span>
                      {candidate.project_name ? (
                        <span className="truncate text-xs text-muted-foreground">
                          {candidate.project_name}
                        </span>
                      ) : null}
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );

  const renderDependencyRow = (
    dependencyId: string,
    relatedTaskId: string,
    direction: DependencyDirection,
  ) => {
    const label = taskLabel(relatedTaskId);
    const deleting = deletingIds.has(dependencyId);
    return (
      <div
        key={dependencyId}
        className="flex min-w-0 items-center gap-2 rounded-md border bg-card px-2 py-1.5"
      >
        <button
          type="button"
          className="min-w-0 flex-1 truncate text-left text-sm hover:text-primary hover:underline"
          onClick={() => onOpenTask?.(relatedTaskId)}
        >
          {direction === "prerequisite" ? (
            <>
              {label}
              <ArrowRight className="mx-2 inline size-3.5 text-muted-foreground" />
              <span className="text-muted-foreground">このタスク</span>
            </>
          ) : (
            <>
              <span className="text-muted-foreground">このタスク</span>
              <ArrowRight className="mx-2 inline size-3.5 text-muted-foreground" />
              {label}
            </>
          )}
        </button>
        {!readOnly ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            disabled={deleting || adding}
            aria-label={`${label}との依存関係を削除`}
            onClick={() => void removeDependency(dependencyId)}
          >
            {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
          </Button>
        ) : null}
      </div>
    );
  };

  return (
    <section className="rounded-lg border" aria-label="依存関係">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium hover:bg-muted/50"
        aria-expanded={expanded}
        onClick={() => {
          const nextExpanded = !expanded;
          setExpanded(nextExpanded);
          if (nextExpanded) void loadTaskCandidates();
        }}
      >
        <Network className="size-4 text-muted-foreground" />
        <span className="flex-1">依存関係</span>
        {expanded && !isLoading ? (
          <span className="text-xs font-normal text-muted-foreground">
            {dependencies.length}件
          </span>
        ) : null}
        <ChevronDown
          className={cn(
            "size-4 text-muted-foreground transition-transform",
            expanded && "rotate-180",
          )}
        />
      </button>

      {expanded ? (
        <div className="space-y-4 border-t p-3">
          {isLoading ? (
            <div
              role="status"
              className="flex items-center gap-2 py-3 text-sm text-muted-foreground"
            >
              <Loader2 className="size-4 animate-spin" />
              依存関係を読み込み中
            </div>
          ) : error && !hasLoadedData ? (
            errorAlert
          ) : (
            <>
              {errorAlert}
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-xs font-semibold text-muted-foreground">
                    前提タスク
                  </h3>
                  {!readOnly
                    ? renderPicker("prerequisite", "前提タスクを追加")
                    : null}
                </div>
                {prerequisites.length > 0 ? (
                  <div className="space-y-1.5">
                    {prerequisites.map((dependency) =>
                      renderDependencyRow(
                        dependency.id,
                        dependency.depends_on_task_id,
                        "prerequisite",
                      ),
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    前提タスクはありません
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-xs font-semibold text-muted-foreground">
                    このタスクがブロックするタスク
                  </h3>
                  {!readOnly
                    ? renderPicker("blocking", "ブロック先を追加")
                    : null}
                </div>
                {blocking.length > 0 ? (
                  <div className="space-y-1.5">
                    {blocking.map((dependency) =>
                      renderDependencyRow(
                        dependency.id,
                        dependency.task_id,
                        "blocking",
                      ),
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    ブロックしているタスクはありません
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}
