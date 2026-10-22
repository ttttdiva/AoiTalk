import { executionDuration, executionTime, type WorkExecution } from "@/lib/operations-execution-api";
import type { ExecutionNode } from "./operations-execution-model";
export function OperationsExecutionDetail({ node, data }: { node: ExecutionNode; data: WorkExecution }) {
  const events = data.events.filter(event => event.run_id === node.runId);
  return <aside aria-label="選択した実行の詳細" className="min-w-0 space-y-3 rounded-xl border border-border bg-card p-4">
    <h3 className="break-all font-semibold">{node.title}</h3>
    <dl className="space-y-2 break-all text-sm">
      <div><dt>状態</dt><dd>{node.state}</dd></div>
      <div><dt>所属仕事</dt><dd>{node.workId || "未記録"}</dd></div>
      <div><dt>現在の工程 / Tool</dt><dd>{node.phase || "未記録"} / {node.tool || "未記録"}</dd></div>
      <div><dt>停止理由</dt><dd>{node.reason || "未記録"}</dd></div>
      <div><dt>次に進む条件</dt><dd>{node.state === "awaiting_approval" ? "正本の画面で承認内容を確認" : "未記録。正本の画面で確認"}</dd></div>
      <div><dt>最終活動 / 経過</dt><dd>{executionTime(node.activity)} / {executionDuration(node.elapsed)}</dd></div>
      <div><dt>Attempt</dt><dd>{node.attempt ?? "未記録"}</dd></div>
    </dl>
    {node.warning ? <p role="status" className="text-sm text-amber-700">{node.warning}</p> : null}
    <p className="text-xs text-muted-foreground">親子線は委任の記録です。依存待ちの関係は未記録。Heartbeatは生存確認であり、実進捗ではありません。</p>
    <h4 className="text-sm font-medium">イベント根拠（取得済み {events.length}件）</h4>
    <ul className="max-h-64 space-y-2 overflow-auto text-xs">{events.map(event => <li key={event.id} className="break-all rounded border border-border p-2">#{event.sequence} {event.event_type} · {event.status || "状態未記録"} · {event.phase || "工程未記録"} · {executionTime(event.created_at)}</li>)}</ul>
    {!events.length ? <p className="text-xs">この選択のイベント根拠は未取得です。</p> : null}
  </aside>;
}
