"use client";

import Link from "next/link";
import { HelpPopover } from "@/components/ui/help-popover";
import { useState } from "react";
import {
  employeeApi,
  employeeKey,
  type AgentDelegatePolicy,
  type AgentModelOverrides,
  type Employee,
  type EmployeeCatalog,
  type EmployeeRevision,
} from "@/lib/agent-employees-api";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeSelect, employeeInputClass } from "./employee-fields";
import { EmployeeDelegatePicker, EmployeeModelOverrides, EmployeeToolPicker, overridesSummary, sameJson, workersFromTools } from "./employee-model-overrides";

const EMPTY_POLICY: AgentDelegatePolicy = { agents: [], workers: [] };

export function EmployeeRole({ agent, revisions, catalog, onSaved }: { agent: Employee; revisions: EmployeeRevision[]; catalog: EmployeeCatalog; onSaved: (revision?: EmployeeRevision) => void }) {
  const current = [...revisions].sort((a, b) => b.version - a.version)[0];
  // Revisions written before Agents owned their models and tools keep their
  // historical projection until a new revision is saved.
  const legacy = Boolean(current) && current.model_overrides == null;
  const [name, setName] = useState(current?.display_name ?? agent.display_name);
  const [mission, setMission] = useState(current?.mission ?? "");
  const [summary, setSummary] = useState(current?.responsibility_summary ?? "");
  const [instructions, setInstructions] = useState(current?.operational_instructions ?? "");
  const [overrides, setOverrides] = useState<AgentModelOverrides>(current?.model_overrides ?? {});
  const [toolIds, setToolIds] = useState<string[]>(current?.tool_ids ?? current?.capability_ceiling ?? []);
  const [delegatePolicy, setDelegatePolicy] = useState<AgentDelegatePolicy>(current?.delegate_policy ?? EMPTY_POLICY);
  const [capabilities, setCapabilities] = useState(current?.capability_ceiling ?? []);
  const [runBudget, setRunBudget] = useState(String(current?.budget_policy.max_run_cost_micros ?? ""));
  const [dailyBudget, setDailyBudget] = useState(String(current?.budget_policy.max_daily_cost_micros ?? ""));
  const [currency, setCurrency] = useState(current?.budget_policy.currency ?? "");
  const [parallel, setParallel] = useState(String(current?.concurrency_policy.max_parallel_runs ?? ""));
  const [key] = useState(employeeKey);
  const edited = <T,>(set: (value: T) => void) => (value: T) => { set(value); };
  const toggle = (items: string[], id: string, checked: boolean) => checked ? [...items, id] : items.filter(x => x !== id);
  const policy = { ...delegatePolicy, workers: workersFromTools(toolIds) };
  // On a legacy revision, unchanged model/tool fields are left to the server,
  // which preserves the historical route projection.
  const modelsChanged = !sameJson(overrides, current?.model_overrides ?? {});
  const toolsChanged = !sameJson(toolIds, current?.tool_ids ?? current?.capability_ceiling ?? []);
  const policyChanged = !sameJson(policy, current?.delegate_policy ?? EMPTY_POLICY);
  const changes = [
    ["表示名", current?.display_name, name], ["ミッション", current?.mission, mission],
    ["責務", current?.responsibility_summary, summary], ["業務指示", current?.operational_instructions, instructions],
    ["モデル", overridesSummary(current?.model_overrides), modelsChanged ? overridesSummary(overrides) : overridesSummary(current?.model_overrides)],
    ["使う tool", (current?.tool_ids ?? []).join(", "), toolsChanged ? toolIds.join(", ") : (current?.tool_ids ?? []).join(", ")],
    ["委任先", JSON.stringify(current?.delegate_policy ?? null), policyChanged ? JSON.stringify(policy) : JSON.stringify(current?.delegate_policy ?? null)],
    ["権限上限", current?.capability_ceiling.join(", "), capabilities.join(", ")],
    ["1実行の予算", String(current?.budget_policy.max_run_cost_micros ?? ""), runBudget],
    ["日次予算", String(current?.budget_policy.max_daily_cost_micros ?? ""), dailyBudget],
    ["予算通貨", current?.budget_policy.currency ?? "", currency],
    ["同時実行数", String(current?.concurrency_policy.max_parallel_runs ?? ""), parallel],
  ].filter(([, before, after]) => before !== after);
  return <section className="space-y-4"><h3 className="inline-flex items-center gap-1 text-lg font-semibold">職務・モデル — {current ? `現在 v${current.version}` : "未設定"}<HelpPopover label="職務・モデル"><p>Agent のモデル候補は順序付きで保持され、実行時には1候補を使用します。ほかの Agent や外部の作業者（Codex など）には、tool で仕事を任せます。</p><p data-testid="employee-model-settings-hint">役割ごとのモデルは、何も選ばなければ{" "}<Link className="underline underline-offset-2" href="/settings#llm-model">設定 → 言語モデル → モデル</Link>{" "}の全体の表を使います。</p><p>未設定の予算・通貨・同時実行数は空欄のまま保持します。変更した項目だけを保存し、設定済みの項目を空欄に戻すとその指定を解除します。</p></HelpPopover></h3>
    {agent.is_default_chat_agent ? <p className="rounded-lg border bg-muted/40 p-3 text-sm">この Agent は、普通の Chat を受け持つ既定の Agent です。ここでの変更は Chat にも反映されます。</p> : null}
    <EmployeeForm label={`新しい職務 revision v${(current?.version ?? 0) + 1} を保存`} disabled={!catalog.can_manage || agent.state === "retired"} reload={() => onSaved()} onSave={async () => {
      const budget = { ...current?.budget_policy };
      const concurrency = { ...current?.concurrency_policy };
      // A revision carries all existing policy keys. Only edited fields change.
      if (runBudget !== String(current?.budget_policy.max_run_cost_micros ?? "")) {
        if (runBudget) budget.max_run_cost_micros = Number(runBudget); else delete budget.max_run_cost_micros;
      }
      if (dailyBudget !== String(current?.budget_policy.max_daily_cost_micros ?? "")) {
        if (dailyBudget) budget.max_daily_cost_micros = Number(dailyBudget); else delete budget.max_daily_cost_micros;
      }
      if (currency !== (current?.budget_policy.currency ?? "")) {
        if (currency) budget.currency = currency; else delete budget.currency;
      }
      if (parallel !== String(current?.concurrency_policy.max_parallel_runs ?? "")) {
        if (parallel) concurrency.max_parallel_runs = Number(parallel); else delete concurrency.max_parallel_runs;
      }
      const result = await employeeApi.revision(agent.id, { display_name: name.trim(), mission, responsibility_summary: summary, operational_instructions: instructions,
        capability_ceiling: capabilities,
        ...(!legacy || modelsChanged ? { model_overrides: overrides } : {}),
        ...(!legacy || toolsChanged ? { tool_ids: toolIds } : {}),
        ...(!legacy || policyChanged ? { delegate_policy: policy } : {}),
        budget_policy: budget,
        concurrency_policy: concurrency,
        ...(current?.wake_policy ? { wake_policy: current.wake_policy } : {}), version: (current?.version ?? 0) + 1, idempotency_key: key }); onSaved(result.revision);
    }}>
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="職務の表示名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => edited(setName)(e.target.value)} /></EmployeeField>
        <EmployeeField label="ミッション"><textarea className={employeeInputClass} maxLength={10000} value={mission} onChange={e => edited(setMission)(e.target.value)} /></EmployeeField>
        <EmployeeField label="責務の説明"><textarea className={employeeInputClass} maxLength={10000} value={summary} onChange={e => edited(setSummary)(e.target.value)} /></EmployeeField>
        <EmployeeField label="業務指示"><textarea className={employeeInputClass} rows={5} maxLength={30000} value={instructions} onChange={e => edited(setInstructions)(e.target.value)} /></EmployeeField>
      </div>
      <EmployeeModelOverrides disabled={!catalog.can_manage || agent.state === "retired"} overrides={overrides} effective={current?.effective_model_routes} legacy={legacy} onChange={edited(setOverrides)} />
      <EmployeeToolPicker toolIds={toolIds} capabilities={capabilities} onChange={edited(setToolIds)} />
      {toolIds.includes("delegate") ? <EmployeeDelegatePicker agentId={agent.id} policy={delegatePolicy} onChange={edited(setDelegatePolicy)} /> : null}
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">権限上限（組織・所属・実行時の制限がさらに適用されます）</legend>{catalog.capabilities.map(c => <EmployeeCheck key={c.id} label={`${c.id} (${c.family} / ${c.access})`} checked={capabilities.includes(c.id)} onChange={checked => edited(setCapabilities)(toggle(capabilities, c.id, checked))} />)}</fieldset>
      <div className="grid gap-4 md:grid-cols-4">
        <EmployeeField label="1実行の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={runBudget} onChange={e => edited(setRunBudget)(e.target.value)} /></EmployeeField>
        <EmployeeField label="1日の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={dailyBudget} onChange={e => edited(setDailyBudget)(e.target.value)} /></EmployeeField>
        <EmployeeSelect label="予算通貨" value={currency} options={[{ value: "JPY", label: "JPY" }, { value: "USD", label: "USD" }]} onChange={edited(setCurrency)} />
        <EmployeeField label="最大同時実行数"><input className={employeeInputClass} type="number" min={1} max={100} value={parallel} onChange={e => edited(setParallel)(e.target.value)} /></EmployeeField>
      </div>
      <div className="rounded-lg border p-3 text-sm"><h4 className="font-medium">変更内容の確認</h4><dl className="mt-2 space-y-2">{changes.map(([label, before, after]) => <div key={label}><dt>{label}</dt><dd className="whitespace-pre-wrap break-words text-muted-foreground">{before || "未設定"} → {after || "未設定"}</dd></div>)}</dl>
        <p className="my-3">実行中の仕事は既存の正確な revision を使い続けます。自動化の設定も、参照する revision を明示的に更新するまで変わりません。</p>
      </div>
    </EmployeeForm>
    <details><summary>職務 revision 履歴（{revisions.length}件）</summary><ul className="space-y-1 text-sm">{revisions.map(r => <li key={r.id}>v{r.version} · {r.display_name} · {r.model_overrides == null ? `旧設定 ${r.legacy?.agent_team_id ?? "未設定"} / ${r.legacy?.execution_profile_id ?? "未設定"}` : overridesSummary(r.model_overrides)} · {r.id}</li>)}</ul></details>
  </section>;
}
