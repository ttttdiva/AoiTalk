"use client";

import useSWR from "swr";
import { HelpPopover } from "@/components/ui/help-popover";
import { getLlmModelCatalog } from "@/lib/chat-api";
import { routeLabel, WORK_ROLE_LABELS, WORK_ROLES, type ModelRoute, type WorkRole } from "@/lib/model-roles-api";
import {
  employeeApi,
  employeeRequest,
  type AgentDelegatePolicy,
  type AgentEffectiveRoute,
  type AgentModelOverrides,
  type AgentToolCatalogEntry,
  type Employee,
} from "@/lib/agent-employees-api";
import { EmployeeCheck, employeeInputClass } from "./employee-fields";
import { FallbackConditionsEditor } from "./fallback-conditions-editor";
import { AppSelect } from "@/components/ui/app-select";

export const AGENT_ROLES = ["chat", ...WORK_ROLES] as const;
export type AgentRole = typeof AGENT_ROLES[number];

const EFFORTS = ["low", "medium", "high", "xhigh"];
const SOURCE_LABELS: Record<string, string> = {
  agent: "この Agent だけ上書き",
  global: "全体の表から継承",
  inherit_chat: "会話のモデルを継承",
  legacy: "旧設定",
};
const TOOL_GROUP_LABELS: Record<string, string> = {
  workspace: "Workspace", command: "コマンド", aoi: "AoiTalk", docs: "Docs", project: "Project", web: "Web",
  browser: "ブラウザ", media: "メディア", action: "外部操作", delegation: "委任", personal: "個人", capability: "その他",
};
export const WORKER_KINDS = ["codex", "antigravity", "claude", "grok", "muse"] as const;

function routeValue(route: ModelRoute | null | undefined): string {
  if (!route) return "";
  return route.kind === "pool" ? `pool:${route.pool_id}` : `llm:${route.provider}/${route.model}`;
}

function parseRoute(value: string): ModelRoute | null {
  if (value.startsWith("pool:")) return { kind: "pool", pool_id: value.slice(5) };
  if (!value.startsWith("llm:")) return null;
  const [provider, ...rest] = value.slice(4).split("/");
  return provider && rest.length ? { kind: "llm", provider, model: rest.join("/") } : null;
}

export function EmployeeModelOverrides({ overrides, effective, onChange, legacy, disabled = false }: {
  overrides: AgentModelOverrides;
  effective: Partial<Record<AgentRole, AgentEffectiveRoute>> | undefined;
  onChange: (next: AgentModelOverrides) => void;
  legacy: boolean;
  disabled?: boolean;
}) {
  const models = useSWR("employee-llm-models", getLlmModelCatalog, { shouldRetryOnError: false, revalidateOnFocus: false });
  const pools = useSWR("employee-free-team-pools", () => employeeRequest<{ profile?: { pools?: Record<string, unknown> } }>("/api/free-team/settings"), { shouldRetryOnError: false, revalidateOnFocus: false });
  const providers = (models.data?.providers ?? []).filter(provider => provider.selection_kind !== "routing_profile");
  const poolIds = Object.keys(pools.data?.profile?.pools ?? {});
  const setRoutes = (role: AgentRole, routes: ModelRoute[]) => {
    const next = { ...overrides };
    if (!routes.length) delete next[role];
    else next[role] = { ...overrides[role], routes };
    onChange(next);
  };
  return <section aria-label="役割ごとのモデル" className="space-y-2 rounded-lg border p-3 text-sm">
    <h4 className="inline-flex items-center gap-1 font-medium">役割ごとのモデル<HelpPopover label="役割ごとのモデル"><p>実行時は1モデル、候補は複数と順序を設定可能。未設定の役割は全体の表から継承します。</p><p>実行開始前の型付き preflight エラーのうち、モデル・プロバイダーの利用不可、レート制限、接続タイムアウトが指定条件に合う場合、次の候補を試します。開始後の失敗や権限・プライバシーなどの拒否は対象外です。</p><p>従量課金への切替許可は認証情報や課金権限を付与しません。</p></HelpPopover></h4>
    {legacy ? <p className="text-xs text-muted-foreground">この版は旧設定の読み取り投影で動いています。保存すると、ここで選んだ内容の新しい版になります。</p> : null}
    <ul className="divide-y rounded-md border">
      {AGENT_ROLES.map(role => {
        const block = overrides[role];
        const routes = block?.routes ?? [];
        const current = effective?.[role];
        return <li key={role} className="space-y-2 p-3" data-agent-role={role}>
          <div className="flex items-center justify-between gap-2"><span className="font-medium">{WORK_ROLE_LABELS[role]}</span>{routes.length ? <button type="button" className="text-xs underline" onClick={() => setRoutes(role, [])}>全体の設定を継承</button> : null}</div>
          {!routes.length && current?.route ? <p className="text-xs text-muted-foreground">今は {routeLabel(current.route)}（{SOURCE_LABELS[current.source] ?? current.source}）</p> : null}
          {routes.map((route, index) => {
            const value = routeValue(route);
            const known = route.kind === "llm"
              ? providers.some(provider => provider.models.some(model => model.selection_kind !== "routing_profile" && `llm:${provider.id}/${model.id}` === value))
              : poolIds.some(id => `pool:${id}` === value);
            const catalog = route.kind === "llm" ? models : pools;
            const status = catalog.error ? "一覧を取得できません" : !catalog.data ? "一覧を読み込み中" : "一覧にありません";
            const replace = (next: ModelRoute) => setRoutes(role, routes.map((item, at) => at === index ? next : item));
            const move = (to: number) => { const next = [...routes]; [next[index], next[to]] = [next[to], next[index]]; setRoutes(role, next); };
            return <div key={index} className="flex flex-wrap items-center gap-2 rounded-md bg-muted/40 p-2">
              <span className="text-xs">{index + 1}</span>
              <div className="min-w-48 flex-1"><AppSelect aria-label={`${WORK_ROLE_LABELS[role]}の候補${index + 1}`} className={employeeInputClass} value={value} onValueChange={next => { const parsed = parseRoute(next); if (parsed) replace(parsed); }}>
                {!known ? <option value={value}>{routeLabel(route)}（{status}）</option> : null}
                {providers.map(provider => <optgroup key={provider.id} label={provider.label || provider.id}>{provider.models.filter(model => model.selection_kind !== "routing_profile").map(model => <option key={model.id} value={`llm:${provider.id}/${model.id}`}>{`${provider.label || provider.id} · ${model.label || model.id}`}</option>)}</optgroup>)}
                {poolIds.length ? <optgroup label="無料Team の pool">{poolIds.map(id => <option key={id} value={`pool:${id}`}>{id}</option>)}</optgroup> : null}
              </AppSelect></div>
              {route.kind === "llm" ? <AppSelect aria-label={`${WORK_ROLE_LABELS[role]}の候補${index + 1}の effort`} className={employeeInputClass + " max-w-36"} value={route.effort ?? ""} onValueChange={effort => replace({ ...route, effort: effort || null })}><option value="">effort 既定</option>{route.effort && !EFFORTS.includes(route.effort) ? <option value={route.effort}>{route.effort}</option> : null}{EFFORTS.map(effort => <option key={effort} value={effort}>{effort}</option>)}</AppSelect> : null}
              <button type="button" aria-label={`${WORK_ROLE_LABELS[role]}の候補${index + 1}を上へ`} disabled={index === 0} onClick={() => move(index - 1)}>↑</button>
              <button type="button" aria-label={`${WORK_ROLE_LABELS[role]}の候補${index + 1}を下へ`} disabled={index === routes.length - 1} onClick={() => move(index + 1)}>↓</button>
              <button type="button" aria-label={`${WORK_ROLE_LABELS[role]}の候補${index + 1}を削除`} onClick={() => setRoutes(role, routes.filter((_, at) => at !== index))}>削除</button>
            </div>;
          })}
          <AppSelect aria-label={`${WORK_ROLE_LABELS[role]}に候補を追加`} className={employeeInputClass} disabled={routes.length >= 64} value="" onValueChange={value => { const route = parseRoute(value); if (route) setRoutes(role, [...routes, route]); }}>
            <option value="">モデルまたは pool を追加</option>
            {providers.map(provider => <optgroup key={provider.id} label={provider.label || provider.id}>{provider.models.filter(model => model.selection_kind !== "routing_profile").map(model => <option key={model.id} value={`llm:${provider.id}/${model.id}`}>{`${provider.label || provider.id} · ${model.label || model.id}`}</option>)}</optgroup>)}
            {poolIds.length ? <optgroup label="無料Team の pool">{poolIds.map(id => <option key={id} value={`pool:${id}`}>{id}</option>)}</optgroup> : null}
          </AppSelect>
          {block ? <FallbackConditionsEditor disabled={disabled} idPrefix={`fallback-${role}`} value={block.fallback_on} onChange={fallback_on => onChange({ ...overrides, [role]: { ...block, fallback_on } })} /> : null}
          {block ? <AppSelect aria-label={`${WORK_ROLE_LABELS[role]}の従量課金 fallback`} className={employeeInputClass} disabled={disabled} value={block.allow_paid_fallback == null ? "inherit" : block.allow_paid_fallback ? "allow" : "deny"} onValueChange={value => { const next = { ...block }; if (value === "inherit") delete next.allow_paid_fallback; else next.allow_paid_fallback = value === "allow"; onChange({ ...overrides, [role]: next }); }}>
            <option value="inherit">従量課金 fallback: 継承</option><option value="allow">従量課金 fallback: 許可</option><option value="deny">従量課金 fallback: 許可しない</option>
          </AppSelect> : null}
        </li>;
      })}
    </ul>
  </section>;
}

/** Tools shown to the Agent. The capability ceiling still decides what it may actually do. */
export function EmployeeToolPicker({ toolIds, capabilities, onChange }: { toolIds: string[]; capabilities: string[]; onChange: (next: string[]) => void }) {
  const catalog = useSWR("employee-tool-catalog", employeeApi.toolCatalog, { shouldRetryOnError: false, revalidateOnFocus: false });
  const tools = catalog.data?.tools ?? [];
  const groups = [...new Set(tools.map(tool => tool.group))];
  const missing = (tool: AgentToolCatalogEntry) => tool.requires_capabilities.filter(capability => !capabilities.includes(capability));
  return <fieldset className="space-y-2 rounded-lg border p-3">
    <legend className="px-1 text-sm font-medium">使う tool</legend>
    {catalog.error ? <p className="text-xs text-destructive">tool の一覧を取得できません。</p> : null}
    {groups.map(group => <div key={group} className="space-y-1">
      <p className="text-xs font-semibold text-muted-foreground">{TOOL_GROUP_LABELS[group] ?? group}</p>
      <div className="grid gap-1 sm:grid-cols-2">{tools.filter(tool => tool.group === group).map(tool => {
        const lacking = missing(tool);
        return <div key={tool.id} title={tool.description}>
          <EmployeeCheck label={tool.label} checked={toolIds.includes(tool.id)}
            onChange={checked => onChange(checked ? [...toolIds, tool.id] : toolIds.filter(id => id !== tool.id))} />
          {lacking.length && toolIds.includes(tool.id) ? <p className="ml-6 text-[11px] text-amber-700 dark:text-amber-300">権限上限に {lacking.join("、")} が無いため使えません</p> : null}
        </div>;
      })}</div>
    </div>)}
  </fieldset>;
}

/** Which Agents `delegate` may hand work to. External workers follow the checked delegate_worker tools. */
export function EmployeeDelegatePicker({ agentId, policy, onChange }: { agentId: string; policy: AgentDelegatePolicy; onChange: (next: AgentDelegatePolicy) => void }) {
  const agents = useSWR("employee-delegate-targets", employeeApi.list, { shouldRetryOnError: false, revalidateOnFocus: false });
  const targets = (agents.data?.agents ?? []).filter((agent: Employee) => agent.id !== agentId && agent.state !== "retired");
  const any = policy.agents === "any_owned";
  const chosen = Array.isArray(policy.agents) ? policy.agents : [];
  return <fieldset className="space-y-2 rounded-lg border p-3 text-sm">
    <legend className="px-1 font-medium">委任してよい Agent</legend>
    <EmployeeCheck label="同じ所有者の Agent すべて" checked={any} onChange={checked => onChange({ ...policy, agents: checked ? "any_owned" : [] })} />
    {!any ? <div className="grid gap-1 sm:grid-cols-2">{targets.map(agent => <EmployeeCheck key={agent.id} label={agent.display_name} checked={chosen.includes(agent.id)}
      onChange={checked => onChange({ ...policy, agents: checked ? [...chosen, agent.id] : chosen.filter(id => id !== agent.id) })} />)}</div> : null}
  </fieldset>;
}

export function workersFromTools(toolIds: string[]): string[] {
  return toolIds.filter(id => id.startsWith("delegate_worker:")).map(id => id.slice("delegate_worker:".length));
}

export function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

export function overridesSummary(overrides: AgentModelOverrides | null | undefined): string {
  const entries = Object.entries(overrides ?? {}).filter(([, value]) => value?.routes.length);
  return entries.length ? entries.map(([role, value]) => `${WORK_ROLE_LABELS[role as AgentRole]}=${value!.routes.map(routeLabel).join(" → ")} (条件: ${value!.fallback_on == null ? "既定" : value!.fallback_on.length ? value!.fallback_on.join(", ") : "無効"})`).join("、") : "上書きなし";
}

export type { WorkRole };
