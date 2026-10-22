"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Save, Workflow } from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { HelpPopover } from "@/components/ui/help-popover";
import { SettingsDisclosure } from "./settings-disclosure";
import { pyFetch } from "./llm-model-section-types";

type RunnerModelSettings = {
  model: string;
  reasoning_effort: string;
};

type ClaudeRunnerSettings = RunnerModelSettings & {
  permission_mode: string;
};

type HarnessSettings = {
  enabled: boolean;
  auto_start: boolean;
  runner: string;
  max_concurrent_agents: number;
  codex: RunnerModelSettings;
  claude: ClaudeRunnerSettings;
};

type HarnessConfigResponse = {
  settings?: Partial<Omit<HarnessSettings, "codex" | "claude">> & {
    model?: string;
    effort?: string;
    codex?: Partial<RunnerModelSettings>;
    claude?: Partial<ClaudeRunnerSettings>;
  };
  claude_code?: {
    installed?: boolean;
    version?: string | null;
    model_min_version?: string | null;
    warnings?: string[];
    flags?: Record<string, boolean | null>;
  };
};

const DEFAULT_SETTINGS: HarnessSettings = {
  enabled: false,
  auto_start: false,
  runner: "codex_exec",
  max_concurrent_agents: 1,
  codex: { model: "", reasoning_effort: "" },
  claude: { model: "", reasoning_effort: "", permission_mode: "acceptEdits" },
};

const CODEX_MODEL_SUGGESTIONS = ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "gpt-5-codex"];
const CODEX_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"];
const CLAUDE_MODEL_SUGGESTIONS = ["opus", "claude-opus-5-5", "sonnet", "claude-sonnet-5", "haiku", "fable"];
const CLAUDE_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh", "max"];
const CLAUDE_PERMISSION_OPTIONS: Array<[string, string]> = [
  ["acceptEdits", "ファイル編集を許可（推奨）"],
  ["dontAsk", "事前許可したツールのみ"],
  ["plan", "計画のみ（編集しない）"],
  ["default", "読み取りのみ"],
];

function isClaudeRunner(runner: string): boolean {
  return runner === "claude_code" || runner === "claude_cli";
}

function isCodexRunner(runner: string): boolean {
  return runner === "codex_exec" || runner === "codex_cli";
}

function withCurrent(options: string[], current: string): string[] {
  return current && !options.includes(current) ? [...options, current] : options;
}

function fromResponse(current: HarnessSettings, response: HarnessConfigResponse["settings"]): HarnessSettings {
  if (!response) return current;
  const runner = response.runner ?? current.runner;
  // Older servers only return flat model/effort for the selected runner.
  const flat = { model: response.model ?? "", reasoning_effort: response.effort ?? "" };
  return {
    enabled: response.enabled ?? current.enabled,
    auto_start: response.auto_start ?? current.auto_start,
    runner,
    max_concurrent_agents: response.max_concurrent_agents ?? current.max_concurrent_agents,
    codex: { ...current.codex, ...(response.codex ?? (isCodexRunner(runner) ? flat : {})) },
    claude: { ...current.claude, ...(response.claude ?? (isClaudeRunner(runner) ? flat : {})) },
  };
}

export function AutonomousTaskExecutionSection() {
  const [draft, setDraft] = useState<HarnessSettings>(DEFAULT_SETTINGS);
  const [claudeStatus, setClaudeStatus] = useState<HarnessConfigResponse["claude_code"]>();
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const claudeStatusSequence = useRef(0);
  const claudeStatusAbort = useRef<AbortController | null>(null);

  const refreshClaudeStatus = useCallback((modelValue: string, effortValue: string) => {
    claudeStatusAbort.current?.abort();
    const controller = new AbortController();
    claudeStatusAbort.current = controller;
    const sequence = ++claudeStatusSequence.current;
    const params = new URLSearchParams({ runner: "claude_code" });
    const model = modelValue.trim();
    const effort = effortValue.trim();
    if (model) params.set("model", model);
    if (effort) params.set("effort", effort);
    void pyFetch<HarnessConfigResponse>(`/agent-harness/config?${params.toString()}`, { signal: controller.signal })
      .then((response) => {
        if (sequence === claudeStatusSequence.current) setClaudeStatus(response.claude_code);
      })
      .catch((reason) => {
        if (controller.signal.aborted || sequence !== claudeStatusSequence.current) return;
        setError(reason instanceof Error ? reason.message : "Claude Codeの状態を取得できませんでした");
      });
  }, []);

  useEffect(() => () => { claudeStatusAbort.current?.abort(); }, []);

  useEffect(() => {
    if (!isClaudeRunner(draft.runner)) {
      claudeStatusAbort.current?.abort();
      setClaudeStatus(undefined);
      return;
    }
    const timer = window.setTimeout(
      () => refreshClaudeStatus(draft.claude.model, draft.claude.reasoning_effort),
      250,
    );
    return () => window.clearTimeout(timer);
  }, [draft.runner, draft.claude.model, draft.claude.reasoning_effort, refreshClaudeStatus]);

  useEffect(() => {
    let active = true;
    void pyFetch<HarnessConfigResponse>("/agent-harness/config")
      .then((response) => {
        if (!active) return;
        setDraft((current) => fromResponse(current, response.settings));
        setClaudeStatus(response.claude_code);
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
      // Send each runner's own branch; switching runners never rewrites the
      // other runner's model/effort.
      const response = await pyFetch<HarnessConfigResponse>("/agent-harness/config", {
        method: "PUT",
        body: JSON.stringify({
          settings: {
            enabled: draft.enabled,
            auto_start: draft.auto_start,
            runner: draft.runner,
            max_concurrent_agents: draft.max_concurrent_agents,
            codex: { model: draft.codex.model.trim(), reasoning_effort: draft.codex.reasoning_effort },
            claude: {
              model: draft.claude.model.trim(),
              reasoning_effort: draft.claude.reasoning_effort,
              permission_mode: draft.claude.permission_mode,
            },
          },
        }),
      });
      if (response?.settings) setDraft((current) => fromResponse(current, response.settings));
      setMessage("自律タスク実行設定を保存しました。実行中のハーネスへはサーバー再起動後に反映されます。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "自律タスク実行設定の保存に失敗しました");
    } finally {
      setSaving(false);
    }
  };

  const claude = isClaudeRunner(draft.runner);
  const codex = isCodexRunner(draft.runner);
  const updateCodex = (patch: Partial<RunnerModelSettings>) =>
    setDraft((current) => ({ ...current, codex: { ...current.codex, ...patch } }));
  const updateClaude = (patch: Partial<ClaudeRunnerSettings>) =>
    setDraft((current) => ({ ...current, claude: { ...current.claude, ...patch } }));

  return (
    <SettingsDisclosure
      title="自律タスク実行"
      icon={<Workflow className="size-4" />}
      targetId="autonomous-task-execution"
      summary={<Badge variant={draft.enabled ? "secondary" : "outline"}>{draft.enabled ? "有効" : "無効"}</Badge>}
      help="Execution Team topologyとは独立して、バックグラウンドタスクの実行方法を管理します。モデル設定は実行方式ごとに保存されます。"
    >
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
          <AppSelect aria-label="自律タスク実行の実行方式" value={draft.runner} onChange={(event) => {
            const runner = event.target.value;
            setDraft((current) => ({ ...current, runner }));
          }} disabled={saving} className="h-8 w-full">
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

      {codex && (
        <div className="grid gap-3 rounded-md border p-3 md:grid-cols-2" data-testid="harness-codex-settings">
          <p className="text-xs font-medium md:col-span-2">Codex CLI のモデル</p>
          <label className="space-y-1 text-xs">
            <span>モデル（空欄でCodex CLI既定）</span>
            <Input aria-label="Codex CLIのモデル" list="harness-codex-models" value={draft.codex.model} onChange={(event) => updateCodex({ model: event.target.value })} disabled={saving} className="h-8" placeholder="既定" />
            <datalist id="harness-codex-models">
              {CODEX_MODEL_SUGGESTIONS.map((model) => <option key={model} value={model} />)}
            </datalist>
          </label>
          <label className="space-y-1 text-xs">
            <span>推論effort</span>
            <AppSelect aria-label="Codex CLIの推論effort" value={draft.codex.reasoning_effort} onChange={(event) => updateCodex({ reasoning_effort: event.target.value })} disabled={saving} className="h-8 w-full">
              <option value="">既定</option>
              {withCurrent(CODEX_EFFORT_OPTIONS, draft.codex.reasoning_effort).map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </AppSelect>
          </label>
        </div>
      )}

      {claude && (
        <div className="grid gap-3 rounded-md border p-3 md:grid-cols-2" data-testid="harness-claude-settings">
          <p className="inline-flex items-center gap-1 text-xs font-medium md:col-span-2">
            Claude Code のモデル
            <HelpPopover label="Claude Code のモデル">
              <p>`opus` は Claude Code 側で最新Opus（Anthropic APIでは Opus 5.5）を指します。Opus 5.5 には Claude Code v2.1.280 以降が必要です。</p>
            </HelpPopover>
          </p>
          <label className="space-y-1 text-xs">
            <span>モデル（alias または フルモデル名。空欄でClaude Code既定）</span>
            <Input aria-label="Claude Codeのモデル" list="harness-claude-models" value={draft.claude.model} onChange={(event) => updateClaude({ model: event.target.value })} disabled={saving} className="h-8" placeholder="既定（例: opus）" />
            <datalist id="harness-claude-models">
              {CLAUDE_MODEL_SUGGESTIONS.map((model) => <option key={model} value={model} />)}
            </datalist>
          </label>
          <label className="space-y-1 text-xs">
            <span>推論effort</span>
            <AppSelect aria-label="Claude Codeの推論effort" value={draft.claude.reasoning_effort} onChange={(event) => updateClaude({ reasoning_effort: event.target.value })} disabled={saving} className="h-8 w-full">
              <option value="">既定</option>
              {withCurrent(CLAUDE_EFFORT_OPTIONS, draft.claude.reasoning_effort).map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </AppSelect>
          </label>
          <label className="space-y-1 text-xs md:col-span-2">
            <span>権限モード</span>
            <AppSelect aria-label="Claude Codeの権限モード" value={draft.claude.permission_mode} onChange={(event) => updateClaude({ permission_mode: event.target.value })} disabled={saving} className="h-8 w-full">
              {CLAUDE_PERMISSION_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </AppSelect>
          </label>
          {claudeStatus && (
            <div className="space-y-1 text-xs md:col-span-2" data-testid="harness-claude-status">
              <p className="text-muted-foreground">
                検出した Claude Code: {claudeStatus.installed ? (claudeStatus.version ?? "バージョン不明") : "未検出"}
              </p>
              {(claudeStatus.warnings ?? []).map((warning) => (
                <p key={warning} role="alert" className="rounded border border-amber-500/40 p-2 text-amber-700 dark:text-amber-300">{warning}</p>
              ))}
            </div>
          )}
        </div>
      )}

      <Button type="button" size="sm" variant="outline" onClick={() => void save()} disabled={saving}>
        {saving ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}
        自律タスク実行を保存
      </Button>
    </SettingsDisclosure>
  );
}
