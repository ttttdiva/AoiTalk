"use client";

import Link from "next/link";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { personaWorkspaceApi } from "@/lib/persona-workspace-api";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";
import { PersonaDelegationsPanel } from "./persona-delegations-panel";

const roles: Record<string, string> = { operator: "運用", strategist: "戦略", researcher: "調査", creator: "制作", analyst: "分析", publisher: "公開" };

export function PersonaTeamPanel({ scope }: { scope: PersonaWorkspaceScope }) {
  const runtime = useOptionalRuntimeContext();
  const features = runtime?.runtimeFeatures?.application_features;
  const enabled = features?.virtual_company === true && features?.autonomous_agent_runtime === true;
  const team = useSWR(enabled ? ["persona-team", scope.personaId, scope.projectId, scope.ownerUserId] : null,
    () => personaWorkspaceApi.team(scope.projectId), { shouldRetryOnError: false, revalidateOnFocus: false });
  const agents = team.data?.agents?.filter(agent => agent.persona_assignments?.some(a => a.persona_id === scope.personaId && a.state === "active")) ?? [];
  const work = team.data?.current_work?.filter(item => item.project_id === scope.projectId) ?? [];
  return <section aria-label="担当Agentと仕事" className="rounded-xl border bg-card p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">担当Agentと仕事</h2>
      <Link href="/operations?tab=agents" className="text-xs text-primary hover:underline">組織・担当を設定</Link></div>
    {!enabled ? <p className="mt-2 text-xs text-muted-foreground">Agent機能は未有効です。人格の制作・公開は下のworkspaceから利用できます。</p>
      : team.error ? <div role="alert" className="mt-2 text-sm text-destructive">このProjectの担当情報を取得できませんでした。
        <Button size="sm" variant="outline" onClick={() => void team.mutate()}>担当情報を再取得</Button></div>
      : team.isLoading ? <p role="status" className="mt-2 text-sm">担当情報を確認中…</p>
      : <div className="mt-3 grid gap-4 md:grid-cols-2"><div className="space-y-2">
        {agents.length ? agents.map(agent => <article key={agent.id} className="rounded-lg border p-3 text-sm">
          <Link className="break-words font-medium text-primary" href={`/operations?tab=agents&agent=${encodeURIComponent(agent.id)}`}>{agent.display_name}</Link>
          <p className="mt-1 text-xs text-muted-foreground">{agent.job_title || "役職未設定"} · {agent.state}</p>
          <p className="mt-1 text-xs">{agent.persona_assignments?.filter(a => a.persona_id === scope.personaId && a.state === "active").map(a => roles[a.assignment_role ?? ""] ?? a.assignment_role ?? "担当").join(" / ")}</p>
          {agent.responsibility_summary && <p className="mt-1 whitespace-pre-wrap break-words text-xs text-muted-foreground">{agent.responsibility_summary}</p>}
        </article>) : <p className="text-sm text-muted-foreground">この人格の担当Agentは未割当です。</p>}</div>
        <div className="space-y-2">{work.length ? work.map(item => <article key={item.id} className="rounded-lg border p-3 text-sm">
          <Link className="break-words font-medium text-primary" href={`/operations?tab=work&scope=project&project_id=${encodeURIComponent(scope.projectId)}&work_id=${encodeURIComponent(item.id)}`}>{item.task_title || "制作の実行記録"}</Link>
          <p className="mt-1 text-xs text-muted-foreground">{item.agent || "担当未設定"} · {item.state}{item.execution_summary?.current_phase ? ` · ${item.execution_summary.current_phase}` : ""}</p>
        </article>) : <p className="text-sm text-muted-foreground">このProjectの実行中の仕事はありません。</p>}</div>
      </div>}
    {enabled && team.data && !team.error && <PersonaDelegationsPanel key={`${scope.ownerUserId}:${scope.projectId}:${scope.personaId}`} scope={scope} assignedIds={agents.map(a => a.id)} />}
  </section>;
}
