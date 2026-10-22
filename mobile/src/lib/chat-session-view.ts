import type { ConversationHistoryStatus, ConversationSession } from "../types/api";

export type ChatHistoryView = "timeline" | "project";
export type ChatHistoryFilter = "all" | "ready" | "needs_attention" | "working";

export const CHAT_HISTORY_FILTER_OPTIONS: ReadonlyArray<{
  value: ChatHistoryFilter;
  label: string;
  icon: string;
}> = [
  { value: "all", label: "すべて", icon: "message-text-outline" },
  { value: "ready", label: "準備完了", icon: "check-circle-outline" },
  { value: "needs_attention", label: "要確認", icon: "alert-circle-outline" },
  { value: "working", label: "処理中", icon: "progress-clock" },
];

const CHAT_HISTORY_STATUSES: ReadonlySet<ConversationHistoryStatus> = new Set([
  "idle",
  "ready",
  "needs_attention",
  "working",
]);

/** Validate the additive server projection before it reaches the UI. */
export function normalizeChatHistoryStatus(
  value: unknown,
): ConversationHistoryStatus | null {
  return typeof value === "string" && CHAT_HISTORY_STATUSES.has(value as ConversationHistoryStatus)
    ? (value as ConversationHistoryStatus)
    : null;
}

export function chatHistoryFilterLabel(filter: ChatHistoryFilter): string {
  return CHAT_HISTORY_FILTER_OPTIONS.find((option) => option.value === filter)?.label ?? "すべて";
}

/**
 * Filter only by the server-owned projection. Missing/idle rows remain in
 * "すべて" and are intentionally absent from the three actionable filters.
 */
export function filterChatSessions(
  sessions: readonly ConversationSession[],
  filter: ChatHistoryFilter,
): ConversationSession[] {
  if (filter === "all") return [...sessions];
  return sessions.filter((session) => session.history_status === filter);
}

export function filterChatSearchResults<T extends { history_status?: ConversationHistoryStatus | null }>(
  results: readonly T[],
  filter: ChatHistoryFilter,
): T[] {
  if (filter === "all") return [...results];
  return results.filter((result) => result.history_status === filter);
}

export function sessionActivityTime(session: ConversationSession): number {
  return new Date(session.last_activity ?? session.session_start ?? 0).getTime();
}

/** The spinner follows the server-owned projection, never development_status. */
export function isChatSessionWorking(session: ConversationSession): boolean {
  return session.history_status === "working";
}

/** 現在開いていない完了応答だけを未読マーカーの対象にする。 */
export function isChatSessionUnread(
  session: ConversationSession,
  activeSessionId?: string | null,
): boolean {
  return (
    session.is_unread === true &&
    activeSessionId !== session.id
  );
}

export function sortChatSessions(sessions: ConversationSession[]) {
  return [...sessions].sort(
    (a, b) =>
      Number(Boolean(b.is_pinned)) - Number(Boolean(a.is_pinned)) ||
      sessionActivityTime(b) - sessionActivityTime(a) ||
      new Date(b.session_start ?? 0).getTime() - new Date(a.session_start ?? 0).getTime() ||
      b.id.localeCompare(a.id),
  );
}

export type ChatSessionProjectGroup = {
  key: string;
  label: string;
  sessions: ConversationSession[];
  latestActivity: number;
};

export function groupChatSessionsByProject(
  sessions: ConversationSession[],
  projectNameById: Map<string, string>,
): ChatSessionProjectGroup[] {
  const sorted = sortChatSessions(sessions);
  const pinned = sorted.filter((session) => session.is_pinned);
  const groups = new Map<string, ChatSessionProjectGroup>();
  for (const session of sorted.filter((item) => !item.is_pinned)) {
    const key = !session.project_id
      ? "__none__"
      : projectNameById.has(session.project_id)
        ? session.project_id
        : "__unknown__";
    const label = !session.project_id
      ? "プロジェクトなし"
      : projectNameById.get(session.project_id) ?? "不明なプロジェクト";
    const group = groups.get(key) ?? {
      key,
      label,
      sessions: [],
      latestActivity: sessionActivityTime(session),
    };
    group.sessions.push(session);
    group.latestActivity = Math.max(group.latestActivity, sessionActivityTime(session));
    groups.set(key, group);
  }
  const projectGroups = [...groups.values()].sort(
    (a, b) => b.latestActivity - a.latestActivity || a.label.localeCompare(b.label, "ja"),
  );
  if (pinned.length === 0) return projectGroups;
  return [
    {
      key: "__pinned__",
      label: "ピン留め",
      sessions: pinned,
      latestActivity: Math.max(...pinned.map(sessionActivityTime)),
    },
    ...projectGroups,
  ];
}
