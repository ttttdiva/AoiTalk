"use client";

import { useRef, useState } from "react";
import useSWR from "swr";
import { operationsApi, type OperationsConnection } from "@/lib/operations-api";
import { employeeKey, employeeRequest, type ActionDefinition, type CrowdWorksConstraints, type EmployeePolicy } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";

/** The only action with a policy form: CrowdWorks applications. */
const ENGAGEMENT_ACTION = "engagement.submit_application";

export function EmployeePolicies({ agentId, canManage }: { agentId: string; canManage: boolean }) {
  const policies = useSWR(["employee-policies", agentId], () => employeeRequest<{ policies: EmployeePolicy[] }>(`/api/agent-action-policies?agent_id=${encodeURIComponent(agentId)}`), { shouldRetryOnError: false });
  const registry = useSWR("employee-action-registry", () => employeeRequest<{ actions: ActionDefinition[] }>("/api/integrations/actions"), { shouldRetryOnError: false });
  const connections = useSWR("employee-connections", () => operationsApi.listConnections(), { shouldRetryOnError: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  async function state(policy: EmployeePolicy, next: string) {
    setError(null); setBusy(policy.id);
    try { await employeeRequest(`/api/agent-action-policies/${encodeURIComponent(policy.id)}`, "PATCH", { state: next, expected_version: policy.version }); await policies.mutate(); }
    catch (e) { setError(e); } finally { setBusy(""); }
  }
  return <section className="space-y-4 border-t pt-5"><div className="flex items-center justify-between gap-3"><h3 className="inline-flex items-center gap-1 text-lg font-semibold">登録済みアクション・実行ポリシー<HelpPopover label="登録済みアクション・実行ポリシー">Agent が外部操作してよい範囲を保存します。保存だけでは外部操作しません。有効化時も組織の許可と接続準備が必要です。結果が要確認の操作は自動再送せず照合します。</HelpPopover></h3>{canManage && <Button type="button" onClick={() => setSelected("new")}>ポリシーを追加</Button>}</div>
    <EmployeeNotice error={policies.error || registry.error || connections.error || error} reload={() => { void policies.mutate(); void registry.mutate(); void connections.mutate(); }} />
    <ul className="space-y-3">{policies.data?.policies.map(p => {
      const definition = registry.data?.actions.find(a => a.action_type === p.current_revision?.action_type);
      return <li className="space-y-2 rounded-lg border p-3" key={p.id}><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{p.display_name}</span><EmployeeStatus state={p.state} />{p.current_revision && <EmployeeStatus state={p.current_revision.authorization_mode} />}</div>
        <p className="text-sm">{definition?.display_name ?? p.current_revision?.action_type ?? "revision 未設定"} · v{p.current_revision?.version ?? 0} · 接続 {connections.data?.find(c => c.id === p.current_revision?.connection_id)?.display_name ?? "未設定"}</p>
        {definition && <p className="text-sm">提供元の状態: <EmployeeStatus state={definition.status} /></p>}
        {p.current_revision?.action_type === ENGAGEMENT_ACTION && <p className="break-all text-sm">CrowdWorks検索 {p.current_revision.constraints.search_urls?.length ?? 0}件 / {p.current_revision.constraints.scan_interval_minutes ?? 30}分ごと / 1回最大{p.current_revision.constraints.max_applications_per_scan ?? 5}応募 / 最低期待利益 {p.current_revision.constraints.min_expected_profit ?? 0} {p.current_revision.constraints.currency ?? "JPY"} / 最低利益/時間 {p.current_revision.constraints.min_profit_per_hour ?? 0} {p.current_revision.constraints.currency ?? "JPY"}/h</p>}
        {canManage && p.state !== "retired" && <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setSelected(p.id)}>編集・新しい版</Button>
          <Button type="button" size="sm" disabled={busy === p.id || !p.current_revision || definition?.status !== "automatable" || p.state === "active"} onClick={() => void state(p, "active")}>ポリシーを有効化</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy === p.id || p.state !== "active"} onClick={() => void state(p, "paused")}>一時停止</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy === p.id} onClick={() => { if (window.confirm("ポリシーを廃止します。再開できません。続行しますか？")) void state(p, "retired"); }}>廃止</Button></div>}
      </li>;
    })}</ul>
    {!policies.isLoading && !policies.data?.policies.length && <p className="text-sm text-muted-foreground">実行ポリシーはまだありません。</p>}
    {selected && registry.data && connections.data && <PolicyEditor key={`${selected}:${policies.data?.policies.find(p => p.id === selected)?.version ?? 0}`} agentId={agentId} policy={policies.data?.policies.find(p => p.id === selected)} definitions={registry.data.actions} connections={connections.data} onSaved={async id => { await policies.mutate(); setSelected(id); }} onClose={() => setSelected(null)} />}
  </section>;
}
function PolicyEditor({ agentId, policy, definitions, connections, onSaved, onClose }: { agentId: string; policy?: EmployeePolicy; definitions: ActionDefinition[]; connections: OperationsConnection[]; onSaved: (id: string) => Promise<void>; onClose: () => void }) {
  const revision = policy?.current_revision;
  const [name, setName] = useState(policy?.display_name ?? "");
  const [actionType, setAction] = useState(revision?.action_type ?? ENGAGEMENT_ACTION);
  const [connectionId, setConnection] = useState(revision?.connection_id ?? "");
  const [mode, setMode] = useState(revision?.authorization_mode ?? "human_approval");
  const [crowdworks, setCrowdworks] = useState<CrowdWorksConstraints>({
    search_urls: ["https://crowdworks.jp/public/jobs/"],
    scan_interval_minutes: 30,
    max_pages_per_scan: 3,
    max_opportunities_per_scan: 100,
    max_applications_per_scan: 5,
    min_expected_profit: 0,
    min_profit_per_hour: 0,
    currency: "JPY",
    llm_provider: null,
    llm_model: null,
    llm_effort: null,
    ...(revision?.action_type === ENGAGEMENT_ACTION ? revision.constraints : {}),
  });
  const [dedupe, setDedupe] = useState(revision?.dedupe_window_seconds ?? 86400);
  const [windowSeconds, setWindowSeconds] = useState(revision?.rate_limit.window_seconds ?? 2592000);
  const [maxActions, setMaxActions] = useState(revision?.rate_limit.max_actions ?? 4);
  const [fallback, setFallback] = useState(revision?.fallback_behavior ?? "block");
  const [consent, setConsent] = useState(false);
  const [stable, setStable] = useState(policy);
  const [keys] = useState(() => ({ stable: employeeKey(), revision: employeeKey() }));
  const createIntent = useRef<{ agent_id: string; display_name: string; idempotency_key: string } | null>(null);
  const definition = definitions.find(d => d.action_type === actionType);
  const updateCrowdworks = <K extends keyof CrowdWorksConstraints>(key: K, value: CrowdWorksConstraints[K]) => { setCrowdworks({ ...crowdworks, [key]: value }); setConsent(false); };
  return <div className="space-y-3 rounded-lg border bg-card p-4"><div className="flex justify-between"><h4 className="font-semibold">実行ポリシーの編集</h4><Button type="button" size="sm" variant="ghost" onClick={onClose}>閉じる</Button></div>
    <EmployeeForm label="ポリシー revision を保存" reload={() => { if (stable) void onSaved(stable.id); }} onSave={async () => {
      if (actionType !== ENGAGEMENT_ACTION) throw new Error("unsupported form");
      if ((!crowdworks.search_urls.length || crowdworks.search_urls.some(url => !/^https?:\/\/(?:www\.)?crowdworks\.jp\//u.test(url)))) throw new Error("CrowdWorksの検索URLを1件以上入力してください");
      if (mode === "bounded_auto" && !consent) throw new Error("review required");
      let saved = stable;
      if (!saved) { createIntent.current ??= { agent_id: agentId, display_name: name.trim(), idempotency_key: keys.stable }; saved = (await employeeRequest<{ policy: EmployeePolicy }>("/api/agent-action-policies", "POST", createIntent.current)).policy; setStable(saved); }
      if (saved.display_name !== name.trim()) { saved = (await employeeRequest<{ policy: EmployeePolicy }>(`/api/agent-action-policies/${encodeURIComponent(saved.id)}`, "PATCH", { display_name: name.trim(), expected_version: saved.version })).policy; setStable(saved); }
      const result = await employeeRequest<{ policy: EmployeePolicy }>(`/api/agent-action-policies/${encodeURIComponent(saved.id)}/revisions`, "POST", {
        expected_version: saved.version, idempotency_key: keys.revision, action_type: actionType, connection_id: connectionId, authorization_mode: mode,
        constraints: crowdworks,
        rate_limit: { window_seconds: windowSeconds, max_actions: maxActions }, dedupe_window_seconds: dedupe, fallback_behavior: fallback,
        ...(revision?.active_from ? { active_from: revision.active_from } : {}), ...(revision?.active_until ? { active_until: revision.active_until } : {}),
      }); await onSaved(result.policy.id);
    }}>
      <EmployeeField label="ポリシー名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => setName(e.target.value)} /></EmployeeField>
      <EmployeeSelect label="登録済みアクション" required value={actionType} onChange={v => { setAction(v); setConnection(""); setConsent(false); }} options={definitions.map(d => ({ value: d.action_type, label: `${d.display_name} — ${d.status}`, disabled: d.action_type !== ENGAGEMENT_ACTION }))} />
      {definition?.status !== "automatable" && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">提供元は未検証または利用不可です。下書きの保存は可能ですが、有効化と注文実行はできません。</p>}
      <EmployeeSelect label="登録済み接続先" required value={connectionId} onChange={v => { setConnection(v); setConsent(false); }} options={connections.filter(c => definition?.connection_provider_keys.includes(c.provider_key)).map(c => ({ value: c.id, label: c.display_name }))} />
      <EmployeeSelect label="承認方式" required value={mode} onChange={v => { setMode(v as typeof mode); setConsent(false); }} options={[{ value: "human_approval", label: "毎回人間承認" }, { value: "bounded_auto", label: "範囲内自動実行" }]} />
      <div className="space-y-3 rounded border p-3">
        <p className="inline-flex items-center gap-1 text-sm font-medium">CrowdWorks応募ポリシー<HelpPopover label="CrowdWorks応募ポリシー"><p>選択中PCのログイン済みEdgeからCrowdWorks検索結果を一括取得し、採算評価→応募→契約監視→レビュー用成果物作成まで既存AgentWorkで進めます。</p><p>評価・応募文作成のモデルはAgentの「職務・モデル」で設定した役割ごとのモデルを使います。</p></HelpPopover></p>
        <EmployeeField label="検索URL（1行1件）"><textarea className={employeeInputClass + " min-h-24"} required value={crowdworks.search_urls.join("\n")} onChange={e => updateCrowdworks("search_urls", e.target.value.split(/\r?\n/u).map(v => v.trim()).filter(Boolean))} /></EmployeeField>
        <div className="grid gap-4 md:grid-cols-2">
          <EmployeeField label="スキャン間隔（分）"><input type="number" className={employeeInputClass} required min={5} max={1440} step={1} value={crowdworks.scan_interval_minutes} onChange={e => updateCrowdworks("scan_interval_minutes", Number(e.target.value))} /></EmployeeField>
          <EmployeeField label="1検索あたり最大ページ"><input type="number" className={employeeInputClass} required min={1} max={20} step={1} value={crowdworks.max_pages_per_scan} onChange={e => updateCrowdworks("max_pages_per_scan", Number(e.target.value))} /></EmployeeField>
          <EmployeeField label="1回の最大評価案件数"><input type="number" className={employeeInputClass} required min={1} max={500} step={1} value={crowdworks.max_opportunities_per_scan} onChange={e => updateCrowdworks("max_opportunities_per_scan", Number(e.target.value))} /></EmployeeField>
          <EmployeeField label="1回の最大応募案件数"><input type="number" className={employeeInputClass} required min={1} max={50} step={1} value={crowdworks.max_applications_per_scan} onChange={e => updateCrowdworks("max_applications_per_scan", Number(e.target.value))} /></EmployeeField>
          <EmployeeField label="最低期待利益"><input type="number" className={employeeInputClass} required step="any" value={crowdworks.min_expected_profit} onChange={e => updateCrowdworks("min_expected_profit", Number(e.target.value))} /></EmployeeField>
          <EmployeeField label="最低期待利益/時間"><input type="number" className={employeeInputClass} required step="any" value={crowdworks.min_profit_per_hour} onChange={e => updateCrowdworks("min_profit_per_hour", Number(e.target.value))} /></EmployeeField>
          <EmployeeSelect label="通貨" required value={crowdworks.currency} onChange={v => updateCrowdworks("currency", v)} options={[{ value: "JPY", label: "JPY" }]} />
        </div>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="同一操作の重複禁止期間（秒）"><input type="number" className={employeeInputClass} required min={1} step={1} value={dedupe} onChange={e => { setDedupe(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeField label="回数制限の集計期間（秒）"><input type="number" className={employeeInputClass} required min={1} step={1} value={windowSeconds} onChange={e => { setWindowSeconds(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeField label="期間内の最大実行回数"><input type="number" className={employeeInputClass} required min={1} step={1} value={maxActions} onChange={e => { setMaxActions(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeSelect label="範囲外・準備不足のとき" required value={fallback} onChange={v => { setFallback(v); setConsent(false); }} options={[{ value: "block", label: "停止" }, { value: "require_approval", label: "人間の承認を要求" }]} />
      </div>
      {(revision?.active_from || revision?.active_until) && <p className="text-sm">有効期間を保持: {revision.active_from || "開始指定なし"} ～ {revision.active_until || "終了指定なし"}</p>}
      {mode === "bounded_auto" && <div className="space-y-2 rounded border border-amber-500/50 p-3 text-sm"><p>この範囲内の操作は毎回の人間承認なしで実行されます。</p>
        <p>CrowdWorks検索{crowdworks.search_urls.length}件、最低期待利益 {crowdworks.min_expected_profit} {crowdworks.currency}、最低利益/時間 {crowdworks.min_profit_per_hour} {crowdworks.currency}/h、1回最大{crowdworks.max_applications_per_scan}応募、重複禁止 {dedupe}秒、{windowSeconds}秒あたり{maxActions}回。</p>
        <EmployeeCheck label="上記の許可範囲を確認しました" required checked={consent} onChange={setConsent} /></div>}
    </EmployeeForm>
  </div>;
}
