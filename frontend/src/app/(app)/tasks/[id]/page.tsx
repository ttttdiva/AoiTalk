"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { TaskDescriptionEditor } from "@/components/editor/task-description-editor";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TaskStatusMenuItems } from "@/components/tasks/task-status-menu-items";
import {
  ArrowLeft,
  Play,
  Square,
  Clock,
  Send,
  Trash2,
  Repeat,
  ChevronDown,
} from "lucide-react";
import { taskApi, type Task } from "@/lib/task-api";
import {
  createTaskCompletionUndoEntry,
  dispatchTaskCompletionUndoBatch,
  isTaskCompletionTransition,
} from "@/lib/task-completion-undo";
import {
  toLocalDateTimeInputValue,
  toTaskDatePayloadValue,
} from "@/lib/date-time";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { cn } from "@/lib/utils";
import { formatTimerClock, getElapsedTimerSeconds } from "@/lib/task-time";
import { DatePickerPopover } from "@/components/tasks/date-picker-popover";

function formatDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleString("ja-JP");
}

const WEEKDAYS = [
  { key: "MO", label: "月" },
  { key: "TU", label: "火" },
  { key: "WE", label: "水" },
  { key: "TH", label: "木" },
  { key: "FR", label: "金" },
  { key: "SA", label: "土" },
  { key: "SU", label: "日" },
] as const;

function buildRrule(freq: string, interval: number, byDay: string[]): string {
  let rrule = `FREQ=${freq};INTERVAL=${interval}`;
  if (freq === "WEEKLY" && byDay.length > 0) {
    rrule += `;BYDAY=${byDay.join(",")}`;
  }
  return rrule;
}

export default function TaskDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const taskId = params.id;

  const [task, setTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [timerLoading, setTimerLoading] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  // 編集状態
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editingTitle, setEditingTitle] = useState(false);

  // コメント
  const [comments, setComments] = useState<
    { id: string; content: string; created_at: string; user_id?: string }[]
  >([]);
  const [commentText, setCommentText] = useState("");
  const [sendingComment, setSendingComment] = useState(false);

  // 繰り返し設定
  const [recurrenceEnabled, setRecurrenceEnabled] = useState(false);
  const [recurrenceFreq, setRecurrenceFreq] = useState("DAILY");
  const [recurrenceInterval, setRecurrenceInterval] = useState(1);
  const [recurrenceByDay, setRecurrenceByDay] = useState<string[]>([]);

  // debounce用
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // タスク取得
  const fetchTask = useCallback(async () => {
    try {
      const t = await taskApi.getTask(taskId);
      setTask(t);
      setEditTitle(t.title);
      setEditDescription(t.description || "");
    } catch (err) {
      console.error("タスク取得失敗:", err);
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchTask();
  }, [fetchTask]);

  useTaskCompletionRefresh(fetchTask);

  // タイマー表示更新
  useEffect(() => {
    if (!task?.active_time_entry?.started_at) {
      setElapsedSeconds(0);
      return;
    }
    const updateElapsed = () => {
      setElapsedSeconds(
        getElapsedTimerSeconds(task.active_time_entry?.started_at),
      );
    };
    updateElapsed();
    const interval = setInterval(updateElapsed, 1000);
    return () => clearInterval(interval);
  }, [task?.active_time_entry?.started_at]);

  // debounce更新
  const debouncedUpdate = useCallback(
    (data: Record<string, unknown>) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(async () => {
        try {
          const updated = await taskApi.updateTask(taskId, data);
          setTask(updated);
        } catch (err) {
          console.error("更新失敗:", err);
        }
      }, 500);
    },
    [taskId],
  );

  // 即時更新（select変更用）
  const immediateUpdate = useCallback(
    async (data: Record<string, unknown>) => {
      try {
        const previousTask = task;
        const updated = await taskApi.updateTask(taskId, data);
        setTask(updated);
        if (
          previousTask &&
          typeof data.status === "string" &&
          isTaskCompletionTransition(previousTask.status, data.status)
        ) {
          dispatchTaskCompletionUndoBatch({
            entries: [createTaskCompletionUndoEntry(previousTask)],
          });
        }
      } catch (err) {
        console.error("更新失敗:", err);
      }
    },
    [task, taskId],
  );

  // タイマー操作
  const handleTimer = useCallback(async () => {
    setTimerLoading(true);
    try {
      if (task?.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
        setElapsedSeconds(0);
        setTask((prev) =>
          prev ? { ...prev, active_time_entry: null } : prev,
        );
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: null },
          }),
        );
      } else {
        const started = await taskApi.startTimer(taskId);
        setElapsedSeconds(0);
        setTask((prev) =>
          prev ? { ...prev, active_time_entry: started } : prev,
        );
        window.dispatchEvent(
          new CustomEvent("timer-changed", {
            detail: { activeEntry: started },
          }),
        );
      }
      await fetchTask();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    } finally {
      setTimerLoading(false);
    }
  }, [task, taskId, fetchTask]);

  // ヘッダー等でタイマーが変わったら再取得
  useEffect(() => {
    const onTimerChanged = () => {
      fetchTask();
    };
    window.addEventListener("timer-changed", onTimerChanged);
    return () => window.removeEventListener("timer-changed", onTimerChanged);
  }, [fetchTask]);

  // コメント送信
  const handleSendComment = useCallback(async () => {
    if (!commentText.trim()) return;
    setSendingComment(true);
    try {
      await taskApi.addComment(taskId, commentText.trim());
      setCommentText("");
      // コメント一覧を再取得（APIが返す場合）
      // 簡易的にローカル追加
      setComments((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          content: commentText.trim(),
          created_at: new Date().toISOString(),
        },
      ]);
    } catch (err) {
      console.error("コメント送信失敗:", err);
    } finally {
      setSendingComment(false);
    }
  }, [taskId, commentText]);

  // タスク削除
  const handleDelete = useCallback(async () => {
    try {
      await taskApi.deleteTask(taskId);
      router.push("/tasks");
    } catch (err) {
      console.error("削除失敗:", err);
    }
  }, [taskId, router]);

  // 曜日トグル
  const toggleWeekday = useCallback((dayKey: string) => {
    setRecurrenceByDay((prev) =>
      prev.includes(dayKey)
        ? prev.filter((d) => d !== dayKey)
        : [...prev, dayKey],
    );
  }, []);

  // 繰り返し設定の保存
  const [recurrenceSaving, setRecurrenceSaving] = useState(false);
  const handleSaveRecurrence = useCallback(async () => {
    if (!taskId) return;
    setRecurrenceSaving(true);
    try {
      if (!recurrenceEnabled) {
        await taskApi.deleteRecurrence(taskId);
      } else {
        const rrule = buildRrule(
          recurrenceFreq,
          recurrenceInterval,
          recurrenceByDay,
        );
        await taskApi.saveRecurrence(taskId, { rrule });
      }
    } catch (err) {
      console.error("繰り返し設定の保存に失敗:", err);
    } finally {
      setRecurrenceSaving(false);
    }
  }, [
    recurrenceEnabled,
    recurrenceFreq,
    recurrenceInterval,
    recurrenceByDay,
    taskId,
  ]);

  if (loading) {
    return (
      <div className="p-4 space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (!task) {
    return (
      <div className="flex items-center justify-center p-16 text-muted-foreground">
        タスクが見つかりません
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col lg:flex-row">
      {/* 左カラム: メイン */}
      <div className="flex-1 overflow-auto p-4 space-y-6">
        {/* 戻るボタン */}
        <Button variant="ghost" size="sm" onClick={() => router.push("/tasks")}>
          <ArrowLeft className="size-4" />
          タスク一覧
        </Button>

        {/* タイトル */}
        <div>
          {editingTitle ? (
            <Input
              value={editTitle}
              onChange={(e) => {
                setEditTitle(e.target.value);
                debouncedUpdate({ title: e.target.value });
              }}
              onBlur={() => setEditingTitle(false)}
              onKeyDown={(e) => {
                if (e.key === "Enter") setEditingTitle(false);
              }}
              className="text-xl font-bold border-none shadow-none px-0 focus-visible:ring-0"
              autoFocus
            />
          ) : (
            <h1
              className="text-xl font-bold cursor-pointer hover:text-primary/80 transition-colors"
              onClick={() => setEditingTitle(true)}
            >
              {editTitle || task.title}
            </h1>
          )}
        </div>

        {/* 説明 */}
        <div className="space-y-2">
          <Label>説明</Label>
          <TaskDescriptionEditor
            value={editDescription}
            onChange={(value) => {
              setEditDescription(value);
              debouncedUpdate({ description: value });
            }}
            placeholder="説明を追加..."
            minHeight={112}
            maxHeight={360}
          />
        </div>

        <Separator />

        {/* コメント */}
        <div className="space-y-4">
          <h2 className="text-sm font-medium">コメント</h2>
          {comments.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              コメントはまだありません
            </p>
          ) : (
            <div className="space-y-3">
              {comments.map((c) => (
                <div
                  key={c.id}
                  className="rounded-lg border p-3 text-sm space-y-1"
                >
                  <p>{c.content}</p>
                  <p className="text-xs text-muted-foreground">
                    {formatDateTime(c.created_at)}
                  </p>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <Textarea
              value={commentText}
              onChange={(e) => setCommentText(e.target.value)}
              placeholder="コメントを入力..."
              rows={2}
              className="resize-none flex-1"
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  handleSendComment();
                }
              }}
            />
            <Button
              size="icon"
              onClick={handleSendComment}
              disabled={sendingComment || !commentText.trim()}
              className="shrink-0 self-end"
            >
              <Send className="size-4" />
            </Button>
          </div>
        </div>
      </div>

      {/* 右カラム: サイドバー */}
      <div className="w-full lg:w-80 border-t lg:border-t-0 lg:border-l overflow-auto p-4 space-y-5">
        {/* ステータス */}
        <div className="space-y-2">
          <Label>ステータス</Label>
          <DropdownMenu>
            <DropdownMenuTrigger className="flex h-8 w-full items-center justify-between gap-1.5 rounded-lg border border-input bg-transparent py-2 pr-2 pl-2.5 text-sm outline-none transition-colors hover:bg-accent focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 data-[state=open]:border-ring dark:bg-input/30 dark:hover:bg-input/50">
              <span>
                {{
                  open: "未着手",
                  in_progress: "進行中",
                  on_hold: "保留",
                  review: "確認待ち",
                  closed: "完了",
                }[task.status] || task.status}
              </span>
              <ChevronDown className="size-4 text-muted-foreground" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-40">
              <TaskStatusMenuItems
                currentStatus={task.status}
                onSelect={(status) => void immediateUpdate({ status })}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* 優先度 */}
        <div className="space-y-2">
          <Label>優先度</Label>
          <Select
            value={task.priority}
            onValueChange={(v) => v && immediateUpdate({ priority: v })}
          >
            <SelectTrigger className="w-full">
              <span>
                {{
                  urgent: "Urgent",
                  high: "High",
                  medium: "Medium",
                  low: "Low",
                  none: "None",
                }[task.priority] || task.priority}
              </span>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="urgent">Urgent</SelectItem>
              <SelectItem value="high">High</SelectItem>
              <SelectItem value="medium">Medium</SelectItem>
              <SelectItem value="low">Low</SelectItem>
              <SelectItem value="none">None</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {/* Start / due date */}
        <div className="space-y-2">
          <Label>Start Date</Label>
          <DatePickerPopover
            value={toLocalDateTimeInputValue(task.start_at, {
              allDay: task.all_day,
            })}
            onChange={(v) =>
              immediateUpdate({
                start_at: toTaskDatePayloadValue(v, {
                  allDay: task.all_day,
                }),
              })
            }
            label="Start Date"
            placeholder="Select start date"
            allDay={task.all_day}
          />
        </div>
        <div className="space-y-2">
          <Label>Due Date</Label>
          <DatePickerPopover
            value={toLocalDateTimeInputValue(task.end_at, {
              allDay: task.all_day,
            })}
            onChange={(v) =>
              immediateUpdate({
                end_at: toTaskDatePayloadValue(v, {
                  allDay: task.all_day,
                }),
              })
            }
            label="Due Date"
            placeholder="Select due date"
            allDay={task.all_day}
          />
        </div>

        {/* 担当者 */}
        {task.assignees.length > 0 && (
          <div className="space-y-2">
            <Label>担当者</Label>
            <div className="flex flex-wrap gap-1.5">
              {task.assignees.map((a) => (
                <Badge key={a.id} variant="secondary">
                  {a.display_name || a.username || a.user_id}
                  {a.is_primary && " (主担当)"}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {/* タグ */}
        <div className="space-y-2">
          <Label>タグ</Label>
          <div className="flex flex-wrap gap-1.5">
            {task.tags.length === 0 && (
              <span className="text-sm text-muted-foreground">なし</span>
            )}
            {task.tags.map((tag) => (
              <Badge
                key={tag.id}
                variant="outline"
                style={
                  tag.color
                    ? { borderColor: tag.color, color: tag.color }
                    : undefined
                }
              >
                {tag.name}
              </Badge>
            ))}
          </div>
        </div>

        <Separator />

        {/* 繰り返し設定 */}
        <Card size="sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Repeat className="size-4" />
              繰り返し
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2">
              <Checkbox
                checked={recurrenceEnabled}
                onCheckedChange={(checked: boolean) =>
                  setRecurrenceEnabled(checked)
                }
              />
              <Label className="cursor-pointer text-sm">繰り返し設定</Label>
            </div>

            {recurrenceEnabled && (
              <div className="space-y-3">
                {/* 頻度 */}
                <div className="space-y-1">
                  <Label className="text-xs">頻度</Label>
                  <Select
                    value={recurrenceFreq}
                    onValueChange={(v) => {
                      if (!v) return;
                      setRecurrenceFreq(v);
                      if (v !== "WEEKLY") {
                        setRecurrenceByDay([]);
                      }
                    }}
                  >
                    <SelectTrigger className="w-full">
                      <span>
                        {{ DAILY: "毎日", WEEKLY: "毎週", MONTHLY: "毎月" }[
                          recurrenceFreq
                        ] || recurrenceFreq}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="DAILY">毎日</SelectItem>
                      <SelectItem value="WEEKLY">毎週</SelectItem>
                      <SelectItem value="MONTHLY">毎月</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {/* 間隔 */}
                <div className="space-y-1">
                  <Label className="text-xs">間隔</Label>
                  <Input
                    type="number"
                    min={1}
                    max={99}
                    value={recurrenceInterval}
                    onChange={(e) => {
                      const val = parseInt(e.target.value, 10);
                      if (!isNaN(val) && val >= 1) {
                        setRecurrenceInterval(val);
                      }
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    {recurrenceInterval === 1
                      ? "毎回"
                      : `${recurrenceInterval - 1}回おき`}
                  </p>
                </div>

                {/* 曜日選択（毎週のみ） */}
                {recurrenceFreq === "WEEKLY" && (
                  <div className="space-y-1">
                    <Label className="text-xs">曜日</Label>
                    <div className="flex flex-wrap gap-1">
                      {WEEKDAYS.map((day) => (
                        <Button
                          key={day.key}
                          type="button"
                          size="sm"
                          variant={
                            recurrenceByDay.includes(day.key)
                              ? "default"
                              : "outline"
                          }
                          className="h-8 w-8 p-0 text-xs"
                          onClick={() => toggleWeekday(day.key)}
                        >
                          {day.label}
                        </Button>
                      ))}
                    </div>
                  </div>
                )}

                {/* RRULE プレビュー */}
                <div className="space-y-1">
                  <Label className="text-xs">RRULE</Label>
                  <p className="rounded bg-muted px-2 py-1 font-mono text-xs break-all">
                    {buildRrule(
                      recurrenceFreq,
                      recurrenceInterval,
                      recurrenceByDay,
                    )}
                  </p>
                </div>

                {/* 保存ボタン */}
                <Button
                  size="sm"
                  className="w-full"
                  onClick={handleSaveRecurrence}
                  disabled={recurrenceSaving}
                >
                  {recurrenceSaving ? "保存中..." : "保存"}
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        <Separator />

        {/* タイマー */}
        <Card size="sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Clock className="size-4" />
              タイマー
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {task.active_time_entry && (
              <div className="text-center">
                <p className="text-2xl font-mono font-bold tabular-nums text-green-600">
                  {formatTimerClock(elapsedSeconds)}
                </p>
                <p className="text-xs text-muted-foreground mt-1">計測中</p>
              </div>
            )}
            <Button
              className={cn(
                "w-full",
                task.active_time_entry &&
                  "bg-red-600 hover:bg-red-700 text-white",
              )}
              onClick={handleTimer}
              disabled={timerLoading}
            >
              {task.active_time_entry ? (
                <>
                  <Square className="size-4" />
                  停止
                </>
              ) : (
                <>
                  <Play className="size-4" />
                  開始
                </>
              )}
            </Button>
          </CardContent>
        </Card>

        <Separator />

        {/* メタ情報 */}
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>作成: {formatDateTime(task.created_at)}</p>
          <p>更新: {formatDateTime(task.updated_at)}</p>
          {task.completed_at && (
            <p>完了: {formatDateTime(task.completed_at)}</p>
          )}
        </div>

        {/* 削除 */}
        <Button
          variant="destructive"
          size="sm"
          className="w-full"
          onClick={handleDelete}
        >
          <Trash2 className="size-4" />
          タスクを削除
        </Button>
      </div>
    </div>
  );
}
