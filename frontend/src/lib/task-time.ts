import { parseLocalDateTime } from "@/lib/date-time";

export const DEFAULT_TASK_TIMEZONE = "Asia/Tokyo";

export function normalizeTaskTimezone(value: unknown): string {
  const timezone = String(value ?? "").trim();
  if (!timezone || timezone.toUpperCase() === "UTC") {
    return DEFAULT_TASK_TIMEZONE;
  }
  return timezone;
}

export function getElapsedTimerSeconds(
  startedAt: string | null | undefined,
  nowMs = Date.now(),
): number {
  const startedMs = parseTaskTimerDate(startedAt)?.getTime() ?? NaN;
  if (Number.isNaN(startedMs)) return 0;
  return Math.max(0, Math.floor((nowMs - startedMs) / 1000));
}

export function parseTaskTimerDate(
  value: string | null | undefined,
): Date | null {
  if (!value) return null;
  const parsed = parseLocalDateTime(value) ?? new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatTimerClock(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  const h = String(Math.floor(safeSeconds / 3600)).padStart(2, "0");
  const m = String(Math.floor((safeSeconds % 3600) / 60)).padStart(2, "0");
  const s = String(safeSeconds % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}
