"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import type { EmployeeCatalog } from "@/lib/agent-employees-api";
import { employeeOrganizationApi, parseOrganizationManifest, organizationSnapshot, organizationSetupReadback, createOrganizationPending, importOrganizationPreview, legacyOrganizationPendingKey, organizationPendingKey, readOrganizationPending, reconcileOrganizationPending, type OrganizationManifest, type OrganizationGraph } from "@/lib/employee-organization-setup-api";
import { EmployeeCheck, EmployeeField, EmployeeSelect, employeeInputClass } from "./employee-fields";
import type { components } from "@/lib/api-types.gen";
import { mediaBrandPortfolioApi } from "@/lib/media-brand-portfolio-api";
import { formatOrganizationFeedback } from "@/lib/formatOrganizationFeedback";
import { HelpPopover } from "@/components/ui/help-popover";

const message = (error: unknown) => error instanceof Error ? error.message : "組織設定を照合できません。";
type Row = { id: string; job?: string; responsibility?: string; manager?: string; activate?: boolean };
export function EmployeeOrganizationSetup({ catalog, onSaved, onCreate }: { catalog: EmployeeCatalog; onSaved: () => Promise<unknown>; onCreate: () => void }) {
  const graph = useSWR("employee-organization-setup", employeeOrganizationApi.graph, { shouldRetryOnError: false, revalidateOnFocus: false });
  const portfolio = useSWR("employee-organization-brand-scopes", mediaBrandPortfolioApi.get, { shouldRetryOnError: false, revalidateOnFocus: false });
  const owner = useSWR("employee-organization-owner", employeeOrganizationApi.owner, { shouldRetryOnError: false, revalidateOnFocus: false });
  const [rows, setRows] = useState<Row[]>([]); const [search, setSearch] = useState(""); const [advanced, setAdvanced] = useState(false);
  const [raw, setRaw] = useState('{"employees": []}');
  const [preview, setPreview] = useState<{ body: OrganizationManifest; snapshot: string; graph: OrganizationGraph; owner: string } | null>(null);
  const [reviewed, setReviewed] = useState(false); const [busy, setBusy] = useState(false); const [unknown, setUnknown] = useState(false);
  const [error, setError] = useState(""); const [result, setResult] = useState<components["schemas"]["OrganizationSetupResponse"] | null>(null);
  const [notice, setNotice] = useState("");
  const [pendingBody, setPendingBody] = useState<{ owner: string; body: OrganizationManifest } | null>(null);
  const pending = useRef(false); const active = useRef(false);
  useEffect(() => { active.current = true; const refresh = () => { try { const stored = owner.data ? localStorage.getItem(organizationPendingKey(owner.data)) : null; setUnknown(localStorage.getItem(legacyOrganizationPendingKey) !== null || stored !== null); try { const body = stored && owner.data ? readOrganizationPending(stored, owner.data).body : null; setPendingBody(body && Array.isArray(body.employees) && owner.data ? { owner: owner.data, body } : null); } catch { setPendingBody(null); } } catch { setError("ブラウザの保存記録を確認できません。"); setUnknown(true); } }; const timer = window.setTimeout(refresh, 0); window.addEventListener("storage", refresh); return () => { active.current = false; window.clearTimeout(timer); window.removeEventListener("storage", refresh); }; }, [owner.data]);
  async function inspect() { try { await graph.mutate(); } catch (e) { setError(message(e)); } }
  async function importFile(file?: File) {
    if (!file || busy) return; setError(""); setPreview(null); setReviewed(false);
    try {
      if (file.size > 1024 * 1024) throw new Error("preview JSONは1MB以下のファイルを選択してください。");
      const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("ログイン中のユーザーが変わりました。画面を再読込してください。");
      const current = await graph.mutate(); if (!current) throw new Error("組織を取得できません。");
      const data = await portfolio.mutate().catch(() => undefined);
      const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      const body = importOrganizationPreview(source, current, catalog, data, who, unknown);
      setRaw(JSON.stringify(body, null, 2)); setAdvanced(true); setResult(null);
    } catch (e) { setError(message(e)); }
  }
  function manifest(current: OrganizationGraph, reconcile = false) {
    if (advanced) return parseOrganizationManifest(raw, current, catalog, reconcile);
    const included = new Set(rows.map(r => r.id));
    rows.forEach(r => { if (r.manager && r.manager !== "none") included.add(r.manager); });
    if ([...included].some(id => !current.nodes.some(n => n.agent.id === id))) throw new Error("選択したAgentを取得できません。組織を再読込して確認してください。");
    const key = (id: string) => `agent-${id}`;
    const employees = [...current.nodes].filter(n => included.has(n.agent.id)).sort((a, b) => a.agent.id.localeCompare(b.agent.id)).map(n => {
      const row = rows.find(r => r.id === n.agent.id);
      return { key: key(n.agent.id), agent_id: n.agent.id, display_name: n.agent.display_name,
        ...(row?.job !== undefined ? { job_title: row.job } : {}),
        ...(row?.responsibility !== undefined ? { responsibility_summary: row.responsibility } : {}),
        ...(row?.manager !== undefined ? { manager_key: row.manager === "none" ? null : key(row.manager) } : {}),
        ...(row?.activate ? { activate: true } : {}) };
    });
    return parseOrganizationManifest(JSON.stringify({ employees }), current, catalog, reconcile);
  }
  function change(id: string, patch: Partial<Row>) { setRows(old => old.map(r => r.id === id ? { ...r, ...patch } : r)); setPreview(null); setReviewed(false); }
  async function reconcile() {
    if (pending.current || !owner.data) return; pending.current = true; setBusy(true); setError("");
    try {
      if (!navigator.locks) throw new Error("複数タブの保存記録を照合できません。");
      await navigator.locks.request(organizationPendingKey(owner.data), async () => {
        const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("ログイン中のユーザーが変わりました。前の記録は保持します。");
        if (localStorage.getItem(legacyOrganizationPendingKey) !== null) throw new Error("旧送信記録にはowner・内容hash・保全条件がありません。完了を証明できないため解除しません。");
        const key = organizationPendingKey(who); const stored = localStorage.getItem(key); if (!stored) throw new Error("本人の送信記録がありません。");
        const proof = readOrganizationPending(stored, who); const saved = await graph.mutate(); if (!saved) throw new Error("組織を取得できません。");
        const body = proof.body; if (!body || !Array.isArray(body.employees)) throw new Error("以前の送信記録には保存内容がないため、自動照合できません。記録は保持します。");
        if (!await reconcileOrganizationPending(proof, who, body, saved) || await employeeOrganizationApi.owner() !== who || localStorage.getItem(key) !== stored) throw new Error("同じ設定の全Agent・直属上司・プロフィール・職務の保存結果が一致しません。途中保存や未確認差分の記録は解除しません。");
        localStorage.removeItem(key); setPendingBody(null); setUnknown(false); setPreview(null); setReviewed(false); setResult(null); setNotice("保存結果を確認しました。"); await onSaved();
      });
    } catch (e) { setError(message(e)); } finally { pending.current = false; if (active.current) setBusy(false); }
  }
  async function review() {
    setError(""); setReviewed(false); setPreview(null);
    try { const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("ログイン中のユーザーが変わりました。画面を再読込してください。"); const current = await graph.mutate(); if (!current) throw new Error("組織を取得できません。"); setPreview({ body: manifest(current), snapshot: organizationSnapshot(current), graph: current, owner: who }); }
    catch (e) { setError(message(e)); }
  }
  async function save() {
    if (pending.current || !preview || !reviewed || unknown || !catalog.can_manage) return;
    pending.current = true; setBusy(true); setError("");
    try {
      if (!navigator.locks) throw new Error("複数タブの同時保存を防ぐ機能を利用できません。対応ブラウザで保存してください。");
      const pendingKey = organizationPendingKey(preview.owner);
      await navigator.locks.request(pendingKey, async () => {
      if (await employeeOrganizationApi.owner() !== preview.owner) throw new Error("ログイン中のユーザーが変わりました。保存しません。");
      if (localStorage.getItem(pendingKey) !== null || localStorage.getItem(legacyOrganizationPendingKey) !== null) { setUnknown(true); throw new Error("別の組織保存が送信済みです。再送せず保存状態を確認してください。"); }
      const current = await graph.mutate(); if (!current || organizationSnapshot(current) !== preview.snapshot) throw new Error("既存Agentの状態が変わりました。保存内容を再確認してください。");
      if (!active.current) return;
      const proof = await createOrganizationPending(preview.body, preview.graph, preview.owner);
      if (!active.current || await employeeOrganizationApi.owner() !== preview.owner) throw new Error("ユーザーまたは表示中の画面が変わりました。保存しません。");
      const encoded = JSON.stringify(proof); localStorage.setItem(pendingKey, encoded); if (localStorage.getItem(pendingKey) !== encoded) throw new Error("保存操作の記録を保持できません。");
      setPendingBody({ owner: preview.owner, body: preview.body }); setUnknown(true);
      const response = await employeeOrganizationApi.setup(preview.body);
      const saved = await graph.mutate(); if (!saved || !organizationSetupReadback(preview.body, response, saved) || !await reconcileOrganizationPending(proof, preview.owner, preview.body, saved) || await employeeOrganizationApi.owner() !== preview.owner) throw new Error("保存したAgent・直属上司・プロフィール・職務の読戻しが一致しません。再送せず保存状態を確認してください。");
      if (localStorage.getItem(pendingKey) !== encoded) throw new Error("保存記録が変更されたため、確認を中断しました。");
      localStorage.removeItem(pendingKey); setPendingBody(null);
      if (active.current) { setResult(response); setUnknown(false); setPreview(null); setReviewed(false); await onSaved(); }
      });
    } catch (e) { if (active.current) setError(message(e)); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  const nodes = graph.data?.nodes ?? [];
  return <section aria-label="Agent組織の設定" className="min-w-0 space-y-4 rounded-lg border p-4">
    <h2 className="inline-flex items-center gap-1 text-lg font-semibold">Agent組織を設定<HelpPopover label="Agent組織を設定">直属上司と役職を設定します。仕事の流れや接続はマップで設計します。職務・モデルや所属権限は各Agentの画面で設定します。</HelpPopover></h2>
    <div className="flex flex-wrap gap-2"><Link className="underline text-sm" href="/operations?tab=overview&overview_view=map&map_mode=design">マップでTeamを設計</Link><Button variant="outline" disabled={busy} onClick={onCreate}>新しいAgentを追加</Button><Button variant="outline" disabled={busy} onClick={() => void inspect()}>組織を再読込</Button></div>
    {(error || graph.error || owner.error) && <p role="alert" className="break-words text-sm text-destructive">{error || message(graph.error || owner.error)}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {unknown && <><p role="status" className="text-sm">{formatOrganizationFeedback({ status: "uncertain" }).message}</p>{pendingBody && pendingBody.owner === owner.data && <details><summary>確認する保存内容</summary><ul>{pendingBody.body.employees.map((entry, i) => <li key={i}>{entry.display_name} {"job_title" in entry && `· 役職: ${entry.job_title || "（空）"}`} {"manager_key" in entry && `· 上司: ${entry.manager_key === null ? "なし" : pendingBody.body.employees.find(e => e.key === entry.manager_key)?.display_name ?? "確認できません"}`}</li>)}</ul></details>}<Button variant="outline" disabled={busy || !owner.data || !!owner.error || !graph.data} onClick={() => void reconcile()}>保存結果を確認</Button></>}
    {graph.isLoading && <p role="status">{formatOrganizationFeedback({ status: "loading" }).message}</p>}
    {graph.data && !graph.error && <>
      {nodes.length === 0 ? <p>Agentはまだいません。「新しいAgentを追加」から作成できます。</p> : <>
      <EmployeeField label="Agentを検索"><input type="search" className={employeeInputClass} value={search} onChange={e => setSearch(e.target.value)} /></EmployeeField>
      <EmployeeSelect label="編集するAgentを追加" value="" disabled={busy} onChange={id => { if (id) { setRows(old => [...old, { id }]); setPreview(null); setReviewed(false); } }} options={nodes.filter(n => !rows.some(r => r.id === n.agent.id) && n.agent.display_name.toLocaleLowerCase().includes(search.toLocaleLowerCase())).map(n => ({ value: n.agent.id, label: n.agent.display_name }))} />
      </>}
      <div className="space-y-3">{rows.map(row => { const node = nodes.find(n => n.agent.id === row.id); if (!node) return <p key={row.id}>選択したAgentを取得できません。再読込してください。</p>; const profile = node.organization_profile; const currentManager = nodes.find(n => n.agent.id === profile?.manager_agent_id);
        return <div key={row.id} className="space-y-2 rounded border p-3"><div className="flex justify-between gap-2"><strong>{node.agent.display_name}</strong><Button variant="ghost" disabled={busy} onClick={() => { setRows(old => old.filter(r => r.id !== row.id)); setPreview(null); setReviewed(false); }}>行を外す</Button></div>
          <EmployeeSelect label="直属上司" disabled={busy} value={row.manager ?? "keep"} onChange={value => change(row.id, { manager: value === "keep" ? undefined : value })} options={[{ value: "keep", label: `現在の設定を保持（${currentManager?.agent.display_name ?? (profile?.manager_agent_id ? "取得できない上司" : "なし")}）` }, { value: "none", label: "上司なしにする" }, ...nodes.filter(n => n.agent.id !== row.id).map(n => ({ value: n.agent.id, label: n.agent.display_name }))]} />
          <EmployeeField label="役職"><input className={employeeInputClass} disabled={busy} value={row.job ?? String(profile?.job_title ?? "")} onChange={e => change(row.id, { job: e.target.value })} /></EmployeeField>
          <EmployeeField label="責務"><textarea className={employeeInputClass} disabled={busy} value={row.responsibility ?? String(profile?.responsibility_summary ?? "")} onChange={e => change(row.id, { responsibility: e.target.value })} /></EmployeeField>
          <Link className="text-sm underline" href={`/operations?tab=agents&agent=${encodeURIComponent(node.agent.id)}&panel=role`}>職務・モデル・利用機能を編集</Link>{node.agent.state === "draft" && node.revision && <EmployeeCheck label="この下書きを有効化する" checked={!!row.activate} onChange={v => change(row.id, { activate: v })} disabled={busy} />}
        </div>; })}</div>
    </>}
    <details className="text-sm"><summary>詳細・移行用</summary><div className="space-y-2"><EmployeeField label="設定ファイルを読み込む"><input type="file" accept=".json,application/json" disabled={busy || !owner.data || !!owner.error} onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; void importFile(file); }} /></EmployeeField><p className="text-xs">scripts/setup_employee_organization.py の --crowdworks または --sns に --output 設定名.json を指定します。ファイルはこのチェックアウト専用のOS一時フォルダーに保存され、保存先の絶対パスが標準エラー出力に表示されます。--applyは付けません。</p><EmployeeCheck label="詳細設定を使用" checked={advanced} onChange={v => { setAdvanced(v); setPreview(null); setReviewed(false); }} disabled={busy} /><EmployeeField label="設定内容（JSON）"><textarea className={`${employeeInputClass} min-h-40 font-mono`} value={raw} disabled={busy} onChange={e => { setRaw(e.target.value); setAdvanced(true); setPreview(null); setReviewed(false); }} /></EmployeeField></div></details>
    <Button variant="outline" disabled={busy || unknown || !catalog.can_manage || !owner.data || !!owner.error || !graph.data || !!graph.error} onClick={() => void review()}>保存前の内容を確認</Button>
    {preview && <div aria-label="保存予定のAgent組織" className="space-y-3 rounded border p-3"><ul className="space-y-2 text-sm">{preview.body.employees.map(e => <li key={e.key}>{e.display_name} · 上司: {!("manager_key" in e) ? "現在の設定を保持" : e.manager_key === null ? "なし" : preview.body.employees.find(n => n.key === e.manager_key)?.display_name} {e.activate ? "· 有効化" : ""}{"job_title" in e && <p>役職: {e.job_title || "（空）"}</p>}{"responsibility_summary" in e && <p className="whitespace-pre-wrap">責務: {e.responsibility_summary || "（空）"}</p>}{e.revision && <details><summary>職務設定の変更内容</summary><pre className="whitespace-pre-wrap break-all">{JSON.stringify(e.revision, null, 2)}</pre></details>}</li>)}</ul><EmployeeCheck label="保存内容を確認しました" checked={reviewed} onChange={setReviewed} disabled={busy} /><Button disabled={busy || !reviewed || unknown} onClick={() => void save()}>{busy ? "保存状態を確認中…" : "組織を保存"}</Button></div>}
    {result && <div role="status" className="space-y-2 text-sm"><p>{formatOrganizationFeedback({ status: "saved" }).message} Agent組織の保存記録を読戻しました。{result.ready ? "組織・職務の構造は準備済みです。Project/Persona権限とprovider接続は別途確認してください。" : "仕事の開始には未設定項目の確認が必要です。"}</p><ul>{result.blockers.map((b, i) => <li key={i} className="break-all">{String(b.key ?? "Agent")}: {b.code === "manager_activation_required" ? "直属上司の職務設定・有効化が必要です" : "Agentの職務revision・有効化が必要です"}</li>)}</ul></div>}
  </section>;
}
