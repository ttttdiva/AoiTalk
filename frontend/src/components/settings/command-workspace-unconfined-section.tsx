"use client";

import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import { Loader2, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";

export const COMMAND_WORKSPACE_UNCONFINED_KEY =
  "local_agent_tasks.command_workspace_unconfined";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type AgentCall = {
  agent_id: string;
  revision_id: string;
  caller_user_ids: string[];
};

type UnconfinedSetting = {
  user_ids: string[];
  agent_calls: AgentCall[];
};

type SettingsPayload = {
  settings?: {
    local_agent_tasks?: {
      command_workspace_unconfined?: UnconfinedSetting;
    };
  };
};

const EMPTY_SETTING: UnconfinedSetting = { user_ids: [], agent_calls: [] };

export function parseUuid(value: string): string | null {
  const trimmed = value.trim();
  if (!UUID_RE.test(trimmed)) return null;
  return trimmed.toLowerCase();
}

function parseUuidList(value: string): string[] | null {
  const parts = value
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (parts.length === 0) return [];
  const parsed: string[] = [];
  const seen = new Set<string>();
  for (const part of parts) {
    const uuid = parseUuid(part);
    if (uuid === null) return null;
    if (!seen.has(uuid)) {
      seen.add(uuid);
      parsed.push(uuid);
    }
  }
  return parsed;
}

function sameSetting(left: UnconfinedSetting, right: UnconfinedSetting): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const message =
      typeof detail?.detail === "string" ? detail.detail : `API Error: ${res.status}`;
    throw new Error(message);
  }
  return res.json();
}

function normalizeSetting(value: UnconfinedSetting | undefined): UnconfinedSetting {
  return {
    user_ids: [...(value?.user_ids ?? [])],
    agent_calls: (value?.agent_calls ?? []).map((call) => ({
      agent_id: call.agent_id,
      revision_id: call.revision_id,
      caller_user_ids: [...(call.caller_user_ids ?? [])],
    })),
  };
}

export function CommandWorkspaceUnconfinedSection({
  isAdmin = false,
}: {
  isAdmin?: boolean;
}) {
  const [userIdDraft, setUserIdDraft] = useState("");
  const [agentIdDraft, setAgentIdDraft] = useState("");
  const [revisionIdDraft, setRevisionIdDraft] = useState("");
  const [callerDraft, setCallerDraft] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const { data, error, isLoading, mutate } = useSWR<UnconfinedSetting>(
    isAdmin ? "settings/command-workspace-unconfined" : null,
    async () => {
      const payload = await pyFetch<SettingsPayload>("/settings");
      return normalizeSetting(payload.settings?.local_agent_tasks?.command_workspace_unconfined);
    },
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
    },
  );
  const setting = data ?? EMPTY_SETTING;

  const confined = useMemo(
    () => setting.user_ids.length === 0 && setting.agent_calls.length === 0,
    [setting],
  );

  const persist = useCallback(
    async (next: UnconfinedSetting) => {
      setSaving(true);
      setFormError(null);
      try {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: COMMAND_WORKSPACE_UNCONFINED_KEY,
            value: next,
          }),
        });
        await mutate(next, { revalidate: false });
        toast.success(
          next.user_ids.length === 0 && next.agent_calls.length === 0
            ? "作業場所の制限を維持する設定を保存しました"
            : "作業場所の制限解除を保存しました",
        );
      } catch (err) {
        const message = err instanceof Error ? err.message : "保存に失敗しました";
        setFormError(message);
        toast.error(message);
      } finally {
        setSaving(false);
      }
    },
    [mutate],
  );

  const addUserId = useCallback(() => {
    const parsed = parseUuid(userIdDraft);
    if (parsed === null) {
      setFormError("ユーザーIDはUUIDで指定してください");
      return;
    }
    setFormError(null);
    setUserIdDraft("");
    if (setting.user_ids.includes(parsed)) return;
    void persist({ ...setting, user_ids: [...setting.user_ids, parsed] });
  }, [persist, setting, userIdDraft]);

  const removeUserId = useCallback(
    (userId: string) => {
      void persist({
        ...setting,
        user_ids: setting.user_ids.filter((item) => item !== userId),
      });
    },
    [persist, setting],
  );

  const addAgentCall = useCallback(() => {
    const agentId = parseUuid(agentIdDraft);
    const revisionId = parseUuid(revisionIdDraft);
    const callers = parseUuidList(callerDraft);
    if (agentId === null || revisionId === null || callers === null || callers.length === 0) {
      setFormError("Agent / revision / 呼び出し元はUUIDで指定してください");
      return;
    }
    const nextCall: AgentCall = {
      agent_id: agentId,
      revision_id: revisionId,
      caller_user_ids: callers,
    };
    if (setting.agent_calls.some((call) => JSON.stringify(call) === JSON.stringify(nextCall))) {
      setAgentIdDraft("");
      setRevisionIdDraft("");
      setCallerDraft("");
      setFormError(null);
      return;
    }
    setFormError(null);
    setAgentIdDraft("");
    setRevisionIdDraft("");
    setCallerDraft("");
    void persist({ ...setting, agent_calls: [...setting.agent_calls, nextCall] });
  }, [agentIdDraft, callerDraft, persist, revisionIdDraft, setting]);

  const removeAgentCall = useCallback(
    (index: number) => {
      void persist({
        ...setting,
        agent_calls: setting.agent_calls.filter((_, itemIndex) => itemIndex !== index),
      });
    },
    [persist, setting],
  );

  const restoreConfinement = useCallback(() => {
    if (sameSetting(setting, EMPTY_SETTING)) return;
    void persist(EMPTY_SETTING);
  }, [persist, setting]);

  if (!isAdmin) return null;

  return (
    <SettingsDisclosure
      title="コマンド作業場所の制限解除"
      icon={<ShieldCheck className="size-4" />}
      id="command-workspace-unconfined-card"
      targetId="command-workspace-unconfined"
      summary={<Badge variant="outline">admin only</Badge>}
      help="認証済みの有効なadminだけをUUIDで指定します。解除後は存在する絶対パスのディレクトリを作業場所に使えます。リンク、filesystem root、相対パスは拒否したままです。"
    >
      <div className="space-y-4" data-command-workspace-unconfined>
        {isLoading && !data ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            <Loader2 className="size-4 animate-spin" />
            設定を読み込んでいます…
          </div>
        ) : error ? (
          <p className="text-sm text-destructive" role="alert">
            設定を読み込めませんでした。
          </p>
        ) : (
          <>
            <div className="space-y-2">
              <Label htmlFor="command-workspace-unconfined-user-id">admin ユーザーID</Label>
              <div className="flex gap-2">
                <Input
                  id="command-workspace-unconfined-user-id"
                  value={userIdDraft}
                  onChange={(event) => setUserIdDraft(event.target.value)}
                  placeholder="00000000-0000-4000-8000-000000000001"
                  autoComplete="off"
                />
                <Button type="button" size="sm" onClick={addUserId} disabled={saving}>
                  <Plus className="size-3.5" />
                  追加
                </Button>
              </div>
              <ul className="space-y-1">
                {setting.user_ids.map((userId) => (
                  <li key={userId} className="flex items-center justify-between gap-2 text-sm">
                    <span className="min-w-0 truncate font-mono">{userId}</span>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => removeUserId(userId)}
                      disabled={saving}
                    >
                      <Trash2 className="size-3.5" />
                      削除
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
            <div className="space-y-2">
              <Label>Agent revision 呼び出し</Label>
              <div className="grid gap-2 sm:grid-cols-3">
                <Input
                  aria-label="agent_id"
                  value={agentIdDraft}
                  onChange={(event) => setAgentIdDraft(event.target.value)}
                  placeholder="agent_id"
                  autoComplete="off"
                />
                <Input
                  aria-label="revision_id"
                  value={revisionIdDraft}
                  onChange={(event) => setRevisionIdDraft(event.target.value)}
                  placeholder="revision_id"
                  autoComplete="off"
                />
                <Input
                  aria-label="caller_user_ids"
                  value={callerDraft}
                  onChange={(event) => setCallerDraft(event.target.value)}
                  placeholder="caller UUID"
                  autoComplete="off"
                />
              </div>
              <Button type="button" size="sm" onClick={addAgentCall} disabled={saving}>
                <Plus className="size-3.5" />
                呼び出しを追加
              </Button>
              <ul className="space-y-1">
                {setting.agent_calls.map((call, index) => (
                  <li key={`${call.agent_id}:${call.revision_id}:${index}`} className="flex items-start justify-between gap-2 text-sm">
                    <span className="min-w-0 truncate font-mono">
                      {call.agent_id} / {call.revision_id} / {call.caller_user_ids.join(", ")}
                    </span>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => removeAgentCall(index)}
                      disabled={saving}
                    >
                      <Trash2 className="size-3.5" />
                      削除
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
            {formError ? (
              <p className="text-sm text-destructive" role="alert">
                {formError}
              </p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={restoreConfinement}
                disabled={saving || confined}
              >
                空にして制限へ戻す
              </Button>
            </div>
          </>
        )}
      </div>
    </SettingsDisclosure>
  );
}
