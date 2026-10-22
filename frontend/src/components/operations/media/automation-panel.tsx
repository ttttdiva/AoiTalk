"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Bot, Copy, ExternalLink, Loader2, Play, RefreshCw, Save, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { AppSelect } from "@/components/ui/app-select";
import { mediaGenerationApi, type GenerationWorkspace } from "@/lib/media-operations-generation-api";
import { mediaResearchApi, type ResearchRoutine } from "@/lib/media-operations-research-api";
import {
  mediaAutomationApi,
  type AutomationCandidate,
  type AutomationExecutionMode,
  type AutomationProgram,
  type AutomationRevisionInput,
  type AutomationRun,
  type PresetCatalogItem,
} from "@/lib/media-operations-automation-api";

function key(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return `${prefix}-${crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}
function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function lines(value: unknown): string[] {
  return array(value).map((item) => text(item)).filter(Boolean);
}
function readable(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Automation APIでエラーが発生しました";
}
function stateLabel(value: string): string {
  return ({
    scheduled: "予定", theme_discovery: "Theme取得", research: "Research", brief: "Brief",
    concept_planning: "Concept作成", prompt_planning: "Prompt作成", waiting_review: "レビュー待ち",
    generation_submitting: "生成送信中", generation_running: "生成中", complete: "完了",
    failed: "失敗", uncertain: "要照合",
  } as Record<string, string>)[value] ?? value;
}

function Field({ label, help, children }: { label: string; help?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5 text-xs font-medium text-foreground">
      <div className="inline-flex items-center gap-1">
        {label}
        {help ? <HelpPopover label={label}>{help}</HelpPopover> : null}
      </div>
      <label className="block">
        <span className="sr-only">{label}</span>
        {children}
      </label>
    </div>
  );
}
function Status({ value }: { value: string }) {
  return <span className="inline-flex rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">{stateLabel(value)}</span>;
}

type FormState = {
  name: string;
  mode: AutomationExecutionMode;
  triggerType: string;
  dedupePeriod: string;
  schedule: string;
  assignedAgentId: string;
  discoveryType: string;
  theme: string;
  discoveryUrl: string;
  discoveryQuery: string;
  researchRoutineId: string;
  researchRoutineVersion: string;
  fetchPages: boolean;
  novelty: string;
  candidateCount: string;
  maxGenerationRetries: string;
  maxActiveGenerations: string;
  workspaceId: string;
  presetId: string;
  revisionPolicy: string;
  pinnedRevisionId: string;
  fallbackTheme: string;
  fallbackResearch: string;
};

const blank: FormState = {
  name: "", mode: "review_before_generate", triggerType: "manual", dedupePeriod: "daily", schedule: "", assignedAgentId: "",
  discoveryType: "manual", theme: "", discoveryUrl: "", discoveryQuery: "", researchRoutineId: "",
  researchRoutineVersion: "", fetchPages: true, novelty: "balanced", candidateCount: "3", maxGenerationRetries: "1", maxActiveGenerations: "1", workspaceId: "",
  presetId: "", revisionPolicy: "latest_at_submission", pinnedRevisionId: "", fallbackTheme: "",
  fallbackResearch: "observation_only",
};

function formFromProgram(program: AutomationProgram): FormState {
  const rev = program.current_revision;
  const trigger = rev.trigger ?? {};
  const discovery = rev.discovery ?? {};
  const research = rev.research_binding ?? {};
  const planning = rev.planning_policy ?? {};
  const generation = rev.generation_action ?? {};
  const fallback = rev.fallback ?? {};
  return {
    name: program.name,
    mode: rev.execution_mode,
    triggerType: text(trigger.type) || "manual",
    dedupePeriod: text(trigger.dedupe_period) || "daily",
    schedule: text(trigger.schedule),
    assignedAgentId: text(trigger.assigned_agent_id),
    discoveryType: text(discovery.type) || "manual",
    theme: text(discovery.theme),
    discoveryUrl: text(discovery.url),
    discoveryQuery: text(discovery.query) || text(discovery.theme_query),
    researchRoutineId: text(research.research_routine_id),
    researchRoutineVersion: text(research.routine_version),
    fetchPages: research.fetch_pages !== false,
    novelty: text(planning.novelty_policy) || "balanced",
    candidateCount: text(planning.candidate_count) || "3",
    maxGenerationRetries: text(planning.max_generation_retries ?? 1),
    maxActiveGenerations: text(planning.max_active_generations ?? 1),
    workspaceId: text(generation.studio_workspace_binding_id),
    presetId: text(generation.preset_id),
    revisionPolicy: text(generation.preset_revision_policy) || "latest_at_submission",
    pinnedRevisionId: text(generation.preset_revision_id),
    fallbackTheme: text(fallback.theme) || text(fallback.manual_theme),
    fallbackResearch: text(fallback.research_policy) || "observation_only",
  };
}

function revisionInput(form: FormState): AutomationRevisionInput {
  const discovery: Record<string, unknown> = { type: form.discoveryType };
  if (form.discoveryType === "manual") discovery.theme = form.theme.trim();
  else if (form.discoveryType === "web_search") discovery.query = form.discoveryQuery.trim();
  else if (form.discoveryType === "rss" || form.discoveryType === "json_feed") discovery.url = form.discoveryUrl.trim();
  else if (form.discoveryType === "local_pool") discovery.themes = form.theme.split(/\n/u).map((item) => item.trim()).filter(Boolean);
  const research: Record<string, unknown> = {};
  if (form.researchRoutineId) {
    research.research_routine_id = form.researchRoutineId;
    research.routine_version = Math.max(1, Number(form.researchRoutineVersion) || 1);
    research.fetch_pages = form.fetchPages;
  }
  const generation: Record<string, unknown> = {};
  if (form.mode === "review_before_generate" || form.mode === "auto_generate") {
    generation.kind = "image";
    generation.studio_workspace_binding_id = form.workspaceId;
    generation.preset_id = form.presetId;
    generation.preset_revision_policy = form.revisionPolicy;
    if (form.revisionPolicy === "pinned") generation.preset_revision_id = form.pinnedRevisionId;
  }
  return {
    execution_mode: form.mode,
    trigger: { type: form.triggerType, dedupe_period: form.dedupePeriod, schedule: form.schedule.trim() || undefined, assigned_agent_id: form.assignedAgentId.trim() || undefined },
    discovery,
    research_binding: research,
    planning_policy: { novelty_policy: form.novelty, candidate_count: Math.max(1, Number(form.candidateCount) || 3), max_generation_retries: Number(form.maxGenerationRetries), max_active_generations: Number(form.maxActiveGenerations) },
    generation_action: generation,
    fallback: { theme: form.fallbackTheme.trim() || undefined, research_policy: form.fallbackResearch || undefined },
  };
}

export function MediaAutomationPanel() {
  const [programs, setPrograms] = useState<AutomationProgram[]>([]);
  const [selectedProgramId, setSelectedProgramId] = useState("");
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [form, setForm] = useState<FormState>(blank);
  const [workspaces, setWorkspaces] = useState<GenerationWorkspace[]>([]);
  const [routines, setRoutines] = useState<ResearchRoutine[]>([]);
  const [presets, setPresets] = useState<PresetCatalogItem[]>([]);
  const [presetError, setPresetError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const selectedProgram = useMemo(() => programs.find((item) => item.id === selectedProgramId) ?? null, [programs, selectedProgramId]);
  const selectedRun = useMemo(() => runs.find((item) => item.id === selectedRunId) ?? selectedProgram?.latest_run ?? null, [runs, selectedRunId, selectedProgram]);
  const selectedWorkspace = useMemo(() => workspaces.find((item) => item.id === form.workspaceId) ?? null, [workspaces, form.workspaceId]);

  const load = useCallback(async () => {
    setBusy("load"); setError(null);
    try {
      const [nextPrograms, nextWorkspaces, nextRoutines] = await Promise.all([
        mediaAutomationApi.listPrograms(), mediaGenerationApi.listWorkspaces(), mediaResearchApi.listRoutines(),
      ]);
      setPrograms(nextPrograms); setWorkspaces(nextWorkspaces); setRoutines(nextRoutines);
      setSelectedProgramId((current) => nextPrograms.some((item) => item.id === current) ? current : nextPrograms[0]?.id ?? "");
    } catch (next) { setError(next); }
    finally { setBusy(null); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!selectedProgram) { setRuns([]); setSelectedRunId(""); return; }
    setForm(formFromProgram(selectedProgram));
    void mediaAutomationApi.listRuns(selectedProgram.id).then((next) => {
      setRuns(next); setSelectedRunId((current) => next.some((item) => item.id === current) ? current : next[0]?.id ?? "");
    }).catch(setError);
  }, [selectedProgram]);
  useEffect(() => {
    setPresetError("");
    if (!form.workspaceId) { setPresets([]); return; }
    let active = true;
    void mediaAutomationApi.presetCatalog(form.workspaceId)
      .then((next) => { if (active) setPresets(next); })
      .catch((next) => { if (active) { setPresets([]); setPresetError(readable(next)); } });
    return () => { active = false; };
  }, [form.workspaceId]);

  const update = <K extends keyof FormState>(name: K, value: FormState[K]) => setForm((current) => ({ ...current, [name]: value }));
  const refreshProgram = async (programId: string, runId?: string) => {
    const [program, nextRuns] = await Promise.all([mediaAutomationApi.getProgram(programId), mediaAutomationApi.listRuns(programId)]);
    setPrograms((current) => current.map((item) => item.id === programId ? program : item)); setRuns(nextRuns);
    if (runId) setSelectedRunId(runId);
  };

  async function createProgram() {
    setBusy("create"); setError(null);
    try {
      const created = await mediaAutomationApi.createProgram({ name: form.name.trim(), enabled: true, ...revisionInput(form) }, key("automation-create"));
      setPrograms((current) => [created, ...current]); setSelectedProgramId(created.id); toast.success("Automationを作成しました");
    } catch (next) { setError(next); } finally { setBusy(null); }
  }
  async function saveRevision() {
    if (!selectedProgram) return;
    setBusy("save"); setError(null);
    try {
      await mediaAutomationApi.appendRevision(selectedProgram.id, { expected_version: selectedProgram.current_revision.version, ...revisionInput(form) }, key("automation-revision"));
      await refreshProgram(selectedProgram.id); toast.success("Recipe revisionを保存しました");
    } catch (next) { setError(next); } finally { setBusy(null); }
  }
  async function runNow() {
    if (!selectedProgram) return;
    setBusy("run"); setError(null);
    try {
      const run = await mediaAutomationApi.trigger(selectedProgram.id, key("manual-run"), "manual");
      await refreshProgram(selectedProgram.id, run.id); toast.success(`Run ${stateLabel(run.state)}`);
    } catch (next) { setError(next); } finally { setBusy(null); }
  }

  const runAction = async (name: string, action: () => Promise<AutomationRun>) => {
    if (!selectedProgram) return;
    setBusy(name); setError(null);
    try { const run = await action(); await refreshProgram(selectedProgram.id, run.id); }
    catch (next) { setError(next); } finally { setBusy(null); }
  };

  const submission = selectedRun?.novelty_snapshot?.generation_submission;
  const retryLedger = submission && typeof submission === "object" && !Array.isArray(submission) ? submission as Record<string, unknown> : null;
  const attemptCount = typeof retryLedger?.attempt_count === "number" ? retryLedger.attempt_count : null;
  const retryLimit = typeof retryLedger?.max_generation_retries === "number" ? retryLedger.max_generation_retries : null;
  const retryBlocked = attemptCount === null || retryLimit === null || attemptCount >= 1 + retryLimit;
  const outputIds = selectedRun ? lines(selectedRun.generation_result?.output_asset_ids) : [];
  const sourceRefs = selectedRun ? lines(selectedRun.research_brief?.source_refs) : [];
  const keyFacts = selectedRun ? lines(selectedRun.research_brief?.key_facts) : [];
  const unverifiedSignals = selectedRun ? lines(selectedRun.research_brief?.unverified_signals) : [];
  const researchStatus = text(selectedRun?.research_brief?.research_status);
  const researchStatusLabel = ({ sources_only: "出典候補を取得・主張は未検証", empty: "該当候補なし", partial: "一部の取得に失敗", not_executed: "調査未実行・入力情報のみ" } as Record<string, string>)[researchStatus] || researchStatus;
  const studioLink = selectedRun?.result_deep_link
    ? selectedRun.result_deep_link.startsWith("http") ? selectedRun.result_deep_link : `${text(selectedWorkspace?.base_url).replace(/\/$/u, "")}${selectedRun.result_deep_link}`
    : null;

  return (
    <div className="space-y-4" data-testid="media-automation-panel">
      {error ? <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{readable(error)}</div> : null}
      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card size="sm">
          <CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-2 text-sm"><Bot className="size-4" /><span className="inline-flex items-center gap-1">Automations<HelpPopover label="Automations">41 AoiTalkがworkflow ownerです。</HelpPopover></span></CardTitle></CardHeader>
          <CardContent className="space-y-3 pt-3">
            <Button size="sm" variant="outline" className="w-full" onClick={() => { setSelectedProgramId(""); setForm(blank); }}><Sparkles className="size-3.5" /> 新規Automation</Button>
            {programs.map((program) => <button type="button" key={program.id} onClick={() => setSelectedProgramId(program.id)} className={`w-full rounded-md border p-3 text-left text-sm ${selectedProgramId === program.id ? "border-primary bg-primary/5" : "border-border hover:bg-muted/40"}`}><div className="flex items-center justify-between gap-2"><span className="font-medium">{program.name}</span><span className="text-[10px] text-muted-foreground">r{program.current_revision.version}</span></div><div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground"><span>{program.current_revision.execution_mode}</span><span>{program.enabled ? "Enabled" : "Disabled"}</span></div>{program.latest_run ? <div className="mt-2"><Status value={program.latest_run.state} /></div> : null}</button>)}
            {!programs.length && busy !== "load" ? <p className="py-6 text-center text-xs text-muted-foreground">Automationはまだありません。</p> : null}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card size="sm">
            <CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Automation Editor<HelpPopover label="Automation Editor">Presetの中身は73に保持し、ここでは参照とcreative intentだけを設定します。</HelpPopover></CardTitle></CardHeader>
            <CardContent className="space-y-4 pt-4">
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <Field label="名前"><Input value={form.name} onChange={(event) => update("name", event.target.value)} disabled={Boolean(selectedProgram)} /></Field>
                <Field label="Execution mode"><AppSelect value={form.mode} onValueChange={(value) => update("mode", value as AutomationExecutionMode)}><option value="research_only">Research only</option><option value="draft">Draft</option><option value="review_before_generate">Review before generate</option><option value="auto_generate">Auto generate</option></AppSelect></Field>
                <Field label="Trigger"><AppSelect value={form.triggerType} onValueChange={(value) => update("triggerType", value)}><option value="manual">Manual / Run Now</option><option value="windows_task_scheduler">Windows Task Scheduler</option><option value="external">External trigger</option><option value="heartbeat">AoiTalk heartbeat</option></AppSelect></Field>
                <Field label="重複抑止の時間粒度"><AppSelect value={form.dedupePeriod} onValueChange={(value) => update("dedupePeriod", value)}><option value="daily">Daily</option><option value="hourly">Hourly</option><option value="minute">Minute</option></AppSelect></Field>
                <Field label="Schedule label / cron（任意）"><Input value={form.schedule} onChange={(event) => update("schedule", event.target.value)} placeholder="07:00 daily / external schedule note" /></Field>
                <Field label="Agent ID（heartbeat時）"><Input value={form.assignedAgentId} onChange={(event) => update("assignedAgentId", event.target.value)} placeholder="Agent assignmentがある場合のみ" /></Field>
                <Field label="Discovery"><AppSelect value={form.discoveryType} onValueChange={(value) => update("discoveryType", value)}><option value="manual">Manual theme</option><option value="local_pool">Local theme pool</option><option value="calendar">Calendar / seasonal</option><option value="web_search">Web Search</option><option value="rss">RSS / Atom</option><option value="json_feed">JSON feed</option></AppSelect></Field>
              </div>
              {form.discoveryType === "manual" || form.discoveryType === "local_pool" ? <Field label={form.discoveryType === "local_pool" ? "Theme pool（1行1件）" : "Theme"}>{form.discoveryType === "local_pool" ? <Textarea value={form.theme} onChange={(event) => update("theme", event.target.value)} /> : <Input value={form.theme} onChange={(event) => update("theme", event.target.value)} />}</Field> : null}
              {form.discoveryType === "web_search" ? <Field label="Theme discovery query"><Input value={form.discoveryQuery} onChange={(event) => update("discoveryQuery", event.target.value)} /></Field> : null}
              {form.discoveryType === "rss" || form.discoveryType === "json_feed" ? <Field label="Feed URL"><Input value={form.discoveryUrl} onChange={(event) => update("discoveryUrl", event.target.value)} /></Field> : null}

              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <Field label="Research Routine"><AppSelect value={form.researchRoutineId} onValueChange={(value) => { update("researchRoutineId", value); const found = routines.find((item) => item.id === value); update("researchRoutineVersion", found ? String(found.current_revision?.version ?? 1) : ""); }}><option value="">Themeのみ（Researchなし）</option>{routines.map((routine) => <option key={routine.id} value={routine.id}>{text(routine.current_revision?.name) || routine.id}</option>)}</AppSelect></Field>
                <Field label="Research revision"><Input value={form.researchRoutineVersion} onChange={(event) => update("researchRoutineVersion", event.target.value)} disabled={!form.researchRoutineId} /></Field>
                <Field label="Novelty"><AppSelect value={form.novelty} onValueChange={(value) => update("novelty", value)}><option value="conservative">Conservative</option><option value="balanced">Balanced</option><option value="exploratory">Exploratory</option></AppSelect></Field>
                <Field label="Concept数"><Input type="number" min={1} max={12} value={form.candidateCount} onChange={(event) => update("candidateCount", event.target.value)} /></Field>
              </div>

              {(form.mode === "review_before_generate" || form.mode === "auto_generate") ? <div className="rounded-md border border-border p-3"><p className="mb-3 text-xs font-semibold">Generation Studio binding</p><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4"><Field label="Workspace"><AppSelect value={form.workspaceId} onValueChange={(value) => { update("workspaceId", value); update("presetId", ""); }}><option value="">選択してください</option>{workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.external_workspace_id} · {workspace.status}</option>)}</AppSelect></Field><Field label="Preset"><AppSelect value={form.presetId} onValueChange={(value) => { update("presetId", value); const preset = presets.find((item) => item.preset_id === value); if (preset) update("pinnedRevisionId", preset.current_revision_id); }}><option value="">選択してください</option>{presets.map((preset) => <option key={preset.preset_id} value={preset.preset_id}>{preset.name} · r{preset.current_revision}</option>)}</AppSelect></Field><Field label="Revision policy"><AppSelect value={form.revisionPolicy} onValueChange={(value) => update("revisionPolicy", value)}><option value="latest_at_submission">Latest at submission</option><option value="pinned">Pinned</option></AppSelect></Field><Field label="Pinned revision ID"><Input value={form.pinnedRevisionId} onChange={(event) => update("pinnedRevisionId", event.target.value)} disabled={form.revisionPolicy !== "pinned"} /></Field></div>{presetError ? <p role="alert" className="mt-2 text-xs text-destructive">{presetError}</p> : null}{selectedWorkspace?.base_url ? <p className="mt-2 text-[11px] text-muted-foreground">Presetの作成・編集はGeneration Studio側で行います: {text(selectedWorkspace.base_url)}</p> : null}</div> : null}

              <Field label="処理中の子生成上限（0–16件）" help="0は新規送信を停止。結果不明の生成も枠を使い、生成先で終了を確認してから空き枠に戻ります。"><Input type="number" min={0} max={16} step={1} value={form.maxActiveGenerations} onChange={(event) => update("maxActiveGenerations", event.target.value)} /></Field>
              <Field label="生成の明示再試行上限（0–10回）" help="初回送信を除く上限です。変更は新しいRunから適用され、結果不明の送信は自動で繰り返しません。"><Input type="number" min={0} max={10} step={1} value={form.maxGenerationRetries} onChange={(event) => update("maxGenerationRetries", event.target.value)} /></Field>
              <div className="grid gap-3 md:grid-cols-2"><Field label="Fallback theme（任意）"><Input value={form.fallbackTheme} onChange={(event) => update("fallbackTheme", event.target.value)} /></Field><Field label="Research fallback"><AppSelect value={form.fallbackResearch} onValueChange={(value) => update("fallbackResearch", value)}><option value="observation_only">Theme observationで継続</option><option value="fail">失敗として停止</option></AppSelect></Field></div>
              <div className="flex flex-wrap justify-end gap-2">
                {selectedProgram ? <><Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void mediaAutomationApi.setEnabled(selectedProgram.id, !selectedProgram.enabled).then(() => refreshProgram(selectedProgram.id)).catch(setError)}>{selectedProgram.enabled ? "Disable" : "Enable"}</Button><Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void mediaAutomationApi.duplicate(selectedProgram.id, `${selectedProgram.name} copy`, key("duplicate")).then(load).catch(setError)}><Copy className="size-3.5" /> Duplicate</Button><Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void saveRevision()}><Save className="size-3.5" /> Save revision</Button><Button size="sm" disabled={busy !== null} onClick={() => void runNow()}><Play className="size-3.5" /> Run Now</Button></> : <Button size="sm" disabled={busy !== null || !form.name.trim()} onClick={() => void createProgram()}><Sparkles className="size-3.5" /> Create</Button>}
              </div>
            </CardContent>
          </Card>

          {selectedProgram ? <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Recipe revision history<HelpPopover label="Recipe revision history">各Runはexact revisionをpinします。</HelpPopover></CardTitle></CardHeader><CardContent className="pt-3"><div className="flex flex-wrap gap-2">{selectedProgram.revisions.map((revision) => <span key={revision.id} className="rounded border border-border px-2 py-1 text-xs">r{revision.version} · {revision.execution_mode} · {revision.content_hash.slice(0, 8)}</span>)}</div></CardContent></Card> : null}

          {selectedProgram ? <Card size="sm"><CardHeader className="border-b border-border/70"><CardTitle className="inline-flex items-center gap-1 text-sm">Daily Board / Run detail<HelpPopover label="Daily Board / Run detail">ResearchからGeneration receiptまで上位provenanceを追跡します。</HelpPopover></CardTitle></CardHeader><CardContent className="space-y-4 pt-4"><div className="flex flex-wrap items-center gap-2"><AppSelect value={selectedRun?.id ?? ""} onValueChange={setSelectedRunId}><option value="">Runを選択</option>{runs.map((run) => <option key={run.id} value={run.id}>{run.created_at?.slice(0, 19) ?? run.id} · {stateLabel(run.state)}</option>)}</AppSelect>{selectedRun ? <Status value={selectedRun.state} /> : null}{(selectedRun?.external_run_id && ["generation_running", "uncertain", "complete", "failed"].includes(selectedRun.state)) ? <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void runAction("reconcile", () => mediaAutomationApi.reconcile(selectedRun.id))}><RefreshCw className="size-3.5" /> Reconcile</Button> : null}{selectedRun?.state === "uncertain" ? <Button size="sm" variant="outline" disabled={busy !== null || retryBlocked} onClick={() => void runAction("retry", () => mediaAutomationApi.retryUncertain(selectedRun.id))}>Explicit retry</Button> : null}</div>
            {selectedRun?.state === "scheduled" && selectedRun.error_code === "generation_capacity_full" ? <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void runAction("resume", () => mediaAutomationApi.resume(selectedRun.id))}>生成枠を再確認して再開</Button> : null}
            {selectedRun?.selected_candidate_id ? <p className="text-xs text-muted-foreground">{attemptCount === null || retryLimit === null ? "過去の生成送信回数が不明です。生成先で照合してください。" : `生成送信 ${attemptCount} 回 · 明示再試行 ${Math.max(0, attemptCount - 1)} / ${retryLimit} 回${retryBlocked ? "（上限到達・送信内容を保持）" : ""}`}</p> : null}
            {selectedRun ? <><div className="grid gap-3 md:grid-cols-2"><div className="rounded-md border border-border p-3"><p className="text-[11px] font-semibold uppercase text-muted-foreground">Theme</p><p className="mt-1 font-medium">{text(selectedRun.observation?.title) || "—"}</p><p className="mt-1 text-xs text-muted-foreground">{text(selectedRun.observation?.description)}</p></div><div className="rounded-md border border-border p-3"><p className="text-[11px] font-semibold uppercase text-muted-foreground">Research</p><p className="mt-1 text-xs">検証済み主張: {keyFacts.slice(0, 5).join(" / ") || "なし"}</p>{unverifiedSignals.length ? <p className="mt-1 text-xs text-muted-foreground">未検証の情報: {unverifiedSignals.slice(0, 5).join(" / ")}</p> : null}{researchStatusLabel ? <p className="mt-1 text-xs text-muted-foreground">{researchStatusLabel}</p> : null}<p className="mt-2 text-[11px] text-muted-foreground">Sources: {sourceRefs.length} · ResearchRun: {selectedRun.research_run_id ?? "—"}</p></div></div>
            <div><div className="mb-2 flex items-center justify-between"><p className="text-xs font-semibold">Concept / Prompt Candidates</p>{selectedRun.state === "waiting_review" ? <Button size="sm" variant="outline" onClick={() => void runAction("regenerate", () => mediaAutomationApi.regenerate(selectedRun.id, key("regenerate")))}><Sparkles className="size-3.5" /> Regenerate</Button> : null}</div><div className="grid gap-3 lg:grid-cols-2">{selectedRun.candidates.map((candidate) => <CandidateCard key={candidate.candidate_id} run={selectedRun} candidate={candidate} busy={busy !== null} onSaved={(payload) => runAction("edit", () => mediaAutomationApi.editCandidate(selectedRun.id, candidate.candidate_id, payload))} onGenerate={() => runAction("approve", () => mediaAutomationApi.approve(selectedRun.id, candidate.candidate_id))} />)}</div></div>
            <div className="rounded-md border border-border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="text-[11px] font-semibold uppercase text-muted-foreground">Generated output</p><p className="mt-1 text-xs">73 Run: {selectedRun.external_run_id ?? "—"} · Preset: {selectedRun.preset_id ?? "—"} / r{selectedRun.preset_revision_number ?? "—"}</p></div>{studioLink ? <a className="inline-flex items-center gap-1 text-xs text-primary hover:underline" href={studioLink} target="_blank" rel="noreferrer">Open in Generation Studio <ExternalLink className="size-3" /></a> : null}</div>{outputIds.length ? <div className="mt-2 flex flex-wrap gap-2">{outputIds.map((asset) => <span key={asset} className="rounded border px-2 py-1 font-mono text-[10px]">{asset}</span>)}</div> : <p className="mt-2 text-xs text-muted-foreground">出力はまだありません。</p>}</div>
            {selectedRun.error_code ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">{selectedRun.error_code}: {selectedRun.error_message}</div> : null}</> : <p className="py-8 text-center text-sm text-muted-foreground">Runを選択してください。</p>}
          </CardContent></Card> : null}
        </div>
      </div>
      {busy === "load" ? <div className="flex justify-center py-4"><Loader2 className="size-5 animate-spin" /></div> : null}
    </div>
  );
}

function CandidateCard({ run, candidate, busy, onSaved, onGenerate }: { run: AutomationRun; candidate: AutomationCandidate; busy: boolean; onSaved: (payload: Record<string, unknown>) => Promise<void>; onGenerate: () => Promise<void> }) {
  const [payload, setPayload] = useState<Record<string, unknown>>(candidate.payload);
  const set = (name: string, value: string) => setPayload((current) => ({ ...current, [name]: value }));
  return <div className="space-y-2 rounded-md border border-border p-3"><div className="flex items-center justify-between gap-2"><p className="font-medium">{text(payload.title) || candidate.candidate_id}</p><span className="text-[10px] text-muted-foreground">{candidate.status}</span></div><p className="text-xs text-muted-foreground">{text(payload.concept)}</p><Field label="Prompt"><Textarea value={text(payload.prompt)} onChange={(event) => set("prompt", event.target.value)} disabled={run.state !== "waiting_review"} /></Field><div className="grid gap-2 sm:grid-cols-2"><Field label="Negative additions"><Input value={text(payload.negative_prompt_additions)} onChange={(event) => set("negative_prompt_additions", event.target.value)} disabled={run.state !== "waiting_review"} /></Field><Field label="Palette"><Input value={text(payload.palette)} onChange={(event) => set("palette", event.target.value)} disabled={run.state !== "waiting_review"} /></Field></div>{run.state === "waiting_review" ? <div className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void onSaved(payload)}><Save className="size-3.5" /> Edit</Button><Button size="sm" disabled={busy} onClick={() => void onGenerate()}><Play className="size-3.5" /> Generate</Button></div> : null}</div>;
}
