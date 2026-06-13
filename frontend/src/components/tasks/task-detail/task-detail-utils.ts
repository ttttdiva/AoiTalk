import type {
  LinkDisplayMode,
  LinkDisplayModeMap,
} from "@/components/editor/task-description-editor";
import { formatDateTimeLocal } from "@/components/tasks/task-form-utils";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
  type TaskOccurrence,
} from "@/lib/task-api";
import { isClosedTaskStatus } from "@/lib/task-status";
import { parseTaskTimerDate } from "@/lib/task-time";

export function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}時間${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

export function formatDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  return new Date(dateStr).toLocaleString("ja-JP");
}

export function occurrenceToContext(
  occurrence: TaskOccurrence,
): RecurringOccurrenceContext {
  return {
    occurrence_id:
      occurrence.id.startsWith("generated-") || occurrence.id.startsWith("base-")
        ? null
        : occurrence.id,
    start_at: occurrence.start_at ?? "",
    end_at: occurrence.end_at ?? null,
    original_start_at: occurrence.original_start_at ?? occurrence.start_at ?? null,
    source_kind: occurrence.source_kind ?? "task_schedule",
    status: occurrence.status ?? null,
  };
}

export async function fetchCurrentOccurrenceContext(
  task: Task,
): Promise<RecurringOccurrenceContext | null> {
  if (!task.has_recurrence || !task.project_id) return null;
  const rangeStart = new Date();
  rangeStart.setHours(0, 0, 0, 0);
  const rangeEnd = new Date(rangeStart);
  rangeEnd.setDate(rangeEnd.getDate() + 60);
  rangeEnd.setHours(23, 59, 59, 999);
  const occurrences = await taskApi.listOccurrences(
    { project_id: task.project_id },
    formatDateTimeLocal(rangeStart),
    formatDateTimeLocal(rangeEnd),
  );
  const current = occurrences
    .filter(
      (occurrence) =>
        occurrence.task_id === task.id &&
        !!occurrence.start_at &&
        !isClosedTaskStatus(occurrence.status),
    )
    .sort((a, b) => {
      const aTime = a.start_at ? new Date(a.start_at).getTime() : 0;
      const bTime = b.start_at ? new Date(b.start_at).getTime() : 0;
      return aTime - bTime;
    })[0];
  return current ? occurrenceToContext(current) : null;
}

export function formatBytes(value: number | null | undefined): string {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function shouldPrepareTaskForAgent(
  metadata: Record<string, unknown> | null | undefined,
): boolean {
  const status =
    metadata && typeof metadata.agent_triage_status === "string"
      ? metadata.agent_triage_status
      : "pending";
  return status === "pending" || status === "failed";
}

export const TASK_DESCRIPTION_LINK_DISPLAY_MODES_KEY =
  "description_link_display_modes";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function getTaskDescriptionLinkDisplayModes(
  metadata: Record<string, unknown> | null | undefined,
): LinkDisplayModeMap {
  const raw = metadata?.[TASK_DESCRIPTION_LINK_DISPLAY_MODES_KEY];
  if (!isRecord(raw)) return {};

  return Object.fromEntries(
    Object.entries(raw).filter(
      (entry): entry is [string, LinkDisplayMode] =>
        entry[1] === "embed" || entry[1] === "link",
    ),
  );
}

export function buildTaskDescriptionLinkDisplayModeMetadata({
  metadata,
  url,
  mode,
}: {
  metadata: Record<string, unknown> | null | undefined;
  url: string;
  mode: LinkDisplayMode;
}): Record<string, unknown> {
  const base = isRecord(metadata) ? { ...metadata } : {};
  return {
    ...base,
    [TASK_DESCRIPTION_LINK_DISPLAY_MODES_KEY]: {
      ...getTaskDescriptionLinkDisplayModes(base),
      [url]: mode,
    },
  };
}

export function formatTimeRange(
  startedAt: string | null | undefined,
  endedAt: string | null | undefined,
): string {
  if (!startedAt) return "-";
  const start = parseTaskTimerDate(startedAt);
  if (!start) return "-";
  const end = endedAt ? parseTaskTimerDate(endedAt) : null;
  const startText = start.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (!end) return `${startText} - 計測中`;
  return `${startText} - ${end.toLocaleTimeString("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function isEditableTarget(target: EventTarget | null): boolean {
  const element = target instanceof HTMLElement ? target : null;
  if (!element) return false;
  return (
    element.tagName === "INPUT" ||
    element.tagName === "TEXTAREA" ||
    element.tagName === "SELECT" ||
    element.isContentEditable
  );
}

export const STATUS_DOT_COLORS: Record<string, string> = {
  todo: "bg-gray-400",
  open: "bg-gray-400",
  in_progress: "bg-red-500",
  on_hold: "bg-pink-400",
  review: "bg-sky-400",
  closed: "bg-green-500",
};
