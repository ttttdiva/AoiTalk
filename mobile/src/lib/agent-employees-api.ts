import { fetchApiAtServerFingerprint, isApiHttpError, createApiRequestId } from './api-client';

export type ModelRoute = { kind: 'llm'; provider: string; model: string; effort?: string | null } | { kind: 'pool'; pool_id: string };
export type Role = 'chat' | 'code' | 'review' | 'research' | 'summarize';
export type Fallback = 'model_unavailable' | 'provider_unavailable' | 'rate_limited' | 'connect_timeout';
export type AgentModelOverrides = Partial<Record<Role, { routes: ModelRoute[]; allow_paid_fallback?: boolean | null; fallback_on?: Fallback[] | null }>>;
export type Employee = { id: string; display_name: string; state: string; created_by: string | null; is_default_chat_agent: boolean };
export type EmployeeRevision = { id: string; agent_id: string; version: number; display_name: string; mission: string; responsibility_summary: string; operational_instructions: string; model_overrides: AgentModelOverrides | null; capability_ceiling: string[]; budget_policy: Record<string, unknown>; concurrency_policy: Record<string, unknown>; wake_policy: Record<string, unknown>; tool_ids: string[] | null; delegate_policy: Record<string, unknown> | null };
export type RevisionInput = { version: number; display_name: string; mission: string; responsibility_summary: string; operational_instructions: string; model_overrides?: AgentModelOverrides | null; idempotency_key: string };
export type ModelCatalog = { providers: { id: string; label: string; selection_kind?: string; models: { id: string; label?: string; selection_kind?: string }[] }[] };
const path = (id: string) => `/api/agents/${encodeURIComponent(id)}`;
const request = <T,>(server: string, url: string, signal?: AbortSignal, body?: unknown) => fetchApiAtServerFingerprint<T>(server, url, { method: body === undefined ? 'GET' : 'POST', signal, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
export const employeeKey = () => createApiRequestId('agent');
export const employeeApi = {
  list: (server: string, signal?: AbortSignal) => request<{ agents: Employee[] }>(server, '/api/agents', signal),
  catalog: async (server: string, signal?: AbortSignal) => { try { return await request<{ can_manage: boolean }>(server, '/api/agents/catalog', signal); } catch (error) { if (isApiHttpError(error) && error.status === 403) return { can_manage: false }; throw error; } },
  revisions: (server: string, id: string, signal?: AbortSignal) => request<{ revisions: EmployeeRevision[] }>(server, `${path(id)}/revisions`, signal),
  models: (server: string, signal?: AbortSignal) => request<ModelCatalog>(server, '/api/llm/models', signal),
  pools: (server: string, signal?: AbortSignal) => request<{ profile?: { pools?: Record<string, unknown> } }>(server, '/api/free-team/settings', signal),
  create: (server: string, display_name: string, idempotency_key: string, signal?: AbortSignal) => request<{ agent: Employee }>(server, '/api/agents', signal, { display_name, idempotency_key }),
  revision: (server: string, id: string, body: RevisionInput, signal?: AbortSignal) => request<{ revision: EmployeeRevision }>(server, `${path(id)}/revisions`, signal, body),
};
export function employeeError(error: unknown): string {
  if (isApiHttpError(error)) return error.status === 403 ? '管理者権限または所属範囲を確認してください。' : error.status === 409 ? '他の更新と競合しました。変更を保持しています。再読み込みして確認してください。' : error.status === 401 ? 'ログイン状態を確認してください。' : error.status === 404 || error.status === 503 ? 'Agent 機能または接続先を利用できません。' : '入力内容と接続先を確認してください。';
  return '通信結果を確認できません。再読み込みして保存状態を確認してください。';
}

/** Uncertain requests stay bound to their actor and server for this process. */
export const employeePending = new Map<string, { key: string; payload: string }>();
