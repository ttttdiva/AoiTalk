"use client";

import { useRef, useState } from "react";
import useSWR from "swr";
import { employeeKey, employeeRequest, employeeScopeRequest, type EmployeePolicy, type ScopeOption } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";
import { optionLabel } from "./employee-assignments";

export type RuleBinding = { action_policy_revision_id: string; input_mapping: Record<string, { constant: string | number | boolean } | { extracted: string } | { event: string } | { source: string }>; on_noop: "continue" | "stop" };
export type RuleRevision = { id: string; version: number; agent_revision_id: string; event_type: "chat.message.created"; trigger_config: { human_only: true; project_id?: string; space_id?: string; conversation_session_id?: string }; condition_mode: "always" | "keywords" | "semantic"; condition_config: { operator?: "any" | "all"; phrases?: string[]; situation_description?: string; positive_examples?: string[]; negative_examples?: string[]; extraction_schema?: Record<string, unknown> }; semantic_readiness?: { supported: boolean; error_code?: string }; actions: RuleBinding[]; max_attempts: number; priority: number; concurrency_key?: string | null; active_from?: string | null; active_until?: string | null };
export type EmployeeRule = { id: string; display_name: string; state: string; version: number; current_revision: RuleRevision | null; revisions?: RuleRevision[] };
const rulePath = (id: string) => `/api/agent-automation/rules/${encodeURIComponent(id)}`;
const lines = (text: string) => text.split(/\r?\n/u).map(s => s.trim()).filter(Boolean);

export function EmployeeAutomation({ agentId, revisionId, canManage }: { agentId: string; revisionId: string | null; canManage: boolean }) {
  const rules = useSWR(["employee-rules", agentId], () => employeeRequest<{ rules: EmployeeRule[] }>(`/api/agent-automation/rules?agent_id=${encodeURIComponent(agentId)}`), { shouldRetryOnError: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null); const [busy, setBusy] = useState("");
  const detail = useSWR(selected && selected !== "new" ? ["employee-rule", selected] : null, () => employeeRequest<{ rule: EmployeeRule }>(rulePath(selected!)), { shouldRetryOnError: false });
  const refresh = async () => { await rules.mutate(); await detail.mutate(); };
  async function state(rule: EmployeeRule, next: string) {
    setError(null); setBusy(rule.id);
    try { await employeeRequest(rulePath(rule.id) + "/state", "POST", { state: next, expected_version: rule.version }); await refresh(); }
    catch (e) { setError(e); } finally { setBusy(""); }
  }
  return <section className="space-y-4"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-lg font-semibold">自動化ルール</h3>{canManage && <Button type="button" disabled={!revisionId} onClick={() => setSelected("new")}>自動化ルールを追加</Button>}</div>
    {!revisionId && <p className="text-sm">先に職務 revision を保存してください。</p>}
    <EmployeeNotice error={rules.error || detail.error || error} reload={() => void refresh()} />
    <ul className="space-y-3">{rules.data?.rules.map(rule => <li className="space-y-2 rounded-lg border p-3" key={rule.id}><div className="flex flex-wrap gap-2"><span className="font-medium">{rule.display_name}</span><EmployeeStatus state={rule.state} /></div>
      <p className="text-sm">チャット受信 · 条件 {rule.current_revision?.condition_mode === "semantic" ? "意味判定" : rule.current_revision?.condition_mode === "keywords" ? "キーワード" : "常時"} · revision v{rule.current_revision?.version ?? 0} · 操作 {rule.current_revision?.actions.length ?? 0}件</p>
      <p className="break-all text-xs text-muted-foreground">社員の参照 revision: {rule.current_revision?.agent_revision_id ?? "未設定"}</p>
      {rule.current_revision?.condition_mode === "semantic" && rule.current_revision.semantic_readiness?.supported !== true && <p className="text-sm text-amber-700 dark:text-amber-300">意味判定の実行設定は利用できません。職務の Team / Execution Profile とサーバー設定を確認してください。{rule.current_revision.semantic_readiness?.error_code}</p>}
      {canManage && rule.state !== "retired" && <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setSelected(rule.id)}>ルールを編集・テスト</Button><Button type="button" size="sm" disabled={busy === rule.id || !rule.current_revision || rule.state === "active" || (rule.current_revision.condition_mode === "semantic" && rule.current_revision.semantic_readiness?.supported !== true)} onClick={() => void state(rule, "active")}>ルールを有効化</Button><Button type="button" size="sm" variant="outline" disabled={busy === rule.id || rule.state !== "active"} onClick={() => void state(rule, "paused")}>一時停止</Button><Button type="button" size="sm" variant="outline" disabled={busy === rule.id} onClick={() => { if (window.confirm("ルールを廃止すると再開できません。続行しますか？")) void state(rule, "retired"); }}>廃止</Button></div>}
    </li>)}</ul>
    {!rules.isLoading && !rules.data?.rules.length && <p className="text-sm text-muted-foreground">ルールはまだありません。自動化は後から追加できます。</p>}
    {selected && revisionId && (selected === "new" || detail.data) && <RuleEditor key={`${selected}:${detail.data?.rule.version ?? 0}`} agentId={agentId} revisionId={revisionId} rule={selected === "new" ? undefined : detail.data?.rule} onSaved={async id => { await refresh(); setSelected(id); }} onClose={() => setSelected(null)} />}
  </section>;
}
function RuleEditor({ agentId, revisionId, rule, onSaved, onClose }: { agentId: string; revisionId: string; rule?: EmployeeRule; onSaved: (id: string) => Promise<void>; onClose: () => void }) {
  const current = rule?.current_revision;
  const [name, setName] = useState(rule?.display_name ?? "");
  const [scopeKind, setScopeKind] = useState(current?.trigger_config.space_id ? "space_id" : "project_id");
  const [scope, setScope] = useState(current?.trigger_config.space_id ?? current?.trigger_config.project_id ?? "");
  const [mode, setMode] = useState<RuleRevision["condition_mode"]>(current?.condition_mode ?? "keywords");
  const [operator, setOperator] = useState(current?.condition_config.operator ?? "any");
  const [phrases, setPhrases] = useState(current?.condition_config.phrases?.join("\n") ?? "");
  const [situation, setSituation] = useState(current?.condition_config.situation_description ?? "");
  const [positive, setPositive] = useState(current?.condition_config.positive_examples?.join("\n") ?? "");
  const [negative, setNegative] = useState(current?.condition_config.negative_examples?.join("\n") ?? "");
  const [bindings, setBindings] = useState<RuleBinding[]>(current?.actions.map(a => ({ action_policy_revision_id: a.action_policy_revision_id, input_mapping: a.input_mapping, on_noop: a.on_noop })) ?? []);
  const [revision, setRevision] = useState(current?.agent_revision_id ?? revisionId);
  const [attempts, setAttempts] = useState(current?.max_attempts ?? 3);
  const [priority, setPriority] = useState(current?.priority ?? 0);
  const [stable, setStable] = useState(rule);
  const [keys] = useState(() => ({ stable: employeeKey(), revision: employeeKey() }));
  const createIntent = useRef<{ agent_id: string; display_name: string; idempotency_key: string } | null>(null);
  const [testText, setTestText] = useState(""); const [testResult, setTestResult] = useState<{ matched: boolean; dry_run: boolean } | null>(null);
  const [bindRevision, setBindRevision] = useState("");
  const scopes = useSWR(["employee-rule-scopes", scopeKind], () => employeeScopeRequest<{ projects?: ScopeOption[]; spaces?: ScopeOption[] }>(scopeKind === "space_id" ? "/api/spaces" : "/api/projects"), { shouldRetryOnError: false });
  const revisions = useSWR(["employee-revisions", agentId], () => employeeRequest<{ revisions: { id: string; version: number }[] }>(`/api/agents/${encodeURIComponent(agentId)}/revisions`), { shouldRetryOnError: false });
  const policies = useSWR(["employee-policies", agentId], () => employeeRequest<{ policies: EmployeePolicy[] }>(`/api/agent-action-policies?agent_id=${encodeURIComponent(agentId)}`), { shouldRetryOnError: false });
  const savedBindings = current?.actions ?? [];
  const availablePolicies = policies.data?.policies.filter(p => p.current_revision && p.state !== "retired") ?? [];
  return <div className="space-y-4 rounded-lg border bg-background p-4"><div className="flex justify-between gap-2"><h4 className="font-semibold">チャット自動化の設定</h4><Button type="button" size="sm" variant="ghost" onClick={onClose}>閉じる</Button></div>
    <EmployeeNotice error={scopes.error || policies.error || revisions.error} reload={() => { void scopes.mutate(); void policies.mutate(); void revisions.mutate(); }} />
    <EmployeeForm label="ルール revision を保存" reload={() => { if (stable) void onSaved(stable.id); }} onSave={async () => {
      let saved = stable;
      if (!saved) { createIntent.current ??= { agent_id: agentId, display_name: name.trim(), idempotency_key: keys.stable }; saved = (await employeeRequest<{ rule: EmployeeRule }>("/api/agent-automation/rules", "POST", createIntent.current)).rule; setStable(saved); }
      if (saved.display_name !== name.trim()) { saved = (await employeeRequest<{ rule: EmployeeRule }>(rulePath(saved.id), "PATCH", { display_name: name.trim(), expected_version: saved.version })).rule; setStable(saved); }
      const condition = mode === "always" ? {} : mode === "keywords" ? { operator, phrases: lines(phrases) } : { situation_description: situation, positive_examples: lines(positive), negative_examples: lines(negative), ...(current?.condition_config.extraction_schema ? { extraction_schema: current.condition_config.extraction_schema } : {}) };
      const result = await employeeRequest<{ rule: EmployeeRule }>(rulePath(saved.id) + "/revisions", "POST", {
        agent_revision_id: revision, idempotency_key: keys.revision, expected_version: saved.version, event_type: "chat.message.created",
        trigger_config: { human_only: true, [scopeKind]: scope, ...(current?.trigger_config.conversation_session_id ? { conversation_session_id: current.trigger_config.conversation_session_id } : {}) },
        condition_mode: mode, condition_config: condition, priority, max_attempts: attempts, actions: bindings,
        ...(current?.concurrency_key ? { concurrency_key: current.concurrency_key } : {}), ...(current?.active_from ? { active_from: current.active_from } : {}), ...(current?.active_until ? { active_until: current.active_until } : {}),
      }); await onSaved(result.rule.id);
    }}>
      <EmployeeField label="ルール名"><input className={employeeInputClass} required maxLength={160} value={name} onChange={e => setName(e.target.value)} /></EmployeeField>
      <EmployeeSelect label="実行する社員の職務 revision" required value={revision} onChange={setRevision} options={(revisions.data?.revisions ?? []).map(r => ({ value: r.id, label: `v${r.version} — ${r.id}` }))} />
      <p className="text-sm">トリガー: チャットメッセージの保存時。人間の投稿だけを対象にします。電話受付は「電話受付」で設定します。</p>
      <div className="grid gap-4 md:grid-cols-2"><EmployeeSelect label="チャット範囲の種類" required value={scopeKind} onChange={v => { setScopeKind(v); setScope(""); }} options={[{ value: "project_id", label: "Project" }, { value: "space_id", label: "Space" }]} /><EmployeeSelect label="対象のチャット範囲" required value={scope} onChange={setScope} options={(scopes.data?.projects ?? scopes.data?.spaces ?? []).map(s => ({ value: s.id, label: optionLabel(s) }))} /></div>
      {current?.trigger_config.conversation_session_id && <p className="break-all text-xs">既存の会話限定: {current.trigger_config.conversation_session_id}（保持されます）</p>}
      <EmployeeSelect label="反応する条件" required value={mode} onChange={v => setMode(v as typeof mode)} options={[{ value: "always", label: "常に反応" }, { value: "keywords", label: "キーワード" }, { value: "semantic", label: "意味で判定" }]} />
      {mode === "keywords" && <><EmployeeSelect label="キーワード判定" required value={operator} onChange={v => setOperator(v as typeof operator)} options={[{ value: "any", label: "どれかを含む（ANY）" }, { value: "all", label: "すべて含む（ALL）" }]} /><EmployeeField label="キーワード（1行に1つ）"><textarea className={employeeInputClass} required value={phrases} onChange={e => setPhrases(e.target.value)} /></EmployeeField></>}
      {mode === "semantic" && <><EmployeeField label="どんな報告に反応するか"><textarea className={employeeInputClass} required maxLength={4000} placeholder="社員がウォーターサーバー用の交換水がない、空になった、在庫切れと報告したとき" value={situation} onChange={e => setSituation(e.target.value)} /></EmployeeField><EmployeeField label="反応する例（1行に1つ）"><textarea className={employeeInputClass} value={positive} onChange={e => setPositive(e.target.value)} /></EmployeeField><EmployeeField label="反応しない例（1行に1つ）"><textarea className={employeeInputClass} value={negative} onChange={e => setNegative(e.target.value)} /></EmployeeField></>}
      <fieldset className="space-y-3 rounded border p-3"><legend className="px-1 text-sm font-medium">操作とポリシーの紐付け</legend><EmployeeSelect label="追加する操作ポリシー" value={bindRevision} onChange={setBindRevision} options={availablePolicies.filter(p => !bindings.some(b => b.action_policy_revision_id === p.current_revision!.id)).map(p => ({ value: p.current_revision!.id, label: `${p.display_name} v${p.current_revision!.version} — ${p.current_revision!.authorization_mode === "bounded_auto" ? "範囲内自動" : "毎回承認"}` }))} />
        <Button type="button" size="sm" variant="outline" disabled={!bindRevision} onClick={() => { setBindings([...bindings, { action_policy_revision_id: bindRevision, input_mapping: {}, on_noop: "continue" }]); setBindRevision(""); }}>操作を紐付ける</Button>
        {bindings.map((binding, index) => { const policy = availablePolicies.find(p => p.current_revision?.id === binding.action_policy_revision_id); return <div className="space-y-2 rounded border p-3" key={binding.action_policy_revision_id}><p className="break-all text-sm">{index + 1}. {policy?.display_name ?? "既存の固定ポリシー revision"} · {binding.action_policy_revision_id}</p><p className="text-xs text-muted-foreground">固定商品・配送先・通常数量は選択したポリシーから取得します。モデルは範囲を変更できません。</p>
          <EmployeeSelect label={`操作 ${index + 1} が重複・不要だったとき`} required value={binding.on_noop} onChange={value => setBindings(bindings.map((b, i) => i === index ? { ...b, on_noop: value as RuleBinding["on_noop"] } : b))} options={[{ value: "continue", label: "次の操作へ" }, { value: "stop", label: "以降を停止" }]} />
          <Button type="button" size="sm" variant="outline" onClick={() => setBindings(bindings.filter((_, i) => i !== index))}>紐付けを外す</Button></div>; })}
        {!bindings.length && <p className="text-sm">操作は未設定です。条件だけを保存・テストできます。購買ポリシーは下のフォームで作成します。</p>}
        {savedBindings.some(b => !availablePolicies.some(p => p.current_revision?.id === b.action_policy_revision_id)) && <p className="text-sm">以前のポリシー revision への参照は保持しています。新版を使う場合は紐付けを外して選び直してください。</p>}
      </fieldset>
      <div className="grid gap-4 md:grid-cols-2"><EmployeeField label="優先度"><input className={employeeInputClass} type="number" min={-10000} max={10000} step={1} value={priority} onChange={e => setPriority(Number(e.target.value))} /></EmployeeField><EmployeeField label="最大試行回数"><input className={employeeInputClass} type="number" min={1} max={10} step={1} value={attempts} onChange={e => setAttempts(Number(e.target.value))} /></EmployeeField></div>
      <EmployeeCheck label="人間の投稿のみ（自動通知の連鎖を防止）" checked onChange={() => undefined} disabled />
      <p className="text-sm text-muted-foreground">保存済み revision は変更されません。有効化には社員・操作ポリシー・接続先の準備が必要です。</p>
    </EmployeeForm>
    {rule?.current_revision && <div className="space-y-3 border-t pt-4"><h4 className="font-medium">保存済み条件の手動テスト（評価のみ）</h4><EmployeeForm label="条件をテスト" onSave={async () => { const result = await employeeRequest<{ evaluation: { matched: boolean; dry_run: boolean } }>(rulePath(rule.id) + "/test", "POST", { rule_revision_id: rule.current_revision!.id, text: testText }); setTestResult(result.evaluation); }}><EmployeeField label="テストする報告"><textarea className={employeeInputClass} required maxLength={16000} value={testText} onChange={e => { setTestText(e.target.value); setTestResult(null); }} /></EmployeeField></EmployeeForm>
      {testResult && <p role="status" className="text-sm">{testResult.matched ? "条件に一致しました" : "条件に一致しませんでした"}。評価のみで、仕事の作成・注文・電話は実行していません。</p>}</div>}
    {rule?.revisions && <details><summary>ルール revision 履歴</summary><ul className="text-sm">{rule.revisions.map(r => <li key={r.id}>v{r.version} · {r.condition_mode} · {r.agent_revision_id}</li>)}</ul></details>}
  </div>;
}
