"use client";

export type SkillProposalStatus =
  | "pending"
  | "applied"
  | "rejected"
  | "stale";

export type SkillProposalOperation = "create" | "update";
export type SkillProposalScope = "global" | "project";
export type SkillProposalReason = "manual" | "correction" | "procedure";

export interface SkillProposalContent {
  name: string;
  description: string;
  prompt_template: string;
  trigger_mode: string;
  aliases: string[];
  bound_tools: string[];
  examples: string[];
  tags: string[];
  parameters: Record<string, unknown>;
}

export interface SkillProposalHistoryEntry {
  id: string;
  proposal_id: string;
  sequence: number;
  event: string;
  actor_user_id: string;
  from_status?: string | null;
  to_status?: string | null;
  observed_hash?: string | null;
  result_hash?: string | null;
  details: Record<string, unknown>;
  created_at?: string | null;
}

export interface SkillProposal {
  id: string;
  user_id: string;
  project_id?: string | null;
  receipt_id?: string | null;
  operation: SkillProposalOperation;
  reason_type: SkillProposalReason;
  target_scope: SkillProposalScope;
  target_name: string;
  target_path: string;
  status: SkillProposalStatus;
  proposed_content: SkillProposalContent;
  base_snapshot?: SkillProposalContent | null;
  base_hash?: string | null;
  base_version?: string | null;
  applied_hash?: string | null;
  applied_version?: string | null;
  provenance: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  applied_at?: string | null;
  rejected_at?: string | null;
  stale_at?: string | null;
  rolled_back_at?: string | null;
  history?: SkillProposalHistoryEntry[];
}

export interface SkillUsageReceipt {
  id: string;
  user_id: string;
  project_id?: string | null;
  session_id?: string | null;
  message_id?: string | null;
  agent_run_id?: string | null;
  tool_call_id?: string | null;
  invocation_path: string;
  outcome: "success" | "error" | string;
  skill_name: string;
  skill_scope: SkillProposalScope;
  skill_path: string;
  skill_hash: string;
  skill_version: string;
  provenance: Record<string, unknown>;
  created_at?: string | null;
}

export interface CreateSkillProposalInput {
  operation: SkillProposalOperation;
  target_scope: SkillProposalScope;
  target_name: string;
  project_id?: string;
  reason_type?: SkillProposalReason;
  proposed_content: Partial<SkillProposalContent>;
  receipt_id?: string;
  evidence?: Array<Record<string, unknown>>;
  provenance?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface SkillProposalRevisionToken {
  updated_at?: string | null;
  status: SkillProposalStatus;
  base_hash?: string | null;
  base_version?: string | null;
  applied_hash?: string | null;
  applied_version?: string | null;
  rolled_back_at?: string | null;
}

export type SkillLearningApiErrorCode =
  | "network"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "validation"
  | "conflict"
  | "stale"
  | "server"
  | "unknown";

export class SkillLearningApiError extends Error {
  readonly status: number;
  readonly code: SkillLearningApiErrorCode;
  readonly proposal?: SkillProposal;

  constructor(
    message: string,
    status: number,
    code: SkillLearningApiErrorCode,
    proposal?: SkillProposal,
  ) {
    super(message);
    this.name = "SkillLearningApiError";
    this.status = status;
    this.code = code;
    this.proposal = proposal;
  }
}

const API_BASE = "/api/python-proxy/skills";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function proposalFromUnknown(value: unknown): SkillProposal | undefined {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.target_name !== "string" ||
    typeof value.status !== "string"
  ) {
    return undefined;
  }
  return value as unknown as SkillProposal;
}

function errorCodeForStatus(
  status: number,
  proposal?: SkillProposal,
): SkillLearningApiErrorCode {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 400 || status === 422) return "validation";
  if (status === 409 && proposal?.status === "stale") return "stale";
  if (status === 409) return "conflict";
  if (status >= 500) return "server";
  return "unknown";
}

function fallbackErrorMessage(status: number): string {
  if (status === 401) return "認証が必要です。";
  if (status === 403) return "この Skill 提案を操作する権限がありません。";
  if (status === 404) return "Skill 提案または使用実績が見つかりません。";
  if (status === 400 || status === 422) {
    return "Skill 提案の入力内容を確認してください。";
  }
  if (status === 409) {
    return "Skill または提案が更新されています。最新状態を確認してください。";
  }
  if (status >= 500) {
    return "Skill 提案の処理に失敗しました。再読み込みして再試行してください。";
  }
  return `Skill 提案 API エラー (${status})`;
}

async function apiError(response: Response): Promise<SkillLearningApiError> {
  const payload = await response.json().catch(() => null);
  const detail = isRecord(payload) ? payload.detail : undefined;

  let detailMessage: string | undefined;
  let proposal: SkillProposal | undefined;

  if (typeof detail === "string") {
    detailMessage = detail;
  } else if (isRecord(detail)) {
    if (typeof detail.message === "string") {
      detailMessage = detail.message;
    }
    proposal = proposalFromUnknown(detail.proposal);
  }

  const code = errorCodeForStatus(response.status, proposal);
  const allowBackendMessage =
    response.status === 400 ||
    response.status === 404 ||
    response.status === 409 ||
    response.status === 422;
  const message =
    allowBackendMessage && detailMessage
      ? detailMessage
      : fallbackErrorMessage(response.status);

  return new SkillLearningApiError(message, response.status, code, proposal);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { headers, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      credentials: "include",
      ...rest,
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
    });
  } catch {
    throw new SkillLearningApiError(
      "Skill 提案 API に接続できません。",
      0,
      "network",
    );
  }

  if (!response.ok) {
    throw await apiError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

export function proposalRevisionToken(
  proposal: SkillProposal,
): SkillProposalRevisionToken {
  return {
    updated_at: proposal.updated_at,
    status: proposal.status,
    base_hash: proposal.base_hash,
    base_version: proposal.base_version,
    applied_hash: proposal.applied_hash,
    applied_version: proposal.applied_version,
    rolled_back_at: proposal.rolled_back_at,
  };
}

function sameRevision(
  proposal: SkillProposal,
  expected: SkillProposalRevisionToken,
): boolean {
  return (
    proposal.updated_at === expected.updated_at &&
    proposal.status === expected.status &&
    proposal.base_hash === expected.base_hash &&
    proposal.base_version === expected.base_version &&
    proposal.applied_hash === expected.applied_hash &&
    proposal.applied_version === expected.applied_version &&
    proposal.rolled_back_at === expected.rolled_back_at
  );
}

async function getProposal(
  proposalId: string,
): Promise<{ success: boolean; proposal: SkillProposal }> {
  return request(
    `/proposals/${encodeURIComponent(proposalId)}`,
  );
}

async function assertProposalRevision(
  proposalId: string,
  expected?: SkillProposalRevisionToken,
): Promise<void> {
  if (!expected) return;
  const current = (await getProposal(proposalId)).proposal;
  if (sameRevision(current, expected)) return;

  throw new SkillLearningApiError(
    "提案は別の操作で更新されています。最新状態を確認してください。",
    409,
    current.status === "stale" ? "stale" : "conflict",
    current,
  );
}

export const skillLearningApi = {
  async listProposals(
    options: {
      status?: SkillProposalStatus;
      projectId?: string;
      limit?: number;
    } = {},
  ): Promise<{ success: boolean; proposals: SkillProposal[] }> {
    const params = new URLSearchParams();
    if (options.status) params.set("status", options.status);
    if (options.projectId) params.set("project_id", options.projectId);
    params.set("limit", String(options.limit ?? 100));
    return request(`/proposals?${params.toString()}`);
  },

  getProposal,

  async createProposal(
    input: CreateSkillProposalInput,
  ): Promise<{ success: boolean; proposal: SkillProposal }> {
    return request("/proposals", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  async reviseProposal(
    proposalId: string,
    proposedContent: SkillProposalContent,
    provenance: Record<string, unknown> = {},
    expected?: SkillProposalRevisionToken,
  ): Promise<{ success: boolean; proposal: SkillProposal }> {
    await assertProposalRevision(proposalId, expected);
    return request(`/proposals/${encodeURIComponent(proposalId)}`, {
      method: "PUT",
      body: JSON.stringify({
        proposed_content: proposedContent,
        provenance,
      }),
    });
  },

  async applyProposal(
    proposalId: string,
    expected?: SkillProposalRevisionToken,
  ): Promise<{ success: boolean; proposal: SkillProposal }> {
    await assertProposalRevision(proposalId, expected);
    return request(`/proposals/${encodeURIComponent(proposalId)}/apply`, {
      method: "POST",
    });
  },

  async rejectProposal(
    proposalId: string,
    reason?: string,
    expected?: SkillProposalRevisionToken,
  ): Promise<{ success: boolean; proposal: SkillProposal }> {
    await assertProposalRevision(proposalId, expected);
    return request(`/proposals/${encodeURIComponent(proposalId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: reason?.trim() || null }),
    });
  },

  async rollbackProposal(
    proposalId: string,
    expected?: SkillProposalRevisionToken,
  ): Promise<{ success: boolean; proposal: SkillProposal }> {
    await assertProposalRevision(proposalId, expected);
    return request(`/proposals/${encodeURIComponent(proposalId)}/rollback`, {
      method: "POST",
    });
  },

  async getUsageReceipt(
    receiptId: string,
  ): Promise<{ success: boolean; receipt: SkillUsageReceipt }> {
    return request(`/usage-receipts/${encodeURIComponent(receiptId)}`);
  },
};
