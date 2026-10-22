"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import type { EmployeeCatalog } from "@/lib/agent-employees-api";
import { employeeOrganizationApi, parseOrganizationManifest, organizationSnapshot, organizationSetupReadback, createOrganizationPending, importOrganizationPreview, legacyOrganizationPendingKey, organizationPendingKey, readOrganizationPending, reconcileOrganizationPending, type OrganizationManifest, type OrganizationGraph } from "@/lib/employee-organization-setup-api";
import { EmployeeCheck, EmployeeField, employeeInputClass } from "./employee-fields";
import type { components } from "@/lib/api-types.gen";
import { mediaBrandPortfolioApi } from "@/lib/media-brand-portfolio-api";
import { HelpPopover } from "@/components/ui/help-popover";

const message = (error: unknown) => error instanceof Error ? error.message : "組織設定を照合できません。";
export function EmployeeOrganizationSetup({ catalog, onSaved }: { catalog: EmployeeCatalog; onSaved: () => Promise<unknown> }) {
  const graph = useSWR("employee-organization-setup", employeeOrganizationApi.graph, { shouldRetryOnError: false, revalidateOnFocus: false });
  const portfolio = useSWR("employee-organization-brand-scopes", mediaBrandPortfolioApi.get, { shouldRetryOnError: false, revalidateOnFocus: false });
  const owner = useSWR("employee-organization-owner", employeeOrganizationApi.owner, { shouldRetryOnError: false, revalidateOnFocus: false });
  const [raw, setRaw] = useState('{"employees": []}');
  const [preview, setPreview] = useState<{ body: OrganizationManifest; snapshot: string; graph: OrganizationGraph; owner: string } | null>(null);
  const [reviewed, setReviewed] = useState(false); const [busy, setBusy] = useState(false); const [unknown, setUnknown] = useState(false);
  const [error, setError] = useState(""); const [result, setResult] = useState<components["schemas"]["OrganizationSetupResponse"] | null>(null);
  const [notice, setNotice] = useState("");
  const pending = useRef(false); const active = useRef(false);
  useEffect(() => { active.current = true; const refresh = () => { try { setUnknown(localStorage.getItem(legacyOrganizationPendingKey) !== null || !!owner.data && localStorage.getItem(organizationPendingKey(owner.data)) !== null); } catch { setError("ブラウザの保存記録を確認できません。"); setUnknown(true); } }; const timer = window.setTimeout(refresh, 0); window.addEventListener("storage", refresh); return () => { active.current = false; window.clearTimeout(timer); window.removeEventListener("storage", refresh); }; }, [owner.data]);
  async function inspect() { try { await graph.mutate(); } catch (e) { setError(message(e)); } }
  async function importFile(file?: File) {
    if (!file || busy) return; setError(""); setPreview(null); setReviewed(false);
    try {
      if (file.size > 1024 * 1024) throw new Error("preview JSONは1MB以下のファイルを選択してください。");
      const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("本人ownerが変わりました。画面を再読込してください。");
      const current = await graph.mutate(); if (!current) throw new Error("組織を取得できません。");
      const data = await portfolio.mutate().catch(() => undefined);
      const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      const body = importOrganizationPreview(source, current, catalog, data, who, unknown);
      setRaw(JSON.stringify(body, null, 2)); setResult(null);
    } catch (e) { setError(message(e)); }
  }
  async function reconcile() {
    if (pending.current || !owner.data) return; pending.current = true; setBusy(true); setError("");
    try {
      if (!navigator.locks) throw new Error("複数タブの保存記録を照合できません。");
      await navigator.locks.request(organizationPendingKey(owner.data), async () => {
        const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("本人ownerが変わりました。前の記録は解除しません。");
        if (localStorage.getItem(legacyOrganizationPendingKey) !== null) throw new Error("旧送信記録にはowner・内容hash・保全条件がありません。完了を証明できないため解除しません。");
        const key = organizationPendingKey(who); const stored = localStorage.getItem(key); if (!stored) throw new Error("本人の送信記録がありません。");
        const proof = readOrganizationPending(stored, who); const saved = await graph.mutate(); if (!saved) throw new Error("組織を取得できません。");
        const body = parseOrganizationManifest(raw, saved, catalog, true);
        if (!await reconcileOrganizationPending(proof, who, body, saved) || await employeeOrganizationApi.owner() !== who || localStorage.getItem(key) !== stored) throw new Error("同じ設定の全Agent・直属上司・プロフィール・職務の保存結果が一致しません。途中保存や未確認差分の記録は解除しません。");
        localStorage.removeItem(key); setUnknown(false); setPreview(null); setReviewed(false); setResult(null); setNotice("同じ本人・内容hashの保存結果を全行確認しました。再送せず送信記録を解除しました。"); await onSaved();
      });
    } catch (e) { setError(message(e)); } finally { pending.current = false; if (active.current) setBusy(false); }
  }
  async function review() {
    setError(""); setReviewed(false); setPreview(null);
    try { const who = await employeeOrganizationApi.owner(); if (who !== owner.data) throw new Error("本人ownerが変わりました。画面を再読込してください。"); const current = await graph.mutate(); if (!current) throw new Error("組織を取得できません。"); setPreview({ body: parseOrganizationManifest(raw, current, catalog), snapshot: organizationSnapshot(current), graph: current, owner: who }); }
    catch (e) { setError(message(e)); }
  }
  async function save() {
    if (pending.current || !preview || !reviewed || unknown || !catalog.can_manage) return;
    pending.current = true; setBusy(true); setError("");
    try {
      if (!navigator.locks) throw new Error("複数タブの同時保存を防ぐ機能を利用できません。対応ブラウザで保存してください。");
      const pendingKey = organizationPendingKey(preview.owner);
      await navigator.locks.request(pendingKey, async () => {
      if (await employeeOrganizationApi.owner() !== preview.owner) throw new Error("本人ownerが変わりました。保存しません。");
      if (localStorage.getItem(pendingKey) !== null || localStorage.getItem(legacyOrganizationPendingKey) !== null) { setUnknown(true); throw new Error("別の組織保存が送信済みです。再送せず保存状態を確認してください。"); }
      const current = await graph.mutate(); if (!current || organizationSnapshot(current) !== preview.snapshot) throw new Error("既存Agentの状態が変わりました。保存内容を再確認してください。");
      if (!active.current) return;
      const proof = await createOrganizationPending(preview.body, preview.graph, preview.owner);
      if (!active.current || await employeeOrganizationApi.owner() !== preview.owner) throw new Error("本人ownerまたは表示中の画面が変わりました。保存しません。");
      const encoded = JSON.stringify(proof); localStorage.setItem(pendingKey, encoded); if (localStorage.getItem(pendingKey) !== encoded) throw new Error("保存操作の記録を保持できません。");
      setUnknown(true);
      const response = await employeeOrganizationApi.setup(preview.body);
      const saved = await graph.mutate(); if (!saved || !organizationSetupReadback(preview.body, response, saved) || !await reconcileOrganizationPending(proof, preview.owner, preview.body, saved) || await employeeOrganizationApi.owner() !== preview.owner) throw new Error("保存したAgent・直属上司・プロフィール・職務の読戻しが一致しません。再送せず保存状態を確認してください。");
      localStorage.removeItem(pendingKey);
      if (active.current) { setResult(response); setUnknown(false); setPreview(null); setReviewed(false); await onSaved(); }
      });
    } catch (e) { if (active.current) setError(message(e)); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  const nodes: OrganizationGraph["nodes"] = !graph.error ? graph.data?.nodes ?? [] : [];
  return <section aria-label="Agent組織の設定" className="min-w-0 space-y-4 rounded-lg border p-4">
    <h2 className="inline-flex items-center gap-1 text-lg font-semibold">Agent組織を設定<HelpPopover label="Agent組織を設定"><p>既存AgentをIDまたはslugで再利用し、直属上司と未設定の職務・Execution Team/Profileを確認して保存します。新しいAgentは下書きです。activateを明示したdraftだけを有効化します。</p><p>この保存のアクセスgrant変更: なし。職務の権限上限は指定したrevisionだけに適用します。Project/Persona担当・credential・組織policyは追加しません。新brandへの担当は各Agentの「組織・所属」で別途確認してください。職務や担当が不足するAgentは仕事を開始できません。</p><p>Agent委任に必要な最小範囲: 職務のworkspace_read、選択Projectだけのviewer/read権限、対象Persona担当、ProjectにSpaceがあればそのSpace所属。役職名と実行権限は別です。delete・管理者権限・全Projectアクセスは追加しません。</p></HelpPopover></h2>
    {portfolio.error ? <p className="text-sm">SNSのowner-bound担当範囲を取得できません。別のProjectへ切り替えず、人格workspaceの保存構成を確認してください。</p> : portfolio.data?.status === "ready" && <details className="text-sm"><summary>SNS v6の新brand担当範囲を確認（この保存でgrantは変更しません）</summary><p>owner {portfolio.data.owner_user_id} · 非公開本部Project {portfolio.data.headquarters_project_id}</p><ul>{portfolio.data.brands.map(b => <li key={b.definition.brand_key} className="break-all">{b.definition.display_name} · Project {b.project_id} · Persona {b.persona?.id || "未設定"}</li>)}</ul></details>}
    {(error || graph.error || owner.error) && <p role="alert" className="break-words text-sm text-destructive">{error || message(graph.error || owner.error)}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {unknown && <><p role="status" className="text-sm">前の組織保存が送信済み、または結果不明です。同じpreviewファイルを読み込み、本人・内容hash・全行の保存結果を読取照合してください。途中保存は解除せず、自動再送しません。</p><Button variant="outline" disabled={busy || !owner.data || !!owner.error} onClick={() => void reconcile()}>同じ設定の保存完了を照合して送信記録を解除</Button></>}
    <Button variant="outline" disabled={busy} onClick={() => void inspect()}>組織の保存状態を再読込</Button>
    <ul aria-label="既存Agentとモデル設定" className="space-y-2 text-sm">{nodes.map(n => <li key={n.agent.id} className="min-w-0 rounded border p-2"><p className="break-words">{n.agent.display_name} · {n.agent.state}</p><p className="break-all text-xs">ID {n.agent.id} · slug {n.agent.slug} · 上司 {String(n.organization_profile?.manager_agent_id ?? "未設定")}</p><p className="break-all text-xs">Execution Team {String(n.revision?.agent_team_id ?? "未設定")} / Profile {String(n.revision?.execution_profile_id ?? "未設定")}</p><p className="text-xs">workspace_read: {Array.isArray(n.revision?.capability_ceiling) && n.revision.capability_ceiling.includes("workspace_read") ? "職務の上限に設定あり・所属と実効権限の確認が必要" : "職務の上限に未設定・読み取り権限の確認が必要"}</p><Link className="underline" href={`/operations?tab=agents&agent=${encodeURIComponent(n.agent.id)}&panel=role`}>職務・モデルを確認</Link>{" · "}<Link className="underline" href={`/operations?tab=agents&agent=${encodeURIComponent(n.agent.id)}&panel=assignments`}>組織・所属を確認</Link></li>)}</ul>
    <details className="text-sm"><summary>設定する項目と既存Execution Team</summary><p className="mt-2">employeesの各行: key、display_name、既存agent_idまたはslug。manager_keyは同じ設定内の上司key。省略で既存上司を保持、nullで明示解除。未設定Agentのrevisionには既存agent_team_id・execution_profile_idと職務を明示します。</p><ul>{catalog.teams.map(t => <li key={t.team_id} className="break-all">{t.name} · {t.team_id} · Profile: {(t.execution_profiles ?? Object.values(t.execution_profile_presets ?? {})).map(p => p.profile_id || p.name).join(" / ") || "職務・モデル画面で確認"}</li>)}</ul></details>
    <EmployeeField label="正本scriptのpreview JSONを読み込む"><input type="file" accept=".json,application/json" disabled={busy || !owner.data || !!owner.error} onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; void importFile(file); }} /></EmployeeField>
    <p className="text-xs">scripts/setup_employee_organization.py の --crowdworks または --sns に --output .local/設定名.json を指定します。--applyは付けません。ファイル読込は保存せず、SNS担当範囲を現在の本人portfolioと照合します。</p>
    <EmployeeField label="Agent組織の設定内容（JSON）"><textarea className={`${employeeInputClass} min-h-40 font-mono`} value={raw} disabled={busy} onChange={e => { setRaw(e.target.value); setPreview(null); setReviewed(false); }} /></EmployeeField>
    <p className="text-xs">保存前のGET確認はserver CASではありません。同時更新を原子的に保護する機能ではなく、保存後にも既存プロフィール・職務と明示変更を照合します。</p>
    <Button variant="outline" disabled={busy || unknown || !catalog.can_manage || !owner.data || !!owner.error || graph.isLoading || !!graph.error} onClick={() => void review()}>保存前の内容を確認</Button>
    {preview && <div aria-label="保存予定のAgent組織" className="min-w-0 space-y-3 rounded border p-3"><ul className="space-y-3 text-sm">{preview.body.employees.map(e => {
      const existing = nodes.find(n => e.agent_id ? n.agent.id === e.agent_id : n.agent.slug === e.slug);
      const manager = preview.body.employees.find(n => n.key === e.manager_key);
      const savedCaps = existing?.revision?.capability_ceiling;
      const caps = e.revision?.capability_ceiling ?? (Array.isArray(savedCaps) ? savedCaps.filter((v): v is string => typeof v === "string") : []);
      const brand = !portfolio.error && portfolio.data?.status === "ready" ? portfolio.data.brands.find(b => e.key === `sns-brand-${b.definition.brand_key}`) : undefined;
      return <li key={e.key} className="break-all"><p>{e.display_name} · {existing ? "既存Agentを再利用" : "新しいAgentを作成"} · {e.agent_id || e.slug}</p><p>直属上司: {!("manager_key" in e) ? "既存の上司を保持" : manager?.display_name || "明示解除"}</p><p>Execution Team/Profile: {e.revision ? `${e.revision.agent_team_id} / ${e.revision.execution_profile_id}` : existing?.revision ? "既存設定を保持" : "未設定・職務設定が必要"} · 有効化: {e.activate === true ? "draftのみ明示有効化" : "状態を保持"}</p><p>職務の権限上限: {caps.join(" / ") || "未設定"} · {caps.includes("workspace_read") ? "入力読取の職務条件あり・所属と実効権限は別確認" : "Agent委任のworkspace_readが未準備"}</p><p>Project/Persona担当: この保存では変更なし</p>{brand && <p>正規SNS設定keyの担当予定: {brand.definition.display_name} / Project {brand.project_id} / Persona {brand.persona?.id || "未設定"}。所属は別の確認・保存が必要です。</p>}{("job_title" in e || "responsibility_summary" in e) && <p>明示した役職・責務を更新</p>}</li>;
    })}</ul><EmployeeCheck label="作成対象・直属上司・モデル設定・担当と権限の変更範囲を確認しました" checked={reviewed} onChange={setReviewed} disabled={busy} /><Button disabled={busy || !reviewed || unknown} onClick={() => void save()}>{busy ? "保存状態を照合中…" : "確認したAgent組織を保存"}</Button></div>}
    {result && <div role="status" className="space-y-2 text-sm"><p>Agent組織の保存記録を読戻しました。{result.ready ? "組織・職務の構造は準備済みです。Project/Persona権限とprovider接続は別途確認してください。" : "仕事の開始には未設定項目の確認が必要です。"}</p><ul>{result.blockers.map((b, i) => <li key={i} className="break-all">{String(b.key ?? "Agent")}: {b.code === "manager_activation_required" ? "直属上司の職務設定・有効化が必要です" : "Agentの職務revision・有効化が必要です"}</li>)}</ul></div>}
  </section>;
}
