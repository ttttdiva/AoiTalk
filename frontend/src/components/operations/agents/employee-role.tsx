"use client";

import Link from "next/link";
import { HelpPopover } from "@/components/ui/help-popover";
import { useState } from "react";
import {
  employeeApi,
  employeeKey,
  type Employee,
  type EmployeeCatalog,
  type EmployeeExecutionProfile,
  type EmployeePhaseRoute,
  type EmployeeRevision,
  type EmployeeSubagentRoute,
} from "@/lib/agent-employees-api";
import { executionRouteBackendLabel, isJevRoute } from "@/components/settings/llm-model-section-types";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeSelect, employeeInputClass } from "./employee-fields";

const routeSourceLabels: Record<string, string> = {
  subagent_override: "Subagent Override",
  profile_default: "Default Subagent Route",
  standard_model: "標準モデル（Base Model）",
  free_team: "Free Team",
  legacy_profile: "旧Profile（Base Model継承）",
};

const appDevelopmentPhaseCandidates: Record<string, string[]> = {
  planner: ["architecture_planner", "general_researcher", "general_worker", "employee_operator"],
  worker: ["code_implementer", "workspace_operator", "general_worker", "employee_operator"],
  reviewer: ["code_reviewer", "general_researcher", "architecture_planner", "employee_operator"],
  operator: ["employee_operator", "general_worker"],
};

type NormalizedEmployeeExecutionProfile = EmployeeExecutionProfile & { profile_id: string };

function profileEntries(value: EmployeeExecutionProfile[] | Record<string, EmployeeExecutionProfile> | undefined): NormalizedEmployeeExecutionProfile[] {
  if (Array.isArray(value)) {
    return value.map((profile, index) => ({
      ...profile,
      profile_id: profile.profile_id || profile.preset_id || `preset_${index}`,
    }));
  }
  return Object.entries(value ?? {}).map(([profile_id, profile]) => ({ ...profile, profile_id: profile.profile_id || profile_id }));
}

function profilesForTeam(catalog: EmployeeCatalog, team: EmployeeCatalog["teams"][number]): NormalizedEmployeeExecutionProfile[] {
  const profiles = [...profileEntries(team.execution_profiles)];
  const aliases = [...profileEntries(team.execution_profile_presets), ...profileEntries(team.profile_presets)];
  const topLevel = [...profileEntries(catalog.execution_profile_presets), ...profileEntries(catalog.profile_presets)]
    .filter((profile) => !profile.team_id || profile.team_id === team.team_id);
  for (const profile of [...aliases, ...topLevel]) {
    if (!profiles.some((item) => item.profile_id === profile.profile_id)) profiles.push(profile);
  }
  return profiles;
}

function phaseRouteEntries(value: EmployeePhaseRoute[] | Record<string, EmployeePhaseRoute> | undefined): EmployeePhaseRoute[] {
  if (Array.isArray(value)) return value;
  return Object.entries(value ?? {}).map(([phase, route]) => ({ ...route, phase: route.phase || phase }));
}

function isAppDevelopmentTeam(team: EmployeeCatalog["teams"][number] | undefined): boolean {
  const id = String(team?.team_id ?? "").trim().toLowerCase();
  const name = String(team?.name ?? "").trim().toLowerCase();
  return id === "app_development" || id === "app" || name === "app development" || name.includes("アプリ開発");
}

function safeRouteSource(route: EmployeeSubagentRoute): string {
  const raw = String(route.route_source ?? "").trim();
  if (!raw || raw.toLowerCase().includes("jev")) return "内部選択ルート";
  return routeSourceLabels[raw] ?? raw;
}

function routeLabel(route: EmployeeSubagentRoute): string {
  if (isJevRoute(route)) return "内部選択ルート";
  const provider = String(route.provider ?? "").trim();
  const model = String(route.model ?? "").trim();
  if (route.route_source === "free_team" || (provider === "routing-profile" && model === "free-team")) return "Free Team";
  return `${provider || "(provider未設定)"} / ${model || "(model未設定)"}`;
}

function routeClassification(route: EmployeeSubagentRoute): string {
  if (isJevRoute(route)) return "";
  return executionRouteBackendLabel(route);
}

/** Read-only preview; model editing stays in Settings → 言語モデル → Execution Team. */
export function EmployeeRoutePreview({ routes, allowed }: { routes: EmployeeSubagentRoute[] | undefined; allowed: string[] }) {
  if (!routes) return null;
  return <section aria-label="実効モデル" className="space-y-2 rounded-lg border p-3 text-sm">
    <h4 className="inline-flex items-center gap-1 font-medium">実効モデル（読み取り専用）<HelpPopover label="実効モデル">仕事ではこのExecution ProfileのSubagentごとのルートを使います。モデルの編集は「設定 → 言語モデル → Execution Team → Execution Profile」で行います。</HelpPopover></h4>
    {routes.length === 0 ? <p>このTeamにはSubagentがありません。</p> : <ul className="space-y-1">{routes.map(route => {
      const enabled = allowed.includes(route.subagent_id);
      return <li key={route.subagent_id} className={enabled ? "" : "text-muted-foreground"}>
        <span className="font-medium">{route.name}</span>{enabled ? "" : "（未許可）"}: {route.error_code
          ? <span role="status" className="text-amber-700 dark:text-amber-300">解決できません（{route.error_code}）</span>
          : <span>{routeLabel(route)}{route.effort ? ` · effort ${route.effort}` : ""} · {safeRouteSource(route)}{route.inherits_main ? "（Base Model継承）" : ""}{routeClassification(route) ? ` · ${routeClassification(route)}` : ""}</span>}
      </li>;
    })}</ul>}
  </section>;
}

function phaseRouteFor(
  phase: string,
  team: EmployeeCatalog["teams"][number],
  profile: EmployeeExecutionProfile,
  routes: EmployeeSubagentRoute[],
  allowed: string[],
): EmployeePhaseRoute | EmployeeSubagentRoute | null {
  const members = new Set(team.subagents.map((item) => item.subagent_id));
  const supplied = phaseRouteEntries(profile.phase_routes).find((item) =>
    item.phase === phase
    || item.role === phase
    || (phase === "worker" && (item.phase === "code_worker" || item.role === "code_worker")),
  );
  if (supplied) {
    const suppliedId = supplied.acting_subagent_id || supplied.subagent_id;
    // A role-level error has no candidate identity and is safe to display as
    // the backend's final result.  A candidate-specific failure must not
    // suppress the backend's later allowed candidate fallback.
    if (supplied.error_code) {
      if (!suppliedId) return supplied;
    } else if (!suppliedId || (members.has(suppliedId) && allowed.includes(suppliedId))) {
      return supplied;
    }
  }
  const byId = new Map(routes.map((route) => [route.subagent_id, route]));
  const selected = (appDevelopmentPhaseCandidates[phase] ?? []).find((candidate) =>
    members.has(candidate)
    && allowed.includes(candidate)
    && !byId.get(candidate)?.error_code,
  );
  return selected ? (byId.get(selected) ?? { subagent_id: selected, name: selected, error_code: "agent_route_unavailable" }) : null;
}

/** App Development role resolution preview; never edits the Employee revision. */
export function AppDevelopmentPhasePreview({
  team,
  profile,
  allowed,
}: {
  team: EmployeeCatalog["teams"][number];
  profile: EmployeeExecutionProfile;
  allowed: string[];
}) {
  if (!isAppDevelopmentTeam(team)) return null;
  const routes = profile.routes ?? [];
  return <section aria-label="App Development phase解決" className="space-y-2 rounded-lg border p-3 text-sm">
    <h4 className="inline-flex items-center gap-1 font-medium">App Development phase解決（読み取り専用）<HelpPopover label="App Development phase解決">candidate orderとこのRevisionで許可されたSubagentに沿った解決結果です。自動reviewを強制する設定ではありません。</HelpPopover></h4>
    <ul className="grid gap-1 sm:grid-cols-2">
      {Object.keys(appDevelopmentPhaseCandidates).map((phase) => {
        const route = phaseRouteFor(phase, team, profile, routes, allowed);
        const routeName = route?.name || route?.subagent_id || route?.acting_subagent_id || "解決できません";
        return <li key={phase} className="rounded border border-border/70 p-2">
          <span className="font-medium">{phase}</span>
          <span className="ml-1 text-muted-foreground">→ {route ? routeName : "許可された候補なし"}</span>
          {route?.error_code ? <span className="ml-1 text-amber-700 dark:text-amber-300">（{route.error_code}）</span> : null}
          {route && !route.error_code ? <div className="mt-1 text-[11px] text-muted-foreground">{routeLabel(route)}{route.effort ? ` · effort ${route.effort}` : ""} · {safeRouteSource(route)}{routeClassification(route) ? ` · ${routeClassification(route)}` : ""}</div> : null}
        </li>;
      })}
    </ul>
  </section>;
}

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
  const [currency, setCurrency] = useState(current?.budget_policy.currency ?? "");
  const [parallel, setParallel] = useState(String(current?.concurrency_policy.max_parallel_runs ?? ""));
  const [reviewed, setReviewed] = useState(false);
  const [key] = useState(employeeKey);
  const team = catalog.teams.find(t => t.team_id === teamId);
  const teamProfiles = team ? profilesForTeam(catalog, team) : [];
  const selectedProfile = teamProfiles.find(p => p.profile_id === profileId);
  const toggle = (items: string[], id: string, checked: boolean) => checked ? [...items, id] : items.filter(x => x !== id);
  const changes = [
    ["表示名", current?.display_name, name], ["ミッション", current?.mission, mission],
    ["責務", current?.responsibility_summary, summary], ["業務指示", current?.operational_instructions, instructions],
    ["Execution Team", current?.agent_team_id, teamId], ["Execution Profile", current?.execution_profile_id, profileId],
    ["Subagents", current?.allowed_subagent_ids.join(", "), subagents.join(", ")],
    ["権限上限", current?.capability_ceiling.join(", "), capabilities.join(", ")],
    ["1実行の予算", String(current?.budget_policy.max_run_cost_micros ?? ""), runBudget],
    ["日次予算", String(current?.budget_policy.max_daily_cost_micros ?? ""), dailyBudget],
    ["予算通貨", current?.budget_policy.currency ?? "", currency],
    ["同時実行数", String(current?.concurrency_policy.max_parallel_runs ?? ""), parallel],
  ].filter(([, before, after]) => before !== after);
  return <section className="space-y-4"><h3 className="inline-flex items-center gap-1 text-lg font-semibold">職務・モデル — {current ? `現在 v${current.version}` : "未設定"}<HelpPopover label="職務・モデル"><p>Execution TeamはSubagentとExecution Profileによる実行構成です。担当・直属上司を管理するAgent組織とは別に設定します。OperationsとChatで同じTeamを選んでも、実行時の指示やtoolsがすべて共通になるわけではありません。</p><p data-testid="employee-model-settings-hint">使用するモデル（Claude Code・ローカルモデル等のprovider / model）はここでは直接指定しません。{" "}<Link className="underline underline-offset-2" href="/settings#llm-model">Settings → LLM → Execution Team → Team → Execution Profiles</Link>{" "}で設定し、ここでは使用する Execution Profile を選びます。</p><p>未設定の予算・通貨・同時実行数は空欄のまま保持します。変更した項目だけを保存し、設定済みの項目を空欄に戻すとその指定を解除します。</p></HelpPopover></h3>
    {current?.route_warnings?.map(warning => <p key={warning.code} role="alert" className="rounded-lg border border-amber-500/40 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
      外部操作operator: 未許可 — {warning.message}
    </p>)}
    <EmployeeForm label={`新しい職務 revision v${(current?.version ?? 0) + 1} を保存`} disabled={!catalog.can_manage || agent.state === "retired"} reload={onSaved} onSave={async () => {
      if (!reviewed || !team || !teamProfiles.some(p => p.profile_id === profileId)) throw new Error("review required");
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
      await employeeApi.revision(agent.id, { display_name: name.trim(), mission, responsibility_summary: summary, operational_instructions: instructions,
        agent_team_id: teamId, execution_profile_id: profileId, allowed_subagent_ids: subagents, capability_ceiling: capabilities,
        budget_policy: budget,
        concurrency_policy: concurrency,
        ...(current?.wake_policy ? { wake_policy: current.wake_policy } : {}), version: (current?.version ?? 0) + 1, idempotency_key: key }); onSaved();
    }}>
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="職務の表示名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => { setName(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="ミッション"><textarea className={employeeInputClass} maxLength={10000} value={mission} onChange={e => { setMission(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="責務の説明"><textarea className={employeeInputClass} maxLength={10000} value={summary} onChange={e => { setSummary(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="業務指示"><textarea className={employeeInputClass} rows={5} maxLength={30000} value={instructions} onChange={e => { setInstructions(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeSelect label="Execution Team" required value={teamId} options={catalog.teams.map(t => ({ value: t.team_id, label: t.name }))} onChange={value => { setTeam(value); setProfile(""); setSubagents([]); setCapabilities([]); setReviewed(false); }} />
        <EmployeeSelect label="Execution Profile" required value={profileId} options={teamProfiles.map(p => ({ value: p.profile_id, label: p.name }))} onChange={value => { setProfile(value); setReviewed(false); }} />
      </div>
      <EmployeeRoutePreview routes={selectedProfile?.routes} allowed={subagents} />
      {team && selectedProfile ? <AppDevelopmentPhasePreview team={team} profile={selectedProfile} allowed={subagents} /> : null}
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">許可する Subagents</legend>{team?.subagents.map(s => <EmployeeCheck key={s.subagent_id} label={s.name} checked={subagents.includes(s.subagent_id)} onChange={checked => { setSubagents(toggle(subagents, s.subagent_id, checked)); setReviewed(false); }} />)}</fieldset>
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">権限上限（組織・所属・実行時の制限がさらに適用されます）</legend>{catalog.capabilities.map(c => <EmployeeCheck key={c.id} label={`${c.id} (${c.family} / ${c.access})`} checked={capabilities.includes(c.id)} onChange={checked => { setCapabilities(toggle(capabilities, c.id, checked)); setReviewed(false); }} />)}</fieldset>
      <div className="grid gap-4 md:grid-cols-4">
        <EmployeeField label="1実行の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={runBudget} onChange={e => { setRunBudget(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeField label="1日の予算上限（百万分の1通貨単位）"><input type="number" min={0} step={1} className={employeeInputClass} value={dailyBudget} onChange={e => { setDailyBudget(e.target.value); setReviewed(false); }} /></EmployeeField>
        <EmployeeSelect label="予算通貨" value={currency} options={[{ value: "JPY", label: "JPY" }, { value: "USD", label: "USD" }]} onChange={v => { setCurrency(v); setReviewed(false); }} />
        <EmployeeField label="最大同時実行数"><input className={employeeInputClass} type="number" min={1} max={100} value={parallel} onChange={e => { setParallel(e.target.value); setReviewed(false); }} /></EmployeeField>
      </div>
      <div className="rounded-lg border p-3 text-sm"><h4 className="font-medium">変更内容の確認</h4><dl className="mt-2 space-y-2">{changes.map(([label, before, after]) => <div key={label}><dt>{label}</dt><dd className="whitespace-pre-wrap break-words text-muted-foreground">{before || "未設定"} → {after || "未設定"}</dd></div>)}</dl>
        <p className="my-3">実行中の仕事は既存の正確な revision を使い続けます。自動化・電話の設定も、参照する revision を明示的に更新するまで変わりません。</p>
        <EmployeeCheck label="変更内容を確認しました" required checked={reviewed} onChange={setReviewed} />
      </div>
    </EmployeeForm>
    <details><summary>職務 revision 履歴（{revisions.length}件）</summary><ul className="space-y-1 text-sm">{revisions.map(r => <li key={r.id}>v{r.version} · {r.display_name} · {r.agent_team_id} / {r.execution_profile_id} · {r.id}</li>)}</ul></details>
  </section>;
}
