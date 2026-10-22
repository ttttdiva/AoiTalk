/** Ordinary admin session API for bounded local reading tasks. No credential or actor overrides. */
import type { components } from "./api-types.gen";
export type InstructionReview = components["schemas"]["InstructionReviewPreview"];
export type InstructionReviewApproval = components["schemas"]["InstructionReviewApproveRequest"];
export type InstructionReviewStatus = Omit<components["schemas"]["InstructionReviewStatus"], "task"> & { task: LocalTask | null };
export type LocalTaskRoute = { provider: string; model: string; effort?: string };
export type LocalTaskStatus = {
  enabled: boolean; running: boolean; model: string; allowed_roots: string[];
  max_concurrency: number; queued: number; limits: Record<string, number>;
  supports_task_routes?: boolean; supported_providers?: string[]; default_route?: LocalTaskRoute;
  codex_app_profile_connection_enabled?: boolean; codex_app_profile_connector_available?: boolean;
};
export type LocalTask = {
  task_id: string; status: "queued" | "running" | "completed" | "failed" | "cancelled";
  goal: string; workspace: string; mode: string; is_terminal: boolean;
  created_at?: string; finished_at?: string | null; current_step?: string;
  latest_activity?: string; counters?: Record<string, number>; failure_reason?: string | null;
  failure_detail?: unknown; summary_text?: string | null; completion_summary?: unknown; result?: unknown;
  llm?: LocalTaskRoute | null; request?: Record<string, unknown>; changed_files?: unknown[];
  cancel_requested?: boolean; worker_alive?: boolean; stalled?: boolean;
};
export type LocalTaskInput = {
  goal: string; workspace: string; completion_criteria: string[];
  max_tool_rounds: number; timeout_sec: number; route?: LocalTaskRoute;
  codex_connection?: "app_profile";
};
export type LocalTaskBody = Omit<LocalTaskInput, "route"> & {
  mode: "read_only"; allow_commands: false; allow_adb: false; allow_git_commit: false;
  command_timeout_sec: 30; constraints: string[]; provider?: string; model?: string; effort?: string;
  codex_connection?: "app_profile";
};
export class LocalTaskApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) { super(message); this.name = "LocalTaskApiError"; }
}
const PREFIX = "/api/python-proxy/api/local-agent";
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "include", cache: "no-store", ...init });
  const body = await response.json().catch(() => null) as Record<string, unknown> | null;
  if (!response.ok) throw new LocalTaskApiError(typeof body?.detail === "string" ? body.detail : `ローカルタスクAPIエラー (${response.status})`, response.status, typeof body?.error === "string" ? body.error : undefined);
  if (!body) throw new LocalTaskApiError("ローカルタスクAPIの応答を確認できませんでした", response.status);
  return body as T;
}
function taskId(id: string) { if (!/^[a-f0-9]{32}$/u.test(id)) throw new Error("タスクIDが不正です"); return id; }
export function readingTaskBody(input: LocalTaskInput): LocalTaskBody {
  if (!input.goal.trim() || input.goal.length > 8000 || !input.workspace.trim() || input.workspace.length > 1024) throw new Error("目的と作業フォルダーを入力してください");
  if (!Number.isInteger(input.max_tool_rounds) || input.max_tool_rounds < 1 || input.max_tool_rounds > 1000 || !Number.isInteger(input.timeout_sec) || input.timeout_sec < 30 || input.timeout_sec > 86400) throw new Error("実行回数・時間の上限が不正です");
  if (!Array.isArray(input.completion_criteria) || input.completion_criteria.length > 20 || input.completion_criteria.some(x => typeof x !== "string" || x.length > 512)) throw new Error("完了条件は1行512文字・20件までです");
  const body: LocalTaskBody = {
    goal: input.goal.trim(), workspace: input.workspace.trim(), completion_criteria: input.completion_criteria,
    max_tool_rounds: input.max_tool_rounds, timeout_sec: input.timeout_sec,
    mode: "read_only", allow_commands: false, allow_adb: false, allow_git_commit: false, command_timeout_sec: 30,
    constraints: ["指定フォルダーを読み取り、ファイル・設定・Git状態を変更しない", "秘密情報を読み取らず、結果にも含めない"],
  };
  if (input.route) {
    if (!["openai_compatible_local", "codex-cli"].includes(input.route.provider) || !input.route.model.trim() || input.route.model.length > 255 || (input.route.effort?.length ?? 0) > 32) throw new Error("実行経路が不正です");
    body.provider = input.route.provider; body.model = input.route.model.trim();
    if (input.route.effort) body.effort = input.route.effort;
  }
  if (input.codex_connection !== undefined) {
    if (input.codex_connection !== "app_profile" || input.route?.provider !== "codex-cli") throw new Error("専用Codex接続の実行経路が不正です");
    body.codex_connection = "app_profile";
  }
  return body;
}
export const operationsLocalTasksApi = {
  owner: () => request<{ authenticated: boolean; user?: { id: string; role: string } }>("/api/auth/status"),
  status: () => request<LocalTaskStatus>(`${PREFIX}/status`),
  list: () => request<{ tasks: LocalTask[] }>(`${PREFIX}/tasks?limit=20`),
  get: (id: string) => request<{ task: LocalTask }>(`${PREFIX}/tasks/${taskId(id)}`),
  tail: (id: string) => request<{ events: unknown[]; truncated: boolean }>(`${PREFIX}/tasks/${taskId(id)}/tail?limit=30&max_bytes=16000`),
  create: (body: LocalTaskBody, key: string) => request<{ task: LocalTask; created: boolean }>(`${PREFIX}/tasks`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(body) }),
  cancel: (id: string) => request<{ task: LocalTask }>(`${PREFIX}/tasks/${taskId(id)}/cancel`, { method: "POST" }),
  previewInstructions: (body: LocalTaskBody) => request<InstructionReview>(`${PREFIX}/instruction-reviews/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  instructionReviewStatus: (nonce: string) => request<InstructionReviewStatus>(`${PREFIX}/instruction-reviews/${taskId(nonce)}`),
  approveInstructions: (nonce: string, approval: InstructionReviewApproval) => request<{ task: LocalTask; created: boolean }>(`${PREFIX}/instruction-reviews/${taskId(nonce)}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(approval) }),
};

type ProfileSchemas = components["schemas"];
function profilePost<T>(action: string, body: unknown = {}) {
  return request<T>(`${PREFIX}/codex-app-profile/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}
export const codexProfileApi = {
  status: () => request<ProfileSchemas["CodexProfileStatus"]>(`${PREFIX}/codex-app-profile/status`),
  handoff: () => profilePost<ProfileSchemas["CodexProfileHandoff"]>("handoff"),
  approve: (body: ProfileSchemas["CodexProfileApprovalRequest"]) => profilePost<ProfileSchemas["CodexProfileApprovalReceipt"]>("approve", body),
  login: (approval_receipt: string) => profilePost<ProfileSchemas["CodexProfileLoginHandoff"]>("login", { approval_receipt }),
  poll: () => profilePost<ProfileSchemas["CodexProfileStatus"]>("poll"),
  disconnect: () => profilePost<ProfileSchemas["CodexProfileStatus"]>("disconnect"),
};
export type PendingLocalTask = { owner: string; key: string; input: LocalTaskInput; body: LocalTaskBody };
export function pendingLocalTaskKey(owner: string) { return `aoitalk:local-reading-task:${owner}`; }
/** A route preference only; never a connection grant or credential. */
export function codexTaskConnectionKey(owner: string) { return `aoitalk:local-task-codex-route:${owner}`; }
export function parsePendingLocalTask(raw: string | null, owner: string): PendingLocalTask | null {
  if (!raw) return null;
  if (raw.length > 64000) throw new Error("保存された受付情報が大きすぎます");
  const value = JSON.parse(raw) as PendingLocalTask;
  if (!value || value.owner !== owner || !/^[a-zA-Z0-9_-]{8,128}$/u.test(value.key)) throw new Error("保存された受付情報の所有者またはキーが不正です");
  const canonical = readingTaskBody(value.input);
  if (JSON.stringify(canonical) !== JSON.stringify(value.body)) throw new Error("保存された受付情報が変更されています");
  return value;
}
