/** AI employee management uses the authenticated FastAPI proxy, never the projection writer. */
export class EmployeeApiError extends Error {
  constructor(public readonly status: number) {
    super(status === 409 ? "他の更新と競合しました。再読み込みして変更を確認してください。"
      : status === 403 ? "管理者権限、所属範囲または実行環境の許可がありません。"
      : status === 401 ? "ログイン状態を確認してください。"
      : status === 422 ? "入力内容または参照先が無効です。必須項目と権限の範囲を確認してください。"
      : status === 404 ? "対象または機能が見つかりません。設定と有効化状態を確認してください。"
      : status === 503 ? "接続先または実行サービスを利用できません。準備状態を確認してください。"
      : "通信に失敗しました。再読み込みして保存状態を確認してください。");
  }
}

export function employeeKey(): string { return crypto.randomUUID(); }
export async function employeeRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  // Error bodies may contain echoed credentials or validation input; never retain them.
  const response = await fetch(`/api/python-proxy${path.replace(/^\/api(?=\/)/u, "")}`, {
    method, credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }).catch(() => { throw new EmployeeApiError(0); });
  if (!response.ok) throw new EmployeeApiError(response.status);
  return response.json() as Promise<T>;
}
export async function employeeScopeRequest<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "include", cache: "no-store" })
    .catch(() => { throw new EmployeeApiError(0); });
  if (!response.ok) throw new EmployeeApiError(response.status);
  return response.json() as Promise<T>;
}
export type Employee = { id: string; display_name: string; slug: string; state: "draft" | "active" | "paused" | "retired"; character_id?: string | null; updated_at?: string };
export type EmployeeRevision = {
  id: string; agent_id: string; version: number; display_name: string; mission: string;
  responsibility_summary: string; operational_instructions: string; agent_team_id: string;
  execution_profile_id: string; allowed_subagent_ids: string[]; capability_ceiling: string[];
  budget_policy: { max_run_cost_micros?: number; max_daily_cost_micros?: number; currency?: string };
  concurrency_policy: { max_parallel_runs?: number; max_parallel_tasks?: number; queue_class?: string };
  wake_policy?: Record<string, string | number>; created_at?: string;
};
export type EmployeeCatalog = {
  can_manage: boolean; runtime_profile?: string;
  teams: { team_id: string; name: string; subagents: { subagent_id: string; name: string; capability_ids: string[] }[]; execution_profiles: { profile_id: string; name: string }[] }[];
  capabilities: { id: string; family: string; access: string; native: boolean }[];
};
export type EmployeeProfile = { job_title?: string; responsibility_summary?: string; primary_space_id?: string | null; autonomy_level?: string; employment_state?: string };
export type ScopeOption = { id: string; name?: string; display_name?: string; title?: string; project_id?: string | null; space_id?: string | null; current_revision?: { display_name?: string } };
export type AssignmentKind = "space-assignments" | "project-grants" | "task-assignments" | "persona-operator-assignments";
export type EmployeeAssignment = { id: string; state: string; space_id?: string; project_id?: string; task_id?: string; persona_id?: string; role?: string; assignment_kind?: string; assignment_role?: string };
export type ActionDefinition = { action_type: string; display_name: string; category: string; status: string; connection_provider_keys: string[]; required_capabilities: string[]; constraints_schema: Record<string, unknown> | null };
export type ProcurementConstraints = { fixed_item_ref: string; fixed_ship_to_ref: string; min_quantity: number; max_quantity: number; default_quantity: number; currency: string; max_order_total_minor: number; require_quote_before_execute: boolean };
export type PolicyRevision = { id: string; version: number; action_type: string; connection_id: string; authorization_mode: "human_approval" | "bounded_auto"; constraints: Partial<ProcurementConstraints> & { route_id?: string; allowed_destination_keys?: string[] }; rate_limit: { window_seconds: number; max_actions: number }; dedupe_window_seconds: number; fallback_behavior: string; active_from?: string | null; active_until?: string | null };
export type EmployeePolicy = { id: string; display_name: string; state: string; version: number; current_revision: PolicyRevision | null };
export const assignmentEnvelope: Record<AssignmentKind, string> = { "space-assignments": "space_assignments", "project-grants": "project_grants", "task-assignments": "task_assignments", "persona-operator-assignments": "persona_operator_assignments" };
export const employeePath = (id: string) => `/api/agents/${encodeURIComponent(id)}`;
export const employeeApi = {
  list: () => employeeRequest<{ agents: Employee[] }>("/api/agents"),
  catalog: () => employeeRequest<EmployeeCatalog>("/api/agents/catalog"),
  get: (id: string) => employeeRequest<{ agent: Employee }>(employeePath(id)),
  revisions: (id: string) => employeeRequest<{ revisions: EmployeeRevision[] }>(`${employeePath(id)}/revisions`),
  profile: (id: string) => employeeRequest<{ organization_profile: EmployeeProfile }>(`${employeePath(id)}/organization-profile`),
  create: (body: { display_name: string; slug?: string; character_id?: string; idempotency_key: string }) => employeeRequest<{ agent: Employee }>("/api/agents", "POST", body),
  state: (id: string, state: Employee["state"], expected_state: Employee["state"]) => employeeRequest<{ agent: Employee }>(`${employeePath(id)}/state`, "PATCH", { state, expected_state }),
  revision: (id: string, body: Omit<EmployeeRevision, "id" | "agent_id" | "created_at"> & { idempotency_key: string }) => employeeRequest<{ revision: EmployeeRevision }>(`${employeePath(id)}/revisions`, "POST", body),
  saveProfile: (id: string, body: EmployeeProfile) => employeeRequest<{ organization_profile: EmployeeProfile }>(`${employeePath(id)}/organization-profile`, "PATCH", body),
};
