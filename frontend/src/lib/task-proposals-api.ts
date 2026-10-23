export type TaskProposalStatus = "pending" | string;

export type TaskProposal = {
  id: string;
  task_id: string;
  session_id: string;
  agent_run_id: string;
  revision: number;
  plan_id: string;
  plan_digest: string;
  action_digest: string;
  material_action_preview_digest: string;
  title: string;
  summary: string;
  plan_text: string;
  status: TaskProposalStatus;
};

export type TaskProposalDecisionAction = "approve" | "reject";

export type TaskProposalDecisionPayload = {
  action: TaskProposalDecisionAction;
  revision: number;
  plan_id: string;
  plan_digest: string;
  action_digest: string;
  material_action_preview_digest: string;
};

export const MAX_TASK_PROPOSAL_TASK_IDS = 50;
export const TASK_PROPOSALS_LIST_PATH = "/api/python-proxy/task-proposals";
export const TASK_PROPOSALS_REFRESH_EVENT = "aoitalk:task-proposals-refresh";

/**
 * 「確認」の遷移先。提案が属する既存の会話を開き、どの提案かを
 * `proposal` で識別する。実行・決定は一切送らない。
 */
export function taskProposalChatPath(
  proposal: Pick<TaskProposal, "id" | "session_id">,
): string {
  const query = new URLSearchParams({
    s: proposal.session_id,
    proposal: proposal.id,
  });
  return `/chat?${query.toString()}`;
}

export class TaskProposalsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly responseBody: unknown = null,
  ) {
    super(message);
    this.name = "TaskProposalsApiError";
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const detail = (body as { detail?: unknown }).detail;
  return typeof detail === "string" && detail.trim() ? detail : fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new TaskProposalsApiError(
      errorMessage(body, response.statusText || `HTTP ${response.status}`),
      response.status,
      body,
    );
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function normalizeTaskIds(taskIds: readonly string[]): string[] {
  return [...new Set(taskIds.map((taskId) => taskId.trim()).filter(Boolean))];
}

function taskIdBatches(taskIds: readonly string[]): string[][] {
  const batches: string[][] = [];
  for (let index = 0; index < taskIds.length; index += MAX_TASK_PROPOSAL_TASK_IDS) {
    batches.push(taskIds.slice(index, index + MAX_TASK_PROPOSAL_TASK_IDS));
  }
  return batches;
}

function isTaskProposal(value: unknown): value is TaskProposal {
  if (typeof value !== "object" || value === null) return false;
  const proposal = value as Partial<TaskProposal>;
  return (
    typeof proposal.id === "string" &&
    proposal.id.trim().length > 0 &&
    typeof proposal.task_id === "string" &&
    proposal.task_id.trim().length > 0 &&
    typeof proposal.session_id === "string" &&
    proposal.session_id.trim().length > 0 &&
    typeof proposal.agent_run_id === "string" &&
    proposal.agent_run_id.trim().length > 0 &&
    typeof proposal.revision === "number" &&
    Number.isFinite(proposal.revision) &&
    typeof proposal.plan_id === "string" &&
    proposal.plan_id.trim().length > 0 &&
    typeof proposal.plan_digest === "string" &&
    proposal.plan_digest.trim().length > 0 &&
    typeof proposal.action_digest === "string" &&
    proposal.action_digest.trim().length > 0 &&
    typeof proposal.material_action_preview_digest === "string" &&
    proposal.material_action_preview_digest.trim().length > 0 &&
    typeof proposal.title === "string" &&
    proposal.title.trim().length > 0 &&
    typeof proposal.summary === "string" &&
    typeof proposal.plan_text === "string" &&
    typeof proposal.status === "string"
  );
}

export function pendingTaskProposalsFromResponse(
  payload: unknown,
  taskIds: readonly string[],
): TaskProposal[] {
  if (typeof payload !== "object" || payload === null) return [];
  const proposals = (payload as { proposals?: unknown }).proposals;
  if (!Array.isArray(proposals)) return [];
  const allowedTaskIds = new Set(normalizeTaskIds(taskIds));
  const normalized: TaskProposal[] = [];
  for (const proposal of proposals) {
    if (
      !isTaskProposal(proposal) ||
      proposal.status !== "pending" ||
      !allowedTaskIds.has(proposal.task_id)
    ) {
      throw new TaskProposalsApiError(
        "提案APIの応答に不正な提案が含まれています",
        502,
        payload,
      );
    }
    normalized.push(proposal);
  }
  const taskOrder = new Map(
    normalizeTaskIds(taskIds).map((taskId, index) => [taskId, index]),
  );
  normalized.sort(
    (left, right) =>
      (taskOrder.get(left.task_id) ?? Number.MAX_SAFE_INTEGER) -
      (taskOrder.get(right.task_id) ?? Number.MAX_SAFE_INTEGER),
  );
  return normalized;
}

function deduplicateTaskProposals(proposals: TaskProposal[]): TaskProposal[] {
  const seen = new Set<string>();
  return proposals.filter((proposal) => {
    const key = JSON.stringify([proposal.id, proposal.revision]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export const taskProposalsApi = {
  list: async (
    taskIds: readonly string[],
    init?: RequestInit,
  ): Promise<TaskProposal[]> => {
    const normalizedTaskIds = normalizeTaskIds(taskIds);
    if (normalizedTaskIds.length === 0) return [];
    const payloads = await Promise.all(
      taskIdBatches(normalizedTaskIds).map(async (batch) => {
        const query = new URLSearchParams({ task_ids: batch.join(",") });
        const payload = await request<{
          success?: boolean;
          proposals?: unknown;
        }>(`${TASK_PROPOSALS_LIST_PATH}?${query.toString()}`, init);
        if (payload.success !== true || !Array.isArray(payload.proposals)) {
          throw new TaskProposalsApiError("提案APIの応答が不正です", 502, payload);
        }
        return pendingTaskProposalsFromResponse(payload, batch);
      }),
    );
    return deduplicateTaskProposals(payloads.flat());
  },

  decide: (
    taskId: string,
    proposalId: string,
    payload: TaskProposalDecisionPayload,
  ) =>
    request<unknown>(
      `/api/python-proxy/tasks/${encodeURIComponent(taskId)}/agent-proposals/${encodeURIComponent(proposalId)}/decision`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
};
