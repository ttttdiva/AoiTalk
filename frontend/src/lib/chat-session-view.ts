import type {
  ChatHistoryStatus,
  ConversationSearchResult,
  ConversationSession,
} from "@/lib/chat-api";

export type ChatHistoryView = "timeline" | "project";
/** The popup intentionally groups idle sessions under the default "all" item. */
export type ChatHistoryStatusFilter = Exclude<ChatHistoryStatus, "idle"> | "all";

export const CHAT_HISTORY_STATUS_KEY = "aoitalk-chat-history-status";

export const CHAT_HISTORY_STATUS_OPTIONS: ReadonlyArray<{
  value: ChatHistoryStatusFilter;
  label: string;
}> = [
  { value: "all", label: "すべて" },
  { value: "ready", label: "準備完了" },
  { value: "needs_attention", label: "要確認" },
  { value: "working", label: "処理中" },
];

export function readChatHistoryStatusFilter(): ChatHistoryStatusFilter | null {
  if (typeof window === "undefined") return null;
  try {
    const saved = window.localStorage.getItem(CHAT_HISTORY_STATUS_KEY);
    return isChatHistoryStatusFilter(saved) ? saved : null;
  } catch {
    return null;
  }
}

export function persistChatHistoryStatusFilter(status: ChatHistoryStatusFilter) {
  try {
    window.localStorage.setItem(CHAT_HISTORY_STATUS_KEY, status);
  } catch {
    // localStorageが無効でも現在の選択はメモリ上で維持する。
  }
}

export function isChatHistoryStatusFilter(
  value: string | null | undefined,
): value is ChatHistoryStatusFilter {
  return value === "all" || value === "ready" ||
    value === "needs_attention" || value === "working";
}

/** Return the canonical server projection without deriving it from legacy fields. */
export function getChatSessionHistoryStatus(
  session: ConversationSession,
): ChatHistoryStatus | null {
  return session.history_status ?? null;
}

export function filterChatSessionsByStatus(
  sessions: readonly ConversationSession[],
  status: ChatHistoryStatusFilter,
): ConversationSession[] {
  if (status === "all") return [...sessions];
  return sessions.filter(
    (session) => getChatSessionHistoryStatus(session) === status,
  );
}

/** Search rows carry the same server-owned projection as history rows. */
export function filterChatSearchResults(
  results: readonly ConversationSearchResult[],
  status: ChatHistoryStatusFilter,
): ConversationSearchResult[] {
  if (status === "all") return [...results];
  return results.filter((result) => result.history_status === status);
}

export function sessionActivityTime(session: ConversationSession): number {
  return new Date(session.last_activity ?? session.session_start ?? 0).getTime();
}

/** サーバーが投影した履歴状態をサイドバーの進行中表示へ共有する判定。 */
export function isChatSessionWorking(session: ConversationSession): boolean {
  return getChatSessionHistoryStatus(session) === "working";
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

export function getAdjacentChatSessionId(
  sessions: ConversationSession[],
  currentId: string | null,
  direction: "up" | "down",
) {
  if (sessions.length === 0) return null;
  if (!currentId) return direction === "down" ? sessions[0].id : sessions.at(-1)?.id ?? null;
  const index = sessions.findIndex((session) => session.id === currentId);
  if (index < 0) return direction === "down" ? sessions[0].id : sessions.at(-1)?.id ?? null;
  const targetIndex = direction === "down" ? index + 1 : index - 1;
  return sessions[targetIndex]?.id ?? null;
}

export function flattenChatSessionGroups(
  groups: ChatSessionProjectGroup[],
) {
  return groups.flatMap((group) => group.sessions);
}
