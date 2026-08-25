import type { ConversationSession } from "@/lib/chat-api";

export type ChatHistoryView = "timeline" | "project";

export function sessionActivityTime(session: ConversationSession): number {
  return new Date(session.last_activity ?? session.session_start ?? 0).getTime();
}

/** App開発チャットの進行中表示をサイドバーで共有する判定。 */
export function isChatSessionWorking(session: ConversationSession): boolean {
  return session.development_status === "working" && session.message_count > 0;
}

/** 現在開いていない完了応答だけを未読マーカーの対象にする。 */
export function isChatSessionUnread(
  session: ConversationSession,
  activeSessionId?: string | null,
): boolean {
  return (
    session.is_unread === true &&
    activeSessionId !== session.id &&
    !isChatSessionWorking(session)
  );
}

export function sortChatSessions(sessions: ConversationSession[]) {
  return [...sessions].sort(
    (a, b) =>
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
  const groups = new Map<string, ChatSessionProjectGroup>();
  for (const session of sortChatSessions(sessions)) {
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
  return [...groups.values()].sort(
    (a, b) => b.latestActivity - a.latestActivity || a.label.localeCompare(b.label, "ja"),
  );
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
