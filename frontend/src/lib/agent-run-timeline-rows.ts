import type {
  AgentRun,
  AgentRunTimelineItem,
  AgentRunToolCall,
} from "@/lib/chat-api";

// ─── 表示対象の判定 ───
// Claude Code 風に、途中経過テキスト・思考・意味のある動作行だけを 1 列で並べる。
// run.* / stream.stream_start / stream.stream_end などのライフサイクル行は
// 表示せず、本文のある進捗更新は停止後の履歴にも残す。

export type TimelineRowKind =
  | "tool"
  | "agent"
  | "director"
  | "review"
  | "progress"
  | "text"
  | "thinking";

const INTERNAL_CONTROL_STATUS_RE = /^(?:cli_backend_started|cli_tool_required|cli_tool_results_received|(?:codex|claude|grok|antigravity)_(?:cli_)?(?:turn|session|run|message|item|agent_message|reasoning|tool)_(?:started|completed))$/i;

const INTERNAL_CONTROL_MESSAGE_PATTERNS = [
  /^cli generation started$/i,
  /\b(?:codex|claude|grok build|antigravity)(?:\s+cli)?\s+is\s+running\b/i,
  /\b(?:codex|claude|grok build|antigravity)(?:\s+cli)?\s+turn\s+(?:started|completed)\b/i,
  /tool results received;\s*continuing (?:the )?cli turn/i,
  /required tool check requested a cli follow-up/i,
];

function normalizedTimelineText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export type ToolCallControlParseResult = {
  displayText: string;
  auditArtifacts: string[];
  malformed: boolean;
};

/**
 * Transitional parser for `[TOOL_CALL: ...]`.  It balances nested arrays and
 * ignores brackets inside quoted strings, so JSON arguments cannot terminate
 * the control block early.  A malformed suffix is retained only as an audit
 * artifact and is never exposed in the normal timeline.
 */
export function stripToolCallControlNotation(
  value: string,
): ToolCallControlParseResult {
  const marker = /\[tool_call\s*:/gi;
  const displayParts: string[] = [];
  const auditArtifacts: string[] = [];
  let cursor = 0;
  let malformed = false;

  while (cursor < value.length) {
    marker.lastIndex = cursor;
    const match = marker.exec(value);
    if (!match) {
      displayParts.push(value.slice(cursor));
      break;
    }
    displayParts.push(value.slice(cursor, match.index));
    let squareDepth = 1;
    let quote: "\"" | "'" | null = null;
    let escaped = false;
    let end = -1;
    for (let index = marker.lastIndex; index < value.length; index += 1) {
      const character = value[index];
      if (quote) {
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === quote) quote = null;
        continue;
      }
      if (character === "\"" || character === "'") {
        quote = character;
      } else if (character === "[") {
        squareDepth += 1;
      } else if (character === "]") {
        squareDepth -= 1;
        if (squareDepth === 0) {
          end = index + 1;
          break;
        }
      }
    }
    if (end < 0) {
      malformed = true;
      auditArtifacts.push(value.slice(match.index));
      cursor = value.length;
      break;
    }
    auditArtifacts.push(value.slice(match.index, end));
    cursor = end;
  }

  return {
    displayText: displayParts.join("").replace(/\n{3,}/g, "\n\n").trim(),
    auditArtifacts,
    malformed,
  };
}

function isControlStatusEvent(eventType: string): boolean {
  return [
    "stream.status_update",
    "stream.reasoning_progress",
    "stream.steering_update",
  ].includes(eventType);
}

const PROGRESS_EVENT_TYPES = new Set([
  "stream.reasoning_progress",
  "stream.status_update",
  "stream.steering_update",
]);

function isActionRequiredItem(item: AgentRunTimelineItem): boolean {
  const status = String(item.status ?? item.display_status ?? "").toLowerCase();
  const message = String(item.message ?? "").toLowerCase();
  return Boolean(
    item.error ||
      item.success === false ||
      /(?:error|failed|failure|approval|permission|retry|retrying|needs[_\s-]*human|waiting[_\s-]*for[_\s-]*user)/i.test(
        `${status} ${message}`,
      ) ||
      /(?:承認|許可|再試行|確認が必要|人間の操作)/.test(
        `${status} ${message}`,
      ),
  );
}

/** CLIの内部制御イベント。監査ログには残すが通常表示には出さない。 */
export function isInternalControlTimelineItem(
  item: AgentRunTimelineItem,
): boolean {
  if (item.visibility === "audit") return true;
  if (item.visibility === "normal") return false;
  if (item.source !== "event") return false;
  const eventType = String(item.event_type ?? "").toLowerCase();
  const status = String(item.status ?? item.display_status ?? "").trim();
  const message = normalizedTimelineText(item.message);
  if (INTERNAL_CONTROL_STATUS_RE.test(status)) return true;
  if (
    isControlStatusEvent(eventType) &&
    INTERNAL_CONTROL_STATUS_RE.test(
      String(item.payload?.status ?? "").trim(),
    )
  ) {
    return true;
  }
  if (!isControlStatusEvent(eventType)) return false;
  return INTERNAL_CONTROL_MESSAGE_PATTERNS.some((pattern) =>
    pattern.test(message),
  );
}

/** reasoning本文ではなく、providerが明示した要約だけを通常表示する。 */
export function isReasoningSummaryRow(item: AgentRunTimelineItem): boolean {
  if (item.event_type !== "stream.thinking") return false;
  const kind = String(item.payload?.kind ?? "").toLowerCase();
  return (
    kind === "summary" ||
    kind === "reasoning_summary" ||
    item.payload?.is_summary === true ||
    item.payload?.reasoning_summary === true
  );
}

/** 通常表示用の本文。CLIの [TOOL_CALL: ...] 制御記法は監査側だけに残す。 */
export function timelineDisplayTextContent(
  item: AgentRunTimelineItem,
): string {
  const raw = timelineTextContent(item);
  if (!raw) return "";
  return stripToolCallControlNotation(raw).displayText;
}

/** 途中経過テキスト行（LLM の説明文） */
export function isAssistantTextRow(item: AgentRunTimelineItem): boolean {
  return (
    item.source === "event" &&
    item.event_type === "stream.assistant_text" &&
    !isInternalControlTimelineItem(item) &&
    Boolean(timelineDisplayTextContent(item))
  );
}

/** 思考行（thinking / reasoning summary） */
export function isThinkingRow(item: AgentRunTimelineItem): boolean {
  return (
    item.source === "event" &&
    isReasoningSummaryRow(item) &&
    Boolean(timelineDisplayTextContent(item))
  );
}

/** ツール実行行（検索・URL 取得・コマンド実行など） */
export function isToolRow(item: AgentRunTimelineItem): boolean {
  if (isAssistantTextRow(item) || isThinkingRow(item)) return false;
  return item.source === "tool_call" || item.event_type === "tool_operation";
}

/** サブエージェント行（agent_team.instance_started/succeeded/failed） */
export function isAgentTeamRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event") return false;
  if (isAssistantTextRow(item) || isThinkingRow(item)) return false;
  const eventType = item.event_type ?? "";
  return (
    eventType === "agent_operation" ||
    eventType.startsWith("agent_team.") ||
    item.actor_type === "agent_team"
  );
}

export type SubagentRunStatus =
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export type SubagentSummary = {
  key: string;
  name: string;
  action: string;
  status: SubagentRunStatus;
  provider: string | null;
  model: string | null;
  childRunId: string | null;
  startedAt: string | null;
  endedAt: string | null;
};

function normalizedStatus(item: AgentRunTimelineItem): SubagentRunStatus {
  const values = [
    item.status,
    item.display_status,
    item.event_type?.split(".").pop(),
  ]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase().replace(/[-\s]/g, "_"));
  if (values.some((value) => ["cancelled", "canceled", "stopped"].includes(value))) {
    return "cancelled";
  }
  if (item.success === false || Boolean(item.error)) return "failed";
  if (item.success === true) return "succeeded";

  for (const value of values) {
    if (["failed", "failure", "error", "errored"].includes(value)) {
      return "failed";
    }
    if (
      ["succeeded", "success", "completed", "complete", "done"].includes(
        value,
      )
    ) {
      return "succeeded";
    }
    if (
      ["running", "started", "pending", "queued", "in_progress"].includes(
        value,
      )
    ) {
      return "running";
    }
  }
  return item.ended_at ? "succeeded" : "running";
}

function subagentKey(item: AgentRunTimelineItem): string {
  const childRunId = resolveChildRunId(item);
  const operationId = lifecyclePayloadText(item, "operation_id");
  const candidates = [
    operationId ? `operation:${operationId}` : null,
    childRunId ? `child:${childRunId}` : null,
    item.event_type === "agent_operation" && item.id
      ? `item:${item.id}`
      : null,
    item.actor_key ? `actor:${item.actor_key}` : null,
    item.actor_label ? `label:${item.actor_label}` : null,
  ];
  const value = candidates.find(
    (candidate) => typeof candidate === "string" && candidate.trim(),
  );
  return value ? String(value).trim() : `item:${item.id}`;
}

function summaryFromItem(
  item: AgentRunTimelineItem,
  key: string,
): SubagentSummary {
  return {
    key,
    name:
      item.actor_label?.trim() ||
      item.actor_key?.trim() ||
      "サブエージェント",
    action: item.action?.trim() || item.message?.trim() || "作業を実行",
    status: normalizedStatus(item),
    provider: item.provider?.trim() || null,
    model: item.model?.trim() || null,
    childRunId: resolveChildRunId(item),
    startedAt: item.started_at ?? item.created_at ?? null,
    endedAt: item.ended_at ?? null,
  };
}

function mergeSummary(
  current: SubagentSummary,
  next: SubagentSummary,
): SubagentSummary {
  const currentTerminal = current.status !== "running";
  const nextTerminal = next.status !== "running";
  const preferred = nextTerminal || !currentTerminal ? next : current;
  return {
    ...preferred,
    key: current.key,
    name: next.name !== "サブエージェント" ? next.name : current.name,
    action: next.action !== "作業を実行" ? next.action : current.action,
    provider: next.provider ?? current.provider,
    model: next.model ?? current.model,
    childRunId: next.childRunId ?? current.childRunId,
    startedAt: current.startedAt ?? next.startedAt,
    endedAt: next.endedAt ?? current.endedAt,
  };
}

/**
 * Agent Teamの開始・完了イベントを1サブエージェント1行へ集約する。
 * 既存のagent_operation集約行がある場合は、agent_team.*のライフサイクル行を
 * 同じエージェントの重複カードとして扱わない。
 */
export function extractSubagentSummaries(
  items: AgentRunTimelineItem[],
): SubagentSummary[] {
  const aggregate = new Map<string, SubagentSummary>();
  const lifecycle = new Map<string, SubagentSummary>();
  const order = new Map<string, number>();

  collapseAgentLifecycleRows(items).forEach((item, index) => {
    if (!isAgentTeamRow(item)) return;
    const key = subagentKey(item);
    order.set(key, order.has(key) ? Math.min(order.get(key)!, index) : index);
    const summary = summaryFromItem(item, key);
    const target = item.event_type === "agent_operation" ? aggregate : lifecycle;
    const existing = target.get(key);
    target.set(key, existing ? mergeSummary(existing, summary) : summary);
  });

  const summaries = [
    ...aggregate.values(),
    ...[...lifecycle.entries()]
      .filter(([key]) => !aggregate.has(key))
      .map(([, summary]) => summary),
  ];
  return summaries.sort(
    (left, right) => (order.get(left.key) ?? 0) - (order.get(right.key) ?? 0),
  );
}

/** 検証ループ行（agentic_review / 進捗検証系の status_update） */
export function isReviewRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event") return false;
  const eventType = item.event_type ?? "";
  if (eventType === "stream.agentic_review") return true;
  if (eventType === "stream.status_update") {
    const status = String(item.payload?.status ?? "").toLowerCase();
    return status === "agentic_review" || status === "agentic_continue";
  }
  return false;
}

/** Web版ChatGPT Directorの往復・状態行 */
export function isDirectorRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event") return false;
  return (
    (item.event_type ?? "").startsWith("director.") ||
    item.actor_type === "director"
  );
}

/** 停止後にも残す、本文付きの推論・状態・ステアリング進捗 */
export function isProgressRow(item: AgentRunTimelineItem): boolean {
  if (item.source !== "event" || !timelineDisplayTextContent(item)) {
    return false;
  }
  if (isInternalControlTimelineItem(item) && !isActionRequiredItem(item)) {
    return false;
  }
  return PROGRESS_EVENT_TYPES.has(item.event_type ?? "");
}

/**
 * 稼働中に通常行へ変換できないイベントが続いている場合の安全な表示。
 * raw reasoning本文やCLI内部メッセージ自体は画面へ出さず、状態だけを示す。
 */
export function liveTimelineActivityLabel(
  items: AgentRunTimelineItem[],
): string | null {
  for (const item of [...items].reverse()) {
    if (item.source !== "event") continue;
    const eventType = item.event_type ?? "";
    if (eventType === "stream.thinking" && !isReasoningSummaryRow(item)) {
      return "思考中";
    }
    if (PROGRESS_EVENT_TYPES.has(eventType) && !isDisplayableTimelineItem(item)) {
      return eventType === "stream.steering_update" ? "追加指示を処理中" : "処理中";
    }
  }
  return null;
}

export function timelineRowKind(
  item: AgentRunTimelineItem,
): TimelineRowKind | null {
  // テキスト／思考は actor_type が agent_team でも本文行として扱うため先に判定する。
  if (isAssistantTextRow(item)) return "text";
  if (isThinkingRow(item)) return "thinking";
  if (isToolRow(item)) return "tool";
  if (isDirectorRow(item)) return "director";
  if (isAgentTeamRow(item)) return "agent";
  if (isReviewRow(item)) return "review";
  if (isProgressRow(item)) return "progress";
  return null;
}

export function isDisplayableTimelineItem(item: AgentRunTimelineItem): boolean {
  if (item.visibility === "audit") return false;
  return timelineRowKind(item) !== null;
}

function isAgentLifecycleRow(item: AgentRunTimelineItem): boolean {
  return (
    item.source === "event" &&
    /^agent_team\.instance_(?:started|succeeded|failed|cancelled|completed|stopped)$/i.test(
      String(item.event_type ?? ""),
    )
  );
}

function lifecyclePayloadText(
  item: AgentRunTimelineItem,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = item.payload?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function mergeAgentLifecycleRows(
  items: AgentRunTimelineItem[],
): AgentRunTimelineItem {
  const start = items.find((item) =>
    /\.instance_started$/i.test(String(item.event_type ?? "")),
  );
  const end = [...items]
    .reverse()
    .find((item) => !/\.instance_started$/i.test(String(item.event_type ?? "")));
  const base = end ?? start ?? items[0];
  const status = normalizedStatus(base);
  const startedAt = start?.started_at ?? start?.created_at ?? base.started_at;
  const endedAt = end?.ended_at ?? end?.created_at ?? base.ended_at;
  const startedMs = startedAt ? Date.parse(startedAt) : NaN;
  const endedMs = endedAt ? Date.parse(endedAt) : NaN;
  const durationMs =
    Number.isFinite(startedMs) && Number.isFinite(endedMs) && endedMs >= startedMs
      ? endedMs - startedMs
      : base.duration_ms;
  const childRunId = resolveChildRunId(end ?? start ?? base) ?? resolveChildRunId(base);
  const mergedPayload = items.reduce<Record<string, unknown>>(
    (payload, item) => ({ ...payload, ...(item.payload ?? {}) }),
    {},
  );
  const payloadResult = lifecyclePayloadText(
    end ?? base,
    "result",
    "result_preview",
  );
  const result =
    end?.result ??
    end?.result_preview ??
    base.result ??
    base.result_preview ??
    (payloadResult || null);
  const payloadError = lifecyclePayloadText(end ?? base, "error");
  const error = end?.error ?? base.error ?? (payloadError || null);
  return {
    ...base,
    id: `operation:agent:${start?.id ?? base.id}`,
    event_type: "agent_operation",
    status,
    display_status: status,
    action:
      lifecyclePayloadText(start ?? base, "task", "objective") ||
      end?.action ||
      start?.action ||
      base.action,
    message: end?.message ?? base.message ?? null,
    result,
    result_preview: end?.result_preview ?? base.result_preview ?? result,
    error,
    success:
      status === "succeeded" ? true : status === "failed" ? false : base.success,
    child_run_id: childRunId,
    payload: mergedPayload,
    started_at: startedAt ?? null,
    ended_at: endedAt ?? null,
    duration_ms: durationMs,
  };
}

/** backendが生のagent_team lifecycleを返す場合にも1作業1行へ正規化する。 */
export function collapseAgentLifecycleRows(
  items: AgentRunTimelineItem[],
): AgentRunTimelineItem[] {
  type Group = {
    items: AgentRunTimelineItem[];
    aggregate?: AgentRunTimelineItem;
    explicitKey?: string;
  };
  const explicitGroups = new Map<string, Group>();
  const openLegacyByActor = new Map<string, Group[]>();
  const output: Array<AgentRunTimelineItem | Group> = [];

  for (const item of items) {
    if (isAgentLifecycleRow(item) || item.event_type === "agent_operation") {
      const operationId = lifecyclePayloadText(item, "operation_id");
      const childRunId = resolveChildRunId(item);
      const explicitKey = operationId
        ? `operation:${operationId}`
        : childRunId
          ? `child:${childRunId}`
          : "";

      if (item.event_type === "agent_operation") {
        const key = explicitKey || `aggregate:${item.id}`;
        let group = explicitGroups.get(key);
        if (!group) {
          group = { items: [], explicitKey: key };
          explicitGroups.set(key, group);
          output.push(group);
        }
        group.aggregate = item;
        continue;
      }

      if (explicitKey) {
        let group = explicitGroups.get(explicitKey);
        if (!group) {
          group = { items: [], explicitKey };
          explicitGroups.set(explicitKey, group);
          output.push(group);
        }
        group.items.push(item);
        continue;
      }

      const actorKey =
        item.actor_key?.trim() || item.actor_label?.trim() || "legacy-agent";
      const queue = openLegacyByActor.get(actorKey) ?? [];
      if (/\.instance_started$/i.test(String(item.event_type ?? ""))) {
        const group: Group = { items: [item] };
        queue.push(group);
        openLegacyByActor.set(actorKey, queue);
        output.push(group);
      } else {
        const group = queue.shift() ?? { items: [] };
        group.items.push(item);
        if (!output.includes(group)) output.push(group);
        if (queue.length > 0) openLegacyByActor.set(actorKey, queue);
        else openLegacyByActor.delete(actorKey);
      }
      continue;
    }
    output.push(item);
  }

  return output.map((entry) => {
    if ("aggregate" in entry || "items" in entry) {
      return entry.aggregate ?? mergeAgentLifecycleRows(entry.items);
    }
    return entry;
  });
}

/** 「N件の操作」カウント対象（テキスト・思考は数えない） */
export function isOperationRow(item: AgentRunTimelineItem): boolean {
  const kind = timelineRowKind(item);
  return kind === "tool" || kind === "agent" || kind === "director" || kind === "review";
}

/** 途中経過テキスト／思考の本文を取り出す */
export function timelineTextContent(item: AgentRunTimelineItem): string {
  const candidates = [
    item.message,
    item.payload?.text,
    item.payload?.content,
    item.payload?.message,
    item.payload?.output,
    item.payload?.description,
    item.result,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.replace(/\s+$/, "");
    }
  }
  return "";
}

/** 思考行の見出し（要約かどうか） */
export function thinkingRowLabel(item: AgentRunTimelineItem): string {
  return isReasoningSummaryRow(item) ? "思考（要約）" : "思考";
}

/** 行の見出し文字列 */
export function timelineItemTitle(item: AgentRunTimelineItem): string {
  const displayMessage = timelineDisplayTextContent(item);
  if (item.event_type === "stream.assistant_text") {
    return item.action || "途中経過";
  }
  if (item.event_type === "stream.thinking") {
    return thinkingRowLabel(item);
  }
  const kind = timelineRowKind(item);
  if (kind === "text") return item.action || "途中経過";
  if (kind === "thinking") return thinkingRowLabel(item);
  if (kind === "review") return displayMessage || item.action || "結果を検証";
  if (kind === "progress") return displayMessage || item.action || "進捗を更新";
  if (kind === "director") return displayMessage || item.action || "Directorと通信";
  return item.action || item.actor_label || item.tool_name || "処理を実行";
}

/** サブエージェント行から子 run の id を解決する（payload の揺れに防御的に対応） */
export function resolveChildRunId(
  item: AgentRunTimelineItem,
): string | null {
  if (!isAgentTeamRow(item)) return null;
  const payload = item.payload ?? {};
  const candidates: unknown[] = [
    // バックエンドは集約項目のトップレベルにも child_run_id を載せる
    item.child_run_id,
    payload.child_run_id,
    payload.run_id,
    payload.agent_run_id,
  ];
  for (const value of candidates) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (!trimmed) continue;
    // 親 run 自身の id は子 run ではない
    if (trimmed === String(item.run_id ?? "")) continue;
    return trimmed;
  }
  return null;
}

/** タイムライン項目に対応する生の tool_call（結果が未クリップ）を探す */
export function findRawToolCall(
  run: AgentRun | null | undefined,
  item: AgentRunTimelineItem,
): AgentRunToolCall | null {
  const calls = run?.tool_calls ?? [];
  if (calls.length === 0) return null;
  const callId = String(item.tool_call_id ?? "").trim();
  if (callId) {
    const matched = calls.find(
      (call) => String(call.tool_call_id ?? "").trim() === callId,
    );
    if (matched) return matched;
  }
  const suffix = String(item.id ?? "").split(":").pop() ?? "";
  if (suffix) {
    const matched = calls.find((call) => call.id === suffix);
    if (matched) return matched;
  }
  return null;
}
