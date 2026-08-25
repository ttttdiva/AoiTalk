"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  Bot,
  CalendarDays,
  CheckCircle2,
  Clock3,
  Loader2,
  RefreshCw,
  Send,
  Timer,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { NumberTicker } from "@/components/magicui/number-ticker";
import {
  buildTaskChatDraft,
  buildTaskChatSessionTitle,
} from "@/lib/task-agent";
import { taskApi, type Task, type TaskOccurrence } from "@/lib/task-api";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import {
  getTaskDisplayAllDay,
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import {
  isClosedTaskStatus,
  TASK_STATUS_DOT_COLORS,
  TASK_STATUS_LABELS,
} from "@/lib/task-status";
import { chatApi } from "@/lib/chat-api";
import { cn } from "@/lib/utils";

type TodayEntryKind = "scheduled" | "active" | "overdue" | "unscheduled";

type TodayEntry = {
  key: string;
  task: Task;
  occurrence?: TaskOccurrence;
  kind: TodayEntryKind;
  title: string;
  projectName: string | null;
  status: string;
  priority: string;
  startAt: string | null;
  endAt: string | null;
  allDay: boolean;
  reason: string;
  score: number;
};

type HomeData = {
  tasks: Task[];
  occurrences: TaskOccurrence[];
};

const PRIORITY_LABELS: Record<string, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
};

const PRIORITY_SCORES: Record<string, number> = {
  urgent: 40,
  high: 28,
  medium: 14,
  low: 4,
};

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

function formatLocalDateTime(value: Date): string {
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(
    value.getDate(),
  )}T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(
    value.getSeconds(),
  )}`;
}

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const dateOnly = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnly) {
    return new Date(
      Number(dateOnly[1]),
      Number(dateOnly[2]) - 1,
      Number(dateOnly[3]),
    );
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function startOfToday(): Date {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function endOfToday(): Date {
  const end = startOfToday();
  end.setDate(end.getDate() + 1);
  end.setMilliseconds(-1);
  return end;
}

function isSameLocalDay(value: string | null | undefined): boolean {
  const date = parseDate(value);
  if (!date) return false;
  const today = startOfToday();
  return (
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  );
}

function overlapsToday(
  startValue: string | null | undefined,
  endValue: string | null | undefined,
): boolean {
  const start = parseDate(startValue);
  const end = parseDate(endValue) ?? start;
  if (!start || !end) return false;
  return start <= endOfToday() && end >= startOfToday();
}

function isOverdueTask(task: Task): boolean {
  if (isClosedTaskStatus(task.status)) return false;
  const endAt = getTaskDisplayEndAt(task);
  const due = parseDate(endAt);
  if (!due) return false;
  if (
    getTaskDisplayAllDay(task) ||
    (due.getHours() === 0 && due.getMinutes() === 0)
  ) {
    return due < startOfToday();
  }
  return due < new Date();
}

function priorityScore(priority: string): number {
  return PRIORITY_SCORES[priority] ?? 8;
}

function taskScore(task: Task, kind: TodayEntryKind): number {
  let score = priorityScore(task.priority);
  if (kind === "overdue") score += 70;
  if (kind === "active") score += 58;
  if (kind === "scheduled") score += 34;
  if (task.active_time_entry) score += 12;
  if (task.status === "review") score += 10;
  const triageStatus = task.metadata?.agent_triage_status;
  if (triageStatus === "ready" || triageStatus === "needs_user") score += 8;
  return score;
}

function getTaskKind(task: Task): TodayEntryKind | null {
  if (task.active_time_entry || task.status === "in_progress") return "active";
  if (isOverdueTask(task)) return "overdue";
  const startAt = getTaskDisplayStartAt(task);
  const endAt = getTaskDisplayEndAt(task);
  if (
    overlapsToday(startAt, endAt) ||
    isSameLocalDay(startAt) ||
    isSameLocalDay(endAt)
  ) {
    return "scheduled";
  }
  if (task.priority === "urgent" || task.priority === "high")
    return "unscheduled";
  return null;
}

function reasonForEntry(kind: TodayEntryKind): string {
  switch (kind) {
    case "active":
      return "進行中";
    case "overdue":
      return "期限超過";
    case "scheduled":
      return "今日";
    case "unscheduled":
      return "高優先度";
  }
}

function taskWithOccurrence(task: Task, occurrence: TaskOccurrence): Task {
  return {
    ...task,
    title: occurrence.title || task.title,
    project_name: occurrence.project_name ?? task.project_name,
    project_color: occurrence.project_color ?? task.project_color,
    status: occurrence.status || task.status,
    all_day: occurrence.all_day,
    effective_start_at: occurrence.start_at ?? null,
    effective_end_at: occurrence.end_at ?? null,
    effective_all_day: occurrence.all_day,
    effective_occurrence_id: occurrence.id,
    effective_occurrence_start_at: occurrence.start_at ?? null,
    effective_occurrence_end_at: occurrence.end_at ?? null,
    effective_occurrence_original_start_at:
      occurrence.original_start_at ?? occurrence.start_at ?? null,
    effective_occurrence_source_kind: occurrence.source_kind,
    effective_occurrence_status: occurrence.status,
    tags: occurrence.tags ?? task.tags,
  };
}

function shouldPrepareTaskForAgent(
  metadata: Record<string, unknown> | null | undefined,
): boolean {
  const status =
    metadata && typeof metadata.agent_triage_status === "string"
      ? metadata.agent_triage_status
      : "pending";
  return status === "pending" || status === "failed";
}

function buildEntries(data: HomeData): TodayEntry[] {
  const taskById = new Map(data.tasks.map((task) => [task.id, task]));
  const occurrenceTaskIds = new Set<string>();
  const entries: TodayEntry[] = [];

  for (const occurrence of data.occurrences) {
    if (isClosedTaskStatus(occurrence.status)) continue;
    const baseTask = taskById.get(occurrence.task_id);
    if (!baseTask || baseTask.parent_task_id) continue;
    occurrenceTaskIds.add(baseTask.id);
    const task = taskWithOccurrence(baseTask, occurrence);
    const kind =
      task.active_time_entry || task.status === "in_progress"
        ? "active"
        : "scheduled";
    entries.push({
      key: `occurrence:${occurrence.id}`,
      task,
      occurrence,
      kind,
      title: occurrence.title || baseTask.title,
      projectName: occurrence.project_name ?? baseTask.project_name ?? null,
      status: occurrence.status,
      priority: baseTask.priority,
      startAt: occurrence.start_at ?? null,
      endAt: occurrence.end_at ?? null,
      allDay: occurrence.all_day,
      reason: reasonForEntry(kind),
      score: taskScore(task, kind),
    });
  }

  for (const task of data.tasks) {
    if (task.parent_task_id || isClosedTaskStatus(task.status)) continue;
    if (occurrenceTaskIds.has(task.id)) continue;
    const kind = getTaskKind(task);
    if (!kind) continue;
    const startAt = getTaskDisplayStartAt(task) ?? null;
    const endAt = getTaskDisplayEndAt(task) ?? null;
    entries.push({
      key: `task:${task.id}`,
      task,
      kind,
      title: task.title,
      projectName: task.project_name ?? null,
      status: task.status,
      priority: task.priority,
      startAt,
      endAt,
      allDay: getTaskDisplayAllDay(task),
      reason: reasonForEntry(kind),
      score: taskScore(task, kind),
    });
  }

  return entries.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    const aTime =
      parseDate(a.startAt ?? a.endAt)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    const bTime =
      parseDate(b.startAt ?? b.endAt)?.getTime() ?? Number.MAX_SAFE_INTEGER;
    return aTime - bTime;
  });
}

function formatEntryTime(entry: TodayEntry): string {
  if (entry.startAt) {
    return formatTaskDateLabel(entry.startAt, {
      allDay: entry.allDay,
      absoluteStyle: "short",
    });
  }
  if (entry.endAt) {
    return `Due ${formatTaskDateLabel(entry.endAt, {
      allDay: entry.allDay,
      absoluteStyle: "short",
    })}`;
  }
  return "";
}

function statusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status;
}

function SummaryMetric({
  icon,
  label,
  value,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: number;
  tone?: string;
}) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2">
      <div className="mb-1 flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className={tone}>{icon}</span>
        <span>{label}</span>
      </div>
      <div className={cn("text-xl font-semibold tabular-nums", tone)}>
        <NumberTicker
          value={value}
          decimalPlaces={0}
          className="tabular-nums text-inherit tracking-normal"
        />
      </div>
    </div>
  );
}

function TaskEntryRow({
  entry,
  launching,
  onOpenTask,
  onStartAgent,
}: {
  entry: TodayEntry;
  launching: boolean;
  onOpenTask: (entry: TodayEntry) => void;
  onStartAgent: (entry: TodayEntry) => void;
}) {
  const timeLabel = formatEntryTime(entry);
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-md border bg-background/70 px-3 py-2.5">
      <span
        className={cn(
          "mt-1.5 size-2.5 shrink-0 rounded-full",
          TASK_STATUS_DOT_COLORS[entry.status] ?? "bg-muted-foreground",
        )}
      />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={() => onOpenTask(entry)}
            className="min-w-0 truncate text-left text-sm font-medium hover:underline"
          >
            {entry.title || "(無題)"}
          </button>
          <Badge
            variant={entry.kind === "overdue" ? "destructive" : "secondary"}
          >
            {entry.reason}
          </Badge>
          {entry.priority && entry.priority !== "medium" ? (
            <Badge variant="outline">
              {PRIORITY_LABELS[entry.priority] ?? entry.priority}
            </Badge>
          ) : null}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {entry.projectName ? <span>{entry.projectName}</span> : null}
          <span>{statusLabel(entry.status)}</span>
          {timeLabel ? <span>{timeLabel}</span> : null}
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-7 gap-1.5"
        disabled={launching}
        onClick={() => onStartAgent(entry)}
      >
        {launching ? (
          <Loader2 className="size-3.5 animate-spin" />
        ) : (
          <Send className="size-3.5" />
        )}
        確認
      </Button>
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  return (
    <div className="rounded-md border border-dashed py-6 text-center text-sm text-muted-foreground">
      {label}
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-20 rounded-md" />
        ))}
      </div>
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-16 rounded-md" />
        ))}
      </div>
    </div>
  );
}

export function HomeTodayOverlay() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<HomeData>({ tasks: [], occurrences: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [launchingKey, setLaunchingKey] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const start = startOfToday();
      const end = endOfToday();
      const [tasks, occurrences] = await Promise.all([
        taskApi.listTasks(),
        taskApi
          .listOccurrences(
            {},
            formatLocalDateTime(start),
            formatLocalDateTime(end),
          )
          .catch((err) => {
            console.error("Home occurrence fetch failed", err);
            return [] as TaskOccurrence[];
          }),
      ]);
      setData({ tasks, occurrences });
    } catch (err) {
      console.error("Home data fetch failed", err);
      setError(err instanceof Error ? err.message : "Home data fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const handler = () => setOpen(true);
    window.addEventListener("global-open-home", handler);
    return () => window.removeEventListener("global-open-home", handler);
  }, []);

  useEffect(() => {
    if (open) void fetchData();
  }, [fetchData, open]);

  const entries = useMemo(() => buildEntries(data), [data]);
  const todayEntries = entries.filter((entry) => entry.kind !== "unscheduled");
  const agentEntries = entries.slice(0, 6);
  const openTaskCount = data.tasks.filter(
    (task) => !task.parent_task_id && !isClosedTaskStatus(task.status),
  ).length;
  const activeCount = entries.filter((entry) => entry.kind === "active").length;
  const overdueCount = entries.filter(
    (entry) => entry.kind === "overdue",
  ).length;
  const readyCount = data.tasks.filter((task) => {
    const status = task.metadata?.agent_triage_status;
    return status === "ready" || status === "needs_user";
  }).length;

  const openTask = useCallback(
    (entry: TodayEntry) => {
      setOpen(false);
      const detailUrl = `/tasks?detail=${encodeURIComponent(entry.task.id)}`;
      router.push(detailUrl);
      window.dispatchEvent(
        new CustomEvent("task-detail-open", {
          detail: { taskId: entry.task.id },
        }),
      );
    },
    [router],
  );

  const startAgent = useCallback(
    async (entry: TodayEntry) => {
      let launchTask = entry.task;
      setLaunchingKey(entry.key);
      try {
        if (shouldPrepareTaskForAgent(launchTask.metadata)) {
          const result = await taskApi.runAgentTriage(launchTask.id);
          launchTask = {
            ...launchTask,
            metadata: {
              ...(launchTask.metadata || {}),
              ...result.metadata,
            },
          };
        }
        const created = await chatApi.createSession(
          await chatApi.getCurrentCharacterName(),
          launchTask.project_id || undefined,
        );
        const sessionId = created.session.id;
        await chatApi.updateSessionTitle(
          sessionId,
          buildTaskChatSessionTitle(launchTask.title),
        );
        await chatApi.dispatchMessage(sessionId, {
          message: buildTaskChatDraft(launchTask),
          project_id: launchTask.project_id || undefined,
          generation_profile: "assisted_work",
        });
        setOpen(false);
        router.push(`/chat?s=${sessionId}`);
      } catch (err) {
        console.error("Failed to start task agent", err);
        toast.error("エージェント確認の開始に失敗しました");
      } finally {
        setLaunchingKey(null);
      }
    },
    [router],
  );

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent
        side="right"
        className="!w-[min(94vw,56rem)] !max-w-none gap-0 overflow-hidden p-0 sm:!max-w-none"
      >
        <SheetHeader className="border-b px-5 py-4">
          <div className="flex items-center justify-between gap-3 pr-9">
            <div className="min-w-0">
              <SheetTitle className="flex items-center gap-2">
                <CalendarDays className="size-4 text-primary" />
                Today
              </SheetTitle>
              <p className="mt-1 text-xs text-muted-foreground">
                {new Date().toLocaleDateString("ja-JP", {
                  year: "numeric",
                  month: "long",
                  day: "numeric",
                  weekday: "short",
                })}
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => void fetchData()}
              disabled={loading}
            >
              {loading ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <RefreshCw className="size-3.5" />
              )}
              更新
            </Button>
          </div>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading && data.tasks.length === 0 ? (
            <LoadingState />
          ) : error ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          ) : (
            <div className="space-y-5">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <SummaryMetric
                  icon={<CheckCircle2 className="size-3.5" />}
                  label="未完了"
                  value={openTaskCount}
                />
                <SummaryMetric
                  icon={<CalendarDays className="size-3.5" />}
                  label="今日"
                  value={todayEntries.length}
                  tone="text-sky-600 dark:text-sky-300"
                />
                <SummaryMetric
                  icon={<AlertTriangle className="size-3.5" />}
                  label="期限超過"
                  value={overdueCount}
                  tone={overdueCount > 0 ? "text-destructive" : undefined}
                />
                <SummaryMetric
                  icon={<Bot className="size-3.5" />}
                  label="準備済み"
                  value={readyCount}
                  tone="text-emerald-600 dark:text-emerald-300"
                />
              </div>

              <section className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="flex items-center gap-2 text-sm font-semibold">
                    <Bot className="size-4 text-primary" />
                    エージェント確認
                  </h2>
                  <Badge variant="outline">{agentEntries.length}件</Badge>
                </div>
                {agentEntries.length > 0 ? (
                  <div className="space-y-2">
                    {agentEntries.map((entry) => (
                      <TaskEntryRow
                        key={entry.key}
                        entry={entry}
                        launching={launchingKey === entry.key}
                        onOpenTask={openTask}
                        onStartAgent={(item) => void startAgent(item)}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState label="エージェントに渡す候補はありません" />
                )}
              </section>

              <section className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="flex items-center gap-2 text-sm font-semibold">
                    <Clock3 className="size-4 text-primary" />
                    今日の流れ
                  </h2>
                  <Badge variant="outline">{todayEntries.length}件</Badge>
                </div>
                {todayEntries.length > 0 ? (
                  <div className="space-y-2">
                    {todayEntries.map((entry) => (
                      <TaskEntryRow
                        key={entry.key}
                        entry={entry}
                        launching={launchingKey === entry.key}
                        onOpenTask={openTask}
                        onStartAgent={(item) => void startAgent(item)}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState label="今日の予定タスクはありません" />
                )}
              </section>

              {activeCount > 0 ? (
                <div className="rounded-md border bg-muted/20 px-3 py-2 text-sm">
                  <div className="flex items-center gap-2 font-medium">
                    <Timer className="size-4 text-primary" />
                    進行中のタスクがあります
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {activeCount}件が進行中またはタイマー計測中です。
                  </p>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
