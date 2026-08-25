import type { TimelineItem } from "./models";

export function findAddedTimelineItemIds(
  previousIds: ReadonlySet<string> | null,
  timeline: readonly TimelineItem[],
): Set<string> {
  if (!previousIds) return new Set();
  return new Set(
    timeline
      .map((item) => item.id)
      .filter((id) => !previousIds.has(id)),
  );
}

export function timelineItemIds(
  timeline: readonly TimelineItem[],
): Set<string> {
  return new Set(timeline.map((item) => item.id));
}
