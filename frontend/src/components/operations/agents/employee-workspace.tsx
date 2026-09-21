"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { mediaOperationsApi } from "@/lib/media-operations-api";
import { employeeApi, employeeKey, employeePath, employeeRequest, EmployeeApiError, type Employee, type EmployeeCatalog } from "@/lib/agent-employees-api";
import { OperationsCommandCenter } from "../operations-command-center";
import { Button } from "@/components/ui/button";
import { EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";
import { EmployeeRole } from "./employee-role";
import { EmployeeAssignments } from "./employee-assignments";
import { EmployeeAutomation } from "./employee-automation";
import { EmployeePolicies } from "./employee-policies";
import { EmployeeIntegrations, EmployeePhone } from "./employee-integrations";
import { EmployeeLiveInfo, type EmployeeSnapshot } from "./employee-observability";

export const employeePanels = [ ["overview", "概要・有効化"], ["role", "職務・モデル"], ["assignments", "組織・所属"], ["automation", "自動化"], ["integrations", "連携・認証情報"], ["phone", "電話受付"], ["activity", "活動・要確認"] ] as const;
export type EmployeePanel = typeof employeePanels[number][0];
export function employeeHref(agent: string | null, panel: EmployeePanel = "overview") {
  const params = new URLSearchParams({ tab: "agents" });
  if (agent) { params.set("agent", agent); params.set("panel", panel); }
  return `/operations?${params}`;
}
export function EmployeeFeatureDisabled({ features }: { features?: Record<string, boolean> }) {
  return <section className="space-y-3 rounded-lg border p-5" data-testid="employee-feature-disabled"><h2 className="text-lg font-semibold">AI社員機能はこの実行環境で無効です</h2>
    <p className="text-sm">virtual_company: {String(features?.virtual_company ?? false)} / autonomous_agent_runtime: {String(features?.autonomous_agent_runtime ?? false)}</p>
    <p className="text-sm">Personal / custom 環境では、起動設定で FEATURE_VIRTUAL_COMPANY=true と FEATURE_AUTONOMOUS_AGENT_RUNTIME=true の両方を指定し、サーバーを再起動してください。設定情報が未取得の場合も編集できません。</p>
    <p className="text-sm">Enterprise profile ではこの機能は禁止されています。フラグを設定しても制限を解除できません。</p>
  </section>;
}
export function EmployeeWorkspace() {
  const router = useRouter();
  const query = useSearchParams();
  const runtime = useOptionalRuntimeContext();
  const features = runtime?.runtimeFeatures?.application_features;
  const enabled = features?.virtual_company === true && features?.autonomous_agent_runtime === true;
  const employees = useSWR(enabled ? "employee-list" : null, employeeApi.list, { shouldRetryOnError: false });
  const catalog = useSWR(enabled ? "employee-catalog" : null, employeeApi.catalog, { shouldRetryOnError: false });
  const snapshot = useSWR(enabled ? "employee-live-snapshot" : null, () => employeeRequest<EmployeeSnapshot>("/api/operations/command-center"), { shouldRetryOnError: false });
  const selected = query?.get("agent") ?? null;
  const requested = query?.get("panel");
  const panel = employeePanels.some(([id]) => id === requested) ? requested as EmployeePanel : "overview";
  const [creating, setCreating] = useState(false);
  const canManage = catalog.data?.can_manage === true;
  const navigate = (id: string | null, next: EmployeePanel = "overview") => router.push(employeeHref(id, next), { scroll: false });
  if (!enabled) return <EmployeeFeatureDisabled features={features} />;
  return <div className="space-y-5" data-testid="employee-workspace"><header className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-2xl font-semibold">AI社員</h1><p className="mt-1 text-sm text-muted-foreground">下書きを作成し、職務・所属・自動化を設定してから有効化します。</p></div>
    <div className="flex gap-2"><Button type="button" variant="outline" onClick={() => { void employees.mutate(); void catalog.mutate(); void snapshot.mutate(); }}>一覧を更新</Button>{canManage && <Button type="button" onClick={() => setCreating(true)}>＋ AI社員を追加</Button>}</div></header>
    <EmployeeNotice error={employees.error} reload={() => void employees.mutate()} />
    <EmployeeNotice error={snapshot.error} reload={() => void snapshot.mutate()} />
    {catalog.error?.status === 403 ? <p className="text-sm">社員の管理には管理者権限が必要です。状態の表示のみ利用できます。</p> : <EmployeeNotice error={catalog.error} reload={() => void catalog.mutate()} />}
    <div className="flex flex-wrap gap-3 text-sm">{(["active", "paused", "draft", "retired"] as const).map(state => <span key={state}><EmployeeStatus state={state} /> {employees.data?.agents.filter(a => a.state === state).length ?? 0}</span>)}</div>
    <p className="text-sm">作業中: {snapshot.data?.summary.working ?? "未取得"} / 承認待ち: {snapshot.data?.summary.awaiting_approval ?? "未取得"} / 要確認: {snapshot.data?.summary.uncertain ?? "未取得"}</p>
    {creating && canManage && <EmployeeCreate onCreated={async id => { await employees.mutate(); setCreating(false); navigate(id, "role"); }} onClose={() => setCreating(false)} />}
    <div className="grid items-start gap-5 xl:grid-cols-[17rem_minmax(0,1fr)]"><aside aria-label="AI社員一覧" className="space-y-2">
      {employees.isLoading && <p>AI社員を読み込み中…</p>}{employees.data?.agents.map(agent => <button type="button" key={agent.id} className={`w-full space-y-2 rounded-lg border p-3 text-left ${selected === agent.id ? "border-primary bg-primary/5" : "bg-card"}`} onClick={() => navigate(agent.id)} aria-pressed={selected === agent.id}>
        <span className="block font-medium">{agent.display_name}</span><EmployeeStatus state={agent.state} />{agent.state === "draft" && <span className="block text-xs text-primary">下書きの設定を再開</span>}<EmployeeLiveInfo projection={snapshot.data?.agents.find(a => a.id === agent.id)} /></button>)}
      {!employees.isLoading && !employees.error && !employees.data?.agents.length && <p className="text-sm text-muted-foreground">AI社員はまだ登録されていません。</p>}
    </aside><div className="min-w-0 rounded-lg border bg-card p-4 sm:p-5">
      {selected ? <EmployeeDetail key={selected} id={selected} panel={panel} catalog={catalog.data} canManage={canManage} navigate={next => navigate(selected, next)} onChanged={() => Promise.all([employees.mutate(), snapshot.mutate()])} /> : <p className="text-sm text-muted-foreground">社員を選択すると、職務・権限と実行履歴を確認できます。</p>}
    </div></div>
  </div>;
}
function EmployeeCreate({ onCreated, onClose }: { onCreated: (id: string) => Promise<void>; onClose: () => void }) {
  const [name, setName] = useState(""); const [slug, setSlug] = useState(""); const [job, setJob] = useState(""); const [summary, setSummary] = useState("");
  const [character, setCharacter] = useState(""); const [employment, setEmployment] = useState("active");
  const [key, setKey] = useState(employeeKey); const [draft, setDraft] = useState<Employee | null>(null);
  const [createIntent, setCreateIntent] = useState<Parameters<typeof employeeApi.create>[0] | null>(null);
  const characters = useSWR("employee-characters", () => mediaOperationsApi.listCharacters(), { shouldRetryOnError: false });
  return <section className="space-y-3 rounded-lg border p-4"><h2 className="font-semibold">AI社員を追加 — 1. 基本情報</h2><p className="text-sm">下書き → 職務 → 所属 → 自動化（任意） → 確認・有効化。途中で閉じても保存済みの下書きは一覧から再開できます。</p>
    <EmployeeForm label="下書きを保存して職務設定へ" onSave={async () => {
      const body = createIntent ?? { display_name: name.trim(), ...(slug.trim() ? { slug: slug.trim() } : {}), ...(character ? { character_id: character } : {}), idempotency_key: key };
      let agent = draft;
      if (!agent) {
        setCreateIntent(body);
        try { agent = (await employeeApi.create(body)).agent; }
        catch (error) { if (error instanceof EmployeeApiError && error.status === 422) { setCreateIntent(null); setKey(employeeKey()); } throw error; }
      }
      setDraft(agent); await employeeApi.saveProfile(agent.id, { job_title: job, responsibility_summary: summary, employment_state: employment }); await onCreated(agent.id);
    }}>
      <div className="grid gap-4 md:grid-cols-2"><EmployeeField label="社員名"><input className={employeeInputClass} required maxLength={160} value={name} disabled={!!draft || !!createIntent} onChange={e => setName(e.target.value)} /></EmployeeField>
        <EmployeeField label="slug（任意）"><input className={employeeInputClass} maxLength={100} value={slug} disabled={!!draft || !!createIntent} onChange={e => setSlug(e.target.value)} /></EmployeeField>
        <EmployeeField label="役職"><input className={employeeInputClass} maxLength={160} value={job} onChange={e => setJob(e.target.value)} /></EmployeeField>
        <EmployeeField label="責務の概要"><textarea className={employeeInputClass} maxLength={10000} value={summary} onChange={e => setSummary(e.target.value)} /></EmployeeField>
        <EmployeeSelect label="Character（任意）" value={character} disabled={!!draft || !!createIntent} onChange={setCharacter} options={(characters.data ?? []).map(c => ({ value: c.id, label: c.current_revision?.display_name ?? c.id }))} />
        <EmployeeSelect label="雇用状態" value={employment} required onChange={setEmployment} options={[{ value: "active", label: "在籍" }, { value: "contractor", label: "業務委託" }, { value: "on_leave", label: "休職" }, { value: "suspended", label: "停止" }]} /></div>
    </EmployeeForm><EmployeeNotice error={characters.error} reload={() => void characters.mutate()} />
    {draft && <p className="text-sm">下書き {draft.display_name} は保存済みです。後続の保存に失敗した場合も同じ社員の設定を再開できます。</p>}
    {createIntent && !draft && <p className="text-sm">作成結果を確認中です。基本情報を固定して同じリクエストを再試行します。基本情報を変更する場合は、閉じて一覧を更新し、保存済みの下書きを確認してください。</p>}
    <Button type="button" variant="ghost" onClick={onClose}>閉じる</Button>
  </section>;
}
function EmployeeDetail({ id, panel, catalog, canManage, navigate, onChanged }: { id: string; panel: EmployeePanel; catalog?: EmployeeCatalog; canManage: boolean; navigate: (panel: EmployeePanel) => void; onChanged: () => Promise<unknown> }) {
  const agent = useSWR(["employee", id], () => employeeApi.get(id), { shouldRetryOnError: false });
  const revisions = useSWR(["employee-revisions", id], () => employeeApi.revisions(id), { shouldRetryOnError: false });
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false);
  const current = [...(revisions.data?.revisions ?? [])].sort((a, b) => b.version - a.version)[0];
  const authority = useSWR(canManage && panel === "overview" ? ["employee-authority", id, current?.id] : null, () => employeeRequest<{ authority: { allowed?: boolean; reason?: string; reason_code?: string; effective_capabilities?: string[]; reasons?: string[] } }>(`${employeePath(id)}/effective-authority${current ? `?revision_id=${encodeURIComponent(current.id)}` : ""}`), { shouldRetryOnError: false });
  const reload = async () => { await Promise.all([agent.mutate(), revisions.mutate(), authority.mutate(), onChanged()]); };
  async function transition(state: Employee["state"]) { if (!agent.data) return; setError(null); setBusy(true); try { await employeeApi.state(id, state, agent.data.agent.state); await reload(); } catch (e) { setError(e); } finally { setBusy(false); } }
  if (agent.isLoading) return <p>社員情報を読み込み中…</p>;
  if (!agent.data) return <EmployeeNotice error={agent.error} reload={() => void agent.mutate()} />;
  const mutable = canManage && agent.data.agent.state !== "retired";
  return <div className="space-y-4"><header><h2 className="text-xl font-semibold">{agent.data.agent.display_name} <EmployeeStatus state={agent.data.agent.state} /></h2><p className="mt-1 break-all text-xs text-muted-foreground">Agent ID: {id} · 職務 {current ? `v${current.version}` : "未設定"}</p></header>
    <nav aria-label="AI社員の設定" className="flex flex-wrap gap-2">{employeePanels.map(([value, label]) => <Button key={value} type="button" size="sm" variant={panel === value ? "default" : "outline"} aria-current={panel === value ? "page" : undefined} onClick={() => navigate(value)}>{label}</Button>)}</nav>
    <EmployeeNotice error={agent.error || revisions.error || error} reload={() => void reload()} />
    {panel === "overview" && <><p className="text-sm">設定手順: 職務 → 組織・所属 → 自動化・連携 → 有効化。下書きは自動で有効化されません。</p>
      {!current && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">職務 revision が未設定です。「職務・モデル」で作成してください。</p>}
      <EmployeeNotice error={authority.error} reload={() => void authority.mutate()} />
      {authority.data && <div className="space-y-1 rounded border p-3 text-sm"><p>実効権限: {authority.data.authority.allowed === true ? "許可" : "制限あり"}</p><p>{authority.data.authority.reason_code || authority.data.authority.reason || authority.data.authority.reasons?.join(" / ")}</p><p>組織・所属・連携と参照 revision は、仕事の開始時にも再検証されます。</p></div>}
      {canManage && <div className="flex flex-wrap gap-2"><Button type="button" disabled={!mutable || busy || !current || agent.data.agent.state === "active"} onClick={() => void transition("active")}>社員を有効化</Button><Button type="button" variant="outline" disabled={!mutable || busy || agent.data.agent.state !== "active"} onClick={() => void transition("paused")}>社員を一時停止</Button><Button type="button" variant="outline" disabled={!mutable || busy} onClick={() => { if (window.confirm("この社員を退職にします。この操作は取り消せず、再有効化できません。履歴は保持されます。続行しますか？")) void transition("retired"); }}>退職</Button></div>}
      <OperationsCommandCenter key={agent.data.agent.state} view="agents" agentId={id} /></>}
    {panel === "role" && catalog && !revisions.isLoading && <EmployeeRole key={current?.id ?? "first"} agent={agent.data.agent} revisions={revisions.data?.revisions ?? []} catalog={catalog} onSaved={() => void reload()} />}
    {panel === "role" && !catalog && <p className="text-sm">管理者向けの職務カタログを取得できません。</p>}
    {panel === "assignments" && <EmployeeAssignments agentId={id} canManage={mutable} />}
    {panel === "automation" && <>{canManage ? <><EmployeeAutomation agentId={id} revisionId={current?.id ?? null} canManage={mutable} /><EmployeePolicies agentId={id} canManage={mutable} /></> : <p>自動化の管理には管理者権限が必要です。</p>}</>}
    {panel === "integrations" && <EmployeeIntegrations agentId={id} revisionId={current?.id ?? null} canManage={mutable} />}
    {panel === "phone" && <EmployeePhone agentId={id} revisionId={current?.id ?? null} canManage={mutable} />}
    {panel === "activity" && <><p className="rounded border border-amber-500/40 p-3 text-sm">要確認の操作は結果を照合するまで再実行できません。注文完了は、提供元の Receipt が存在する場合だけ確認できます。</p><p className="text-sm text-muted-foreground">トリガー → WorkItem → AgentRun → 操作提案 → 人間承認／範囲内ポリシー → 試行 → Receipt／要確認。各記録の参照先から根拠を確認します。</p><OperationsCommandCenter view="activity" agentId={id} /></>}
  </div>;
}
