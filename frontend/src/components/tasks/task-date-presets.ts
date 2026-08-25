export interface TaskDatePreset {
  label: string;
  subLabel: string;
  getDate: () => Date;
}

function startOfDay(date: Date): Date {
  const result = new Date(date);
  result.setHours(0, 0, 0, 0);
  return result;
}

function addDays(date: Date, days: number): Date {
  const result = startOfDay(date);
  result.setDate(result.getDate() + days);
  return result;
}

function daysUntilWeekday(date: Date, weekday: number): number {
  const offset = (weekday - date.getDay() + 7) % 7;
  // Today / Tomorrow と同じ日を重ねず、その次の曜日を候補にする。
  return offset <= 1 ? offset + 7 : offset;
}

function uniquePresetDates(presets: TaskDatePreset[]): TaskDatePreset[] {
  const seen = new Set<number>();
  return presets.filter((preset) => {
    const timestamp = preset.getDate().getTime();
    if (seen.has(timestamp)) return false;
    seen.add(timestamp);
    return true;
  });
}

export function getTaskDatePresets(now = new Date()): TaskDatePreset[] {
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const fmtShort = (date: Date) => `${date.getMonth() + 1}/${date.getDate()}`;
  const make = (
    label: string,
    date: Date,
    subLabel = dayNames[date.getDay()],
  ): TaskDatePreset => ({
    label,
    subLabel,
    getDate: () => new Date(date),
  });

  const today = startOfDay(now);
  const tomorrow = addDays(now, 1);
  const friday = addDays(now, daysUntilWeekday(now, 5));
  const mondayOffset = daysUntilWeekday(now, 1);
  const monday = addDays(now, mondayOffset);
  const nextWeek = addDays(now, mondayOffset + 7);
  const twoWeeks = addDays(now, 14);
  const fourWeeks = addDays(now, 28);

  return uniquePresetDates([
    make("Today", today),
    make("Tomorrow", tomorrow),
    make("Friday", friday),
    make("Monday", monday),
    make("Next week", nextWeek, fmtShort(nextWeek)),
    make("In 2 weeks", twoWeeks, fmtShort(twoWeeks)),
    make("In 4 weeks", fourWeeks, fmtShort(fourWeeks)),
  ]);
}
