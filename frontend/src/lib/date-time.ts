function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

function padMs(value: number): string {
  return value.toString().padStart(3, "0");
}

export function formatLocalDateTime(value: Date): string {
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(
    value.getDate(),
  )}T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(
    value.getSeconds(),
  )}`;
}

export function formatLocalDate(value: Date): string {
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(
    value.getDate(),
  )}`;
}

export function formatLocalDateTimeWithMilliseconds(value: Date): string {
  return `${formatLocalDateTime(value)}.${padMs(value.getMilliseconds())}`;
}

const ISO_DATE_PREFIX = /^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$/;
const DATE_ONLY_VALUE = /^(\d{4}-\d{2}-\d{2})$/;
const LOCAL_DATE_TIME_MINUTE =
  /^(\d{4}-\d{2}-\d{2})[T\s](\d{2}):(\d{2})$/;
const DATE_ONLY_TIMESTAMP =
  /^(\d{4}-\d{2}-\d{2})(?:[T\s]00:00(?::00(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const LOCAL_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

export function parseLocalDateTime(value: string): Date | null {
  const match = value.trim().match(LOCAL_DATE_TIME);
  if (!match) return null;
  const [
    ,
    year,
    month,
    day,
    hour = "0",
    minute = "0",
    second = "0",
    millisecond = "0",
  ] = match;
  return new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
    Number(millisecond.padEnd(3, "0")),
  );
}

export function getIsoDatePrefix(value: string | null | undefined): string | null {
  if (!value) return null;
  const match = value.trim().match(ISO_DATE_PREFIX);
  return match?.[1] ?? null;
}

export function getDateOnlyDatePrefix(
  value: string | null | undefined,
): string | null {
  if (!value) return null;
  const match = value.trim().match(DATE_ONLY_TIMESTAMP);
  return match?.[1] ?? null;
}

export function isDateOnlyDateTimeValue(
  value: string | null | undefined,
): boolean {
  return getDateOnlyDatePrefix(value) !== null;
}

export function toLocalDateTimeInputValue(
  value: string | null | undefined,
  options?: { allDay?: boolean },
): string | null {
  if (!value) return null;
  const trimmed = value.trim();

  const allDayPrefix = options?.allDay ? getIsoDatePrefix(trimmed) : null;
  if (allDayPrefix) return allDayPrefix;

  const dateOnlyValue = trimmed.match(DATE_ONLY_VALUE);
  if (dateOnlyValue) return dateOnlyValue[1];

  const date = parseLocalDateTime(trimmed) ?? new Date(trimmed);
  if (Number.isNaN(date.getTime())) {
    return trimmed.slice(0, 16);
  }

  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function toTaskDatePayloadValue(
  value: string | null | undefined,
  options?: { allDay?: boolean },
): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;

  const dateOnlyValue = trimmed.match(DATE_ONLY_VALUE);
  if (dateOnlyValue) return dateOnlyValue[1];

  const allDayPrefix = options?.allDay ? getIsoDatePrefix(trimmed) : null;
  if (allDayPrefix) return allDayPrefix;

  const minuteValue = trimmed.match(LOCAL_DATE_TIME_MINUTE);
  if (minuteValue) {
    return `${minuteValue[1]}T${minuteValue[2]}:${minuteValue[3]}:00`;
  }

  return trimmed;
}
