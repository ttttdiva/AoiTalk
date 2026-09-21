"use client";

import { useState } from "react";
import { employeeApi, employeeKey, type Employee, type EmployeeCatalog, type EmployeeRevision } from "@/lib/agent-employees-api";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeSelect, employeeInputClass } from "./employee-fields";

export function EmployeeRole({ agent, revisions, catalog, onSaved }: { agent: Employee; revisions: EmployeeRevision[]; catalog: EmployeeCatalog; onSaved: () => void }) {
  const current = [...revisions].sort((a, b) => b.version - a.version)[0];
  const [name, setName] = useState(current?.display_name ?? agent.display_name);
  const [mission, setMission] = useState(current?.mission ?? "");
  const [summary, setSummary] = useState(current?.responsibility_summary ?? "");
  const [instructions, setInstructions] = useState(current?.operational_instructions ?? "");
  const [teamId, setTeam] = useState(current?.agent_team_id ?? "");
  const [profileId, setProfile] = useState(current?.execution_profile_id ?? "");
  const [subagents, setSubagents] = useState(current?.allowed_subagent_ids ?? []);
  const [capabilities, setCapabilities] = useState(current?.capability_ceiling ?? []);
  const [runBudget, setRunBudget] = useState(String(current?.budget_policy.max_run_cost_micros ?? ""));
  const [dailyBudget, setDailyBudget] = useState(String(current?.budget_policy.max_daily_cost_micros ?? ""));
  const [currency, setCurrency] = useState(current?.budget_policy.currency ?? "JPY");
  const [parallel, setParallel] = useState(String(current?.concurrency_policy.max_parallel_runs ?? 1));
  const [reviewed, setReviewed] = useState(false);
  const [key] = useState(employeeKey);
  const team = catalog.teams.find(t => t.team_id === teamId);
  const toggle = (items: string[], id: string, checked: boolean) => checked ? [...items, id] : items.filter(x => x !== id);
  const changes = [
    ["表示名", current?.display_name, name], ["ミッション", current?.mission, mission],
    ["責務", current?.responsibility_summary, summary], ["業務指示", current?.operational_instructions, instructions],
    ["Agent Team", current?.agent_team_id, teamId], ["Execution Profile", current?.execution_profile_id, profileId],
    ["Subagents", current?.allowed_subagent_ids.join(", "), subagents.join(", ")],
    ["権限上限", current?.capability_ceiling.join(", "), capabilities.join(", ")],
    ["1実行の予算", String(current?.budget_policy.max_run_cost_micros ?? ""), runBudget],
    ["日次予算", String(current?.budget_policy.max_daily_cost_micros ?? ""), dailyBudget],
    ["予算通貨", current?.budget_policy.currency, currency],
    ["同時実行数", String(current?.concurrency_policy.max_parallel_runs ?? 1), parallel],
  ].filter(([, before, after]) => before !== after);
  return <section className="space-y-4"><h3 className="text-lg font-semibold">職務・モデル — {current ? `現在 v${current.version}` : "未設定"}</h3>
    <p className="text-sm text-muted-foreground">Agent Teamはこの社員が仕事をするときの内部チーム構成です。AI社員そのものではありません。</p>
    <EmployeeForm label={`新しい職務 revision v${(current?.version ?? 0) + 1} を保存`} disabled={!catalog.can_manage || agent.state === "retired"} reload={onSaved} onSave={async () => {
      if (!reviewed || !team || !team.execution_profiles.some(p => p.profile_id === profileId)) throw new Error("review required");
      await employeeApi.revision(agent.id, { display_name: name.trim(), mission, responsibility_summary: summary, operational_instructions: instructions,
        agent_team_id: teamId, execution_profile_id: profileId, allowed_subagent_ids: subagents, capability_ceiling: capabilities,
        budget_policy: { currency, ...(runBudget ? { max_run_cost_micros: Number(runBudget) } : {}), ...(dailyBudget ? { max_daily_cost_micros: Number(dailyBudget) } : {}) },
        concurrency_policy: { ...current?.concurrency_policy, max_parallel_runs: Number(parallel) },
        ...(current?.wake_policy ? { wake_policy: current.wake_policy } : {}), version: (current?.version ?? 0) + 1, idempotency_key: key }); onSaved();
    }}>
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="職務の表示名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => { setName(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="ミッション"><textarea className={employeeInputClass} maxLength={10000} value={mission} onChange={e => { setMission(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="責務の説明"><textarea className={employeeInputClass} maxLength={10000} value={summary} onChange={e => { setSummary(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="業務指示"><textarea className={employeeInputClass} rows={5} maxLength={30000} value={instructions} onChange={e => { setInstructions(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeSelect label="Agent Team" required value={teamId} options={catalog.teams.map(t => ({ value: t.team_id, label: t.name }))} onChange={value => { setTeam(value); setProfile(""); setSubagents([]); setCapabilities([]); setReviewed(false); }} />
        <EmployeeSelect label="Execution Profile" required value={profileId} options={(team?.execution_profiles ?? []).map(p => ({ value: p.profile_id, label: p.name }))} onChange={value => { setProfile(value); setReviewed(false); }} />
      </div>
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">許可する Subagents</legend>{team?.subagents.map(s => <EmployeeCheck key={s.subagent_id} label={s.name} checked={subagents.includes(s.subagent_id)} onChange={checked => { setSubagents(toggle(subagents, s.subagent_id, checked)); setReviewed(false); }} />)}</fieldset>
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">権限上限（組織・所属・実行時の制限がさらに適用されます）</legend>{catalog.capabilities.map(c => <EmployeeCheck key={c.id} label={`${c.id} (${c.family} / ${c.access})`} checked={capabilities.includes(c.id)} onChange={checked => { setCapabilities(toggle(capabilities, c.id, checked)); setReviewed(false); }} />)}</fieldset>
      <div className="grid gap-4 md:grid-cols-4">
        <EmployeeField label="1実行の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={runBudget} onChange={e => { setRunBudget(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="1日の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={dailyBudget} onChange={e => { setDailyBudget(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeSelect label="予算通貨" required value={currency} options={[{ value: "JPY", label: "JPY" }, { value: "USD", label: "USD" }]} onChange={v => { setCurrency(v); setReviewed(false); }} />
        <EmployeeField label="最大同時実行数"><input className={employeeInputClass} type="number" min={1} max={100} required value={parallel} onChange={e => { setParallel(e.target.value); setReviewed(false); }} /></EmployeeField>
      </div>
      <div className="rounded-lg border p-3 text-sm"><h4 className="font-medium">変更内容の確認</h4><dl className="mt-2 space-y-2">{changes.map(([label, before, after]) => <div key={label}><dt>{label}</dt><dd className="whitespace-pre-wrap break-words text-muted-foreground">{before || "未設定"} → {after || "未設定"}</dd></div>)}</dl>
        <p className="my-3">実行中の仕事は既存の正確な revision を使い続けます。自動化・電話の設定も、参照する revision を明示的に更新するまで変わりません。</p>
        <EmployeeCheck label="変更内容を確認しました" required checked={reviewed} onChange={setReviewed} />
      </div>
    </EmployeeForm>
    <details><summary>職務 revision 履歴（{revisions.length}件）</summary><ul className="space-y-1 text-sm">{revisions.map(r => <li key={r.id}>v{r.version} · {r.display_name} · {r.agent_team_id} / {r.execution_profile_id} · {r.id}</li>)}</ul></details>
  </section>;
}
