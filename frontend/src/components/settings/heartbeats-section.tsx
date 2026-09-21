"use client";

import { useCallback, useState } from "react";
import useSWR from "swr";
import {
  Activity,
  ChevronDown,
  ChevronUp,
  Loader2,
  Pencil,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { Skeleton } from "@/components/ui/skeleton";
import { useConfirm } from "@/hooks/use-confirm";

interface Heartbeat {
  name: string;
  description: string;
  checklist: string;
  interval_minutes: number;
  enabled: boolean;
  active_hours?: {
    start?: string;
    end?: string;
    timezone?: string;
  };
  notify_channel: string;
  actions?: Array<Record<string, unknown>>;
  last_result?: Record<string, unknown> | null;
}

interface HeartbeatRun {
  id: string | null;
  heartbeat_name: string;
  mode?: string | null;
  scope_type: string;
  scope_id: string;
  project_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  status: string;
  success?: boolean | null;
  memory_upsert_count: number;
  forgotten_count: number;
  question_count: number;
  continuation_pending: boolean;
  result_summary?: string | null;
  questions: Array<Record<string, unknown> | string>;
  safe_error_code?: string | null;
  forced?: boolean;
  generic_action_count?: number;
  generic_action_failure_count?: number;
}

interface HeartbeatHistoryPage {
  runs: HeartbeatRun[];
  items?: HeartbeatRun[];
  total_count?: number;
  limit: number;
  offset: number;
  next_cursor?: string | null;
  has_more?: boolean;
}

interface HeartbeatHistoryState {
  loaded: boolean;
  loading: boolean;
  error: string | null;
  data: HeartbeatHistoryPage;
}

const EMPTY_HISTORY_PAGE: HeartbeatHistoryPage = {
  runs: [],
  limit: 10,
  offset: 0,
  next_cursor: null,
  has_more: false,
};

const HISTORY_PAGE_SIZE = 10;

interface HeartbeatForm {
  name: string;
  description: string;
  checklist: string;
  intervalMinutes: string;
  enabled: boolean;
  activeStart: string;
  activeEnd: string;
  timezone: string;
  notifyChannel: string;
  actionsJson: string;
}

const EMPTY_FORM: HeartbeatForm = {
  name: "",
  description: "",
  checklist: "",
  intervalMinutes: "30",
  enabled: true,
  activeStart: "",
  activeEnd: "",
  timezone: "Asia/Tokyo",
  notifyChannel: "websocket",
  actionsJson: "[]",
};

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

function toForm(heartbeat: Heartbeat): HeartbeatForm {
  return {
    name: heartbeat.name,
    description: heartbeat.description,
    checklist: heartbeat.checklist,
    intervalMinutes: String(heartbeat.interval_minutes ?? 30),
    enabled: heartbeat.enabled !== false,
    activeStart: heartbeat.active_hours?.start ?? "",
    activeEnd: heartbeat.active_hours?.end ?? "",
    timezone: heartbeat.active_hours?.timezone ?? "Asia/Tokyo",
    notifyChannel: heartbeat.notify_channel || "websocket",
    actionsJson: JSON.stringify(heartbeat.actions ?? [], null, 2),
  };
}


function buildPayload(form: HeartbeatForm, includeName: boolean) {
  const interval = Number(form.intervalMinutes);
  const payload: Record<string, unknown> = {
    description: form.description.trim(),
    checklist: form.checklist,
    interval_minutes: Number.isFinite(interval) && interval > 0 ? Math.floor(interval) : 30,
    enabled: form.enabled,
    notify_channel: form.notifyChannel.trim() || "websocket",
  };
  if (includeName) payload.name = form.name.trim();
  if (form.activeStart.trim() || form.activeEnd.trim()) {
    payload.active_hours = {
      start: form.activeStart.trim() || "00:00",
      end: form.activeEnd.trim() || "23:59",
      timezone: form.timezone.trim() || "Asia/Tokyo",
    };
  } else {
    payload.active_hours = null;
  }
  return payload;
}

function formatRunDate(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function questionText(question: Record<string, unknown> | string): string {
  if (typeof question === "string") return question;
  for (const key of ["question", "summary", "topic", "title", "message"]) {
    const value = question[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "Question summary unavailable";
}

function runScopeLabel(run: HeartbeatRun): string {
  if (run.scope_type === "project") {
    return run.project_id || run.scope_id || "Project";
  }
  return "Global";
}

function HeartbeatHistoryPanel({
  heartbeatName,
  state,
  onLoad,
}: {
  heartbeatName: string;
  state?: HeartbeatHistoryState;
  onLoad: (cursor: string | null) => void;
}) {
  const data = state?.data || EMPTY_HISTORY_PAGE;
  const loading = state?.loading === true;
  const totalCount = data.total_count;
  const canPrevious = data.offset > 0;
  const canNext = data.has_more === true || (
    typeof totalCount === "number" && data.offset + data.runs.length < totalCount
  );

  return (
    <div
      className="mt-2 space-y-2 rounded border bg-muted/20 p-2"
      aria-label={`${heartbeatName} recent execution history`}
    >
      {loading && data.runs.length === 0 ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3 animate-spin" />
          Loading recent runs...
        </div>
      ) : state?.error ? (
        <div className="flex items-center justify-between gap-2 text-xs text-destructive">
          <span>{state.error}</span>
          <Button type="button" variant="outline" size="sm" onClick={() => onLoad(null)}>
            Retry
          </Button>
        </div>
      ) : data.runs.length === 0 ? (
        <p className="text-xs text-muted-foreground">No execution history.</p>
      ) : (
        <>
          <div className="max-h-80 space-y-2 overflow-auto">
            {data.runs.map((run, index) => {
              const key = run.id || `${run.started_at || "run"}-${index}`;
              const statusVariant =
                run.status === "ok" || run.success === true ? "default" :
                run.status === "running" ? "secondary" : "destructive";
              return (
                <div key={key} className="rounded border bg-background p-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={statusVariant} className="text-[10px]">
                      {run.status || "unknown"}
                    </Badge>
                    <span className="font-medium">{runScopeLabel(run)}</span>
                    <span className="text-muted-foreground">
                      {formatRunDate(run.started_at)} → {formatRunDate(run.completed_at)}
                    </span>
                    {run.forced ? (
                      <Badge variant="outline" className="text-[10px]">
                        Manual
                      </Badge>
                    ) : null}
                    {run.continuation_pending ? (
                      <Badge variant="outline" className="text-[10px]">
                        Continuation pending
                      </Badge>
                    ) : null}
                  </div>
                  <p className="mt-1 text-muted-foreground">
                    Memory {run.memory_upsert_count || 0} · Forgotten {run.forgotten_count || 0} ·
                    Questions {run.question_count || 0}
                    {run.generic_action_count ? (
                      <> · Actions {run.generic_action_count} ({run.generic_action_failure_count || 0} failed)</>
                    ) : null}
                  </p>
                  {run.result_summary ? (
                    <p className="mt-1 whitespace-pre-wrap break-words">{run.result_summary}</p>
                  ) : null}
                  {run.questions?.length ? (
                    <ul className="mt-1 list-disc space-y-0.5 pl-4">
                      {run.questions.slice(0, HISTORY_PAGE_SIZE).map((question, questionIndex) => (
                        <li key={`${key}-question-${questionIndex}`} className="break-words">
                          {questionText(question)}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {run.safe_error_code ? (
                    <p className="mt-1 break-words text-destructive">
                      Error ({run.safe_error_code})
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-muted-foreground">
              {typeof totalCount === "number"
                ? `${data.offset + 1}-${Math.min(data.offset + data.runs.length, totalCount)} of ${totalCount}`
                : `${data.runs.length}${data.has_more ? "+" : ""} recent runs`}
            </span>
            <div className="flex gap-1">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                disabled={!canPrevious || loading}
                onClick={() => onLoad(null)}
              >
                Previous
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                disabled={!canNext || loading}
                onClick={() =>
                  onLoad(data.next_cursor || null)
                }
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export function HeartbeatsSection() {
  const confirm = useConfirm();
  const [expanded, setExpanded] = useState(false);
  // Heartbeat一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（トグル/更新/保存・削除・トリガー後）で駆動するため自動 revalidation は無効化する。
  const { data: heartbeats = [], mutate: mutateHeartbeats } = useSWR<Heartbeat[]>(
    "settings/heartbeats",
    async () => {
      try {
        return (await pyFetch<{ heartbeats: Heartbeat[] }>("/heartbeats")).heartbeats || [];
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Failed to load heartbeats");
        return [];
      }
    },
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  const [loading, setLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [isNew, setIsNew] = useState(false);
  const [form, setForm] = useState<HeartbeatForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [historyOpenName, setHistoryOpenName] = useState<string | null>(null);
  const [historyByName, setHistoryByName] = useState<
    Record<string, HeartbeatHistoryState>
  >({});

  const loadHeartbeats = useCallback(async () => {
    setLoading(true);
    try {
      await mutateHeartbeats();
    } finally {
      setLoading(false);
    }
  }, [mutateHeartbeats]);

  const handleToggle = useCallback(() => {
    if (!expanded && heartbeats.length === 0) void loadHeartbeats();
    setExpanded((value) => !value);
  }, [expanded, heartbeats.length, loadHeartbeats]);

  const openNew = useCallback(() => {
    setIsNew(true);
    setForm(EMPTY_FORM);
    setEditorOpen(true);
  }, []);

  const openEdit = useCallback((heartbeat: Heartbeat) => {
    setIsNew(false);
    setForm(toForm(heartbeat));
    setEditorOpen(true);
  }, []);

  const handleSave = useCallback(async () => {
    if (!form.name.trim() || !form.checklist.trim()) return;
    setSaving(true);
    try {
      if (isNew) {
        await pyFetch("/heartbeats", {
          method: "POST",
          body: JSON.stringify(buildPayload(form, true)),
        });
      } else {
        await pyFetch(`/heartbeats/${encodeURIComponent(form.name.trim())}`, {
          method: "PUT",
          body: JSON.stringify(buildPayload(form, false)),
        });
      }
      setEditorOpen(false);
      await loadHeartbeats();
      toast.success("Heartbeat saved");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to save heartbeat");
    } finally {
      setSaving(false);
    }
  }, [form, isNew, loadHeartbeats]);

  const handleDelete = useCallback(
    async (name: string) => {
      if (
        !(await confirm({
          description: `Delete heartbeat "${name}"?`,
          destructive: true,
        }))
      )
        return;
      setBusyName(name);
      try {
        await pyFetch(`/heartbeats/${encodeURIComponent(name)}`, { method: "DELETE" });
        await loadHeartbeats();
        toast.success("Heartbeat deleted");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Failed to delete heartbeat");
      } finally {
        setBusyName(null);
      }
    },
    [loadHeartbeats, confirm],
  );

  const loadHistory = useCallback(async (name: string, cursor: string | null = null) => {
    setHistoryByName((previous) => ({
      ...previous,
      [name]: {
        ...(previous[name] || { data: EMPTY_HISTORY_PAGE, loaded: false }),
        loaded: previous[name]?.loaded ?? false,
        loading: true,
        error: null,
      },
    }));
    try {
      const params = new URLSearchParams({
        heartbeat_name: name,
        limit: String(HISTORY_PAGE_SIZE),
      });
      if (cursor) params.set("cursor", cursor);
      const data = await pyFetch<HeartbeatHistoryPage>(
        `/heartbeats/history?${params.toString()}`,
      );
      const normalized: HeartbeatHistoryPage = {
        runs: Array.isArray(data?.runs)
          ? data.runs
          : Array.isArray(data?.items)
            ? data.items
            : [],
        total_count:
          typeof data?.total_count === "number" ? data.total_count : undefined,
        limit: Number(data?.limit || HISTORY_PAGE_SIZE),
        offset: Number(data?.offset || 0),
        next_cursor: data?.next_cursor || null,
        has_more: data?.has_more === true,
      };
      setHistoryByName((previous) => ({
        ...previous,
        [name]: {
          loaded: true,
          loading: false,
          error: null,
          data: normalized,
        },
      }));
    } catch (error) {
      setHistoryByName((previous) => ({
        ...previous,
        [name]: {
          ...(previous[name] || { data: EMPTY_HISTORY_PAGE }),
          loaded: previous[name]?.loaded ?? false,
          loading: false,
          error: error instanceof Error ? error.message : "Failed to load run history",
        },
      }));
    }
  }, []);

  const handleTrigger = useCallback(
    async (name: string) => {
      setBusyName(name);
      try {
        await pyFetch(`/heartbeats/${encodeURIComponent(name)}/trigger`, {
          method: "POST",
        });
        await loadHeartbeats();
        if (historyByName[name]?.loaded) {
          await loadHistory(name, null);
        }
        toast.success("Heartbeat triggered");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Failed to trigger heartbeat");
      } finally {
        setBusyName(null);
      }
    },
    [historyByName, loadHeartbeats, loadHistory],
  );

  const toggleHistory = useCallback(
    (name: string) => {
      if (historyOpenName === name) {
        setHistoryOpenName(null);
        return;
      }
      setHistoryOpenName(name);
      if (!historyByName[name]?.loaded && !historyByName[name]?.loading) {
        void loadHistory(name, null);
      }
    },
    [historyByName, historyOpenName, loadHistory],
  );

  const enabledCount = heartbeats.filter((item) => item.enabled !== false).length;

  return (
    <>
      <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
        <CardHeader className="cursor-pointer select-none" onClick={handleToggle}>
          <CardTitle className="flex items-center justify-between gap-3 text-sm">
            <span className="flex min-w-0 items-center gap-2">
              <Activity className="size-4" />
              <span>Heartbeats</span>
              {heartbeats.length > 0 ? (
                <Badge variant="secondary">
                  {enabledCount}/{heartbeats.length} enabled
                </Badge>
              ) : null}
            </span>
            {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
          </CardTitle>
        </CardHeader>
        {expanded && (
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <Button size="sm" variant="outline" onClick={openNew}>
                <Plus className="mr-1 size-3.5" />
                Add
              </Button>
              <Button size="sm" variant="ghost" onClick={loadHeartbeats} disabled={loading}>
                Refresh
              </Button>
            </div>

            {loading ? (
              <div className="space-y-2">
                {Array.from({ length: 3 }).map((_, index) => (
                  <Skeleton key={index} className="h-16 w-full rounded" />
                ))}
              </div>
            ) : heartbeats.length === 0 ? (
              <p className="text-sm text-muted-foreground">No heartbeats configured.</p>
            ) : (
              <div className="space-y-2">
                {heartbeats.map((heartbeat) => (
                  <div key={heartbeat.name} className="rounded border p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="text-sm font-medium">{heartbeat.name}</p>
                          <Badge variant={heartbeat.enabled ? "default" : "secondary"}>
                            {heartbeat.enabled ? "ON" : "OFF"}
                          </Badge>
                          <Badge variant="outline">{heartbeat.interval_minutes} min</Badge>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {heartbeat.description || "No description"}
                        </p>
                        {heartbeat.last_result?.status ? (
                          <p className="mt-1 text-[11px] text-muted-foreground">
                            Last result: {String(heartbeat.last_result.status)}
                          </p>
                        ) : null}
                        {heartbeat.active_hours ? (
                          <p className="mt-1 text-[11px] text-muted-foreground">
                            Active {heartbeat.active_hours.start || "00:00"}-
                            {heartbeat.active_hours.end || "23:59"}{" "}
                            {heartbeat.active_hours.timezone || ""}
                          </p>
                        ) : null}
                      </div>
                      <div className="flex shrink-0 gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7"
                          aria-label={`Trigger ${heartbeat.name}`}
                          disabled={busyName === heartbeat.name}
                          onClick={() => handleTrigger(heartbeat.name)}
                        >
                          {busyName === heartbeat.name ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : (
                            <Play className="size-3.5" />
                          )}
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7"
                          aria-label={`Edit ${heartbeat.name}`}
                          onClick={() => openEdit(heartbeat)}
                        >
                          <Pencil className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-destructive"
                          aria-label={`Delete ${heartbeat.name}`}
                          disabled={busyName === heartbeat.name}
                          onClick={() => handleDelete(heartbeat.name)}
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
                    </div>
                    <div className="mt-3 border-t pt-2">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        aria-expanded={historyOpenName === heartbeat.name}
                        onClick={() => toggleHistory(heartbeat.name)}
                      >
                        {historyOpenName === heartbeat.name ? (
                          <ChevronUp className="mr-1 size-3.5" />
                        ) : (
                          <ChevronDown className="mr-1 size-3.5" />
                        )}
                        {historyOpenName === heartbeat.name
                          ? "Hide recent runs"
                          : "Recent runs"}
                      </Button>
                      {historyOpenName === heartbeat.name ? (
                        <HeartbeatHistoryPanel
                          heartbeatName={heartbeat.name}
                          state={historyByName[heartbeat.name]}
                          onLoad={(nextCursor) =>
                            void loadHistory(heartbeat.name, nextCursor)
                          }
                        />
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        )}
      </Card>

      <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
        <DialogContent size="2xl">
          <DialogHeader>
            <DialogTitle>{isNew ? "Add heartbeat" : "Edit heartbeat"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="heartbeat-name">Name</Label>
                <Input
                  id="heartbeat-name"
                  value={form.name}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, name: event.target.value }))
                  }
                  disabled={!isNew}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="heartbeat-interval">Interval minutes</Label>
                <Input
                  id="heartbeat-interval"
                  type="number"
                  min={1}
                  step={1}
                  value={form.intervalMinutes}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, intervalMinutes: event.target.value }))
                  }
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="heartbeat-description">Description</Label>
              <Input
                id="heartbeat-description"
                value={form.description}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, description: event.target.value }))
                }
              />
            </div>
            <div className="space-y-1">
              <Label>Checklist</Label>
              <LongTextEditor
                value={form.checklist}
                onChange={(value) => setForm((prev) => ({ ...prev, checklist: value }))}
                minHeight={160}
                maxHeight={360}
                fontSize={12}
              />
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="heartbeat-start">Active start</Label>
                <Input
                  id="heartbeat-start"
                  placeholder="09:00"
                  value={form.activeStart}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, activeStart: event.target.value }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="heartbeat-end">Active end</Label>
                <Input
                  id="heartbeat-end"
                  placeholder="20:00"
                  value={form.activeEnd}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, activeEnd: event.target.value }))
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="heartbeat-timezone">Timezone</Label>
                <Input
                  id="heartbeat-timezone"
                  value={form.timezone}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, timezone: event.target.value }))
                  }
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="heartbeat-notify-channel">Notify channel</Label>
              <Input
                id="heartbeat-notify-channel"
                value={form.notifyChannel}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, notifyChannel: event.target.value }))
                }
              />
            </div>
            <div className="space-y-1">
              <Label>Actions JSON（YAML 管理・読み取り専用）</Label>
              <LongTextEditor
                value={form.actionsJson}
                onChange={() => undefined}
                readOnly
                minHeight={180}
                maxHeight={380}
                fontFamily="monospace"
                fontSize={12}
                placeholder={`[
  {
    "type": "notify",
    "run_on": "alert",
    "config": {
      "message": "アラート内容"
    }
  }
]`}
              />
              <p className="text-xs text-muted-foreground">
                actions は config/heartbeats/*.yaml で管理します。HTTP API からは変更できません。
              </p>
            </div>
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Checkbox
                  checked={form.enabled}
                  onCheckedChange={(value) =>
                    setForm((prev) => ({ ...prev, enabled: value === true }))
                  }
                />
                <Label>Enabled</Label>
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditorOpen(false)}>
                  Cancel
                </Button>
                <Button
                  onClick={handleSave}
                  disabled={saving || !form.name.trim() || !form.checklist.trim()}
                >
                  {saving && <Loader2 className="mr-1 size-3.5 animate-spin" />}
                  Save
                </Button>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
