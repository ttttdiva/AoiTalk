"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";
import { BlurFade } from "@/components/magicui/blur-fade";
import { NumberTicker } from "@/components/magicui/number-ticker";
import { taskApi, type Task } from "@/lib/task-api";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Gauge,
  ListTodo,
  PlayCircle,
  Timer,
  TrendingUp,
  Users,
} from "lucide-react";
// recharts を dynamic import すると Cell/Bar の displayName が失われ、
// findAllByType(children, Cell) が Cell を検出できず fill が適用されない（バーが黒くなる）。
// "use client" 側で静的 import し、マウント後のみ描画して SSR を回避する。
import {
  BarChart as RechartsBarChart,
  Bar as RechartsBar,
  PieChart as RechartsPieChart,
  Pie as RechartsPie,
  Cell as RechartsCell,
  XAxis as RechartsXAxis,
  YAxis as RechartsYAxis,
  Tooltip as RechartsTooltip,
  ResponsiveContainer as RechartsResponsiveContainer,
  Legend as RechartsLegend,
} from "recharts";

interface StatusCount {
  status: string;
  count: number;
}

interface PriorityCount {
  priority: string;
  count: number;
}

interface TagStat {
  id: string;
  name: string;
  color: string | null;
  total_seconds: number;
  task_count: number;
}

interface RecentTask {
  id: string;
  title: string;
  completed_at: string | null;
  priority: string;
}

interface MemberTimeStat {
  user_id: string;
  username: string;
  display_name: string | null;
  total_seconds: number;
}

interface EffortTracking {
  project_estimated_hours: number | null;
  task_estimated_hours_total: number;
  task_estimated_count: number;
  actual_hours: number;
  member_stats: MemberTimeStat[];
}

interface DashboardData {
  status_counts: StatusCount[];
  priority_counts: PriorityCount[];
  tag_stats: TagStat[];
  recent_completed: RecentTask[];
  active_timer_count: number;
  total_time_seconds: number;
  effort_tracking: EffortTracking;
}

const STATUS_COLORS: Record<string, string> = {
  todo: "#85948f",
  open: "#85948f",
  in_progress: "#67d9c9",
  on_hold: "#9facb3",
  review: "#bbc8d0",
  closed: "#44ddc1",
};

const STATUS_LABELS: Record<string, string> = {
  todo: "未着手",
  open: "未着手",
  in_progress: "進行中",
  on_hold: "保留",
  review: "確認待ち",
  closed: "完了",
};

const PRIORITY_COLORS: Record<string, string> = {
  urgent: "#ffb4ab",
  high: "#f4b183",
  medium: "#67d9c9",
  low: "#85948f",
};

const PRIORITY_LABELS: Record<string, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
};

const DEFAULT_TAG_COLORS = [
  "#44ddc1",
  "#67d9c9",
  "#bbc8d0",
  "#9facb3",
  "#00bfa5",
  "#85f6e5",
  "#85948f",
  "#3c4a46",
];

function formatTime(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  if (safeSeconds === 0) return "0m";
  const h = Math.floor(safeSeconds / 3600);
  const m = Math.floor((safeSeconds % 3600) / 60);
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

function formatTimeTooltip(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  if (safeSeconds === 0) return "0分";
  const h = Math.floor(safeSeconds / 3600);
  const m = Math.floor((safeSeconds % 3600) / 60);
  if (h === 0) return `${m}分`;
  if (m === 0) return `${h}時間`;
  return `${h}時間${m}分`;
}

// Recharts の既定 tooltip は項目文字を黒、BarChart のカーソルを白系で描画するため、
// アプリのダークテーマではコントラストが崩れる。CSS 変数を使って両テーマに追従させる。
const CHART_TOOLTIP_CONTENT_STYLE = {
  backgroundColor: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
  color: "var(--popover-foreground)",
  fontSize: "12px",
};

const CHART_TOOLTIP_ITEM_STYLE = {
  color: "var(--popover-foreground)",
};

const CHART_TOOLTIP_LABEL_STYLE = {
  color: "var(--popover-foreground)",
};

const CHART_TOOLTIP_CURSOR = {
  fill: "var(--accent)",
  fillOpacity: 0.35,
};

type DrillCategory = "total" | "closed" | "in_progress" | "time";
type DashboardScope =
  | { type: "project"; id: string }
  | { type: "space"; id: string };

export function ProjectDashboard({ scope }: { scope: DashboardScope }) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const summaryHasAnimatedRef = useRef(false);
  const [drillCategory, setDrillCategory] = useState<DrillCategory | null>(
    null,
  );
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  const fetchDashboard = useCallback(async () => {
    setLoading(true);
    try {
      const url =
        scope.type === "project"
          ? `/api/projects/${scope.id}/dashboard`
          : `/api/spaces/${scope.id}/dashboard`;
      const res = await fetch(url, { credentials: "include" });
      if (res.ok) {
        setData(await res.json());
      }
    } catch (err) {
      console.error("ダッシュボード取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [scope.id, scope.type]);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  if (loading) {
    return (
      <div className="space-y-4 px-1 py-1">
        <div className="grid grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 rounded-md" />
          ))}
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Skeleton className="h-64 rounded-md" />
          <Skeleton className="h-64 rounded-md" />
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <p className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
        データの取得に失敗しました
      </p>
    );
  }

  // 集計値
  const statusMap = new Map(data.status_counts.map((s) => [s.status, s.count]));
  const totalTasks =
    (statusMap.get("todo") || 0) +
    (statusMap.get("in_progress") || 0) +
    (statusMap.get("closed") || 0);
  const closedTasks = statusMap.get("closed") || 0;
  const inProgressTasks = statusMap.get("in_progress") || 0;
  const remainingTasks = totalTasks - closedTasks;

  // ステータスチャート用
  const statusData = data.status_counts
    .filter((s) => s.count > 0)
    .map((s) => ({
      name: STATUS_LABELS[s.status || "todo"] || s.status,
      value: s.count,
      color: STATUS_COLORS[s.status || "todo"] || "#6b7280",
    }));

  // 優先度チャート用
  const priorityOrder = ["urgent", "high", "medium", "low"];
  const priorityData = priorityOrder
    .map((p) => {
      const found = data.priority_counts.find((pc) => pc.priority === p);
      return {
        name: PRIORITY_LABELS[p] || p,
        count: found?.count || 0,
        color: PRIORITY_COLORS[p] || "#6b7280",
      };
    })
    .filter((d) => d.count > 0);

  // タグ別時間チャート用 (時間がある or タスクがあるタグのみ)
  const tagData = data.tag_stats
    .filter((t) => t.total_seconds > 0 || t.task_count > 0)
    .sort((a, b) => b.total_seconds - a.total_seconds)
    .map((t, i) => ({
      name: t.name,
      seconds: Math.max(0, t.total_seconds),
      hours: Math.round((Math.max(0, t.total_seconds) / 3600) * 10) / 10,
      taskCount: t.task_count,
      color: t.color || DEFAULT_TAG_COLORS[i % DEFAULT_TAG_COLORS.length],
    }));

  const completionRate =
    totalTasks > 0 ? Math.round((closedTasks / totalTasks) * 100) : 0;

  return (
    <div className="space-y-4 px-1 pb-2">
      {/* サマリーカード */}
      <BlurFade
        initial={summaryHasAnimatedRef.current ? false : "hidden"}
        onAnimationStart={() => {
          summaryHasAnimatedRef.current = true;
        }}
        duration={0.3}
        blur="4px"
        offset={8}
      >
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          <SummaryCard
            icon={<ListTodo className="size-4" />}
            label="総タスク"
            value={totalTasks}
            sub={`完了率 ${completionRate}%`}
            color="text-foreground"
            onClick={() => setDrillCategory("total")}
          />
          <SummaryCard
            icon={<CheckCircle2 className="size-4" />}
            label="完了"
            value={closedTasks}
            color="text-primary"
            onClick={() => setDrillCategory("closed")}
          />
          <SummaryCard
            icon={<PlayCircle className="size-4" />}
            label="進行中"
            value={inProgressTasks}
            sub={
              data.active_timer_count > 0
                ? `${data.active_timer_count}件計測中`
                : undefined
            }
            color="text-primary"
            onClick={() => setDrillCategory("in_progress")}
          />
          <SummaryCard
            icon={<Clock className="size-4" />}
            label="合計時間"
            value={formatTime(data.total_time_seconds)}
            sub={
              data.effort_tracking.project_estimated_hours
                ? `見積 ${data.effort_tracking.project_estimated_hours}h / 残 ${remainingTasks}件`
                : `残タスク ${remainingTasks}件`
            }
            color="text-muted-foreground"
            isText
            onClick={() => setDrillCategory("time")}
          />
        </div>
      </BlurFade>

      <TaskDrillDialog
        scope={scope}
        category={drillCategory}
        onOpenChange={(open) => {
          if (!open) setDrillCategory(null);
        }}
        onSelectTask={(taskId) => setSelectedTaskId(taskId)}
      />

      <TaskDetailModal
        taskId={selectedTaskId}
        open={selectedTaskId !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedTaskId(null);
        }}
        onTaskUpdated={fetchDashboard}
      />

      {/* チャート行1: タグ別時間 + ステータス分布 */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-5">
        {/* タグ別作業時間 */}
        <Card className="border-border bg-card shadow-none xl:col-span-3">
          <CardHeader className="border-b border-border pb-3">
            <CardTitle className="flex items-center gap-1.5 text-sm font-semibold">
              <Timer className="size-3.5" />
              タグ別作業時間
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            {tagData.length > 0 ? (
              <div className="h-56">
                <RechartsResponsiveContainer width="100%" height="100%">
                  <RechartsBarChart
                    data={tagData}
                    layout="vertical"
                    margin={{ top: 0, right: 16, bottom: 0, left: 0 }}
                  >
                    <RechartsXAxis
                      type="number"
                      tickFormatter={(v: number) => formatTime(v)}
                      tick={{ fontSize: 11 }}
                    />
                    <RechartsYAxis
                      type="category"
                      dataKey="name"
                      width={80}
                      tick={{ fontSize: 11 }}
                    />
                    <RechartsTooltip
                      formatter={(value: unknown) => [
                        formatTimeTooltip(Number(value) || 0),
                        "作業時間",
                      ]}
                      contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
                      itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                      labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                      cursor={CHART_TOOLTIP_CURSOR}
                    />
                    <RechartsBar dataKey="seconds" radius={[0, 4, 4, 0]}>
                      {tagData.map((entry, index) => (
                        <RechartsCell key={index} fill={entry.color} />
                      ))}
                    </RechartsBar>
                  </RechartsBarChart>
                </RechartsResponsiveContainer>
              </div>
            ) : (
              <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                タグ付きの作業記録がありません
              </div>
            )}
          </CardContent>
        </Card>

        {/* ステータス分布 */}
        <Card className="border-border bg-card shadow-none xl:col-span-2">
          <CardHeader className="border-b border-border pb-3">
            <CardTitle className="flex items-center gap-1.5 text-sm font-semibold">
              <TrendingUp className="size-3.5" />
              ステータス分布
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            {statusData.length > 0 ? (
              <div className="h-56">
                <RechartsResponsiveContainer width="100%" height="100%">
                  <RechartsPieChart>
                    <RechartsPie
                      data={statusData}
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={80}
                      dataKey="value"
                      nameKey="name"
                      strokeWidth={2}
                      stroke="var(--card)"
                    >
                      {statusData.map((entry, index) => (
                        <RechartsCell key={index} fill={entry.color} />
                      ))}
                    </RechartsPie>
                    <RechartsTooltip
                      formatter={(value: unknown, name: unknown) => [
                        `${value}件`,
                        String(name),
                      ]}
                      contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
                      itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                      labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                      cursor={CHART_TOOLTIP_CURSOR}
                    />
                    <RechartsLegend
                      iconSize={8}
                      wrapperStyle={{ fontSize: "11px" }}
                    />
                  </RechartsPieChart>
                </RechartsResponsiveContainer>
              </div>
            ) : (
              <div className="h-56 flex items-center justify-center text-sm text-muted-foreground">
                タスクがありません
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* 工数予実管理 */}
      {(data.effort_tracking.project_estimated_hours ||
        data.effort_tracking.task_estimated_hours_total > 0) && (
        <EffortTrackingSection effort={data.effort_tracking} />
      )}

      {/* チャート行2: 優先度別 + 最近完了 */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-5">
        {/* 優先度別タスク */}
        <Card className="border-border bg-card shadow-none xl:col-span-3">
          <CardHeader className="border-b border-border pb-3">
            <CardTitle className="text-sm font-semibold">優先度別タスク</CardTitle>
          </CardHeader>
          <CardContent>
            {priorityData.length > 0 ? (
              <div className="h-40">
                <RechartsResponsiveContainer width="100%" height="100%">
                  <RechartsBarChart
                    data={priorityData}
                    margin={{ top: 0, right: 16, bottom: 0, left: 0 }}
                  >
                    <RechartsXAxis dataKey="name" tick={{ fontSize: 11 }} />
                    <RechartsYAxis
                      tick={{ fontSize: 11 }}
                      allowDecimals={false}
                    />
                    <RechartsTooltip
                      formatter={(value: unknown) => [`${value}件`, "タスク数"]}
                      contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
                      itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                      labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                      cursor={CHART_TOOLTIP_CURSOR}
                    />
                    <RechartsBar dataKey="count" radius={[4, 4, 0, 0]}>
                      {priorityData.map((entry, index) => (
                        <RechartsCell key={index} fill={entry.color} />
                      ))}
                    </RechartsBar>
                  </RechartsBarChart>
                </RechartsResponsiveContainer>
              </div>
            ) : (
              <div className="h-40 flex items-center justify-center text-sm text-muted-foreground">
                タスクがありません
              </div>
            )}
          </CardContent>
        </Card>

        {/* 最近完了したタスク */}
        <Card className="border-border bg-card shadow-none xl:col-span-2">
          <CardHeader className="border-b border-border pb-3">
            <CardTitle className="text-sm font-semibold">最近の完了タスク</CardTitle>
          </CardHeader>
          <CardContent>
            {data.recent_completed.length > 0 ? (
              <div className="space-y-2">
                {data.recent_completed.map((task) => (
                  <div key={task.id} className="flex items-start gap-2 text-sm">
                    <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate">{task.title}</p>
                      <div className="flex items-center gap-2 mt-0.5">
                        {task.priority && task.priority !== "medium" && (
                          <Badge
                            variant="outline"
                            className="text-[10px] px-1 py-0"
                            style={{
                              borderColor:
                                PRIORITY_COLORS[task.priority] || undefined,
                              color:
                                PRIORITY_COLORS[task.priority] || undefined,
                            }}
                          >
                            {PRIORITY_LABELS[task.priority] || task.priority}
                          </Badge>
                        )}
                        {task.completed_at && (
                          <span className="text-xs text-muted-foreground">
                            {new Date(task.completed_at).toLocaleDateString(
                              "ja-JP",
                              { month: "short", day: "numeric" },
                            )}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="h-32 flex items-center justify-center text-sm text-muted-foreground">
                完了タスクがありません
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* タグ別タスク数テーブル */}
      {data.tag_stats.length > 0 && (
        <Card className="border-border bg-card shadow-none">
          <CardHeader className="border-b border-border pb-3">
            <CardTitle className="text-sm font-semibold">タグ別サマリー</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {data.tag_stats
                .filter((t) => t.task_count > 0)
                .sort((a, b) => b.task_count - a.task_count)
                .map((tag, i) => (
                  <div
                    key={tag.id}
                    className="flex items-center gap-2 rounded border border-border px-3 py-2"
                  >
                    <div
                      className="size-2.5 rounded-full shrink-0"
                      style={{
                        backgroundColor:
                          tag.color ||
                          DEFAULT_TAG_COLORS[i % DEFAULT_TAG_COLORS.length],
                      }}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-medium truncate">{tag.name}</p>
                      <p className="text-[10px] text-muted-foreground">
                        {tag.task_count}件
                        {tag.total_seconds > 0 &&
                          ` / ${formatTime(tag.total_seconds)}`}
                      </p>
                    </div>
                  </div>
                ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function SummaryCard({
  icon,
  label,
  value,
  sub,
  color,
  isText,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  sub?: string;
  color: string;
  isText?: boolean;
  onClick?: () => void;
}) {
  const clickable = !!onClick;
  return (
    <Card
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick?.();
              }
            }
          : undefined
      }
      className={
        clickable
          ? "cursor-pointer border-border bg-card shadow-none transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          : undefined
      }
    >
      <CardContent className="px-4 pb-3 pt-4">
        <div className="mb-2 flex items-center gap-2">
          <span className={`${color}`}>{icon}</span>
          <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{label}</span>
        </div>
        <p className={`text-3xl font-semibold tracking-tight ${color}`}>
          {typeof value === "number" && !isText ? (
            <NumberTicker
              value={value}
              decimalPlaces={0}
              className="tabular-nums text-inherit tracking-normal"
            />
          ) : (
            value
          )}
        </p>
        {sub && <p className="mt-1 text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  );
}

const DRILL_TITLES: Record<DrillCategory, string> = {
  total: "総タスク",
  closed: "完了タスク",
  in_progress: "進行中タスク",
  time: "作業時間ランキング",
};

function TaskDrillDialog({
  scope,
  category,
  onOpenChange,
  onSelectTask,
}: {
  scope: DashboardScope;
  category: DrillCategory | null;
  onOpenChange: (open: boolean) => void;
  onSelectTask: (taskId: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (category === null) return;
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await taskApi.listTasks(
          scope.type === "project"
            ? { project_id: scope.id }
            : { space_id: scope.id },
        );
        if (!cancelled) setTasks(res);
      } catch (err: unknown) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "取得失敗");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [scope.id, scope.type, category]);

  const filtered = useMemo(() => {
    if (category === null) return [];
    if (category === "total") {
      return [...tasks].sort(
        (a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0),
      );
    }
    if (category === "closed") {
      return tasks
        .filter((t) => t.status === "closed")
        .sort((a, b) => {
          const ta = a.completed_at ? Date.parse(a.completed_at) : 0;
          const tb = b.completed_at ? Date.parse(b.completed_at) : 0;
          return tb - ta;
        });
    }
    if (category === "in_progress") {
      return tasks
        .filter((t) => t.status === "in_progress")
        .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
    }
    // time
    return tasks
      .filter((t) => (t.total_time_seconds ?? 0) > 0)
      .sort(
        (a, b) => (b.total_time_seconds ?? 0) - (a.total_time_seconds ?? 0),
      );
  }, [tasks, category]);

  return (
    <Dialog open={category !== null} onOpenChange={onOpenChange}>
      <DialogContent size="2xl" className="max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>
            {category ? DRILL_TITLES[category] : ""}
            {!loading && (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {filtered.length}件
              </span>
            )}
          </DialogTitle>
        </DialogHeader>
        <div className="overflow-y-auto -mx-4 px-4">
          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-10 rounded-md" />
              ))}
            </div>
          ) : error ? (
            <p className="text-sm text-red-500 py-4">{error}</p>
          ) : filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              該当するタスクがありません
            </p>
          ) : (
            <ul className="divide-y">
              {filtered.map((task) => (
                <li key={task.id}>
                  <button
                    type="button"
                    onClick={() => onSelectTask(task.id)}
                    className="w-full text-left py-2 px-2 rounded-md hover:bg-accent/50 transition-colors flex items-center gap-2"
                  >
                    <StatusDot status={task.status} />
                    <span className="flex-1 truncate text-sm">
                      {task.title}
                    </span>
                    {task.priority && task.priority !== "medium" && (
                      <Badge
                        variant="outline"
                        className="text-[10px] px-1 py-0 shrink-0"
                        style={{
                          borderColor:
                            PRIORITY_COLORS[task.priority] || undefined,
                          color: PRIORITY_COLORS[task.priority] || undefined,
                        }}
                      >
                        {PRIORITY_LABELS[task.priority] || task.priority}
                      </Badge>
                    )}
                    {category === "time" &&
                      (task.total_time_seconds ?? 0) > 0 && (
                        <span className="text-xs text-muted-foreground tabular-nums shrink-0">
                          {formatTime(task.total_time_seconds ?? 0)}
                        </span>
                      )}
                    {category === "closed" && task.completed_at && (
                      <span className="text-xs text-muted-foreground shrink-0">
                        {new Date(task.completed_at).toLocaleDateString(
                          "ja-JP",
                          { month: "short", day: "numeric" },
                        )}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function StatusDot({ status }: { status: string }) {
  const color = STATUS_COLORS[status] || "#6b7280";
  return (
    <span
      className="size-2 rounded-full shrink-0"
      style={{ backgroundColor: color }}
      aria-label={STATUS_LABELS[status] || status}
    />
  );
}

function EffortTrackingSection({ effort }: { effort: EffortTracking }) {
  const estimatedHours = effort.project_estimated_hours || 0;
  const actualHours = effort.actual_hours;
  const taskEstTotal = effort.task_estimated_hours_total;

  // 消化率（プロジェクト見積がある場合）
  const usageRate =
    estimatedHours > 0
      ? Math.round((actualHours / estimatedHours) * 100)
      : null;
  const isOverBudget = usageRate !== null && usageRate > 100;
  const remainingHours =
    estimatedHours > 0
      ? Math.round((estimatedHours - actualHours) * 100) / 100
      : null;

  // プログレスバーの幅（最大100%で表示、超過時は100%+赤色）
  const progressWidth = usageRate !== null ? Math.min(usageRate, 100) : 0;

  // 横棒比較チャート用データ
  const barData = [];
  if (estimatedHours > 0) {
    barData.push({ name: "見積", hours: estimatedHours, color: "#6b7280" });
  }
  if (taskEstTotal > 0) {
    barData.push({
      name: "タスク見積計",
      hours: Math.round(taskEstTotal * 10) / 10,
      color: "#3b82f6",
    });
  }
  barData.push({
    name: "実績",
    hours: actualHours,
    color: isOverBudget ? "#ef4444" : "#22c55e",
  });

  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-5">
      {/* 工数予実 */}
      <Card className="border-border bg-card shadow-none xl:col-span-3">
        <CardHeader className="border-b border-border pb-3">
          <CardTitle className="flex items-center gap-1.5 text-sm font-semibold">
            <Gauge className="size-3.5" />
            工数予実
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* プログレスバー（プロジェクト見積がある場合のみ） */}
          {estimatedHours > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted-foreground">消化率</span>
                <span
                  className={`font-bold ${isOverBudget ? "text-red-500" : usageRate && usageRate > 80 ? "text-amber-500" : "text-green-500"}`}
                >
                  {usageRate}%
                </span>
              </div>
              <div className="h-3 rounded-full bg-muted overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    isOverBudget
                      ? "bg-red-500"
                      : usageRate && usageRate > 80
                        ? "bg-amber-500"
                        : "bg-primary"
                  }`}
                  style={{ width: `${progressWidth}%` }}
                />
              </div>
              <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                <span>
                  実績 {actualHours}h / 見積 {estimatedHours}h
                </span>
                {remainingHours !== null && (
                  <span
                    className={
                      remainingHours < 0 ? "text-red-500 font-medium" : ""
                    }
                  >
                    {remainingHours >= 0
                      ? `残り ${remainingHours}h`
                      : `${Math.abs(remainingHours)}h 超過`}
                  </span>
                )}
              </div>
              {isOverBudget && (
                <div className="flex items-center gap-1.5 rounded-md bg-red-500/10 px-2.5 py-1.5 text-xs text-red-500">
                  <AlertTriangle className="size-3.5" />
                  工数オーバーしています
                </div>
              )}
            </div>
          )}

          {/* 横棒比較 */}
          {barData.length > 1 && (
            <div className="h-32">
              <RechartsResponsiveContainer width="100%" height="100%">
                <RechartsBarChart
                  data={barData}
                  layout="vertical"
                  margin={{ top: 0, right: 16, bottom: 0, left: 0 }}
                >
                  <RechartsXAxis
                    type="number"
                    tick={{ fontSize: 11 }}
                    tickFormatter={(v: number) => `${v}h`}
                  />
                  <RechartsYAxis
                    type="category"
                    dataKey="name"
                    width={80}
                    tick={{ fontSize: 11 }}
                  />
                  <RechartsTooltip
                    formatter={(value: unknown) => [`${value}時間`, "工数"]}
                    contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
                    itemStyle={CHART_TOOLTIP_ITEM_STYLE}
                    labelStyle={CHART_TOOLTIP_LABEL_STYLE}
                    cursor={CHART_TOOLTIP_CURSOR}
                  />
                  <RechartsBar dataKey="hours" radius={[0, 4, 4, 0]}>
                    {barData.map((entry, index) => (
                      <RechartsCell key={index} fill={entry.color} />
                    ))}
                  </RechartsBar>
                </RechartsBarChart>
              </RechartsResponsiveContainer>
            </div>
          )}

          {/* 見積がない場合 */}
          {estimatedHours === 0 && taskEstTotal === 0 && (
            <p className="text-xs text-muted-foreground">
              プロジェクトの見積工数を設定すると予実比較が表示されます
            </p>
          )}
        </CardContent>
      </Card>

      {/* メンバー別工数 */}
      <Card className="border-border bg-card shadow-none xl:col-span-2">
        <CardHeader className="border-b border-border pb-3">
          <CardTitle className="flex items-center gap-1.5 text-sm font-semibold">
            <Users className="size-3.5" />
            メンバー別工数
          </CardTitle>
        </CardHeader>
        <CardContent>
          {effort.member_stats.length > 0 ? (
            <div className="space-y-2">
              {effort.member_stats
                .sort((a, b) => b.total_seconds - a.total_seconds)
                .map((member) => {
                  const memberHours =
                    Math.round((member.total_seconds / 3600) * 10) / 10;
                  const memberPct =
                    estimatedHours > 0
                      ? Math.round((memberHours / estimatedHours) * 100)
                      : 0;
                  return (
                    <div key={member.user_id} className="space-y-1">
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-medium truncate">
                          {member.display_name || member.username}
                        </span>
                        <span className="text-muted-foreground shrink-0 ml-2">
                          {memberHours}h
                          {estimatedHours > 0 && ` (${memberPct}%)`}
                        </span>
                      </div>
                      {estimatedHours > 0 && (
                        <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                          <div
                            className="h-full rounded-full bg-primary transition-all"
                            style={{ width: `${Math.min(memberPct, 100)}%` }}
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
            </div>
          ) : (
            <div className="h-32 flex items-center justify-center text-sm text-muted-foreground">
              作業記録がありません
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
