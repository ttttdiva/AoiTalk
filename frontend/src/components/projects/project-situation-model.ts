// Read-only projection of the Project-scoped Operations contract. Do not infer
// runtime blockers from task status, old memories, or absent sources.
export type ProjectWork = {
  id: string;
  taskId: string | null;
  title: string;
  state: string;
  phase: string | null;
  assignee: string | null;
  reason: string | null;
  updatedAt: string | null;
  href: string;
};
export type ProjectApproval = {
  id: string;
  title: string;
  state: string;
  reason: string | null;
  assignee: string | null;
  workId: string | null;
  humanApproval: boolean;
  href: string;
};
export type ProjectOperations = {
  available: boolean;
  work: ProjectWork[];
  attention: ProjectWork[];
  approvals: ProjectApproval[];
  hasMore: boolean;
  limit: number;
  workPage: {
    coverage: "complete" | "partial" | "unknown";
    limit: number | null;
    fetched: number | null;
    available: number | null;
    hasMore: boolean | null;
    truncated: boolean | null;
    nextCursor: string | null;
  };
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function text(value: unknown, limit = 300): string | null {
  return typeof value === "string" && value.trim()
    ? value
        .replace(/[\u0000-\u001f\u007f]/g, " ")
        .trim()
        .slice(0, limit)
    : null;
}
function id(value: unknown): string | null {
  const valueText = text(value, 128);
  return valueText && /^[a-z0-9][a-z0-9._:-]{0,127}$/i.test(valueText)
    ? valueText
    : null;
}
function code(value: unknown): string | null {
  const valueText = text(value, 120);
  return valueText && /^[a-z0-9][a-z0-9_.:-]{0,119}$/i.test(valueText)
    ? valueText
    : null;
}
function workPage(
  value: unknown,
  received: number,
  accepted: number,
): ProjectOperations["workPage"] {
  const page = record(value);
  const count = (value: unknown) =>
    typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? value
      : null;
  const limit = count(page.limit);
  const fetched = count(page.fetched);
  const available = count(page.available);
  const hasMore = typeof page.has_more === "boolean" ? page.has_more : null;
  const truncated = typeof page.truncated === "boolean" ? page.truncated : null;
  const nextCursor =
    typeof page.next_cursor === "string" && page.next_cursor.trim()
      ? page.next_cursor
      : null;
  // Work and attention have independent pages. An empty attention page says
  // nothing about whether the task's Work is beyond the current Work page.
  const partial = hasMore === true || truncated === true || nextCursor !== null;
  const complete =
    limit !== null &&
    limit > 0 &&
    fetched !== null &&
    fetched <= limit &&
    fetched === received &&
    available === fetched &&
    accepted === received &&
    hasMore === false &&
    truncated === false &&
    page.next_cursor === null;
  return {
    coverage: partial ? "partial" : complete ? "complete" : "unknown",
    limit,
    fetched,
    available,
    hasMore,
    truncated,
    nextCursor,
  };
}
export function projectWorkStateLabel(state: string): string {
  return (
    (
      {
        pending: "待機中",
        claimed: "開始準備",
        running: "実行中",
        awaiting_approval: "承認待ち",
        blocked: "停止中",
        retry_wait: "自動再試行待ち",
        uncertain: "結果の照合が必要",
        succeeded: "完了",
        failed: "実行失敗",
        cancelled: "中止",
        dead_letter: "再試行上限",
      } as Record<string, string>
    )[state] ?? state
  );
}
function workProjection(raw: unknown, projectId: string): ProjectWork | null {
  const row = record(raw);
  const workId = id(row.id);
  if (!workId) return null;
  const execution = record(row.execution_summary);
  const reasons = [
    code(execution.blocker_code),
    code(execution.safe_error_code),
    code(row.error_code),
  ].filter((value): value is string => value !== null);
  const query = new URLSearchParams({
    tab: "work",
    scope: "project",
    project_id: projectId,
    work_id: workId,
  });
  return {
    id: workId,
    taskId: id(row.task_id),
    title: text(row.task_title) ?? `実行 ${workId.slice(0, 8)}`,
    state: text(row.state, 64) ?? "状態未取得",
    phase: text(execution.phase, 120),
    assignee: text(row.agent_name) ?? text(row.agent),
    reason: [...new Set(reasons)].join(" / ") || null,
    updatedAt: text(execution.last_activity_at, 80) ?? text(row.updated_at, 80),
    href: `/operations?${query}`,
  };
}

export function normalizeProjectOperations(
  body: unknown,
  projectId: string,
): ProjectOperations {
  const root = record(body);
  if (
    root.schema_version !== "operations-command-center-v1" ||
    !Array.isArray(root.current_work) ||
    !Array.isArray(root.attention)
  ) {
    throw new Error("実行状況の形式を確認できませんでした");
  }
  const filters = record(root.filters);
  const page = record(root.attention_page);
  // The server uses this shape for disabled capabilities and inaccessible
  // scope. It is not evidence that there are zero blockers or approvals.
  if (
    filters.project_id !== projectId ||
    !(typeof page.limit === "number" && page.limit > 0)
  ) {
    return {
      available: false,
      work: [],
      attention: [],
      approvals: [],
      hasMore: false,
      limit: 0,
      workPage: workPage(null, 0, 0),
    };
  }
  const work = root.current_work
    .filter((item) => record(item).project_id === projectId)
    .map((item) => workProjection(item, projectId))
    .filter((item): item is ProjectWork => item !== null);
  const byId = new Map(work.map((item) => [item.id, item]));
  const attention: ProjectWork[] = [];
  const approvals: ProjectApproval[] = [];
  for (const raw of root.attention) {
    const row = record(raw);
    if (typeof row.project_id === "string" && row.project_id !== projectId)
      continue;
    if (row.kind === "work") {
      const projected = workProjection(row, projectId);
      if (!projected) continue;
      const current = byId.get(projected.id);
      attention.push(
        current
          ? {
              ...current,
              state: projected.state,
              reason: projected.reason ?? current.reason,
              updatedAt: projected.updatedAt ?? current.updatedAt,
            }
          : projected,
      );
    } else if (row.kind === "approval" && row.project_id === projectId) {
      const actionId = id(row.id);
      if (!actionId) continue;
      const summary = record(row.approval_summary);
      const target = text(summary.target_name);
      const operation = text(summary.operation_label);
      const query = new URLSearchParams({
        tab: "actions",
        action: actionId,
        project_id: projectId,
      });
      approvals.push({
        id: actionId,
        title:
          [operation, target].filter(Boolean).join(" · ") ||
          `外部操作 ${actionId.slice(0, 8)}`,
        state: text(row.status, 64) ?? "状態未取得",
        reason: text(summary.decision, 600) ?? text(summary.consequence, 600),
        assignee: text(summary.current_assignee_name),
        workId: id(summary.work_item_id),
        humanApproval:
          row.status === "proposed" &&
          summary.authorization_mode === "human_approval",
        href: `/operations?${query}`,
      });
    }
  }
  const approvalWorkIds = new Set(
    approvals
      .filter((item) => item.humanApproval && item.workId)
      .map((item) => item.workId),
  );
  return {
    available: true,
    work,
    // Only remove a duplicate display when explicit provenance proves that
    // the same awaiting work is represented by a human-approval action.
    attention: attention.filter(
      (item) =>
        item.state !== "awaiting_approval" || !approvalWorkIds.has(item.id),
    ),
    approvals,
    hasMore: page.has_more === true || root.attention_has_more === true,
    limit: Math.min(500, page.limit),
    workPage: workPage(root.work_page, root.current_work.length, work.length),
  };
}
