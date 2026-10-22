"use client";

import { useState } from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { personaWorkspaceApi } from "@/lib/persona-workspace-api";
import type { components } from "@/lib/api-types.gen";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

type Delegation = components["schemas"]["DelegationDTO"];
const options = { shouldRetryOnError: false, revalidateOnFocus: false };
const states: Record<string, string> = { queued: "制作待ち", awaiting_plan: "上司の計画待ち", planning_blocked: "計画の採用停止", awaiting_review: "確認待ち", awaiting_return: "返却待ち", returned: "返却済み", changes_requested: "修正依頼", evidence_missing: "証拠不足", cancelled: "取消済み", awaiting_children: "担当からの返却待ち", child_blocked: "担当の作業停止" };
const phases: Record<string, string> = { plan: "計画", work: "制作", review: "確認", return: "返却" };
const phaseStates: Record<string, string> = { pending: "待機中", queued: "待機中", running: "実行中", leased: "実行中", retry_wait: "再試行待ち", blocked: "条件不足で停止", failed: "失敗", dead_letter: "試行上限で停止", cancelled: "取消済み", succeeded: "完了" };
const stateName = (state: string) => states[state] || (state.includes(":") ? state.split(":").map((part, index) => (index === 0 ? phases[part] : phaseStates[part]) || part).join(": ") : phaseStates[state] || state);
const planningEnabled = (delegation: Delegation) => delegation.planning?.enabled === true || Boolean(delegation.phases.plan);

function DelegationResult({ delegation, artifactId }: { delegation: Delegation; artifactId: string }) {
  const expected = Object.values(delegation.artifacts).find(a => a.id === artifactId);
  const result = useSWR(["persona-delegation-artifact", delegation.project_id, delegation.persona_id, delegation.id, artifactId, expected?.sha256], () => personaWorkspaceApi.delegationArtifact(delegation.id, artifactId), options);
  const artifact = result.data?.artifact;
  const valid = expected && artifact && artifact.id === expected.id && artifact.phase === expected.phase && artifact.sha256 === expected.sha256 && artifact.work_item_id === expected.work_item_id && artifact.agent_run_id === expected.agent_run_id && artifact.size_bytes === expected.size_bytes;
  return <div className="mt-2 min-w-0 rounded-lg border p-3 text-xs" aria-label="Agentの保存済み成果">
    {result.error ? <p role="alert">この委託の成果を取得できませんでした。別Agentの成果へ切り替えず、記録を再取得してください。</p> : result.isLoading ? <p role="status">保存済み成果を確認中…</p> : !valid ? <p role="alert">成果のhash・担当実行記録が一致しません。保存状態を確認してください。</p> : <>
      <p className="font-medium">{artifact.content.summary}</p><p className="mt-2 whitespace-pre-wrap break-words">{artifact.content.body}</p>
      {artifact.phase === "plan" && artifact.content.tasks && <div className="mt-3"><p className="font-medium">計画で採用された担当仕事</p><ul className="mt-2 space-y-2">{artifact.content.tasks.map(task => <li key={task.key} className="rounded border p-2"><p className="break-words font-medium">{task.title}</p><p className="mt-1 break-words">担当: {task.worker_key} · 確認: {task.reviewer_key}</p><p className="mt-1 whitespace-pre-wrap break-words">{task.instruction}</p></li>)}</ul></div>}
      {artifact.content.accepted !== null && artifact.content.accepted !== undefined && <p className="mt-2">確認結果: {artifact.content.accepted ? "受入" : "修正が必要"}</p>}
      <p className="mt-2 break-all text-muted-foreground">保存hash {artifact.sha256} · 実行 {artifact.agent_run_id}</p>
    </>}
  </div>;
}

/** Shared employees remain one organization; only persona-bound work is projected. */
export function PersonaDelegationsPanel({ scope, assignedIds }: { scope: PersonaWorkspaceScope; assignedIds: string[] }) {
  const organization = useSWR(["persona-organization", scope.ownerUserId, scope.projectId, scope.personaId], personaWorkspaceApi.organization, options);
  const requests = useSWR(["persona-delegations", scope.ownerUserId, scope.projectId, scope.personaId], personaWorkspaceApi.delegations, options);
  const [selection, setSelection] = useState<{ delegationId: string; artifactId: string } | null>(null);
  const employees = (organization.data?.nodes ?? []).flatMap(node => {
    const agent = node.agent as { id?: unknown; display_name?: unknown } | undefined;
    return agent && typeof agent.id === "string" && typeof agent.display_name === "string" ? [{ id: agent.id, name: agent.display_name }] : [];
  });
  const name = (agentId: string) => employees.find(a => a.id === agentId)?.name || "Agent名を確認できません";
  const delegations = (requests.data?.delegations ?? []).filter(d => d.created_by === scope.ownerUserId && d.project_id === scope.projectId && d.persona_id === scope.personaId);
  const error = organization.error || requests.error;
  return <div id="persona-delegations" className="mt-4 space-y-3 border-t pt-4" aria-label="組織と委託の進捗">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold">組織・計画→制作→確認→返却</h3><Button size="sm" variant="outline" onClick={() => { void organization.mutate(); void requests.mutate(); }}>組織・委託を更新</Button></div>
    {error ? <p role="alert" className="text-xs text-destructive">この人格の組織・委託記録を取得できません。管理者権限とAgentの有効化状態を確認して、再取得してください。</p> : <>
      <ul className="flex flex-wrap gap-2 text-xs">{assignedIds.map(agentId => {
        const manager = organization.data?.edges.find(e => e.agent_id === agentId)?.manager_agent_id;
        return <li key={agentId} className="rounded-lg border px-3 py-2"><span className="font-medium">{name(agentId)}</span><span className="ml-2 text-muted-foreground">{manager ? `上司: ${name(manager)}` : "上司未設定"}</span></li>;
      })}</ul>
      {requests.isLoading ? <p role="status" className="text-xs">委託の進捗を確認中…</p> : !delegations.length ? <p className="text-xs text-muted-foreground">この人格のAgentへの委託はありません。</p> : <div className="grid min-w-0 gap-3 lg:grid-cols-2">{delegations.map(d => <article key={d.id} className="min-w-0 rounded-lg border p-3 text-xs">
        <p className="break-words text-sm font-medium">{d.title}</p><p className="mt-1">{stateName(d.state)} · 第{d.round}回{d.round > 1 ? "（再作業）" : ""}</p>
        {d.parent_delegation_id && <p className="mt-1 break-words text-muted-foreground">上司への返却先: {delegations.find(parent => parent.id === d.parent_delegation_id)?.title || "別の委託記録"}</p>}
        <p className="mt-2 break-words">依頼: {name(d.delegator_agent_id)} → 制作: {name(d.worker_agent_id)} → 確認: {name(d.reviewer_agent_id)}</p>
        <p className="mt-1 break-words text-muted-foreground">{(planningEnabled(d) ? ["plan", "work", "review", "return"] : ["work", "review", "return"]).map(phase => `${phases[phase]}: ${stateName(String(d.phases[phase]?.state ?? d.phases[phase]?.status ?? "未開始"))}`).join(" / ")}</p>
        {Array.isArray(d.children) && d.children.length > 0 && <div className="mt-2"><p className="font-medium">担当Agentからの返却 {d.children.filter(child => child.state === "returned").length}/{d.children.length}</p><ul className="mt-1 space-y-1">{d.children.map(child => {
          const record = delegations.find(candidate => candidate.id === child.delegation_id);
          return <li key={child.delegation_id} className="break-words">{record?.title || "担当の委託記録"}: {stateName(child.state)}</li>;
        })}</ul></div>}
        {!d.readiness.ready && d.state !== "awaiting_children" && <p className="mt-2 text-destructive">{d.state === "planning_blocked" ? "計画を採用できません。許可された担当・資料・実行上限を確認してください。" : "作業の条件が不足しています。担当・権限・実行環境を確認してください。"}<span className="mt-1 block break-all text-xs">確認コード: {String(d.planning?.error_code || d.readiness.error_code || "未確認")}</span></p>}
        <div className="mt-2 flex flex-wrap gap-2">{Object.values(d.artifacts).map(a => <Button key={a.id} variant="outline" size="sm" onClick={() => setSelection({ delegationId: d.id, artifactId: a.id })}>{a.phase === "plan" ? "採用計画" : a.phase === "return" ? "返却内容" : a.phase === "review" ? "確認結果" : "制作成果"}を確認</Button>)}</div>
        {selection?.delegationId === d.id && Object.values(d.artifacts).some(a => a.id === selection.artifactId) && <DelegationResult key={`${d.id}:${selection.artifactId}`} delegation={d} artifactId={selection.artifactId} />}
      </article>)}</div>}
    </>}
  </div>;
}
