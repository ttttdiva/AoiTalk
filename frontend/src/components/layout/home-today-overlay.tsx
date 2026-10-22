"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
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
  Timer,
} from "lucide-react";

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
import { taskApi } from "@/lib/task-api";
import { TaskProposalList } from "@/components/tasks/task-proposal-card";
import { useTaskProposals } from "@/components/tasks/hooks/use-task-proposals";
import type { TaskProposal } from "@/lib/task-proposals-api";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { TASK_STATUS_DOT_COLORS, TASK_STATUS_LABELS } from "@/lib/task-status";
import { useOptionalProject } from "@/contexts/project-context";
import {
  buildHomeTodayView,
  type HomeTodayData,
  type TodayEntry,
} from "@/lib/home-today-data";
import { cn } from "@/lib/utils";

const PRIORITY_LABELS: Record<string, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
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
  value: number | null;
  tone?: string;
}) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2">
      <div className="mb-1 flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className={tone}>{icon}</span>
        <span>{label}</span>
      </div>
      <div className={cn("text-xl font-semibold tabular-nums", tone)}>
        {value === null ? (
          <span aria-label="不明">—</span>
        ) : (
          <NumberTicker
            value={value}
            decimalPlaces={0}
            className="tabular-nums text-inherit tracking-normal"
          />
        )}
      </div>
    </div>
  );
}

function TaskEntryRow({
  entry,
  onOpenTask,
}: {
  entry: TodayEntry;
  onOpenTask: (entry: TodayEntry) => void;
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
  const projectContext = useOptionalProject();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<HomeTodayData>({
    tasks: [],
    occurrences: [],
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);

  const fetchData = useCallback(async () => {
    const requestGeneration = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const start = startOfToday();
      const end = endOfToday();
      const [tasks, occurrences] = await Promise.all([
        taskApi.listTodayTasks(),
        taskApi.listTodayOccurrences(
          formatLocalDateTime(start),
          formatLocalDateTime(end),
        ),
      ]);
      if (requestGeneration !== requestGenerationRef.current) return;
      setData({ tasks, occurrences });
    } catch (err) {
      if (requestGeneration !== requestGenerationRef.current) return;
      console.error("Home data fetch failed", err);
      setError(
        err instanceof Error
          ? err.message
          : "Today の情報を取得できませんでした",
      );
    } finally {
      if (requestGeneration === requestGenerationRef.current) {
        setLoading(false);
      }
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

  useEffect(() => {
    if (!open) return;
    const refresh = () => void fetchData();
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("task-list-refresh", refresh);
    window.addEventListener("task-sidebar-refresh", refresh);
    window.addEventListener("timer-changed", refresh);
    window.addEventListener("online", refresh);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("task-list-refresh", refresh);
      window.removeEventListener("task-sidebar-refresh", refresh);
      window.removeEventListener("timer-changed", refresh);
      window.removeEventListener("online", refresh);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [fetchData, open]);

  useEffect(
    () => () => {
      requestGenerationRef.current += 1;
    },
    [],
  );

  const {
    todayEntries,
    openTaskCount,
    activeCount,
    overdueCount,
  } = useMemo(() => buildHomeTodayView(data), [data]);

  const proposalTaskIds = useMemo(
    () => data.tasks.map((task) => task.id),
    [data.tasks],
  );
  const proposalState = useTaskProposals({
    taskIds: proposalTaskIds,
    enabled: open,
    loading: open && loading && data.tasks.length === 0,
  });
  const refreshProposalState = proposalState.refresh;
  const pendingProposalCount = proposalState.proposals.length;
  const pendingProposalMetric =
    proposalState.status === "ready" || proposalState.status === "empty"
      ? pendingProposalCount
      : null;
  const projectAccessById = useMemo(
    () =>
      new Map(
        [
          ...(projectContext?.accessibleProjects ?? []),
          ...(projectContext?.projects ?? []),
        ].map((project) => [project.id, project] as const),
      ),
    [projectContext?.accessibleProjects, projectContext?.projects],
  );

  const refreshAll = useCallback(async () => {
    await Promise.all([fetchData(), refreshProposalState()]);
  }, [fetchData, refreshProposalState]);

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

  const confirmProposal = useCallback(
    (proposal: TaskProposal) => {
      setOpen(false);
      router.push(`/chat?s=${encodeURIComponent(proposal.session_id)}`);
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
              onClick={() => void refreshAll()}
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
                  label="承認待ち"
                  value={pendingProposalMetric}
                  tone="text-emerald-600 dark:text-emerald-300"
                />
              </div>

              <section className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="flex items-center gap-2 text-sm font-semibold">
                    <Bot className="size-4 text-primary" />
                    エージェント確認
                  </h2>
                  <Badge variant="outline">
                    {pendingProposalMetric === null
                      ? "—"
                      : String(pendingProposalMetric) + "件"}
                  </Badge>
                </div>
                <TaskProposalList
                  state={proposalState}
                  getDecisionState={proposalState.getDecisionState}
                  onDecision={proposalState.decide}
                  onConfirm={confirmProposal}
                  onRefresh={proposalState.refresh}
                  decisionDisabled={(proposal) => {
                    const task = data.tasks.find(
                      (item) => item.id === proposal.task_id,
                    );
                    const project = task
                      ? projectAccessById.get(task.project_id)
                      : null;
                    return (
                      !task ||
                      !project ||
                      task.source === "remote" ||
                      project.source === "remote" ||
                      project.can_write === false
                    );
                  }}
                  heading={null}
                />
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
                        onOpenTask={openTask}
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
                    タイマー計測中のタスクがあります
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {activeCount}件をタイマー計測中です。
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
