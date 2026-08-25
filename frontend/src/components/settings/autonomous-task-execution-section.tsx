"use client";

import { useEffect, useState } from "react";
import { Loader2, Save, Workflow } from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { SettingsDisclosure } from "./settings-disclosure";
import { pyFetch } from "./llm-model-section-types";

type HarnessSettings = {
  enabled: boolean;
  auto_start: boolean;
  runner: string;
  model: string;
  effort: string;
  max_concurrent_agents: number;
};

const DEFAULT_SETTINGS: HarnessSettings = {
  enabled: false,
  auto_start: false,
  runner: "codex_exec",
  model: "",
  effort: "",
  max_concurrent_agents: 1,
};

export function AutonomousTaskExecutionSection() {
  const [draft, setDraft] = useState<HarnessSettings>(DEFAULT_SETTINGS);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void pyFetch<{ settings?: Partial<HarnessSettings> }>("/agent-harness/config")
      .then((response) => {
        if (active && response.settings) {
          setDraft((current) => ({ ...current, ...response.settings }));
        }
      })
      .catch((reason) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : "自律タスク実行設定を取得できませんでした");
        }
      });
    return () => { active = false; };
  }, []);

  const save = async () => {
    setSaving(true);
    setMessage("");
    setError("");
    try {
      await pyFetch("/agent-harness/config", {
        method: "PUT",
        body: JSON.stringify({ settings: draft }),
      });
      setMessage("自律タスク実行設定を保存しました。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "自律タスク実行設定の保存に失敗しました");
    } finally {
      setSaving(false);
    }
  };

  return (
    <SettingsDisclosure
      title="自律タスク実行"
      icon={<Workflow className="size-4" />}
      targetId="autonomous-task-execution"
      summary={<Badge variant={draft.enabled ? "secondary" : "outline"}>{draft.enabled ? "有効" : "無効"}</Badge>}
    >
      <p className="text-xs text-muted-foreground">
        Agent Team topologyとは独立して、バックグラウンドタスクの実行方法を管理します。
      </p>
      {(message || error) && (
        <p role={error ? "alert" : "status"} className={`rounded border p-2 text-xs ${error ? "border-destructive/40 text-destructive" : "text-muted-foreground"}`}>
          {error || message}
        </p>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        <label className="flex items-center gap-2 text-xs">
          <Checkbox aria-label="自律タスク実行を有効にする" checked={draft.enabled} onCheckedChange={(checked) => setDraft((current) => ({ ...current, enabled: checked === true }))} disabled={saving} />
          有効にする
        </label>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox aria-label="自律タスク実行を自動開始" checked={draft.auto_start} onCheckedChange={(checked) => setDraft((current) => ({ ...current, auto_start: checked === true }))} disabled={saving || !draft.enabled} />
          起動時に自動開始
        </label>
        <label className="space-y-1 text-xs">
          <span>実行方式</span>
          <AppSelect aria-label="自律タスク実行の実行方式" value={draft.runner} onChange={(event) => setDraft((current) => ({ ...current, runner: event.target.value }))} disabled={saving} className="h-8 w-full">
            <option value="codex_exec">Codex CLI</option>
            <option value="claude_code">Claude Code</option>
            <option value="custom_command">カスタムコマンド</option>
          </AppSelect>
        </label>
        <label className="space-y-1 text-xs">
          <span>同時実行数</span>
          <Input aria-label="自律タスク実行の同時実行数" type="number" min={1} max={32} value={draft.max_concurrent_agents} onChange={(event) => setDraft((current) => ({ ...current, max_concurrent_agents: Math.max(1, Math.min(32, Number(event.target.value) || 1)) }))} disabled={saving} className="h-8" />
        </label>
      </div>
      <Button type="button" size="sm" variant="outline" onClick={() => void save()} disabled={saving}>
        {saving ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}
        自律タスク実行を保存
      </Button>
    </SettingsDisclosure>
  );
}
