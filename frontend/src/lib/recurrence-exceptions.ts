import { formatLocalDateTime } from "@/lib/date-time";
import { parseInputDate } from "@/lib/server/db-time";

const RECURRENCE_SKIP_SOURCE_KIND = "recurrence_skip";
const RECURRENCE_OVERRIDE_PREFIX = "ro:";
const LEGACY_RECURRENCE_OVERRIDE_PREFIX = "recurrence_override:";

function pad(value: number, length = 2): string {
  return String(value).padStart(length, "0");
}

function compactOriginalStartAt(originalStartAt: string): string {
  let date: Date;
  try {
    date = parseInputDate(originalStartAt);
  } catch {
    return originalStartAt.replace(/[^0-9A-Za-z]/g, "").slice(0, 29);
  }
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    "T",
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
    pad(date.getMilliseconds(), 3),
  ].join("");
}

function parseCompactOriginalStartAt(value: string): string | null {
  const match = value.match(
    /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{3})Z?$/,
  );
  if (!match) return null;
  const [, year, month, day, hour, minute, second, millisecond] = match;
  return formatLocalDateTime(
    new Date(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second),
      Number(millisecond),
    ),
  );
}

export function buildRecurrenceSkipSourceKind(): string {
  return RECURRENCE_SKIP_SOURCE_KIND;
}

export function buildRecurrenceOverrideSourceKind(
  originalStartAt: string,
): string {
  return `${RECURRENCE_OVERRIDE_PREFIX}${compactOriginalStartAt(originalStartAt)}`;
}

export function isRecurrenceSkipSourceKind(
  sourceKind: string | null | undefined,
): boolean {
  return sourceKind === RECURRENCE_SKIP_SOURCE_KIND;
}

export function isRecurrenceOverrideSourceKind(
  sourceKind: string | null | undefined,
): boolean {
  return (
    !!sourceKind?.startsWith(RECURRENCE_OVERRIDE_PREFIX) ||
    !!sourceKind?.startsWith(LEGACY_RECURRENCE_OVERRIDE_PREFIX)
  );
}

export function canReuseOccurrenceRowForOverride(
  sourceKind: string | null | undefined,
  reuseOccurrenceId: boolean,
): boolean {
  return (
    isRecurrenceOverrideSourceKind(sourceKind) ||
    (reuseOccurrenceId && !isRecurrenceSkipSourceKind(sourceKind))
  );
}

export function shouldFindOccurrenceByStartAt(
  occurrenceId: string | null | undefined,
  reuseOccurrenceId: boolean,
): boolean {
  return reuseOccurrenceId && !occurrenceId;
}

export function parseRecurrenceOriginalStartAt(
  sourceKind: string | null | undefined,
): string | null {
  if (!isRecurrenceOverrideSourceKind(sourceKind)) return null;
  if (sourceKind!.startsWith(LEGACY_RECURRENCE_OVERRIDE_PREFIX)) {
    return sourceKind!.slice(LEGACY_RECURRENCE_OVERRIDE_PREFIX.length) || null;
  }
  const compact = sourceKind!.slice(RECURRENCE_OVERRIDE_PREFIX.length);
  return parseCompactOriginalStartAt(compact) ?? compact ?? null;
}

/**
 * 「今回以降を削除」で、保存済みオカレンス行の cutoff 判定にどの開始時刻を使うかを決める。
 *
 * - 別日へ移動した回（ro: / recurrence_override:）は、行自身の開始時刻が移動先なので
 *   source_kind に埋め込まれた「元の回」の時刻で判定する。
 * - それ以外（recurrence_skip、materialize 済みの recurrence、task_schedule）は
 *   行自身の開始時刻で判定する。
 *
 * 以前は override 以外を一律 対象外にしていたため、実体行 (source_kind="recurrence") が
 * 1件も削除されず「今回以降を削除」を押しても表示が変わらなかった。
 */
export function resolveOccurrenceCutoffSource(
  sourceKind: string | null | undefined,
): { from: "original"; originalStartAt: string } | { from: "row" } {
  const originalStartAt = parseRecurrenceOriginalStartAt(sourceKind);
  return originalStartAt
    ? { from: "original", originalStartAt }
    : { from: "row" };
}

export function resolveOccurrenceOriginalStartAt(
  sourceKind: string | null | undefined,
  startAt: string | Date | null | undefined,
): string | null {
  const originalStartAt = parseRecurrenceOriginalStartAt(sourceKind);
  if (originalStartAt) return originalStartAt;
  if (!startAt) return null;
  return startAt instanceof Date ? formatLocalDateTime(startAt) : startAt;
}
