"use client";

import { useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { employeeApi, employeeKey, employeePath, employeeRequest, EmployeeApiError, type ConversationCharacter, type Employee, type EmployeeCatalog } from "@/lib/agent-employees-api";
import { OperationsCommandCenter } from "../operations-command-center";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";
import { EmployeeRole } from "./employee-role";
import { EmployeeAssignments } from "./employee-assignments";
import { EmployeeAutomation } from "./employee-automation";
import { EmployeePolicies } from "./employee-policies";
import { EmployeeIntegrations, EmployeePhone } from "./employee-integrations";
import { type EmployeeProjection, type EmployeeSnapshot } from "./employee-observability";
import { EmployeeOrganizationSetup } from "./employee-organization-setup";
import { EmployeeChatActivity } from "./employee-chat-activity";
import { EmployeeDirectory } from "./employee-directory";
import { EmployeeThumbnail, EmployeeThumbnailControl } from "./employee-thumbnail";
import styles from "./employee-workspace.module.css";

export const employeePanels = [ ["overview", "概要・有効化"], ["role", "職務・モデル"], ["assignments", "組織・所属"], ["automation", "自動化"], ["integrations", "連携・認証情報"], ["phone", "電話受付"], ["activity", "活動・要確認"] ] as const;
export type EmployeePanel = typeof employeePanels[number][0];
export function employeeHref(agent: string | null, panel: EmployeePanel = "overview") {
  const params = new URLSearchParams({ tab: "agents" });
  if (agent) { params.set("agent", agent); params.set("panel", panel); }
  return `/operations?${params}`;
}
export function EmployeeFeatureDisabled({ features }: { features?: Record<string, boolean> }) {
  return <section className="space-y-3 rounded-lg border p-5" data-testid="employee-feature-disabled"><h2 className="text-lg font-semibold">Agent機能はこの実行環境で無効です</h2>
    <p className="text-sm">virtual_company: {String(features?.virtual_company ?? false)} / autonomous_agent_runtime: {String(features?.autonomous_agent_runtime ?? false)}</p>
    <p className="text-sm">Personal / custom 環境では、起動設定で FEATURE_VIRTUAL_COMPANY=true と FEATURE_AUTONOMOUS_AGENT_RUNTIME=true の両方を指定し、サーバーを再起動してください。設定情報が未取得の場合も編集できません。</p>
    <p className="text-sm">Enterprise profile ではこの機能は禁止されています。フラグを設定しても制限を解除できません。</p>
  </section>;
}
export function EmployeeWorkspace() {
  const { mutate } = useSWRConfig();
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
  const [browsing, setBrowsing] = useState(false);
  const galleryRef = useRef<HTMLDivElement>(null);
  const listVisible = !selected || browsing;
  const canManage = catalog.data?.can_manage === true;
  const thumbnailChanged = async (id: string, artifactId: string | null) => {
    await employees.mutate(current => current ? { ...current, agents: current.agents.map(agent => agent.id === id ? { ...agent, thumbnail_artifact_id: artifactId } : agent) } : current, { revalidate: false });
    await mutate(["employee", id], (current: { agent: Employee } | undefined) => current ? { ...current, agent: { ...current.agent, thumbnail_artifact_id: artifactId } } : current, { revalidate: false });
  };
  const navigate = (id: string | null, next: EmployeePanel = "overview") => router.push(employeeHref(id, next), { scroll: false });
  if (!enabled) return <EmployeeFeatureDisabled features={features} />;
  return <div className={`${styles.workspace} flex min-h-0 flex-1 flex-col gap-3`} data-testid="employee-workspace"><header className="flex shrink-0 flex-wrap items-center justify-between gap-2"><div><h1 className="inline-flex items-center gap-1 text-xl font-semibold">Agent<HelpPopover label="Agent">下書きを作成し、職務・所属・自動化を設定してから有効化します。</HelpPopover></h1></div>
    <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="ghost" onClick={() => { void employees.mutate(); void catalog.mutate(); void snapshot.mutate(); }}>一覧を更新</Button>{canManage && <><Button type="button" size="sm" variant="ghost" onClick={() => router.push("/operations?tab=agents&panel=organization", { scroll: false })}>Agent組織を設定</Button><Button type="button" size="sm" onClick={() => setCreating(true)}>＋ Agentを追加</Button></>}</div></header>
    {requested === "organization" && canManage && catalog.data && <EmployeeOrganizationSetup catalog={catalog.data} onSaved={() => Promise.all([employees.mutate(), snapshot.mutate()])} />}
    <EmployeeNotice error={employees.error} reload={() => void employees.mutate()} />
    <EmployeeNotice error={snapshot.error} reload={() => void snapshot.mutate()} />
    {catalog.error?.status === 403 ? <p className="text-sm">Agentの管理には管理者権限が必要です。状態の表示のみ利用できます。</p> : <EmployeeNotice error={catalog.error} reload={() => void catalog.mutate()} />}
    <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">{(["active", "paused", "draft", "retired"] as const).map(state => {
      const count = employees.data?.agents.filter(a => a.state === state).length ?? 0;
      return count > 0 ? <span key={state}><EmployeeStatus state={state} /> {count}</span> : null;
    })}<span>作業中 {snapshot.data?.summary.working ?? "—"} · 承認待ち {snapshot.data?.summary.awaiting_approval ?? "—"} · 要確認 {snapshot.data?.summary.uncertain ?? "—"}</span></div>
    {creating && canManage && <div className="min-h-0 overflow-y-auto"><EmployeeCreate onCreated={async id => { await employees.mutate(); setCreating(false); setBrowsing(false); navigate(id, "role"); }} onClose={() => setCreating(false)} /></div>}
    <div ref={galleryRef} className={`flex min-h-0 flex-1 flex-col ${creating ? "hidden" : ""}`}>
      <EmployeeDirectory agents={employees.data?.agents ?? []} projections={snapshot.data?.agents ?? []} selected={selected} loading={employees.isLoading} failed={!!employees.error} visible={!creating} onShowDetail={() => setBrowsing(false)} onSelect={id => { setBrowsing(false); navigate(id); }} onThumbnailChanged={canManage ? thumbnailChanged : undefined} />
      <Dialog open={!listVisible} onOpenChange={open => { if (!open) setBrowsing(true); }}>
        <DialogContent keepMounted size="4xl" showCloseButton={false} finalFocus={() => { const target = galleryRef.current?.querySelector<HTMLElement>('button[aria-pressed="true"]') ?? galleryRef.current?.querySelector<HTMLElement>('input[type="search"]'); target?.focus({ preventScroll: true }); return false; }} className={`${styles.workspace} ${styles.detail} max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-[1.25rem] border-border/60 p-4 sm:p-5`} overlayClassName={styles.backdrop}>
          <DialogTitle className="sr-only">Agent詳細</DialogTitle>
          <div role="region" aria-label="Agent詳細" className="min-w-0">
        {selected && <><div className="mb-3"><Button type="button" size="sm" variant="ghost" onClick={() => setBrowsing(true)}><ArrowLeft aria-hidden="true" className="size-4" />一覧に戻る</Button></div>
          <EmployeeDetail key={selected} id={selected} panel={panel} catalog={catalog.data} canManage={canManage} projection={snapshot.data?.agents.find(agent => agent.id === selected)} navigate={next => navigate(selected, next)} onThumbnailChanged={thumbnailChanged} onChanged={() => Promise.all([employees.mutate(), snapshot.mutate()])} /></>
        }
          </div>
        </DialogContent>
      </Dialog>
    </div>
  </div>;
}
function EmployeeCreate({ onCreated, onClose }: { onCreated: (id: string) => Promise<void>; onClose: () => void }) {
  const [name, setName] = useState(""); const [slug, setSlug] = useState(""); const [job, setJob] = useState(""); const [summary, setSummary] = useState("");
  const [character, setCharacter] = useState(""); const [employment, setEmployment] = useState("active");
  const [key, setKey] = useState(employeeKey); const [draft, setDraft] = useState<Employee | null>(null);
  const [createIntent, setCreateIntent] = useState<Parameters<typeof employeeApi.create>[0] | null>(null);
  const characters = useSWR("employee-characters", employeeApi.characters, { shouldRetryOnError: false });
  return <section className="space-y-3 rounded-lg border p-4"><h2 className="inline-flex items-center gap-1 font-semibold">Agentを追加 — 1. 基本情報<HelpPopover label="Agentを追加"><p>下書き → 職務 → 所属 → 自動化（任意） → 確認・有効化。途中で閉じても保存済みの下書きは一覧から再開できます。</p><p>候補は会話用Characterです。Characterは会話の人格・表示を決めるだけで、Agentの職務・権限・実行Profileは変わりません。発信・制作のPersonaとは別のデータです。</p><p>雇用状態は組織上の在籍・契約状態です。Agentの実行状態（下書き・有効・一時停止・退職）とは別に保存されます。</p></HelpPopover></h2>
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
      <div className="grid gap-4 md:grid-cols-2"><EmployeeField label="Agent名"><input className={employeeInputClass} required maxLength={160} value={name} disabled={!!draft || !!createIntent} onChange={e => setName(e.target.value)} /></EmployeeField>
        <EmployeeField label="slug（任意）"><input className={employeeInputClass} maxLength={100} value={slug} disabled={!!draft || !!createIntent} onChange={e => setSlug(e.target.value)} /></EmployeeField>
        <EmployeeField label="役職"><input className={employeeInputClass} maxLength={160} value={job} onChange={e => setJob(e.target.value)} /></EmployeeField>
        <EmployeeField label="責務の概要"><textarea className={employeeInputClass} maxLength={10000} value={summary} onChange={e => setSummary(e.target.value)} /></EmployeeField>
        <EmployeeSelect label="Character（会話人格・任意）" value={character} disabled={!!draft || !!createIntent} onChange={setCharacter} options={(characters.data ?? []).filter(c => c.is_enabled).map(c => ({ value: c.id, label: c.name }))} />
        <EmployeeSelect label="雇用状態" value={employment} required onChange={setEmployment} options={[{ value: "active", label: "在籍" }, { value: "contractor", label: "業務委託" }, { value: "on_leave", label: "休職" }, { value: "suspended", label: "停止" }]} /></div>
    </EmployeeForm><EmployeeNotice error={characters.error} reload={() => void characters.mutate()} />
    {draft && <p className="text-sm">下書き {draft.display_name} は保存済みです。後続の保存に失敗した場合も同じAgentの設定を再開できます。</p>}
    {createIntent && !draft && <p className="text-sm">作成結果を確認中です。基本情報を固定して同じリクエストを再試行します。基本情報を変更する場合は、閉じて一覧を更新し、保存済みの下書きを確認してください。</p>}
    <Button type="button" variant="ghost" onClick={onClose}>閉じる</Button>
  </section>;
}
/** 保存済みの character_id は会話Character一覧に対して解決する。一覧に無い値は書き換えず未解決と表示する。 */
function characterLabel(id: string | null | undefined, list: ConversationCharacter[] | undefined, failed: boolean) {
  if (!id) return "未選択";
  const found = list?.find(c => c.id === id);
  if (found) return found.is_enabled ? found.name : `${found.name}（無効）`;
  return list ? "未解決（保存済みのIDに対応する会話Characterが見つかりません）" : failed ? "確認できません" : "確認中…";
}
function EmployeeDetail({ id, panel, catalog, canManage, projection, navigate, onChanged, onThumbnailChanged }: { id: string; panel: EmployeePanel; catalog?: EmployeeCatalog; canManage: boolean; projection?: EmployeeProjection; navigate: (panel: EmployeePanel) => void; onChanged: () => Promise<unknown>; onThumbnailChanged: (id: string, artifactId: string | null) => Promise<void> }) {
  const agent = useSWR(["employee", id], () => employeeApi.get(id), { shouldRetryOnError: false });
  const revisions = useSWR(["employee-revisions", id], () => employeeApi.revisions(id), { shouldRetryOnError: false });
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState(false);
  const current = [...(revisions.data?.revisions ?? [])].sort((a, b) => b.version - a.version)[0];
  const characters = useSWR(panel === "overview" ? "employee-characters" : null, employeeApi.characters, { shouldRetryOnError: false });
  const authority = useSWR(canManage && panel === "overview" ? ["employee-authority", id, current?.id] : null, () => employeeRequest<{ authority: { allowed?: boolean; reason?: string; reason_code?: string; effective_capabilities?: string[]; reasons?: string[] } }>(`${employeePath(id)}/effective-authority${current ? `?revision_id=${encodeURIComponent(current.id)}` : ""}`), { shouldRetryOnError: false });
  const reload = async () => { await Promise.all([agent.mutate(), revisions.mutate(), authority.mutate(), onChanged()]); };
  async function transition(state: Employee["state"]) { if (!agent.data) return; setError(null); setBusy(true); try { await employeeApi.state(id, state, agent.data.agent.state); await reload(); } catch (e) { setError(e); } finally { setBusy(false); } }
  if (agent.isLoading) return <p>Agent情報を読み込み中…</p>;
  if (!agent.data) return <EmployeeNotice error={agent.error} reload={() => void agent.mutate()} />;
  const mutable = canManage && agent.data.agent.state !== "retired";
  const jobTitle = projection?.job_title?.trim();
  const differentRole = jobTitle && jobTitle.normalize("NFKC").toLocaleLowerCase("ja") !== agent.data.agent.display_name.trim().normalize("NFKC").toLocaleLowerCase("ja");
  return <div className="min-w-0 space-y-3"><header className="flex items-center gap-3"><div className="relative shrink-0"><EmployeeThumbnail agent={agent.data.agent} className="size-20 rounded-2xl" />{canManage && <EmployeeThumbnailControl agent={agent.data.agent} onChanged={onThumbnailChanged} cover className="absolute inset-0" />}</div><div className="min-w-0"><h2 className="break-words text-lg font-semibold leading-snug">{agent.data.agent.display_name}</h2>{differentRole && <p className="mt-0.5 break-words text-sm text-muted-foreground">{jobTitle}</p>}<span className="mt-1 inline-block"><EmployeeStatus state={agent.data.agent.state} /></span></div></header>
    {!!projection?.employee_observability?.uncertain_action_count && <p className="text-sm font-medium text-amber-700 dark:text-amber-300">要確認 {projection.employee_observability.uncertain_action_count}件 — 再実行禁止</p>}
    <nav aria-label="Agentの設定" className="flex gap-1 overflow-x-auto border-b pb-2">{employeePanels.map(([value, label]) => <Button key={value} type="button" size="sm" variant="ghost" className={panel === value ? "bg-accent text-primary" : "text-muted-foreground"} aria-current={panel === value ? "page" : undefined} onClick={() => navigate(value)}>{label}</Button>)}</nav>
    <EmployeeNotice error={agent.error || revisions.error || error} reload={() => void reload()} />
    {panel === "overview" && <>
      {current && <p className="max-w-3xl whitespace-pre-wrap text-sm leading-relaxed">{current.responsibility_summary || current.mission}</p>}
      <p className="text-sm text-muted-foreground" data-testid="employee-character">Character（会話人格）: {characterLabel(agent.data.agent.character_id, characters.data, !!characters.error)}</p>
      {!current && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">有効化するには「職務・モデル」を設定してください。</p>}
      <EmployeeNotice error={authority.error} reload={() => void authority.mutate()} />
      {canManage && mutable && <div className="flex flex-wrap gap-2">{agent.data.agent.state === "active" ? <Button type="button" size="sm" variant="secondary" disabled={busy} onClick={() => void transition("paused")}>Agentを一時停止</Button> : <Button type="button" size="sm" disabled={busy || !current} onClick={() => void transition("active")}>Agentを有効化</Button>}<Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => { if (window.confirm("このAgentを退職にします。この操作は取り消せず、再有効化できません。履歴は保持されます。続行しますか？")) void transition("retired"); }}>退職</Button></div>}
    </>}
    {panel === "role" && catalog && !revisions.isLoading && <EmployeeRole key={current?.id ?? "first"} agent={agent.data.agent} revisions={revisions.data?.revisions ?? []} catalog={catalog} onSaved={() => void reload()} />}
    {panel === "role" && !catalog && <p className="text-sm">管理者向けの職務カタログを取得できません。</p>}
    {panel === "assignments" && <EmployeeAssignments agentId={id} canManage={mutable} />}
    {panel === "automation" && <>{canManage ? <><EmployeeAutomation agentId={id} revisionId={current?.id ?? null} canManage={mutable} /><EmployeePolicies agentId={id} canManage={mutable} /></> : <p>自動化の管理には管理者権限が必要です。</p>}</>}
    {panel === "integrations" && <EmployeeIntegrations agentId={id} revisionId={current?.id ?? null} canManage={mutable} />}
    {panel === "phone" && <EmployeePhone agentId={id} revisionId={current?.id ?? null} canManage={mutable} />}
    {panel === "activity" && <><EmployeeChatActivity agentId={id} agentName={agent.data.agent.display_name} agentState={agent.data.agent.state} />{!!projection?.employee_observability?.uncertain_action_count && <p className="text-sm text-amber-700 dark:text-amber-300">要確認の操作は結果を照合するまで再実行できません。注文完了は、提供元の Receipt が存在する場合だけ確認できます。</p>}<OperationsCommandCenter view="activity" agentId={id} /></>}
  </div>;
}
