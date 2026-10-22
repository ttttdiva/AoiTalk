import { z } from "zod";

const identity = z.string().min(1).max(256);
const optionalText = z.string().nullable().optional();
const kind = z.enum(["task", "opportunity", "content_item"]);
const decimal = z
  .string()
  .regex(/^\d+(?:\.\d+)?$/)
  .max(120);
const reference = z.object({ kind, id: identity });
const attempt = z.object({
  id: identity,
  parent_work_item_id: identity.nullable(),
  state: z.string(),
  current_run_id: identity.nullable(),
  current_run_status: z.string(),
  linkage: z.enum(["recorded", "missing", "partial"]),
  assigned_agent_id: optionalText,
  agent_revision_id: optionalText,
  assigned_agent_display_name: optionalText,
  deadline: optionalText,
  not_before: optionalText,
  started_at: optionalText,
  completed_at: optionalText,
  cancelled_at: optionalText,
  heartbeat_at: optionalText,
  lease_expires_at: optionalText,
  current_phase: optionalText,
  current_phase_at: optionalText,
});
const run = z.object({
  id: identity,
  work_item_id: identity.nullable(),
  parent_run_id: identity.nullable(),
  status: z.string(),
  created_at: z.string().nullable(),
  agent_id: optionalText,
  agent_revision_id: optionalText,
  agent_display_name: optionalText,
  acting_subagent_id: optionalText,
  work_item_attempt: z.number().int().nullable().optional(),
  started_at: optionalText,
  ended_at: optionalText,
  last_event_at: optionalText,
  last_recorded_phase: optionalText,
  last_recorded_phase_at: optionalText,
});
const money = z
  .object({
    category: z.enum(["actual", "estimate", "reservation", "unmeasured"]),
    amount: decimal.nullable(),
    currency: z.string().min(1).max(12).nullable(),
    source: z.string(),
    usage_id: identity.nullable().optional(),
    run_id: identity.nullable().optional(),
  })
  .refine(
    (value) =>
      value.category === "unmeasured"
        ? value.amount === null
        : value.amount !== null && value.currency !== null,
    "Invalid money evidence",
  );
const item = z.object({
  ref: reference,
  project_id: identity.nullable(),
  space_id: identity.nullable(),
  title: z.string().nullable(),
  business_status: z.string(),
  created_at: z.string().nullable(),
  attempts: z.array(attempt),
  runs: z.array(run),
  artifacts: z.array(
    z.object({
      kind: z.enum(["artifact_version", "content_variant_revision"]),
      id: identity,
      availability: z.enum(["available", "unavailable", "unknown"]),
      content_href: z.string().nullable(),
    }),
  ),
  actions: z.array(
    z.object({ id: identity, status: z.string(), version: z.number().int() }),
  ),
  decisions: z.array(
    z.object({
      kind: z.enum(["external_action", "deliverable_review"]),
      id: identity,
      version: z.number().int().nullable(),
    }),
  ),
  costs: z.array(money),
  gaps: z.array(z.string()),
  execution_availability: z.literal("not_advertised"),
  // Optional read-only metadata is not inferred from Space, title or other jobs.
  department: optionalText,
  task_due_at: optionalText,
  content_scheduled_at: optionalText,
  last_recorded_phase: optionalText,
  last_recorded_phase_at: optionalText,
  last_recorded_phase_run_id: optionalText,
});
const projection = z.object({
  schema_version: z.literal("work-read-projection-v1"),
  observed_at: z.string().min(1),
  items: z.array(item),
  page: z.object({
    limit: z.number().int().min(1).max(100),
    fetched: z.number().int().nonnegative(),
    available: z.number().int().nonnegative(),
    has_more: z.boolean(),
    next_cursor: z.string().min(1).nullable(),
  }),
});
export type CanonicalWork = z.infer<typeof item>;
export type WorkProjection = z.infer<typeof projection>;
export type WorkKind = CanonicalWork["ref"]["kind"];
export type WorkScope = {
  projectId?: string;
  spaceId?: string;
  kind?: WorkKind;
  limit?: number;
};
export const workKey = (work: Pick<CanonicalWork, "ref">) =>
  `${work.ref.kind}:${work.ref.id}`;

export function parseWorkProjection(
  input: unknown,
  scope: WorkScope = {},
): WorkProjection {
  const result = projection.parse(input);
  const keys = new Set<string>();
  for (const work of result.items) {
    const key = workKey(work);
    if (keys.has(key)) throw new Error("仕事の重複した参照が返されました。");
    keys.add(key);
    if (
      (scope.projectId && work.project_id !== scope.projectId) ||
      (scope.spaceId && work.space_id !== scope.spaceId) ||
      (scope.kind && work.ref.kind !== scope.kind)
    )
      throw new Error("表示範囲と異なる仕事が返されました。");
    const attempts = new Set(work.attempts.map((value) => value.id));
    const runs = new Map(work.runs.map((value) => [value.id, value]));
    if (
      attempts.size !== work.attempts.length ||
      runs.size !== work.runs.length
    )
      throw new Error("実行の参照が重複しています。");
    for (const value of work.attempts) {
      const current = value.current_run_id
        ? runs.get(value.current_run_id)
        : null;
      if (
        (value.parent_work_item_id &&
          !attempts.has(value.parent_work_item_id)) ||
        (value.current_run_id &&
          (!current || current.work_item_id !== value.id))
      )
        throw new Error("この仕事の実行のつながりを確認できません。");
    }
    for (const value of work.runs) {
      if (
        (value.work_item_id && !attempts.has(value.work_item_id)) ||
        (value.parent_run_id && !runs.has(value.parent_run_id))
      )
        throw new Error("別の仕事の実行は表示できません。");
    }
    if (
      work.last_recorded_phase_run_id &&
      !runs.has(work.last_recorded_phase_run_id)
    )
      throw new Error("工程の実行参照を確認できません。");
    if (work.costs.some((value) => value.run_id && !runs.has(value.run_id)))
      throw new Error("費用の実行参照を確認できません。");
  }
  if (
    result.page.fetched !== result.items.length ||
    result.page.available < result.items.length ||
    (result.page.has_more && !result.page.next_cursor) ||
    (!result.page.has_more && result.page.next_cursor)
  ) {
    throw new Error("追加取得の情報を確認できません。");
  }
  return result;
}

export function scopeFromQuery(query: Pick<URLSearchParams, "get">): WorkScope {
  const requested = query.get("kind");
  return {
    projectId: query.get("project_id")?.trim() || undefined,
    spaceId: query.get("space_id")?.trim() || undefined,
    kind: kind.safeParse(requested).success
      ? (requested as WorkKind)
      : undefined,
    limit: 50,
  };
}
export function projectionUrl(scope: WorkScope, cursor?: string): string {
  const query = new URLSearchParams();
  if (scope.projectId) query.set("project_id", scope.projectId);
  if (scope.spaceId) query.set("space_id", scope.spaceId);
  if (scope.kind) query.set("kind", scope.kind);
  query.set("limit", String(scope.limit ?? 50));
  if (cursor) query.set("cursor", cursor);
  return `/api/python-proxy/operations/work-projection?${query}`;
}
export class WorkProjectionError extends Error {
  constructor(public readonly status: number) {
    super(
      status === 401 || status === 403
        ? "この範囲を表示する権限を確認できません。"
        : "仕事の情報を取得できませんでした。",
    );
  }
}
export async function fetchWorkProjection(
  scope: WorkScope,
  signal: AbortSignal,
  cursor?: string,
) {
  const response = await fetch(projectionUrl(scope, cursor), {
    credentials: "include",
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new WorkProjectionError(response.status);
  return parseWorkProjection(await response.json(), scope);
}

/** Exact base-10 addition. No binary floating-point or missing-value coercion. */
export function addDecimal(left: string, right: string): string {
  const [li, lf = ""] = left.split("."),
    [ri, rf = ""] = right.split(".");
  const places = Math.max(lf.length, rf.length);
  const total =
    BigInt(li + lf.padEnd(places, "0")) + BigInt(ri + rf.padEnd(places, "0"));
  const text = total.toString().padStart(places + 1, "0");
  return places
    ? `${text.slice(0, -places)}.${text.slice(-places)}`.replace(/\.?0+$/, "")
    : text;
}
export function costSummary(works: CanonicalWork[]) {
  const buckets = {
    actual: new Map<string, string>(),
    estimate: new Map<string, string>(),
    reservation: new Map<string, string>(),
  };
  let unknown = 0;
  for (const work of works) {
    if (
      !work.costs.some((value) => value.category === "actual") ||
      work.costs.some((value) => value.category === "unmeasured") ||
      work.gaps.some((value) => /cost|usage/.test(value))
    )
      unknown += 1;
    for (const value of work.costs) {
      if (
        value.category === "unmeasured" ||
        value.amount === null ||
        value.currency === null
      )
        continue;
      const bucket = buckets[value.category];
      bucket.set(
        value.currency,
        addDecimal(bucket.get(value.currency) ?? "0", value.amount),
      );
    }
  }
  return { ...buckets, unknown };
}
export function formatMoney(values: Map<string, string>) {
  return (
    [...values]
      .map(([currency, amount]) => `${amount} ${currency}`)
      .join(" / ") || "未計測"
  );
}
export function sourceHref(work: Pick<CanonicalWork, "ref">) {
  const id = encodeURIComponent(work.ref.id);
  if (work.ref.kind === "task") return `/tasks?detail=${id}`;
  if (work.ref.kind === "opportunity")
    return `/operations?tab=opportunities&opportunity_id=${id}`;
  return `/operations?tab=pipeline&content_item_id=${id}`;
}
export function usageDetailHref(cost: { usage_id?: string | null; run_id?: string | null }) {
  if (!cost.usage_id || !cost.run_id) return null;
  const query = new URLSearchParams({ usage_id: cost.usage_id, run_id: cost.run_id });
  return `/settings?${query.toString()}#cost-dashboard`;
}
export function decisionHref(
  work: CanonicalWork,
  decision: CanonicalWork["decisions"][number],
) {
  return decision.kind === "external_action"
    ? `/operations?tab=actions&action=${encodeURIComponent(decision.id)}`
    : sourceHref(work);
}
export function mergeProjection(
  previous: WorkProjection,
  next: WorkProjection,
): WorkProjection {
  const items = new Map(previous.items.map((value) => [workKey(value), value]));
  next.items.forEach((value) => items.set(workKey(value), value));
  return { ...next, items: [...items.values()] };
}
export const hasTechnicalBlocker = (work: CanonicalWork) =>
  work.attempts.some((value) =>
    ["blocked", "failed", "dead_letter"].includes(value.state),
  );
export const hasRunningRecord = (work: CanonicalWork) =>
  work.attempts.some((value) => value.current_run_status === "running");
export const kindLabel = (value: WorkKind) =>
  ({ task: "Task", opportunity: "Opportunity", content_item: "Content" })[
    value
  ];
export const statusLabel = (value: string) =>
  (
    ({
      running: "実行中の記録",
      claimed: "担当確保",
      queued: "待機中",
      succeeded: "完了",
      completed: "完了",
      failed: "失敗",
      blocked: "技術対応",
      awaiting_approval: "本人判断",
      pending: "待機",
      draft: "下書き",
      unknown: "未確認",
      cancelled: "中止",
      retry_wait: "再試行待ち",
      dead_letter: "要調査",
    }) as Record<string, string>
  )[value] || value;
