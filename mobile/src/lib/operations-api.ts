/** Operations is a live server projection. Never enqueue it or store it in SQLite. */
import { fetchApiAtServerFingerprint } from "./api-client";
import type { components } from "../types/api-types.gen";

/**
 * Operations is a read-only projection, but its server-side aggregation can
 * legitimately take longer than ordinary mobile API calls. Keep this budget
 * local to Operations so the shared API and LLM deadlines remain unchanged.
 */
export const OPERATIONS_READ_TIMEOUT_MS = 30_000;

type ApiWorkExecution = components["schemas"]["OperationsWorkExecutionResponse"];
export type OperationsCanonicalSourceRef =
  components["schemas"]["OperationsCanonicalSourceRef"];
export type ConversationReference = { kind: "conversation"; id: string };
export type OperationsWorkReference = { kind: "work"; id: string };

export type ExecutionSummary = Omit<
  components["schemas"]["OperationsExecutionSummary"],
  "attention_reasons"
> & {
  attention_reasons: NonNullable<
    components["schemas"]["OperationsExecutionSummary"]["attention_reasons"]
  >;
};
export type WorkExecution = Omit<
  ApiWorkExecution,
  "runs" | "events" | "tool_calls" | "work"
> & {
  runs: NonNullable<ApiWorkExecution["runs"]>;
  events: NonNullable<ApiWorkExecution["events"]>;
  tool_calls: NonNullable<ApiWorkExecution["tool_calls"]>;
  work: Omit<ApiWorkExecution["work"], "execution_summary"> & {
    execution_summary: ExecutionSummary;
  };
};
export type OperationsCard = {
  id: string;
  /** Work ID is null for an ExternalAction-only attention row. */
  workId: string | null;
  sourceKind: string;
  title: string;
  state: string;
  agent: string;
  project: string;
  taskId: string | null;
  conversationRef: ConversationReference | null;
  summary: Partial<ExecutionSummary>;
  attention: string[];
  approvalDetails?: string[];
};
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown, fallback = "未記録") => typeof value === "string" && value.trim() ? value.slice(0,160) : fallback;
const id = (value: unknown) => typeof value === "string" && /^[a-z0-9][a-z0-9._:-]{0,127}$/i.test(value) ? value : null;

/**
 * Parse only the bounded typed reference emitted by the Operations contract.
 * Scalar IDs, URLs, paths, and arbitrary metadata are intentionally ignored.
 */
export function parseOperationsCanonicalSourceRef(
  value: unknown,
): OperationsCanonicalSourceRef | null {
  const row = record(value);
  const kind = row.kind;
  if (kind !== "conversation" && kind !== "agent" && kind !== "work") {
    return null;
  }
  const referenceId = id(row.id);
  return referenceId ? { kind, id: referenceId } : null;
}

/**
 * Resolve the canonical/source pair without allowing conflicting identities
 * to retarget a user across scopes. Older projections may expose either one.
 */
export function resolveOperationsCanonicalSourceRef(
  value: unknown,
): OperationsCanonicalSourceRef | null {
  const row = record(value);
  const canonical = parseOperationsCanonicalSourceRef(row.canonical_ref);
  const source = parseOperationsCanonicalSourceRef(row.source_ref);
  if (canonical && source && (canonical.kind !== source.kind || canonical.id !== source.id)) {
    return null;
  }
  return canonical ?? source;
}

export function workConversationReference(
  value: unknown,
): ConversationReference | null {
  const reference = resolveOperationsCanonicalSourceRef(value);
  return reference?.kind === "conversation"
    ? { kind: "conversation", id: reference.id }
    : null;
}

/**
 * Convert a loaded, ACL-filtered AgentRun into a Work navigation reference.
 * The run must be the exact run named by the assistant message and belong to
 * that message's session. This keeps old run-only metadata useful while
 * rejecting stale or cross-conversation records.
 */
export function workReferenceForConversationMessage(
  message: unknown,
  run: unknown,
): OperationsWorkReference | null {
  const messageRow = record(message);
  if (messageRow.role !== "assistant") return null;
  const metadata = record(messageRow.metadata);
  const messageRunId = id(metadata.agent_run_id);
  const runRow = record(run);
  const runId = id(runRow.id);
  const messageSessionId = id(messageRow.session_id);
  const runSessionId = id(runRow.session_id);
  const workId = id(runRow.work_item_id);
  if (
    !messageRunId ||
    !runId ||
    messageRunId !== runId ||
    !messageSessionId ||
    !runSessionId ||
    messageSessionId !== runSessionId ||
    !workId
  ) {
    return null;
  }
  const metadataAgentId = id(metadata.agent_id);
  const runAgentId = id(runRow.agent_id);
  if (metadataAgentId && metadataAgentId !== runAgentId) return null;
  return { kind: "work", id: workId };
}

export function operationsCards(payload: unknown): OperationsCard[] {
  const root = record(payload);
  const workCards = Array.isArray(root.current_work) ? root.current_work.slice(0, 500).flatMap(value => {
    const row = record(value); const workId = id(row.id);
    if (!workId) return [];
    const summary = record(row.execution_summary) as Partial<ExecutionSummary>;
    const state = text(row.state, "unknown");
    const explicit = Array.isArray(summary.attention_reasons) ? summary.attention_reasons.filter(reason => typeof reason === "string") : [];
    const attention = explicit.length ? explicit : ["blocked", "uncertain", "retry_wait", "dead_letter", "awaiting_approval", "failed"].includes(state) ? [state === "dead_letter" ? "retry_exhausted" : state] : [];
    return [{ id: workId, workId, sourceKind: "work", title: text(row.task_title, `${text(row.source_type, "Work")} · ${workId}`),
      state, agent: text(row.agent, "未割り当て"), project: text(row.project, "所属先未設定"),
      taskId: id(row.task_id), conversationRef: workConversationReference(row), summary: { ...summary, attempt_count: summary.attempt_count ?? (typeof row.attempt_count === "number" ? row.attempt_count : 0), max_attempts: summary.max_attempts ?? (typeof row.max_attempts === "number" ? row.max_attempts : undefined) }, attention }];
  }) : [];
  const knownWorkIds = new Set(workCards.map(item => item.id));
  const attentionCards = Array.isArray(root.attention) ? root.attention.slice(0, 500).flatMap(value => {
    const row = record(value);
    const sourceKind = text(row.kind, "attention");
    const state = text(row.state ?? row.status, "attention");
    if (!["proposed", "uncertain", "failed"].includes(state) && sourceKind !== "work") return [];
    const sourceId = id(row.id);
    if (!sourceId) return [];
    const workId = sourceKind === "work"
      ? sourceId
      : id(row.work_item_id ?? row.work_id);
    if (workId && knownWorkIds.has(workId)) return [];
    const explicit = Array.isArray(row.attention_reasons)
      ? row.attention_reasons.filter((reason): reason is string => typeof reason === "string")
      : [];
    const attention = explicit.length ? explicit : [state];
    const approval = record(row.approval_summary);
    const title = text(approval.operation_label ?? row.title ?? row.name, `${sourceKind === "approval" ? "外部操作" : "要対応"} · ${sourceId}`);
    return [{
      id: `${sourceKind}:${sourceId}`,
      workId,
      sourceKind,
      title,
      state,
      agent: text(approval.actor_name ?? row.agent, "担当名未取得"),
      project: text(row.project, "所属先未設定"),
      taskId: id(row.task_id),
      conversationRef: workConversationReference(row),
      summary: {},
      attention,
      approvalDetails: sourceKind === "approval" ? [
        `提案したAgent: ${text(approval.actor_name)} (${text(approval.actor)})`,
        `現在の仕事担当: ${text(approval.current_assignee_name)} (${text(approval.current_assignee)})`,
        `仕事: ${text(approval.work_name, "仕事名未取得")} (${text(approval.work_item_id)})`,
        `操作・対象: ${text(approval.operation_label, "詳細未取得")} → ${text(approval.target_kind)} ${text(approval.target_name, "対象名未取得")}`,
        `外部への影響: ${text(approval.consequence)}`,
        `判断すること: ${text(approval.decision, "承認内容の詳細未取得。正本で確認してください")}`,
      ] : undefined,
    }];
  }) : [];
  return [...workCards, ...attentionCards];
}

export function getOperationsOverview(server: string, scope: { projectId?: string | null; spaceId?: string | null }, signal: AbortSignal): Promise<unknown> {
  const query = new URLSearchParams({ limit: "100" });
  if (scope.projectId) query.set("project_id", scope.projectId);
  else if (scope.spaceId) query.set("space_id", scope.spaceId);
  return fetchApiAtServerFingerprint(
    server,
    `/api/operations/command-center?${query}`,
    { method: "GET", signal },
    OPERATIONS_READ_TIMEOUT_MS,
  );
}

export function getWorkExecution(server: string, workId: string, signal: AbortSignal, page?: "runs" | "events" | "tools", cursor?: string): Promise<WorkExecution> {
  const query = new URLSearchParams({ limit: "50" });
  if (page && cursor) query.set(`${page}_cursor`, cursor);
  return fetchApiAtServerFingerprint<ApiWorkExecution>(
    server,
    `/api/operations/work/${encodeURIComponent(workId)}/execution?${query}`,
    { method: "GET", signal },
    OPERATIONS_READ_TIMEOUT_MS,
  ).then((result) => ({
    ...result,
    runs: result.runs ?? [],
    events: result.events ?? [],
    tool_calls: result.tool_calls ?? [],
    work: {
      ...result.work,
      execution_summary: {
        ...result.work.execution_summary,
        attention_reasons: result.work.execution_summary.attention_reasons ?? [],
      },
    },
  }));
}

export const attentionLabel = (reason: string) => ({ blocked: "停止・要対応", uncertain: "結果不明・要照合", stale: "実行確認期限切れ", retry_wait: "再試行待ち", retry_exhausted: "再試行上限", verification_failed: "検証失敗", ready_for_review: "READY_FOR_REVIEW・最終レビュー待ち", awaiting_approval: "承認待ち", human_action_required: "人の対応待ち", failed: "失敗・要確認" }[reason] ?? reason);
export const activityTime = (value?: string | null) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString("ja-JP") : "未記録";
export function duration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "未記録";
  const value = Math.max(0,Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value/3600)}時間${Math.floor(value%3600/60)}分` : value >= 60 ? `${Math.floor(value/60)}分${value%60}秒` : `${value}秒`;
}


