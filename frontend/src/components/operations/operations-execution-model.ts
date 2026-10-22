/** Hierarchy preparation adapted from overment/limen picture/viewer/viewer.js
 * a024faff93376b5074461f87dda37e5ed40001e2 (MIT, Adam Gospodarczyk 2026).
 * See THIRD_PARTY_NOTICES/limen.txt. No DOM/state/layout code is copied.
 */
import type { WorkExecution } from "@/lib/operations-execution-api";

export type ExecutionNode = {
  id: string; parent: string | null; kind: "context" | "run" | "event" | "tool";
  title: string; state: string; phase?: string | null; tool?: string | null;
  workId?: string | null; runId?: string; activity?: string | null; elapsed?: number | null;
  attempt?: number | null; reason?: string | null; warning?: string; placeholder?: boolean;
};
export function prepareExecution(data: WorkExecution, workName?: string) {
  const context = `context:${data.work.id}`;
  const nodes: ExecutionNode[] = [{ id: context, parent: null, kind: "context", title: workName || "仕事の実行コンテキスト", state: data.work.state,
    workId: data.work.id, phase: data.work.execution_summary.phase, tool: data.work.execution_summary.tool,
    reason: data.work.execution_summary.blocker_code || data.work.execution_summary.safe_error_code }];
  const runIds = new Set(data.runs.map(run => run.id));
  // Collections are paged independently. References are evidence of a run ID,
  // not evidence of its status, owner, or hierarchy beyond an explicit parent.
  const referencedRunIds = new Set([
    ...data.runs.flatMap(run => run.parent_run_id ? [run.parent_run_id] : []),
    ...data.events.map(event => event.run_id),
    ...data.tool_calls.map(tool => tool.run_id),
  ]);
  for (const id of referencedRunIds) if (!runIds.has(id)) nodes.push({
    id: `run:${id}`, parent: context, kind: "run", title: `未取得のRun ${id}`,
    runId: id, state: "unknown", placeholder: true,
    warning: "run本体は未取得です。担当・所属仕事・状態・上位関係は不明です。取得済みの記録を表示しています。",
  });
  for (const run of data.runs) nodes.push({
    id: `run:${run.id}`, parent: run.parent_run_id ? `run:${run.parent_run_id}` : context,
    kind: "run", title: `${run.acting_subagent_id || "担当未記録"} · Run ${run.id}`, state: run.status,
    workId: run.work_item_id, runId: run.id, phase: run.phase, tool: run.tool, activity: run.last_event_at,
    elapsed: run.elapsed_seconds, attempt: run.work_item_attempt, reason: run.error_code,
    warning: run.parent_run_id && !runIds.has(run.parent_run_id) ? "上位実行は未取得" : undefined,
  });
  for (const event of data.events) if (event.phase) nodes.push({
    id: `event:${event.id}`, parent: `run:${event.run_id}`, kind: "event", title: `工程 ${event.phase} · #${event.sequence} ${event.event_type}`,
    state: event.status || event.event_type, phase: event.phase, runId: event.run_id, activity: event.created_at, reason: event.error_code,
  });
  // Tools and phase evidence are siblings: no recorded phase→tool ownership.
  for (const tool of data.tool_calls) nodes.push({
    id: `tool:${tool.id}`, parent: `run:${tool.run_id}`, kind: "tool", title: `Tool ${tool.tool_name} · ${tool.id}`,
    state: tool.status, runId: tool.run_id, activity: tool.ended_at || tool.started_at,
    elapsed: tool.duration_ms == null ? null : tool.duration_ms / 1000, reason: tool.error_code,
  });
  const byId = new Map(nodes.map(node => [node.id, node]));
  for (const node of nodes) {
    const seen = new Set<string>(); let current = node;
    while (current.parent) {
      seen.add(current.id);
      if (seen.has(current.parent)) { current.parent = context; current.warning = "循環する親子記録。閉じる線は非表示"; break; }
      const parent = byId.get(current.parent); if (!parent) break; current = parent;
    }
  }
  return { nodes, byId, context };
}
const isFinished = (node: ExecutionNode) =>
  ["succeeded", "cancelled", "completed", "run.succeeded", "run.cancelled"].includes(node.state)
  || (node.kind === "event" && ["completed", "cancelled"].includes(node.phase || ""));

export function executionLevel(model: ReturnType<typeof prepareExecution>, focusId: string | null, showFinished: boolean) {
  const focus = model.byId.get(focusId || "") || model.byId.get(model.context)!;
  const visibleIds = new Set<string>();
  if (!showFinished) {
    for (const node of model.nodes) {
      // Historical phase events do not establish a currently active child.
      if (node.kind === "event" || isFinished(node)) continue;
      // Keep every ancestor on the path to an unfinished descendant. This
      // retains navigation through completed delegators, without relabeling
      // them as active or inventing a direct edge to their descendants.
      let current: ExecutionNode | undefined = node;
      while (current && !visibleIds.has(current.id)) {
        visibleIds.add(current.id);
        current = current.parent ? model.byId.get(current.parent) : undefined;
      }
    }
    for (const node of model.nodes) {
      const owner = node.parent ? model.byId.get(node.parent) : undefined;
      if (node.kind === "event" && !isFinished(node) && owner && !isFinished(owner) && visibleIds.has(owner.id)) visibleIds.add(node.id);
    }
  }
  const nodes = [focus, ...model.nodes.filter(node => node.parent === focus.id && (showFinished || visibleIds.has(node.id)))];
  const edges = nodes.slice(1).map(node => ({ id: `${focus.id}>${node.id}`, source: focus.id, target: node.id,
    label: focus.kind === "context" ? "実行コンテキスト内" : node.kind === "run" ? "委任親子" : "runの記録" }));
  return { focus, nodes, edges };
}
