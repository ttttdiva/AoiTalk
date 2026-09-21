/** 検索結果のmessage IDではなく、会話IDを選択・削除の単位にする。 */
export function getVisibleSessionIds(
  items: ReadonlyArray<{ id: string; session_id?: string }>,
): string[] {
  return [...new Set(items.map((item) => item.session_id ?? item.id).filter(Boolean))];
}

export function toggleSessionSelection(selected: Set<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** 一覧の再読込で見えなくなった会話を、隠れた削除対象として残さない。 */
export function retainVisibleSelection(selected: Set<string>, visibleIds: readonly string[]): Set<string> {
  const visible = new Set(visibleIds);
  const next = new Set([...selected].filter((id) => visible.has(id)));
  return next.size === selected.size ? selected : next;
}

/**
 * repositoryの削除を順に実行する。SQLite writeを大量に並列化せず、
 * 1件の失敗で後続を打ち切らない。ローカル専用会話はオフラインでも削除可能。
 * サーバー会話はHTTP成功（再試行時の404を含む）とローカル保存の完了が必要。
 */
export async function deleteSelectedSessions(
  sessionIds: readonly string[],
  deleteSession: (id: string) => Promise<void>,
): Promise<{ deletedIds: string[]; failedIds: string[] }> {
  const deletedIds: string[] = [];
  const failedIds: string[] = [];
  for (const id of new Set(sessionIds)) {
    if (!id) continue;
    try {
      await deleteSession(id);
      deletedIds.push(id);
    } catch {
      failedIds.push(id);
    }
  }
  return { deletedIds, failedIds };
}
