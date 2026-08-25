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

  const handleTrigger = useCallback(
    async (name: string) => {
      setBusyName(name);
      try {
        await pyFetch(`/heartbeats/${encodeURIComponent(name)}/trigger`, {
          method: "POST",
        });
        await loadHeartbeats();
        toast.success("Heartbeat triggered");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Failed to trigger heartbeat");
      } finally {
        setBusyName(null);
      }
    },
    [loadHeartbeats],
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
                          onClick={() => openEdit(heartbeat)}
                        >
                          <Pencil className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-destructive"
                          disabled={busyName === heartbeat.name}
                          onClick={() => handleDelete(heartbeat.name)}
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
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
