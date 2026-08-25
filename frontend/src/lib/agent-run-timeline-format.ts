import type {
  AgentRun,
  AgentRunTimelineItem,
  AgentRunUsage,
} from "@/lib/chat-api";

/** 秒数を「12秒」「1分05秒」形式へ */
export function formatSeconds(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  if (safe < 60) return `${safe}秒`;
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}分${seconds.toString().padStart(2, "0")}秒`;
}

export function formatDuration(durationMs?: number | null): string {
  if (typeof durationMs !== "number" || durationMs < 0) return "";
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 10_000) return `${(durationMs / 1000).toFixed(1)}秒`;
  return formatSeconds(durationMs / 1000);
}

/** timezone指定のないバックエンドISO日時はUTCとして解釈する。 */
export function parseAgentRunTimestamp(value?: string | null): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const normalized =
    trimmed.includes("T") && !/[zZ]$|[+-]\d{2}:?\d{2}$/.test(trimmed)
      ? `${trimmed}Z`
      : trimmed;
  const milliseconds = new Date(normalized).getTime();
  return Number.isNaN(milliseconds) ? null : milliseconds;
}

/** ISO 文字列をローカル時刻の短い表記へ（不正値は空文字） */
export function formatTimestamp(value?: string | null): string {
  const milliseconds = parseAgentRunTimestamp(value);
  if (milliseconds === null) return "";
  const date = new Date(milliseconds);
  return date.toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** run 全体の所要時間（ms）。終了していない場合は null */
export function agentRunDurationMs(run?: AgentRun | null): number | null {
  if (!run?.started_at || !run?.ended_at) return null;
  const started = parseAgentRunTimestamp(run.started_at);
  const ended = parseAgentRunTimestamp(run.ended_at);
  if (started === null || ended === null || ended < started) {
    return null;
  }
  return ended - started;
}

/** 実行中は現在時刻、終了後は確定時刻を使った所要時間。 */
export function agentRunElapsedMs(
  run: AgentRun | null | undefined,
  items: AgentRunTimelineItem[] = [],
  nowMs = Date.now(),
  clientStartMs = nowMs,
  live = false,
): number | null {
  const started = parseAgentRunTimestamp(run?.started_at);
  const firstItemTime = items
    .flatMap((item) => [item.created_at, item.started_at, item.ended_at])
    .map(parseAgentRunTimestamp)
    .find((value): value is number => value !== null);
  const base = started ?? firstItemTime ?? (live ? clientStartMs : null);
  if (base === null) return null;

  const ended = live
    ? null
    : (parseAgentRunTimestamp(run?.ended_at) ??
      parseAgentRunTimestamp(run?.last_event_at));
  return Math.max(0, (ended ?? nowMs) - base);
}

function tokenValue(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return null;
  return Math.floor(numeric);
}

/** APIのusage形状を安全に共通形式へ変換する。 */
export function normalizeAgentRunUsage(value: unknown): AgentRunUsage | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const usage: AgentRunUsage = {};
  for (const key of [
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "total_tokens",
  ] as const) {
    const parsed = tokenValue(raw[key]);
    if (parsed !== null) usage[key] = parsed;
  }
  if (Object.keys(usage).length === 0) return null;
  usage.total_tokens ??= (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
  return usage;
}

function addAgentRunUsage(
  current: AgentRunUsage,
  incoming: AgentRunUsage,
): AgentRunUsage {
  return {
    input_tokens: (current.input_tokens ?? 0) + (incoming.input_tokens ?? 0),
    output_tokens: (current.output_tokens ?? 0) + (incoming.output_tokens ?? 0),
    cached_tokens: (current.cached_tokens ?? 0) + (incoming.cached_tokens ?? 0),
    total_tokens: (current.total_tokens ?? 0) + (incoming.total_tokens ?? 0),
  };
}

/** run metadata/resultまたはtimelineイベントからAgentRun単位のusageを返す。 */
export function agentRunUsage(
  run: AgentRun | null | undefined,
  items: AgentRunTimelineItem[] = run?.timeline ?? [],
): AgentRunUsage | null {
  const directCandidates: unknown[] = [
    run?.usage,
    run?.metadata?.usage,
    run?.metadata?.agent_run_usage,
    run?.result?.usage,
    run?.result?.agent_run_usage,
  ];
  for (const candidate of directCandidates) {
    const normalized = normalizeAgentRunUsage(candidate);
    if (normalized) return normalized;
  }

  let total: AgentRunUsage | null = null;
  const seenUsageKeys = new Set<string>();
  for (const item of items) {
    const payload = item.payload;
    const normalized = normalizeAgentRunUsage(
      payload?.usage ?? payload?.agent_run_usage,
    );
    if (!normalized) continue;
    const rawKey = payload?.usage_key;
    const usageKey =
      typeof rawKey === "string" && rawKey.trim() ? rawKey : item.id;
    if (seenUsageKeys.has(usageKey)) continue;
    seenUsageKeys.add(usageKey);
    total = total ? addAgentRunUsage(total, normalized) : normalized;
  }
  return total;
}

export function formatAgentRunTokens(usage: AgentRunUsage | null): string {
  if (!usage) return "";
  const total =
    usage.total_tokens ??
    (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
  return `${total.toLocaleString("en-US")} tokens`;
}

export function agentRunStatusLabel(status?: string | null): string {
  switch (String(status ?? "")) {
    case "succeeded":
      return "成功";
    case "failed":
      return "失敗";
    case "cancelled":
      return "キャンセル";
    case "running":
      return "実行中";
    case "queued":
      return "待機中";
    case "pending":
      return "準備中";
    default:
      return String(status ?? "不明");
  }
}

export function payloadValue(
  item: AgentRunTimelineItem,
  ...keys: string[]
): unknown {
  for (const key of keys) {
    const value = item.payload?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

export function displayModel(model?: string | null): string {
  const value = String(model ?? "").trim();
  if (!value || value.toLowerCase() === "default") return "";
  return value;
}

// ─── ツール引数の 1 行要約 ───

const ARGUMENT_PRIORITY_KEYS = [
  "query",
  "q",
  "search",
  "url",
  "uri",
  "command",
  "cmd",
  "path",
  "file_path",
  "pattern",
  "input",
  "text",
  "prompt",
  "name",
];

export function toolArgumentSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  for (const key of ARGUMENT_PRIORITY_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  for (const value of Object.values(args)) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function operationMeta(item: AgentRunTimelineItem): string {
  const groupId = String(
    item.group_id ?? payloadValue(item, "group_id") ?? "",
  ).trim();
  const groupLabel =
    groupId === "heavy" ? "高負荷" : groupId === "light" ? "軽量" : groupId;
  const provider = String(
    item.provider ?? payloadValue(item, "provider") ?? "",
  ).trim();
  const providerModel = [provider, displayModel(item.model)]
    .filter(Boolean)
    .join("/");
  const mode = String(item.mode ?? "").trim();
  const pool = String(
    item.pool ?? payloadValue(item, "pool", "pool_id") ?? "",
  ).trim();
  const candidate = String(
    item.candidate ?? payloadValue(item, "candidate", "candidate_id") ?? "",
  ).trim();
  const credential = String(
    item.credential_profile ??
      payloadValue(item, "credential_profile", "credential_profile_id") ??
      "",
  ).trim();
  const fallbackCount = Number(
    item.fallback_count ?? payloadValue(item, "fallback_count") ?? 0,
  );
  return [
    formatDuration(item.duration_ms),
    groupLabel,
    pool ? `pool:${pool}` : "",
    providerModel,
    mode,
    candidate ? `candidate:${candidate}` : "",
    credential ? `credential:${credential}` : "",
    fallbackCount > 0 ? `fallback:${fallbackCount}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

export function operationCommand(item: AgentRunTimelineItem): string {
  const command = item.arguments?.command ?? item.arguments?.cmd;
  return typeof command === "string" ? command.trim() : "";
}

export function operationPaths(item: AgentRunTimelineItem): string[] {
  const values: string[] = [];
  for (const value of [item.arguments?.file_path, item.arguments?.path]) {
    if (typeof value === "string" && value.trim()) values.push(value.trim());
  }
  for (const key of ["paths", "files"] as const) {
    const paths = item.arguments?.[key];
    if (Array.isArray(paths)) {
      for (const path of paths) {
        if (typeof path === "string" && path.trim()) values.push(path.trim());
      }
    }
  }
  const changes = item.arguments?.changes;
  if (Array.isArray(changes)) {
    for (const change of changes) {
      if (change && typeof change === "object") {
        const path = (change as { path?: unknown }).path;
        if (typeof path === "string" && path.trim()) values.push(path.trim());
      }
    }
  }
  return [...new Set(values)];
}

export function operationPath(item: AgentRunTimelineItem): string {
  return operationPaths(item)[0] ?? "";
}

export function isFileEdit(item: AgentRunTimelineItem): boolean {
  const toolName = String(item.tool_name ?? "").toLowerCase();
  return ["write_file", "edit_file", "apply_patch"].includes(toolName);
}

const SIMPLE_TOOL_NAMES = new Set([
  "get_current_time",
  "get_weather",
  "calculate",
]);

export function hasMeaningfulDetails(item: AgentRunTimelineItem): boolean {
  if (item.error) return true;
  if (operationCommand(item) || operationPath(item)) return true;
  if (Object.keys(item.arguments ?? {}).length > 0) {
    if (!SIMPLE_TOOL_NAMES.has(String(item.tool_name ?? ""))) return true;
  }
  if (item.event_type === "agent_operation" && item.result) return true;
  if (String(item.result ?? "").includes("\n")) return true;
  return ["exit_code", "exit", "stdout", "stderr", "diff", "patch"].some(
    (key) => payloadValue(item, key) !== undefined,
  );
}

export function toolRowSummary(item: AgentRunTimelineItem): string {
  const input = toolArgumentSummary(item.arguments);
  const result = String(item.result_preview ?? item.result ?? "").trim();
  if (SIMPLE_TOOL_NAMES.has(String(item.tool_name ?? "")) && result) {
    return input ? `${input} → ${result}` : result;
  }
  return input || result;
}

export function operationStatusLabel(item: AgentRunTimelineItem): string {
  if (item.display_status === "started" || item.status === "running")
    return "実行中";
  if (item.status === "cancelled") return "キャンセル";
  if (item.success === false || item.status === "failed" || item.error)
    return "失敗";
  if (item.success === true || item.status === "succeeded") return "成功";
  return "記録済み";
}

/** 参照URL（payload.urls）を http(s) のみに絞る */
export function operationUrls(item: AgentRunTimelineItem): string[] {
  const urls = item.payload?.urls;
  if (!Array.isArray(urls)) return [];
  return urls.filter(
    (url): url is string =>
      typeof url === "string" && /^https?:\/\//i.test(url),
  );
}
