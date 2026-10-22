import type { components } from "@/lib/api-types.gen";

type ApiWorkExecution = components["schemas"]["OperationsWorkExecutionResponse"];
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
export type ExecutionRun = components["schemas"]["OperationsExecutionRun"];
export type ExecutionPageKind = "runs" | "events" | "tools";

/** Resolve Chat only from the permission-checked typed conversation source ref. */
export function workConversationSessionId(
  work: WorkExecution["work"],
): string | null {
  for (const reference of [work.canonical_ref, work.source_ref]) {
    if (reference?.kind !== "conversation") continue;
    const id = reference.id.trim();
    if (id) return id;
  }
  return null;
}

export async function getWorkExecution(
  workId: string,
  options: { signal?: AbortSignal; page?: ExecutionPageKind; cursor?: string } = {},
): Promise<WorkExecution> {
  const query = new URLSearchParams({ limit: "50" });
  if (options.page && options.cursor) query.set(`${options.page}_cursor`, options.cursor);
  const response = await fetch(`/api/python-proxy/operations/work/${encodeURIComponent(workId)}/execution?${query}`, {
    credentials: "include", cache: "no-store", signal: options.signal,
  });
  if (!response.ok) {
    throw new Error(response.status === 404 ? "この実行は存在しないか、閲覧できません" : "実行状況を読み込めませんでした");
  }
  const result = await response.json() as ApiWorkExecution;
  return {
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
  };
}

export const executionAttentionLabel = (reason: string) => ({
  blocked: "停止・要対応", uncertain: "結果不明・要照合", stale: "実行確認期限切れ",
  retry_wait: "再試行待ち", retry_exhausted: "再試行上限", verification_failed: "検証失敗",
  ready_for_review: "READY_FOR_REVIEW・最終レビュー待ち", awaiting_approval: "承認待ち",
  human_action_required: "人の対応待ち", failed: "失敗・要確認",
}[reason] ?? reason);

export function executionTime(value?: string | null): string {
  if (!value) return "未記録";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未記録" : date.toLocaleString("ja-JP");
}

export function executionDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "未記録";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}時間${Math.floor(value % 3600 / 60)}分`
    : value >= 60 ? `${Math.floor(value / 60)}分${value % 60}秒` : `${value}秒`;
}


