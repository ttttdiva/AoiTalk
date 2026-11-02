"use client";

export type EmployeeObservability = {
  management_visible: boolean; rule_count: number | null; active_rule_count: number | null; action_policy_count: number | null;
  integration_readiness: { status: string; executable: false } | null;
  last_automation_trigger: { event_id: string; event_type: string; occurred_at: string; work_item_id: string } | null;
  last_automation_result: { work_item_id: string; run_id?: string; state: string; updated_at: string; reason_code?: string } | null;
  uncertain_action_count: number | null;
};
export type EmployeeProjection = { id: string; job_title?: string; primary_space_id?: string; legacy?: { agent_team_id?: string | null; execution_profile_id?: string | null } | null; current_work?: { id?: string; state?: string }; employee_observability?: EmployeeObservability };
export type EmployeeSnapshot = { summary: { working?: number; awaiting_approval?: number; uncertain?: number }; agents: EmployeeProjection[] };
export function EmployeeLiveInfo({ projection }: { projection?: EmployeeProjection }) {
  if (!projection) return <span className="block text-xs text-muted-foreground">活動状態は未取得</span>;
  const info = projection.employee_observability;
  return <span className="block space-y-1 text-xs text-muted-foreground">
    {projection.job_title && <span className="block">{projection.job_title}</span>}
    <span className="block">主所属: {projection.primary_space_id || "未設定"}</span>
    <span className="block">現在の仕事: {projection.current_work?.state || "待機"}</span>
    {projection.legacy ? <span className="block">旧設定: {projection.legacy.agent_team_id || "未設定"} / {projection.legacy.execution_profile_id || "未設定"}</span> : null}
    {info && <>
      {info.uncertain_action_count !== null && info.uncertain_action_count > 0 && <span className="block font-medium text-amber-700 dark:text-amber-300">要確認 {info.uncertain_action_count}件 — 再実行禁止</span>}
      {info.last_automation_result && <span className="block">直近の結果: {info.last_automation_result.reason_code === "action_dedupe_suppressed" ? "直近の同一注文があるため注文しませんでした" : info.last_automation_result.state} · {info.last_automation_result.updated_at}</span>}
      {info.last_automation_trigger && <span className="block">最終トリガー: {info.last_automation_trigger.occurred_at}</span>}</>}
  </span>;
}
