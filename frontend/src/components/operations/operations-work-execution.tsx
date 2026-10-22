"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { getWorkExecution, executionAttentionLabel, executionDuration, executionTime, workConversationSessionId, type WorkExecution, type ExecutionRun, type ExecutionPageKind } from "@/lib/operations-execution-api";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { prepareExecution, executionLevel } from "./operations-execution-model";
import { OperationsExecutionGraph } from "./operations-execution-graph";
import { OperationsExecutionDetail } from "./operations-execution-detail";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";

function mergeRows<T extends { id: string }>(previous: T[], next: T[]): T[] {
  return Array.from(new Map([...previous, ...next].map(row => [row.id, row])).values());
}

export function ExecutionTree({ data }: { data: WorkExecution }) {
  const ids = new Set(data.runs.map(run => run.id));
  const children = new Map<string, ExecutionRun[]>();
  for (const run of data.runs) {
    const key = run.parent_run_id && ids.has(run.parent_run_id) ? run.parent_run_id : "root";
    children.set(key, [...(children.get(key) ?? []), run]);
  }
  const visited = new Set<string>();
  const renderRun = (run: ExecutionRun, depth = 0): React.ReactNode => {
    if (visited.has(run.id)) return null;
    visited.add(run.id);
    return <li key={run.id} className="min-w-0 space-y-3 rounded-lg border border-border bg-card p-3" data-run-id={run.id}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="break-all font-semibold">{run.acting_subagent_id || (run.parent_run_id ? "Subagent" : "Main agent")}</h3>
        <span className="rounded bg-muted px-2 py-1 text-xs">{run.status}</span>
      </div>
      <p className="break-all text-xs text-muted-foreground">Attempt {run.work_item_attempt ?? "未記録"} · Run {run.id}</p>
      {run.parent_run_id && !ids.has(run.parent_run_id) ? <p className="text-xs text-amber-700">上位実行はこのページに未取得です</p> : null}
      <dl className="grid gap-1 rounded-md border border-primary/30 bg-primary/5 p-2 text-xs sm:grid-cols-2" data-testid="execution-current-position">
        <div><dt className="inline font-medium">現在の工程 </dt><dd className="inline">{run.phase ?? "未記録"}</dd></div>
        <div><dt className="inline font-medium">現在のTool </dt><dd className="inline">{run.tool ?? "未記録"}</dd></div>
      </dl>
      <dl className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
        <div><dt className="inline">開始 </dt><dd className="inline">{executionTime(run.started_at)}</dd></div>
        <div><dt className="inline">終了 </dt><dd className="inline">{executionTime(run.ended_at)}</dd></div>
        <div><dt className="inline">経過 </dt><dd className="inline">{executionDuration(run.elapsed_seconds)}</dd></div>
        <div><dt className="inline">最終活動 </dt><dd className="inline">{executionTime(run.last_event_at)}</dd></div>
      </dl>
      {run.error_code ? <p className="break-words text-sm text-destructive">エラー: {run.error_code}</p> : null}
      <ul className="space-y-2 border-l-2 border-border pl-3" aria-label="Activity履歴">
        {data.events.filter(event => event.run_id === run.id && event.phase).sort((a, b) => a.sequence - b.sequence).map(event => <li key={`phase:${event.id}`} className="text-xs">
          <span className="font-medium">Phase: {event.phase}</span> · {event.status || event.event_type} · {executionTime(event.created_at)}
          {event.error_code ? <span className="ml-2 text-destructive">{event.error_code}</span> : null}
        </li>)}
        {data.tool_calls.filter(tool => tool.run_id === run.id).map(tool => <li key={tool.id} className="rounded border border-border p-2 text-xs">
          <p className="break-all font-medium">Tool: {tool.tool_name} · {tool.status}</p>
          <p className="mt-1 text-muted-foreground">開始 {executionTime(tool.started_at)} · 終了 {executionTime(tool.ended_at)}</p>
          <p className="text-muted-foreground">経過 {executionDuration(tool.duration_ms == null ? null : tool.duration_ms / 1000)}</p>
          {tool.error_code ? <p className="text-destructive">{tool.error_code}</p> : null}
        </li>)}
      </ul>
      {depth < 32 && children.get(run.id)?.length ? <ul className="space-y-3 border-l-2 border-primary/40 pl-3">{children.get(run.id)!.map(child => renderRun(child, depth + 1))}</ul> : null}
    </li>;
  };
  const roots = (children.get("root") ?? []).map(run => renderRun(run));
  // Invalid historical cycles remain visible without recursive rendering.
  const detached = data.runs.filter(run => !visited.has(run.id)).map(run => renderRun(run));
  return <ul className="space-y-3" aria-label="Agent Execution Tree">{roots}{detached}</ul>;
}

export function OperationsWorkExecution({ workId, workName, backHref, embedded = false }: { workId: string; workName?: string; backHref: string; embedded?: boolean }) {
  const router = useRouter(); const pathname = usePathname(); const params = useSearchParams();
  const [showFinished, setShowFinished] = useState(false);

  const [data, setData] = useState<WorkExecution | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const version = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async (page?: ExecutionPageKind, cursor?: string) => {
    const current = ++version.current;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setLoading(true); setError(null);
    try {
      const result = await getWorkExecution(workId, { signal: next.signal, page, cursor });
      if (current !== version.current) return;
      setData(previous => !previous || !page ? result : {
        ...previous, observed_at: result.observed_at, work: result.work,
        runs: mergeRows(previous.runs, result.runs), events: mergeRows(previous.events, result.events),
        tool_calls: mergeRows(previous.tool_calls, result.tool_calls),
        [`${page}_page`]: result[`${page}_page`],
      });
    } catch (failure) {
      if (current !== version.current || next.signal.aborted) return;
      setError(failure instanceof Error ? failure.message : "実行状況を読み込めませんでした");
    } finally { if (current === version.current) setLoading(false); }
  }, [workId]);
  const invalidate = useCallback(() => {
    ++version.current;
    controller.current?.abort();
  }, []);
  useEffect(() => {
    setData(null); void load();
    return invalidate;
  }, [invalidate, load]);
  const model = useMemo(() => data ? prepareExecution(data, workName) : null, [data, workName]);
  const focusId = params?.get("execution_focus") || null;
  const selectedId = params?.get("execution_node") || focusId;
  const level = model ? executionLevel(model, focusId, showFinished) : null;
  const selected = model && level ? model.byId.get(selectedId || "") || level.focus : null;
  const navigate = (id: string | null, drill = false) => {
    const next = new URLSearchParams(params?.toString() || "");
    if (id) next.set("execution_node", id); else next.delete("execution_node");
    if (drill) { if (id) next.set("execution_focus", id); else next.delete("execution_focus"); }
    router.push(`${pathname || "/operations"}?${next}`, { scroll: false });
  };
  const pageKinds = ["runs", "events", "tools"] as const;
  const hasTruncatedHistory = Boolean(
    data && pageKinds.some((kind) => data[`${kind}_page`].truncated),
  );
  const hasAdditionalHistory = Boolean(
    data && pageKinds.some((kind) => data[`${kind}_page`].has_more && data[`${kind}_page`].next_cursor),
  );
  const conversationSessionId = data ? workConversationSessionId(data.work) : null;
  return <section className="space-y-4" data-testid="operations-work-execution">
    <div className="flex flex-wrap items-center justify-between gap-3">
      {!embedded ? <Link href={backHref} className="text-sm text-primary hover:underline">← 仕事一覧へ戻る</Link> : (
        <h2 className="inline-flex items-center gap-1 text-lg font-semibold">
          実行の関係と状態
          <HelpPopover label="実行の関係と状態">
            状態確認専用です。承認や再試行は正本の画面から行います。
          </HelpPopover>
        </h2>
      )}
      <Button type="button" variant="outline" disabled={loading} onClick={() => void load()}>{loading ? "更新中…" : "更新"}</Button>
    </div>
    {!embedded ? (
      <h1 className="inline-flex items-center gap-1 text-2xl font-semibold">
        Agent Execution Tree
        <HelpPopover label="Agent Execution Tree">
          状態確認専用です。承認や再試行は正本の画面から行います。
        </HelpPopover>
      </h1>
    ) : null}
    {error ? <p role="alert" className="rounded border border-destructive p-3 text-destructive">{error}{data ? "。表示中の記録は最新と確認できていません" : ""}</p> : null}
    {loading && !data ? <p role="status">実行状況を読み込み中…</p> : null}
    {data ? <>
      <div className="space-y-2 rounded-xl border border-border p-4">
        <p className="break-words text-sm font-medium">{workName || "仕事の実行状況"} · 仕事の状態 {data.work.state}</p>
        <details><summary className="cursor-pointer text-xs text-muted-foreground">仕事の観測詳細</summary>
        <p className="break-all text-xs">Work {data.work.id}</p>
        <p className="text-sm">Attempt {data.work.execution_summary.attempt_count}/{data.work.execution_summary.max_attempts} · Phase {data.work.execution_summary.phase ?? "未記録"} · Tool {data.work.execution_summary.tool ?? "未記録"}</p>
        <p className="text-xs text-muted-foreground">最終活動 {executionTime(data.work.execution_summary.last_activity_at)} · 経過 {executionDuration(data.work.execution_summary.elapsed_seconds)}</p>
        <p className="text-xs text-muted-foreground">Heartbeat {executionTime(data.work.execution_summary.heartbeat_at)} · Lease {data.work.execution_summary.lease_health === "none" ? "未記録" : data.work.execution_summary.lease_health}</p>
        </details>
        {data.work.execution_summary.attention_reasons.length ? <p className="text-sm text-amber-700">{data.work.execution_summary.attention_reasons.map(executionAttentionLabel).join(" / ")}</p> : null}
        {data.work.execution_summary.blocker_code || data.work.execution_summary.safe_error_code ? <p className="break-all text-sm text-destructive">{data.work.execution_summary.blocker_code || data.work.execution_summary.safe_error_code}</p> : null}
        {data.work.execution_summary.result_summary ? <p className="break-words text-sm">{data.work.execution_summary.result_summary}</p> : null}
        {data.work.execution_summary.observation_status !== "available" ? <p className="text-sm text-amber-700">{data.work.execution_summary.observation_status === "partial" ? "正本の実行記録は一部だけ確認できています" : "正本の実行記録を確認できていません"}</p> : null}
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {data.work.source_href?.startsWith("/") && !data.work.source_href.startsWith("//") ? <Link className="text-sm text-primary hover:underline" href={data.work.source_href}>正本で確認する →</Link> : null}
          {conversationSessionId ? <Link className="text-sm text-primary hover:underline" href={`/chat?s=${encodeURIComponent(conversationSessionId)}`}>Chatで開く →</Link> : null}
        </div>
      </div>
      {hasTruncatedHistory ? <p data-testid="operations-execution-truncated" className="rounded border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200">実行が多いため一部の履歴を省略しています。現在の実行を優先して表示しています。</p> : null}
      {model && level && selected && (data.runs.length || data.events.length || data.tool_calls.length) ? <>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => navigate(level.focus.parent, true)} disabled={!level.focus.parent}>上の階層へ戻る</Button>
          <Button variant="outline" onClick={() => navigate(null, true)}>仕事の実行コンテキスト</Button>
          <label className="flex items-center gap-2 text-sm"><Checkbox checked={showFinished} onCheckedChange={setShowFinished} /> 完了した実行・Toolも表示</label>
        </div>
        <p className="break-all text-xs text-muted-foreground">階層: {level.focus.title} · この階層 {level.nodes.length - 1}件 · 取得済みRun {data.runs.length}件（総数ではありません）</p>
        <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 space-y-3">
            <OperationsExecutionGraph nodes={level.nodes} edges={level.edges} selected={selected.id} onSelect={id => navigate(id)} />
            <ul aria-label="実行階層の選択" className="grid gap-2 sm:grid-cols-2">{level.nodes.map(node => <li key={node.id} className="min-w-0 rounded border border-border p-2 text-xs">
              <button type="button" aria-pressed={selected.id === node.id} className="w-full break-all text-left font-medium hover:text-primary" onClick={() => navigate(node.id)}>{node.title} · {node.state}</button>
              <p>工程 {node.phase || "未記録"} · Tool {node.tool || "未記録"}</p>
              {model.nodes.some(child => child.parent === node.id) ? <Button size="sm" variant="ghost" onClick={() => navigate(node.id, true)}>下の階層を見る</Button> : null}
            </li>)}</ul>
          </div>
          <OperationsExecutionDetail node={selected} data={data} />
        </div>
      </> : null}
      {!data.runs.length ? <p className="text-sm">この仕事には取得済みのrun記録がありません。仕事の状態と実行記録は別々に確認します。</p> : <details><summary className="cursor-pointer text-sm">取得済みの縦treeと全履歴</summary><ExecutionTree data={data} /></details>}
      <div className="flex flex-wrap gap-2">{pageKinds.map(kind => data[`${kind}_page`].has_more && data[`${kind}_page`].next_cursor ? <Button key={kind} type="button" variant="outline" disabled={loading} onClick={() => void load(kind, data[`${kind}_page`].next_cursor ?? undefined)}>{({ runs: "実行", events: "工程", tools: "ツール" })[kind]}をさらに読む</Button> : null)}</div>
      <p className="text-xs text-muted-foreground">最終確認 {executionTime(data.observed_at)} · 件数上限付きの記録です{hasAdditionalHistory ? "。追加で読める履歴があります" : ""}。</p>
    </> : null}
  </section>;
}


