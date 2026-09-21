"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { employeeKey, employeeRequest } from "@/lib/agent-employees-api";
import { operationsApi, type OperationsConnection } from "@/lib/operations-api";

type EmployeePanelProps = { agentId: string; revisionId: string | null; canManage: boolean };
type Policy = {
  id: string; agent_id: string; display_name: string; state: string;
  current_revision: {
    id: string; action_type: string; connection_id: string;
    constraints: { allowed_destination_keys?: string[]; route_id?: string };
  } | null;
};
type RouteKey = {
  key: string; display_name: string; called_number_masked: string; connection_id: string;
  destination_keys: { key: string; display_name: string }[];
};
type PhoneCatalog = { provider: string; provider_status: string; route_keys: RouteKey[] };
type PhoneEmployeeRevision = { id: string; agent_id: string; version: number; display_name: string; agent_team_id: string; execution_profile_id: string };
type Hours = { days?: number[]; start?: string; end?: string };
type PhoneRoute = {
  id: string; display_name: string; state: "draft" | "active" | "paused" | "retired";
  version: number; provider: string; connection_id: string; provider_route_ref: string;
  called_number_masked: string; agent_id: string; agent_revision_id: string;
  timezone: string; business_hours_json: Hours; greeting_override: string | null;
  transfer_policy_json: { action_policy_revision_id?: string; destination_keys?: string[] };
  fallback_mode: string;
};
type Readiness = { ready: boolean; reason_codes: string[]; provider_status: string; external_setup: string };
type PhoneCall = { id: string; state: string; caller_masked: string; called_masked: string; received_at: string };
const fieldClass = "mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-sm";
const panelClass = "space-y-4 rounded-xl border bg-card p-4 text-card-foreground";
const statuses: Record<string, string> = {
  draft: "下書き", active: "稼働中", paused: "一時停止", retired: "終了",
  unverified: "未検証", unavailable: "利用不可", verified: "確認済み",
  verification_pending: "確認待ち", disabled: "無効化済み", invalid: "認証無効",
  unsupported: "非対応", key_unavailable: "暗号鍵を利用できません",
  received: "着信", accepting: "応答中", accepted: "応答済み", rejected: "拒否",
  ended: "終了", completed: "完了", transferred: "転送要求受付済み", uncertain: "要確認", failed: "失敗",
};
const readinessLabels: Record<string, string> = {
  telephony_feature_disabled: "この環境では電話機能が無効です。",
  telephony_route_inactive: "受付は停止中です。設定を確認して有効化してください。",
  telephony_route_key_unavailable: "登録済み回線の設定を確認してください。",
  telephony_webhook_unconfigured: "接続のWebhookシークレットを設定してください。",
  telephony_authority_denied: "AI社員の権限・役割リビジョンを確認してください。",
  telephony_readiness_unavailable: "接続の認証情報とAI社員の権限を確認してください。",
};
function statusLabel(value: string) { return statuses[value] ?? "未確認"; }
function errorStatus(error: unknown): number | undefined {
  return error && typeof error === "object" && "status" in error && typeof error.status === "number" ? error.status : undefined;
}
function safeError(error: unknown): string {
  switch (errorStatus(error)) {
    case 401: return "ログイン状態を確認してください。";
    case 403: return "この操作を行う権限がありません。";
    case 409: return "他の操作で更新されました。再読み込みして最新の状態を確認してください。";
    case 422: return "入力内容または設定の組み合わせを確認してください。";
    case 503: return "接続先または機能を現在利用できません。";
    default: return "処理に失敗しました。再読み込みして確認してください。";
  }
}
function Failure({ message, reload, busy }: { message: string; reload: () => void; busy: boolean }) {
  return <div className="space-y-2"><p role="alert">{message}</p><Button variant="outline" disabled={busy} onClick={reload}>再読み込み</Button></div>;
}

type CredentialEnvelope = {
  credential: { id: string; revision: number; status: string; verified_at: string | null } | null;
  readiness: { ready: boolean; status: string; reason_code: string };
};
type CredentialAudit = { id: string; revision: number; event_type: string; created_at: string };
const auditLabels: Record<string, string> = { add: "登録", rotate: "差し替え", verify: "検証", disable: "無効化", rekey: "暗号鍵更新" };
function dateLabel(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
}

export function EmployeeIntegrations(props: EmployeePanelProps) {
  return <IntegrationsPanel key={`${props.agentId}:${props.revisionId}:${props.canManage}`} {...props} />;
}

function IntegrationsPanel({ agentId, canManage }: EmployeePanelProps) {
  const [connections, setConnections] = useState<OperationsConnection[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [routes, setRoutes] = useState<PhoneRoute[]>([]);
  const [usageError, setUsageError] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true;
    operationsApi.listConnections().then(items => {
      if (current) { setConnections(items); setSelectedId(previous => items.some(item => item.id === previous) ? previous : items[0]?.id ?? ""); setError(""); }
    }).catch(cause => { if (current) setError(safeError(cause)); })
      .finally(() => { if (current) setLoading(false); });
    Promise.all([
      employeeRequest<{ policies: Policy[] }>(`/api/agent-action-policies?agent_id=${encodeURIComponent(agentId)}`),
      employeeRequest<{ routes: PhoneRoute[] }>(`/api/telephony/routes?agent_id=${encodeURIComponent(agentId)}`),
    ]).then(([policyResult, routeResult]) => {
      if (current) { setPolicies(policyResult.policies); setRoutes(routeResult.routes); setUsageError(false); }
    }).catch(() => { if (current) setUsageError(true); });
    return () => { current = false; };
  }, [agentId, reload]);
  const selected = connections.find(item => item.id === selectedId);
  return <section aria-label="外部接続・認証情報" className="space-y-4">
    <h3 className="font-semibold">外部接続・認証情報</h3>
    <p className="text-sm text-muted-foreground">接続に保存した認証情報は、この接続を使う他のAI社員にも影響します。</p>
    {loading && <p role="status">接続を読み込み中…</p>}
    {error && <Failure message={error} busy={loading} reload={() => { setLoading(true); setReload(value => value + 1); }} />}
    {!loading && !error && <>
      {connections.length === 0 ? <p>接続がありません。先にOperationsの接続設定で接続を登録してください。</p> :
        <label className="block">接続<AppSelect aria-label="接続" className="mt-1 w-full" value={selectedId} onValueChange={setSelectedId}>
          {connections.map(item => <option key={item.id} value={item.id}>{item.display_name}（{item.provider_key}）</option>)}
        </AppSelect></label>}
      {usageError && <p className="text-sm">このAI社員での接続の使用状況を取得できませんでした。</p>}
      {selected && <>
        <p className="text-sm">このAI社員の使用先: {[...policies.filter(item => item.current_revision?.connection_id === selected.id).map(item => item.display_name), ...routes.filter(item => item.connection_id === selected.id).map(item => item.display_name)].join("、") || (usageError ? "未確認" : "まだ割り当てられていません")}</p>
        <CredentialCard key={`${selected.id}:${reload}`} connection={selected} canManage={canManage} />
      </>}
    </>}
  </section>;
}

function CredentialCard({ connection, canManage }: { connection: OperationsConnection; canManage: boolean }) {
  const [data, setData] = useState<CredentialEnvelope | null>(null);
  const [audit, setAudit] = useState<CredentialAudit[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [reload, setReload] = useState(0);
  const lock = useRef(false);
  const form = useRef<HTMLFormElement>(null);
  const path = `/api/integrations/connections/${encodeURIComponent(connection.id)}/credential`;
  useEffect(() => {
    let current = true;
    employeeRequest<CredentialEnvelope>(path).then(result => {
      if (current) { setData(result); setError(""); setStale(false); }
    }).catch(cause => { if (current) setError(safeError(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [path, reload]);
  const refresh = () => { form.current?.reset(); setLoading(true); setAudit(null); setReload(value => value + 1); };
  async function mutate(method: string, suffix: string, body: unknown) {
    if (lock.current || !canManage || !data || stale || loading) return;
    lock.current = true; setBusy(true); setError("");
    try { setData(await employeeRequest<CredentialEnvelope>(`${path}${suffix}`, method, body)); setAudit(null); }
    catch (cause) { setError(safeError(cause)); setStale(true); }
    finally { lock.current = false; setBusy(false); }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const apiKey = String(fields.get("api_key") ?? "");
    const webhookSecret = String(fields.get("webhook_secret") ?? "");
    event.currentTarget.reset();
    if (!apiKey.trim()) { setError("APIキーを入力してください。"); return; }
    void mutate(data?.credential ? "PUT" : "POST", "", {
      credential_kind: "api_key", payload: { api_key: apiKey, ...(webhookSecret ? { webhook_secret: webhookSecret } : {}) },
      expected_revision: data?.credential?.revision ?? 0,
    });
  }
  async function loadAudit() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { setAudit((await employeeRequest<{ items: CredentialAudit[] }>(`${path}/audit`)).items); }
    catch (cause) { setError(safeError(cause)); }
    finally { lock.current = false; setBusy(false); }
  }
  return <article className={panelClass} aria-label={`${connection.display_name}の認証情報`}>
    <h4 className="font-medium">{connection.display_name}の認証情報</h4>
    {loading && <p role="status">認証状態を読み込み中…</p>}
    {error && <Failure message={error} reload={refresh} busy={busy || loading} />}
    {data && !loading && <>
      <p role="status">認証情報: {data.credential ? "登録済み（非表示）" : "未登録"} · {data.credential ? statusLabel(data.credential.status) : "未設定"}</p>
      <p className="text-sm">{data.readiness.ready ? "接続の認証準備が整っています" : "接続の認証準備が完了していません"}</p>
      {data.credential && <p className="text-sm">リビジョン: {data.credential.revision} · 最終確認: {dateLabel(data.credential.verified_at)}</p>}
      {canManage ? <>
        <form ref={form} onSubmit={submit} className="space-y-3" autoComplete="off">
          <fieldset disabled={busy || stale} className="space-y-3">
            <label className="block">APIキー<input className={fieldClass} name="api_key" type="password" autoComplete="new-password" required maxLength={8192} /></label>
            {connection.provider_key === "openai_realtime_sip" && <label className="block">Webhookシークレット（任意）<input className={fieldClass} name="webhook_secret" type="password" autoComplete="new-password" maxLength={8192} /></label>}
            <p className="text-sm text-muted-foreground">入力値は送信時に消去します。保存後に秘密を再表示することはできません。差し替え時は必要なWebhookシークレットも再入力してください。</p>
            <Button type="submit" disabled={busy || stale}>{data.credential ? "認証情報を差し替え" : "認証情報を登録"}</Button>
          </fieldset>
        </form>
        {data.credential && <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={busy || stale || data.credential.status === "disabled"} onClick={() => { form.current?.reset(); void mutate("POST", "/verify", { expected_revision: data.credential!.revision }); }}>認証情報を検証</Button>
          <Button variant="destructive" disabled={busy || stale || data.credential.status === "disabled"} onClick={() => { form.current?.reset(); void mutate("POST", "/disable", { expected_revision: data.credential!.revision }); }}>認証情報を無効化</Button>
        </div>}
      </> : <p className="text-sm text-muted-foreground">閲覧専用です。認証情報の変更には管理権限が必要です。</p>}
      <Button variant="outline" disabled={busy || stale} onClick={() => void loadAudit()}>認証情報の操作履歴</Button>
      {audit && <div><h5 className="font-medium">操作履歴（日本時間）</h5>{audit.length === 0 ? <p>操作履歴はありません。</p> : <ul>{audit.map(item => <li key={item.id}>{auditLabels[item.event_type] ?? "操作"} · v{item.revision} · {dateLabel(item.created_at)}</li>)}</ul>}</div>}
    </>}
  </article>;
}

export function EmployeePhone(props: EmployeePanelProps) {
  return <PhonePanel key={`${props.agentId}:${props.revisionId}:${props.canManage}`} {...props} />;
}

function PhonePanel({ agentId, revisionId, canManage }: EmployeePanelProps) {
  const [data, setData] = useState<{ catalog: PhoneCatalog; routes: PhoneRoute[]; policies: Policy[] } | null>(null);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const refresh = () => setReload(value => value + 1);
  useEffect(() => {
    let current = true;
    Promise.all([
      employeeRequest<PhoneCatalog>("/api/telephony/catalog"),
      employeeRequest<{ routes: PhoneRoute[] }>(`/api/telephony/routes?agent_id=${encodeURIComponent(agentId)}`),
      employeeRequest<{ policies: Policy[] }>(`/api/agent-action-policies?agent_id=${encodeURIComponent(agentId)}`),
    ]).then(([catalog, routes, policies]) => {
      if (current) { setData({ catalog, routes: routes.routes, policies: policies.policies }); setError(""); }
    }).catch(cause => { if (current) setError(safeError(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [agentId, reload]);
  const reloadAll = () => { setLoading(true); refresh(); };
  return <section aria-label="電話受付" className="space-y-4">
    <h3 className="font-semibold">電話受付</h3>
    <p className="text-sm text-muted-foreground">実際の公衆電話網（PSTN）との接続は未検証です。設定保存だけでは着信受付は開始しません。</p>
    {loading && <p role="status">電話設定を読み込み中…</p>}
    {error && <Failure message={error} reload={reloadAll} busy={loading} />}
    {data && !loading && !error && <>
      {data.catalog.route_keys.length === 0 ? <p>登録済みの電話回線がありません。管理者による接続先・回線カタログの設定が必要です。</p> :
        canManage && <PhoneRouteForm agentId={agentId} catalog={data.catalog} policies={data.policies} onSaved={reloadAll} />}
      {!canManage && <p className="text-sm text-muted-foreground">閲覧専用です。変更には管理権限が必要です。</p>}
      {data.routes.length === 0 && <p>このAI社員の電話受付は未設定です。</p>}
      {data.routes.map(route => <PhoneRouteCard key={`${route.id}:${route.version}:${reload}`} route={route} agentId={agentId} revisionId={revisionId} canManage={canManage} catalog={data.catalog} policies={data.policies} onSaved={reloadAll} />)}
    </>}
  </section>;
}

function PhoneRouteForm({ agentId, catalog, policies, route, onSaved }: {
  agentId: string; catalog: PhoneCatalog; policies: Policy[];
  route?: PhoneRoute; onSaved: () => void;
}) {
  const [name, setName] = useState(route?.display_name ?? "");
  const [routeKey, setRouteKey] = useState(route?.provider_route_ref ?? "");
  const [timezone, setTimezone] = useState(route?.timezone ?? "Asia/Tokyo");
  const [always, setAlways] = useState(!route?.business_hours_json.days);
  const [days, setDays] = useState(route?.business_hours_json.days ?? [0, 1, 2, 3, 4]);
  const [start, setStart] = useState(route?.business_hours_json.start ?? "09:00");
  const [end, setEnd] = useState(route?.business_hours_json.end ?? "17:00");
  const [greeting, setGreeting] = useState(route?.greeting_override ?? "");
  const [policyRevision, setPolicyRevision] = useState(route?.transfer_policy_json.action_policy_revision_id ?? "");
  const [destinations, setDestinations] = useState(route?.transfer_policy_json.destination_keys ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [boundRevision, setBoundRevision] = useState(route?.agent_revision_id ?? "");
  const [revisions, setRevisions] = useState<PhoneEmployeeRevision[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(true);
  const [revisionsError, setRevisionsError] = useState("");
  const [revisionReload, setRevisionReload] = useState(0);
  useEffect(() => {
    let current = true;
    employeeRequest<{ revisions: PhoneEmployeeRevision[] }>(`/api/agents/${encodeURIComponent(agentId)}/revisions`).then(result => {
      if (current) { setRevisions(result.revisions.filter(item => item.agent_id === agentId)); setRevisionsError(""); }
    }).catch(cause => { if (current) setRevisionsError(safeError(cause)); })
      .finally(() => { if (current) setRevisionsLoading(false); });
    return () => { current = false; };
  }, [agentId, revisionReload]);
  const requestLock = useRef(false);
  const idempotencyKey = useRef<string | null>(null);
  const selected = catalog.route_keys.find(item => item.key === routeKey);
  const choices = policies.filter(item => item.agent_id === agentId && item.state === "active" &&
    item.current_revision?.action_type === "telephony.transfer_call" && item.current_revision.connection_id === selected?.connection_id &&
    (!route || item.current_revision.constraints.route_id === route.id));
  const policy = choices.find(item => item.current_revision?.id === policyRevision)?.current_revision;
  const allowed = selected?.destination_keys.filter(item => policy?.constraints.allowed_destination_keys?.includes(item.key)) ?? [];
  const bindingValid = !policyRevision ? destinations.length === 0 : !!policy && destinations.length > 0 && destinations.length <= 32 && destinations.every(key => allowed.some(item => item.key === key));
  const revisionValid = !revisionsLoading && !revisionsError && revisions.some(item => item.id === boundRevision);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestLock.current || stale || !selected || !revisionValid || !bindingValid) return;
    if (!always && (days.length === 0 || start >= end)) { setError("営業曜日を選び、終了時刻を開始時刻より後にしてください。"); return; }
    try { new Intl.DateTimeFormat("ja-JP", { timeZone: timezone }).format(); }
    catch { setError("有効なタイムゾーンを入力してください（例: Asia/Tokyo）。"); return; }
    requestLock.current = true; setBusy(true); setError("");
    idempotencyKey.current ??= employeeKey();
    const body = {
      display_name: name.trim(), connection_id: selected.connection_id, provider_route_ref: selected.key,
      agent_id: agentId, agent_revision_id: boundRevision, timezone,
      business_hours_json: always ? {} : { days: [...days].sort(), start, end },
      greeting_override: greeting.trim(),
      transfer_policy_json: policyRevision ? { action_policy_revision_id: policyRevision, destination_keys: destinations } : {},
      fallback_mode: "reject",
      ...(route ? { expected_version: route.version } : { idempotency_key: idempotencyKey.current }),
    };
    try {
      await employeeRequest<{ route: PhoneRoute }>(route ? `/api/telephony/routes/${encodeURIComponent(route.id)}` : "/api/telephony/routes", route ? "PATCH" : "POST", body);
      onSaved();
    } catch (cause) { setError(safeError(cause)); setStale(errorStatus(cause) === 409); }
    finally { requestLock.current = false; setBusy(false); }
  }
  return <form onSubmit={submit} className={panelClass} aria-label={route ? "電話受付を編集" : "電話受付を設定"}>
    <h4 className="font-medium">{route ? "電話受付を編集" : "電話受付を設定"}</h4>
    <fieldset disabled={busy || stale} className="space-y-3">
      <label className="block">受付の表示名<input className={fieldClass} required maxLength={160} value={name} onChange={event => setName(event.target.value)} /></label>
      <label className="block">登録済み回線<AppSelect aria-label="登録済み回線" className="mt-1 w-full" disabled={busy || stale} required value={routeKey} onValueChange={value => { setRouteKey(value); setPolicyRevision(""); setDestinations([]); }}>
        <option value="">回線を選択</option>
        {catalog.route_keys.map(item => <option key={item.key} value={item.key}>{item.display_name}（{item.called_number_masked}）</option>)}
      </AppSelect></label>
      <label className="block">電話で使用する役割リビジョン<AppSelect aria-label="電話で使用する役割リビジョン" className="mt-1 w-full" required disabled={busy || stale || revisionsLoading || !!revisionsError} value={boundRevision} onValueChange={setBoundRevision}>
        <option value="">役割リビジョンを選択</option>
        {boundRevision && !revisions.some(item => item.id === boundRevision) && <option value={boundRevision} disabled>設定済みのリビジョン（{boundRevision}）を取得できていません</option>}
        {revisions.map(item => <option key={item.id} value={item.id}>v{item.version} · {item.display_name} · {item.agent_team_id} / {item.execution_profile_id}</option>)}
      </AppSelect></label>
      {revisionsLoading && <p role="status">役割リビジョンを読み込み中…</p>}
      {revisionsError && <Failure message={revisionsError} busy={busy || revisionsLoading} reload={() => { setRevisionsLoading(true); setRevisionReload(value => value + 1); }} />}
      {!revisionsLoading && !revisionsError && revisions.length === 0 && <p>電話受付を設定するには、先にAI社員の役割リビジョンを作成してください。</p>}
      <p className="text-sm">選択した版と実行プロファイルを使用します。電話受付用のプロファイルを確認してください。</p>
      {route && route.agent_revision_id !== boundRevision && <p className="text-sm">保存すると選択した役割リビジョンに更新されます。進行中の通話は元のリビジョンを使用します。</p>}
      <label className="block">タイムゾーン<input className={fieldClass} required maxLength={64} value={timezone} onChange={event => setTimezone(event.target.value)} /></label>
      <label className="flex items-center gap-2"><Checkbox disabled={busy || stale} checked={always} onCheckedChange={setAlways} />終日受付</label>
      {!always && <fieldset className="space-y-2"><legend>営業時間</legend>
        <div className="flex flex-wrap gap-3">{["月", "火", "水", "木", "金", "土", "日"].map((day, index) => <label key={day} className="flex items-center gap-1"><Checkbox disabled={busy || stale} checked={days.includes(index)} onCheckedChange={checked => setDays(current => checked ? [...current, index] : current.filter(value => value !== index))} />{day}曜日</label>)}</div>
        <label className="block">開始時刻<input className={fieldClass} type="time" required value={start} onChange={event => setStart(event.target.value)} /></label>
        <label className="block">終了時刻<input className={fieldClass} type="time" required value={end} onChange={event => setEnd(event.target.value)} /></label>
      </fieldset>}
      <label className="block">受付の挨拶（任意）<textarea className={fieldClass} maxLength={1200} value={greeting} onChange={event => setGreeting(event.target.value)} /></label>
      <label className="block">転送ポリシー<AppSelect aria-label="転送ポリシー" className="mt-1 w-full" disabled={busy || stale} value={policyRevision} onValueChange={value => { setPolicyRevision(value); setDestinations([]); }}>
        <option value="">転送しない</option>
        {policyRevision && !policy && <option value={policyRevision} disabled>現在の転送ポリシーは利用できません</option>}
        {choices.map(item => <option key={item.id} value={item.current_revision!.id}>{item.display_name}</option>)}
      </AppSelect></label>
      {policyRevision && <fieldset><legend>許可された転送先</legend>
        {allowed.length === 0 && <p className="text-sm">選択できる転送先がありません。</p>}
        {allowed.map(item => <label key={item.key} className="flex items-center gap-2"><Checkbox disabled={busy || stale} checked={destinations.includes(item.key)} onCheckedChange={checked => setDestinations(current => checked ? [...current, item.key] : current.filter(key => key !== item.key))} />{item.display_name}</label>)}
      </fieldset>}
      {!bindingValid && <p role="alert">転送ポリシーまたは転送先を選び直してください。</p>}
      <p className="text-sm text-muted-foreground">時間外・利用不可時の対応: 着信を拒否</p>
      <Button type="submit" disabled={busy || stale || !revisionValid || !selected || !bindingValid || !name.trim()}>{route ? "電話受付を更新" : "下書きとして保存"}</Button>
    </fieldset>
    {error && <Failure message={error} reload={onSaved} busy={busy} />}
  </form>;
}

function PhoneRouteCard({ route: initial, agentId, canManage, catalog, policies, onSaved }: EmployeePanelProps & {
  route: PhoneRoute; catalog: PhoneCatalog; policies: Policy[]; onSaved: () => void;
}) {
  const [details, setDetails] = useState<{ route: PhoneRoute; readiness: Readiness; calls: PhoneCall[] } | null>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const path = `/api/telephony/routes/${encodeURIComponent(initial.id)}`;
  useEffect(() => {
    let current = true;
    Promise.all([
      employeeRequest<{ route: PhoneRoute }>(path), employeeRequest<Readiness>(`${path}/readiness`),
      employeeRequest<{ calls: PhoneCall[] }>(`/api/telephony/calls?route_id=${encodeURIComponent(initial.id)}`),
    ]).then(([route, readiness, calls]) => {
      if (current) { setDetails({ route: route.route, readiness, calls: calls.calls }); setError(""); setStale(false); }
    }).catch(cause => { if (current) setError(safeError(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [path, initial.id, reload]);
  const refresh = () => { setLoading(true); setDetails(null); setEditing(false); setReload(value => value + 1); };
  async function changeState(state: PhoneRoute["state"]) {
    if (!details || !canManage || lock.current || stale) return;
    lock.current = true; setBusy(true); setError("");
    try { await employeeRequest(`${path}/state`, "POST", { state, expected_version: details.route.version }); onSaved(); }
    catch (cause) { setError(safeError(cause)); setStale(errorStatus(cause) === 409); }
    finally { lock.current = false; setBusy(false); }
  }
  const route = details?.route ?? initial;
  // A draft is not receiving calls yet; activation rechecks all other readiness on the server.
  const canActivate = details?.readiness.ready || (!!details?.readiness.reason_codes.length && details.readiness.reason_codes.every(code => code === "telephony_route_inactive"));
  return <article className={panelClass} aria-label={route.display_name}>
    <h4 className="font-medium">{route.display_name}</h4>
    <p>{route.called_number_masked} · {statusLabel(route.state)} · v{route.version}</p>
    <p className="text-sm">接続方式: OpenAI Realtime SIP</p>
    <p className="text-sm">役割リビジョン: {route.agent_revision_id}</p>
    <p className="text-sm">{route.timezone} · {route.business_hours_json.days ? `${route.business_hours_json.days.map(day => ["月", "火", "水", "木", "金", "土", "日"][day]).join("・")} ${route.business_hours_json.start}–${route.business_hours_json.end}` : "終日受付"}</p>
    {route.greeting_override && <p>挨拶: {route.greeting_override}</p>}
    <p className="text-sm">時間外・利用不可時: 着信を拒否</p>
    {loading && <p role="status">受付状態を確認中…</p>}
    {error && <Failure message={error} reload={refresh} busy={busy || loading} />}
    {details && !loading && <>
      <p role="status">{details.readiness.ready ? "受付準備が整っています" : "受付準備が完了していません"} · プロバイダー: {statusLabel(details.readiness.provider_status)} · 外部回線: {statusLabel(details.readiness.external_setup)}</p>
      {details.readiness.reason_codes.length > 0 && <ul className="text-sm">{[...new Set(details.readiness.reason_codes.map(code => readinessLabels[code] ?? "未完了の設定があります。AI社員・接続の認証・回線設定・転送ポリシーを確認してください。"))].map(message => <li key={message}>{message}</li>)}</ul>}
      <Button variant="outline" disabled={busy} onClick={refresh}>受付状態・通話履歴を更新</Button>
      {canManage && route.state !== "retired" && <div className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={busy || stale} onClick={() => setEditing(value => !value)}>{editing ? "編集を閉じる" : "電話受付を編集"}</Button>
        {route.state !== "active" && <Button disabled={busy || stale || !canActivate} onClick={() => void changeState("active")}>受付を有効化</Button>}
        {route.state === "active" && <Button variant="outline" disabled={busy || stale} onClick={() => void changeState("paused")}>受付を一時停止</Button>}
        <Button variant="destructive" disabled={busy || stale} onClick={() => void changeState("retired")}>受付を終了</Button>
      </div>}
      {editing && canManage && <PhoneRouteForm key={`${route.id}:${route.version}`} agentId={agentId} route={route} catalog={catalog} policies={policies} onSaved={onSaved} />}
      <h5 className="font-medium">最近の通話</h5>
      {details.calls.length === 0 ? <p>通話履歴はありません。</p> : <ul className="space-y-2">{details.calls.map(call => <li key={call.id}>{call.caller_masked} → {call.called_masked} · {statusLabel(call.state)} · {dateLabel(call.received_at)}（日本時間）</li>)}</ul>}
    </>}
  </article>;
}
