export const STORY_EPISODE_DND_MIME = "application/x-aoitalk-story-episode";

export type StoryDropMode = "before" | "after" | "blocked";

export type StoryDropRect = {
  top: number;
  height: number;
};

/** ClickUp 流の上下半分境界判定。分岐を跨ぐ可否は呼び出し側が判定する。 */
export function resolveStoryDropMode(clientY: number, rect: StoryDropRect, allowed = true): StoryDropMode {
  if (!allowed || rect.height <= 0) return "blocked";
  return clientY - rect.top < rect.height / 2 ? "before" : "after";
}

export function reorderStoryIds(
  ids: readonly string[],
  movingId: string,
  targetId: string,
  mode: Exclude<StoryDropMode, "blocked">,
): string[] | null {
  if (movingId === targetId || !ids.includes(movingId) || !ids.includes(targetId)) return null;
  const next = ids.filter((id) => id !== movingId);
  const targetIndex = next.indexOf(targetId);
  if (targetIndex < 0) return null;
  const insertionIndex = mode === "before" ? targetIndex : targetIndex + 1;
  next.splice(insertionIndex, 0, movingId);
  return next;
}

export function serializeStoryDrag(id: string): string {
  return JSON.stringify({ draggedId: id });
}

export function readStoryDrag(dataTransfer: Pick<DataTransfer, "getData">): string | null {
  const raw = dataTransfer.getData(STORY_EPISODE_DND_MIME) || dataTransfer.getData("text/plain");
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { draggedId?: unknown };
    return typeof parsed.draggedId === "string" ? parsed.draggedId : raw;
  } catch {
    return raw;
  }
}
