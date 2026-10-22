"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { employeeOrganizationApi } from "@/lib/employee-organization-setup-api";
import { personaWorkspaceApi } from "@/lib/persona-workspace-api";
import { mediaResearchApi, type ContentItem } from "@/lib/media-operations-research-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { createPersonaDelegation, delegationContentHash, delegationEmployeesUnchanged, delegationReceiptMatches, delegationRequestBody, DelegationRequestError, type PersonaDelegationRequest, type RequestEmployee } from "@/lib/persona-delegation-request";

export function PersonaDelegationRequestPanel({ scope, brief }: { scope: PersonaWorkspaceScope; brief: ContentItem }) {
  const runtime = useOptionalRuntimeContext(); const features = runtime?.runtimeFeatures?.application_features;
  const enabled = features?.virtual_company === true && features?.autonomous_agent_runtime === true;
  const graph = useSWR(enabled ? ["persona-request-organization", scope.ownerUserId, scope.projectId, scope.personaId] : null, employeeOrganizationApi.graph, { shouldRetryOnError: false, revalidateOnFocus: false });
  const team = useSWR(enabled ? ["persona-request-team", scope.ownerUserId, scope.projectId, scope.personaId] : null, () => personaWorkspaceApi.team(scope.projectId), { shouldRetryOnError: false, revalidateOnFocus: false });
  const [manager, setManager] = useState(""); const [worker, setWorker] = useState(""); const [reviewer, setReviewer] = useState("");
  const [role, setRole] = useState<"writer" | "researcher" | "data_worker" | "general_worker">("writer");
  const [perCall, setPerCall] = useState("2048"); const [total, setTotal] = useState("16384");
  const [preview, setPreview] = useState<PersonaDelegationRequest | null>(null); const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false); const [sent, setSent] = useState(false); const [receipt, setReceipt] = useState(""); const [error, setError] = useState("");
  const pending = useRef(false); const active = useRef(false);
  const storageKey = `aoitalk-persona-delegation-v1:${scope.ownerUserId}:${scope.projectId}:${scope.personaId}:${brief.id}:${brief.content_hash}`;
  useEffect(() => { active.current = true; const refresh = () => { try { const stored = localStorage.getItem(storageKey); setSent(stored !== null); if (stored?.startsWith("saved:")) setReceipt(stored.slice(6)); } catch { setSent(true); setError("依頼の送信記録を保持できません。"); } }; const timer = window.setTimeout(refresh, 0); window.addEventListener("storage", refresh); return () => { active.current = false; clearTimeout(timer); window.removeEventListener("storage", refresh); }; }, [storageKey]);
  const employees: RequestEmployee[] = !graph.error && !team.error ? (graph.data?.nodes ?? []).flatMap(n => {
    const member = team.data?.agents.find(a => a.id === n.agent.id && a.state === "active" && a.persona_assignments?.some(p => p.persona_id === scope.personaId && p.state === "active") && a.project_grants?.some(g => g.project_id === scope.projectId && g.state === "active"));
    return member && n.agent.state === "active" && typeof n.revision?.id === "string" ? [{ id: n.agent.id, revisionId: n.revision.id, name: n.agent.display_name }] : [];
  }) : [];
  const reset = () => { setPreview(null); setReviewed(false); };
  async function confirm() {
    setError(""); reset();
    try { const m = employees.find(e => e.id === manager), w = employees.find(e => e.id === worker), r = employees.find(e => e.id === reviewer); if (!m || !w || !r) throw new Error("この人格の有効な担当Agentを3件選択し、職務revisionを確認してください。"); setPreview(delegationRequestBody(scope, brief, m, w, r, role, Number(perCall), Number(total), crypto.randomUUID())); }
    catch (e) { setError(e instanceof Error ? e.message : "依頼内容を確認できません。"); }
  }
  async function submit() {
    if (pending.current || !preview || !reviewed || sent) return; pending.current = true; setBusy(true); setError("");
    try {
      if (!navigator.locks) throw new Error("複数タブの依頼記録を照合できません。対応ブラウザをご利用ください。");
      await navigator.locks.request(storageKey, async () => {
        if (localStorage.getItem(storageKey) !== null) { setSent(true); throw new Error("このbriefは依頼済み、または結果不明です。進捗を確認してください。"); }
        if (await employeeOrganizationApi.owner() !== scope.ownerUserId) throw new Error("本人ownerが変わりました。画面を再読込してください。");
        const savedBrief = await mediaResearchApi.getContentItem(brief.id); const current = await employeeOrganizationApi.graph();
        if (savedBrief.content_hash !== brief.content_hash || savedBrief.version !== brief.version || savedBrief.brief !== brief.brief || savedBrief.owner_user_id !== scope.ownerUserId || savedBrief.project_id !== scope.projectId || savedBrief.persona_revision_id !== scope.personaRevisionId || savedBrief.status === "archived" || !delegationEmployeesUnchanged(preview, current)) throw new Error("briefまたは選択Agentの職務が変わりました。依頼内容を再確認してください。");
        if (!active.current || await employeeOrganizationApi.owner() !== scope.ownerUserId) return;
        const expectedHash = await delegationContentHash(preview, scope.ownerUserId);
        if (!active.current) return;
        localStorage.setItem(storageKey, "unknown"); if (localStorage.getItem(storageKey) !== "unknown") throw new Error("依頼の送信記録を保持できません。"); setSent(true);
        let result;
        try { result = await createPersonaDelegation(preview); }
        catch (e) {
          // Canonical create rolls back rejected input/authority requests. Unknown/conflict results stay locked.
          if (e instanceof DelegationRequestError && [401, 403, 404, 422].includes(e.status) && localStorage.getItem(storageKey) === "unknown") { localStorage.removeItem(storageKey); if (active.current) { setSent(false); setPreview(null); setReviewed(false); } }
          throw e;
        }
        if (!delegationReceiptMatches(preview, scope.ownerUserId, result) || result.delegation.content_hash !== expectedHash) throw new Error("保存された委託の本人・人格・担当revision・briefと計画予算のhashが一致しません。再送せず進捗を確認してください。");
        const readback = await personaWorkspaceApi.delegation(result.delegation.id);
        if (!delegationReceiptMatches(preview, scope.ownerUserId, readback) || readback.delegation.id !== result.delegation.id || readback.delegation.content_hash !== expectedHash || await employeeOrganizationApi.owner() !== scope.ownerUserId) throw new Error("委託の保存結果を照合できません。再送せず進捗を確認してください。");
        localStorage.setItem(storageKey, `saved:${result.delegation.id}`); if (active.current) { setReceipt(result.delegation.id); setPreview(null); }
      });
    } catch (e) { if (active.current) setError(e instanceof DelegationRequestError ? `${e.message}${e.code ? ` 確認コード: ${e.code}` : ""} 選択Projectのread権限・Persona担当・workspace_read・職務Execution Team/Profile・予算上限を確認してください。` : e instanceof Error ? e.message : "依頼結果を確認できません。"); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  if (!enabled) return <p className="text-xs text-muted-foreground">Agentへの企画依頼にはAgent機能の有効化が必要です。</p>;
  return <section aria-label="企画を統括Agentへ依頼" className="space-y-3 rounded border p-3">
    <h3 className="text-sm font-semibold">この企画を統括Agentへ依頼</h3><p className="text-xs">保存済みbriefを固定し、統括の計画→担当制作→独立review→返却を依頼します。作成には人間管理者権限が必要です。実行費用は既存の予算・権限条件で検証され、公開は本人が別途操作します。</p>
    {(error || graph.error || team.error) && <p role="alert" className="break-words text-xs text-destructive">{error || "この人格の組織・担当を取得できません。管理者権限と機能状態を確認してください。"}</p>}
    {sent ? <p role="status" className="text-xs">{receipt ? "依頼の保存結果を読戻しました。" : "依頼は送信済み、または結果不明です。再送せず委託の進捗を確認してください。"}</p> : <>
      {!graph.isLoading && !team.isLoading && !graph.error && !team.error && employees.length < 3 && <p role="status" className="text-xs">このPersona担当・Project権限・有効な職務を持つ独立した3件のAgentが必要です。「組織・担当を設定」で準備してください。</p>}
      <div className="grid gap-3 sm:grid-cols-3">{([["統括Agent", manager, setManager], ["制作担当Agent", worker, setWorker], ["review担当Agent", reviewer, setReviewer]] as const).map(([label, value, change]) => <label key={label} className="block text-xs"><span>{label}</span><AppSelect aria-label={label} value={value} disabled={busy} onValueChange={v => { change(v); reset(); }}><option value="">Agentを選択</option>{employees.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</AppSelect></label>)}</div>
      <label className="block text-xs"><span>担当の仕事</span><AppSelect aria-label="担当の仕事" value={role} disabled={busy} onValueChange={v => { setRole(v as typeof role); reset(); }}><option value="writer">原稿・制作</option><option value="researcher">調査</option><option value="data_worker">データ整理</option><option value="general_worker">制作進行</option></AppSelect></label>
      <div className="grid gap-3 sm:grid-cols-2"><label className="block text-xs"><span>1呼出の出力token上限</span><Input type="number" min={256} max={8192} value={perCall} disabled={busy} onChange={e => { setPerCall(e.target.value); reset(); }} /></label><label className="block text-xs"><span>依頼全体の出力token上限</span><Input type="number" min={256} max={262144} value={total} disabled={busy} onChange={e => { setTotal(e.target.value); reset(); }} /></label></div>
      <Button size="sm" variant="outline" disabled={busy || employees.length < 3 || !!graph.error || !!team.error} onClick={() => void confirm()}>Agentへの依頼内容を確認</Button>
      {preview && <div className="space-y-2 text-xs" aria-label="Agentへの依頼preview"><p className="break-all">{brief.title} · brief v{brief.version} · hash {brief.content_hash}</p><p className="break-all">Project {scope.projectId} · Persona {scope.personaId} / revision {scope.personaRevisionId}</p><p>{employees.find(e => e.id === manager)?.name} → {employees.find(e => e.id === worker)?.name} → {employees.find(e => e.id === reviewer)?.name}</p><p>担当仕事 最大2件・深さ1・各段階1試行・再作業0回。1呼出 {perCall} / 全体 {total}出力token。Project/Persona担当・grant変更はありません。</p><label className="flex gap-2"><Checkbox checked={reviewed} disabled={busy} onCheckedChange={value => setReviewed(value === true)} />固定したbrief・担当revision・計画と実行上限を確認しました</label><Button size="sm" disabled={busy || !reviewed || sent} onClick={() => void submit()}>{busy ? "依頼の保存結果を照合中…" : "確認した企画を統括Agentへ依頼"}</Button></div>}
    </>}
    <Link className="block text-xs text-primary underline" href={`/operations?tab=studio&persona=${encodeURIComponent(scope.personaId)}&stage=brief&content=${encodeURIComponent(brief.id)}#persona-delegations`}>この人格の委託の進捗・返却内容を確認</Link>
    {receipt && <p className="break-all text-xs">委託ID {receipt}</p>}
  </section>;
}
