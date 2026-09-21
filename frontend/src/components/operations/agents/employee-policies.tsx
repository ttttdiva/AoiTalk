"use client";

import { useRef, useState } from "react";
import useSWR from "swr";
import { operationsApi, type OperationsConnection } from "@/lib/operations-api";
import { employeeKey, employeeRequest, type ActionDefinition, type EmployeePolicy, type ProcurementConstraints } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";

type TransferRoute = { id: string; agent_id: string; display_name: string; provider_route_ref: string; connection_id: string; state: string };
type TransferCatalog = { route_keys: { key: string; connection_id: string; destination_keys: { key: string; display_name: string }[] }[] };

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
  return <section className="space-y-4 border-t pt-5"><div className="flex items-center justify-between gap-3"><h3 className="text-lg font-semibold">登録済みアクション・実行ポリシー</h3>{canManage && <Button type="button" onClick={() => setSelected("new")}>ポリシーを追加</Button>}</div>
    <EmployeeNotice error={policies.error || registry.error || connections.error || error} reload={() => { void policies.mutate(); void registry.mutate(); void connections.mutate(); }} />
    <ul className="space-y-3">{policies.data?.policies.map(p => {
      const definition = registry.data?.actions.find(a => a.action_type === p.current_revision?.action_type);
      return <li className="space-y-2 rounded-lg border p-3" key={p.id}><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{p.display_name}</span><EmployeeStatus state={p.state} />{p.current_revision && <EmployeeStatus state={p.current_revision.authorization_mode} />}</div>
        <p className="text-sm">{definition?.display_name ?? p.current_revision?.action_type ?? "revision 未設定"} · v{p.current_revision?.version ?? 0} · 接続 {connections.data?.find(c => c.id === p.current_revision?.connection_id)?.display_name ?? "未設定"}</p>
        {definition && <p className="text-sm">提供元の状態: <EmployeeStatus state={definition.status} /></p>}
        {p.current_revision?.action_type === "procurement.place_order" && <p className="text-sm">商品 {p.current_revision.constraints.fixed_item_ref} / 数量 {p.current_revision.constraints.min_quantity}–{p.current_revision.constraints.max_quantity} / 配送先 {p.current_revision.constraints.fixed_ship_to_ref} / 上限 {p.current_revision.constraints.max_order_total_minor} {p.current_revision.constraints.currency}（最小通貨単位） / 重複禁止 {p.current_revision.dedupe_window_seconds}秒</p>}
        {p.current_revision?.action_type === "telephony.transfer_call" && <p className="break-all text-sm">電話受付 {p.current_revision.constraints.route_id} / 許可する転送先 {p.current_revision.constraints.allowed_destination_keys?.join("、")}</p>}
        {canManage && p.state !== "retired" && <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setSelected(p.id)}>編集・新しい版</Button>
          <Button type="button" size="sm" disabled={busy === p.id || !p.current_revision || definition?.status !== "automatable" || p.state === "active"} onClick={() => void state(p, "active")}>ポリシーを有効化</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy === p.id || p.state !== "active"} onClick={() => void state(p, "paused")}>一時停止</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy === p.id} onClick={() => { if (window.confirm("ポリシーを廃止します。再開できません。続行しますか？")) void state(p, "retired"); }}>廃止</Button></div>}
      </li>;
    })}</ul>
    {!policies.isLoading && !policies.data?.policies.length && <p className="text-sm text-muted-foreground">実行ポリシーはまだありません。自動化ルールへ接続する前に、操作の許可範囲を保存します。</p>}
    {selected && registry.data && connections.data && <PolicyEditor key={`${selected}:${policies.data?.policies.find(p => p.id === selected)?.version ?? 0}`} agentId={agentId} policy={policies.data?.policies.find(p => p.id === selected)} definitions={registry.data.actions} connections={connections.data} onSaved={async id => { await policies.mutate(); setSelected(id); }} onClose={() => setSelected(null)} />}
  </section>;
}
function PolicyEditor({ agentId, policy, definitions, connections, onSaved, onClose }: { agentId: string; policy?: EmployeePolicy; definitions: ActionDefinition[]; connections: OperationsConnection[]; onSaved: (id: string) => Promise<void>; onClose: () => void }) {
  const revision = policy?.current_revision;
  const [name, setName] = useState(policy?.display_name ?? "");
  const [actionType, setAction] = useState(revision?.action_type ?? "");
  const [connectionId, setConnection] = useState(revision?.connection_id ?? "");
  const [mode, setMode] = useState(revision?.authorization_mode ?? "human_approval");
  const [constraints, setConstraints] = useState<ProcurementConstraints>({ fixed_item_ref: "", fixed_ship_to_ref: "", min_quantity: 1, max_quantity: 2, default_quantity: 1, currency: "JPY", max_order_total_minor: 0, require_quote_before_execute: true, ...(revision?.action_type === "procurement.place_order" ? revision.constraints : {}) });
  const [dedupe, setDedupe] = useState(revision?.dedupe_window_seconds ?? 86400);
  const [windowSeconds, setWindowSeconds] = useState(revision?.rate_limit.window_seconds ?? 2592000);
  const [maxActions, setMaxActions] = useState(revision?.rate_limit.max_actions ?? 4);
  const [fallback, setFallback] = useState(revision?.fallback_behavior ?? "block");
  const [consent, setConsent] = useState(false);
  const [stable, setStable] = useState(policy);
  const [keys] = useState(() => ({ stable: employeeKey(), revision: employeeKey() }));
  const createIntent = useRef<{ agent_id: string; display_name: string; idempotency_key: string } | null>(null);
  const [transferRoute, setTransferRoute] = useState(revision?.constraints.route_id ?? "");
  const [destinations, setDestinations] = useState(revision?.constraints.allowed_destination_keys ?? []);
  const transfer = actionType === "telephony.transfer_call";
  const phoneRoutes = useSWR(transfer ? ["employee-policy-phone-routes", agentId] : null, () => employeeRequest<{ routes: TransferRoute[] }>(`/api/telephony/routes?agent_id=${encodeURIComponent(agentId)}`), { shouldRetryOnError: false });
  const phoneCatalog = useSWR(transfer ? "employee-policy-phone-catalog" : null, () => employeeRequest<TransferCatalog>("/api/telephony/catalog"), { shouldRetryOnError: false });
  const routeOptions = (phoneRoutes.data?.routes ?? []).filter(r => r.agent_id === agentId && r.connection_id === connectionId && r.state !== "retired");
  const route = routeOptions.find(r => r.id === transferRoute);
  const destinationOptions = phoneCatalog.data?.route_keys.find(k => k.key === route?.provider_route_ref && k.connection_id === connectionId)?.destination_keys ?? [];
  const transferValid = !!route && destinations.length > 0 && destinations.every(key => destinationOptions.some(d => d.key === key));
  const definition = definitions.find(d => d.action_type === actionType);
  const update = <K extends keyof ProcurementConstraints>(key: K, value: ProcurementConstraints[K]) => { setConstraints({ ...constraints, [key]: value }); setConsent(false); };
  return <div className="space-y-3 rounded-lg border bg-card p-4"><div className="flex justify-between"><h4 className="font-semibold">実行ポリシーの編集</h4><Button type="button" size="sm" variant="ghost" onClick={onClose}>閉じる</Button></div>
    <EmployeeForm label="ポリシー revision を保存" reload={() => { if (stable) void onSaved(stable.id); }} onSave={async () => {
      if (definition?.action_type !== "procurement.place_order" && !transfer) throw new Error("unsupported form");
      if (transfer && !transferValid) throw new Error("invalid route binding");
      if (mode === "bounded_auto" && !consent) throw new Error("review required");
      let saved = stable;
      if (!saved) { createIntent.current ??= { agent_id: agentId, display_name: name.trim(), idempotency_key: keys.stable }; saved = (await employeeRequest<{ policy: EmployeePolicy }>("/api/agent-action-policies", "POST", createIntent.current)).policy; setStable(saved); }
      if (saved.display_name !== name.trim()) { saved = (await employeeRequest<{ policy: EmployeePolicy }>(`/api/agent-action-policies/${encodeURIComponent(saved.id)}`, "PATCH", { display_name: name.trim(), expected_version: saved.version })).policy; setStable(saved); }
      const result = await employeeRequest<{ policy: EmployeePolicy }>(`/api/agent-action-policies/${encodeURIComponent(saved.id)}/revisions`, "POST", {
        expected_version: saved.version, idempotency_key: keys.revision, action_type: actionType, connection_id: connectionId, authorization_mode: mode,
        constraints: transfer ? { route_id: transferRoute, allowed_destination_keys: destinations } : constraints,
        rate_limit: { window_seconds: windowSeconds, max_actions: maxActions }, dedupe_window_seconds: dedupe, fallback_behavior: fallback,
        ...(revision?.active_from ? { active_from: revision.active_from } : {}), ...(revision?.active_until ? { active_until: revision.active_until } : {}),
      }); await onSaved(result.policy.id);
    }}>
      <EmployeeField label="ポリシー名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => setName(e.target.value)} /></EmployeeField>
      <EmployeeSelect label="登録済みアクション" required value={actionType} onChange={v => { setAction(v); setConnection(""); setTransferRoute(""); setDestinations([]); setConsent(false); }} options={definitions.map(d => ({ value: d.action_type, label: `${d.display_name} — ${d.status}`, disabled: d.action_type !== "procurement.place_order" && d.action_type !== "telephony.transfer_call" }))} />
      {definition?.status !== "automatable" && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">提供元は未検証または利用不可です。下書きの保存は可能ですが、有効化と注文実行はできません。</p>}
      <EmployeeSelect label="登録済み接続先" required value={connectionId} onChange={v => { setConnection(v); setTransferRoute(""); setDestinations([]); setConsent(false); }} options={connections.filter(c => definition?.connection_provider_keys.includes(c.provider_key)).map(c => ({ value: c.id, label: c.display_name }))} />
      <EmployeeSelect label="承認方式" required value={mode} onChange={v => { setMode(v as typeof mode); setConsent(false); }} options={[{ value: "human_approval", label: "毎回人間承認" }, { value: "bounded_auto", label: "範囲内自動実行" }]} />
      {transfer ? <div className="space-y-3 rounded border p-3">
        <EmployeeNotice error={phoneRoutes.error || phoneCatalog.error} reload={() => { void phoneRoutes.mutate(); void phoneCatalog.mutate(); }} />
        <EmployeeSelect label="転送を許可する電話受付" required value={transferRoute} onChange={v => { setTransferRoute(v); setDestinations([]); setConsent(false); }} options={routeOptions.map(r => ({ value: r.id, label: r.display_name }))} />
        {!routeOptions.length && <p className="text-sm">この接続先の受付がありません。「電話受付」で転送なしの下書きを作成してから、ここで転送先を許可してください。</p>}
        <fieldset className="space-y-2"><legend className="mb-2 text-sm">登録済みの転送先</legend>{destinationOptions.map(d => <EmployeeCheck key={d.key} label={d.display_name} checked={destinations.includes(d.key)} onChange={checked => { setDestinations(checked ? [...destinations, d.key] : destinations.filter(key => key !== d.key)); setConsent(false); }} />)}</fieldset>
        {!transferValid && <p className="text-sm">電話受付と、カタログに登録された転送先を1つ以上選んでください。電話番号・URIの自由入力はできません。</p>}
        <p className="text-sm text-muted-foreground">このポリシーを有効化した後、電話受付の編集画面でポリシーの正確な revision を紐付けます。</p>
      </div> : <><p className="text-sm text-muted-foreground">商品・配送先は管理者が確認済みの参照キーを指定します。URL、APIキー、注文検索文は入力しないでください。</p>
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="固定商品参照キー"><input className={employeeInputClass} required maxLength={120} pattern={"[A-Za-z0-9][A-Za-z0-9_.:\\-]{0,119}"} value={constraints.fixed_item_ref} onChange={e => update("fixed_item_ref", e.target.value)} /></EmployeeField>
        <EmployeeField label="固定配送先参照キー"><input className={employeeInputClass} required maxLength={120} pattern={"[A-Za-z0-9][A-Za-z0-9_.:\\-]{0,119}"} value={constraints.fixed_ship_to_ref} onChange={e => update("fixed_ship_to_ref", e.target.value)} /></EmployeeField>
        {([ ["min_quantity", "最小数量"], ["max_quantity", "最大数量"], ["default_quantity", "通常数量"] ] as const).map(([key, label]) => <EmployeeField key={key} label={label}><input type="number" className={employeeInputClass} required min={key === "max_quantity" || key === "default_quantity" ? constraints.min_quantity : 1} max={key === "default_quantity" || key === "min_quantity" ? constraints.max_quantity : 10000} step={1} value={constraints[key]} onChange={e => update(key, Number(e.target.value))} /></EmployeeField>)}
        <EmployeeField label="注文総額上限（最小通貨単位・JPYは円）"><input type="number" className={employeeInputClass} required min={1} step={1} value={constraints.max_order_total_minor} onChange={e => update("max_order_total_minor", Number(e.target.value))} /></EmployeeField>
        <EmployeeSelect label="通貨" required value={constraints.currency} onChange={v => update("currency", v)} options={[{ value: "JPY", label: "JPY" }, { value: "USD", label: "USD" }]} />
      </div><EmployeeCheck label="実行前の見積確認を必須にする" checked={constraints.require_quote_before_execute} onChange={v => update("require_quote_before_execute", v)} /></>}
      <div className="grid gap-4 md:grid-cols-2">
        <EmployeeField label="同一操作の重複禁止期間（秒）"><input type="number" className={employeeInputClass} required min={1} step={1} value={dedupe} onChange={e => { setDedupe(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeField label="回数制限の集計期間（秒）"><input type="number" className={employeeInputClass} required min={1} step={1} value={windowSeconds} onChange={e => { setWindowSeconds(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeField label="期間内の最大実行回数"><input type="number" className={employeeInputClass} required min={1} step={1} value={maxActions} onChange={e => { setMaxActions(Number(e.target.value)); setConsent(false); }} /></EmployeeField>
        <EmployeeSelect label="範囲外・準備不足のとき" required value={fallback} onChange={v => { setFallback(v); setConsent(false); }} options={[{ value: "block", label: "停止" }, { value: "require_approval", label: "人間の承認を要求" }]} />
      </div>
      {(revision?.active_from || revision?.active_until) && <p className="text-sm">有効期間を保持: {revision.active_from || "開始指定なし"} ～ {revision.active_until || "終了指定なし"}</p>}
      {mode === "bounded_auto" && <div className="space-y-2 rounded border border-amber-500/50 p-3 text-sm"><p>この範囲内の操作は毎回の人間承認なしで実行されます。</p>
        {transfer ? <p>電話受付 {route?.display_name || "未設定"}、転送先 {destinations.join("、") || "未設定"}、重複禁止 {dedupe}秒、{windowSeconds}秒あたり{maxActions}回。</p> : <p>商品 {constraints.fixed_item_ref || "未設定"}、数量 {constraints.min_quantity}–{constraints.max_quantity}、配送先 {constraints.fixed_ship_to_ref || "未設定"}、総額 {constraints.max_order_total_minor} {constraints.currency}、重複禁止 {dedupe}秒、{windowSeconds}秒あたり{maxActions}回。</p>}
        <EmployeeCheck label="上記の許可範囲を確認しました" required checked={consent} onChange={setConsent} /></div>}
      <p className="text-sm text-muted-foreground">保存だけでは注文しません。有効化時も組織の許可と検証済み認証情報が必要です。結果が要確認の操作は再注文せず照合します。</p>
    </EmployeeForm>
  </div>;
}
