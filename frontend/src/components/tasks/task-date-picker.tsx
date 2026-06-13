"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useCallback,
  useLayoutEffect,
} from "react";
import { createPortal } from "react-dom";
import { Calendar } from "@/components/ui/calendar";
import { CalendarIcon, X, Repeat, ChevronLeft } from "lucide-react";
import { cn } from "@/lib/utils";
import type { RecurrenceRule } from "@/lib/task-api";
import { computeUpcomingOccurrences } from "@/lib/recurrence-preview";
import { formatTaskDateLabel } from "@/lib/task-date-label";
import { isDateOnlyDateTimeValue, parseLocalDateTime } from "@/lib/date-time";
import {
  recurrenceLabel,
  RECURRENCE_FREQ_OPTIONS as FREQ_OPTIONS,
  RECURRENCE_WEEKDAYS as WEEKDAYS,
} from "@/lib/recurrence-rrule";

export {
  buildRrule,
  parseRrule,
  recurrenceEndDateInputValue,
  recurrenceLabel,
} from "@/lib/recurrence-rrule";

// ─── Utility ───

function pad(n: number) {
  return n.toString().padStart(2, "0");
}

function formatDateTimeLocal(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatDisplay(value: string | null, allDay = false): string {
  return formatTaskDateLabel(value, {
    allDay,
    absoluteStyle: "long",
  });
}

function parseTaskDateValue(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = parseLocalDateTime(value) ?? new Date(value);
  return isNaN(date.getTime()) ? null : date;
}

function normalizeDateText(raw: string): string {
  return raw
    .toLowerCase()
    .trim()
    .replace(/,\s*/g, " ")
    .replace(/\s+/g, " ");
}

const WEEKDAY_ALIASES: Record<string, number> = {
  sunday: 0,
  sun: 0,
  monday: 1,
  mon: 1,
  tuesday: 2,
  tue: 2,
  tues: 2,
  wednesday: 3,
  wed: 3,
  thursday: 4,
  thu: 4,
  thur: 4,
  thurs: 4,
  friday: 5,
  fri: 5,
  saturday: 6,
  sat: 6,
};

function parseFlexibleDate(raw: string): string | null {
  const lower = normalizeDateText(raw);
  if (!lower) return null;
  const now = new Date();

  if (lower === "today" || lower === "tod") {
    const d = new Date(now);
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (
    lower === "tomorrow" ||
    lower === "tomo" ||
    lower === "tom"
  ) {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (lower === "next week") {
    const d = new Date(now);
    d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
    d.setHours(0, 0, 0, 0);
    return formatDateTimeLocal(d);
  }
  if (lower === "next month") {
    return formatDateTimeLocal(
      new Date(now.getFullYear(), now.getMonth() + 1, 1, 0, 0, 0),
    );
  }

  // "today 16:00"
  const todayTime = lower.match(/^(?:today|tod)\s+(\d{1,2}):(\d{2})$/);
  if (todayTime) {
    const d = new Date(now);
    d.setHours(+todayTime[1], +todayTime[2], 0, 0);
    return formatDateTimeLocal(d);
  }

  // "tomo 16:00"
  const tomoTime = lower.match(
    /^(?:tomorrow|tomo|tom)\s+(\d{1,2}):(\d{2})$/,
  );
  if (tomoTime) {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(+tomoTime[1], +tomoTime[2], 0, 0);
    return formatDateTimeLocal(d);
  }

  // "monday" / "friday 13:00" / "next monday 09:30"
  const weekdayMatch = lower.match(
    /^(next\s+)?(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|friday|fri|saturday|sat)(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (weekdayMatch) {
    const targetDay = WEEKDAY_ALIASES[weekdayMatch[2]];
    let daysUntil = (targetDay - now.getDay() + 7) % 7;
    if (weekdayMatch[1] && daysUntil === 0) daysUntil = 7;
    const d = new Date(now);
    d.setDate(d.getDate() + daysUntil);
    d.setHours(
      weekdayMatch[3] ? +weekdayMatch[3] : 0,
      weekdayMatch[4] ? +weekdayMatch[4] : 0,
      0,
      0,
    );
    return formatDateTimeLocal(d);
  }

  // "2025/03/25" or "2025/03/25 10:00"
  const fullDateMatch = lower.match(
    /^(\d{4})\/(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (fullDateMatch) {
    return formatDateTimeLocal(
      new Date(
        +fullDateMatch[1],
        +fullDateMatch[2] - 1,
        +fullDateMatch[3],
        fullDateMatch[4] ? +fullDateMatch[4] : 0,
        fullDateMatch[5] ? +fullDateMatch[5] : 0,
        0,
      ),
    );
  }

  // "3/25" or "3/25 10:00"
  const dateMatch = lower.match(
    /^(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (dateMatch) {
    return formatDateTimeLocal(
      new Date(
        now.getFullYear(),
        +dateMatch[1] - 1,
        +dateMatch[2],
        dateMatch[3] ? +dateMatch[3] : 0,
        dateMatch[4] ? +dateMatch[4] : 0,
        0,
      ),
    );
  }

  // "16:00"
  const timeOnly = lower.match(/^(\d{1,2}):(\d{2})$/);
  if (timeOnly) {
    const d = new Date(now);
    d.setHours(+timeOnly[1], +timeOnly[2], 0, 0);
    return formatDateTimeLocal(d);
  }

  const parsed = parseTaskDateValue(raw.trim());
  return parsed ? formatDateTimeLocal(parsed) : null;
}

function parseTimeOnlyInput(
  raw: string,
): { hours: number; minutes: number } | null {
  const match = normalizeDateText(raw).match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59) return null;
  return { hours, minutes };
}

function applyTimeOnlyToBaseDate(
  raw: string,
  baseValue: string | null,
): string | null {
  const time = parseTimeOnlyInput(raw);
  if (!time || !baseValue) return null;
  const base = parseTaskDateValue(baseValue);
  if (!base) return null;
  base.setHours(time.hours, time.minutes, 0, 0);
  return formatDateTimeLocal(base);
}

function formatParseableDatePrefix(value: string): string {
  const date = parseTaskDateValue(value);
  if (!date) return "Today";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diffDays = Math.round(
    (target.getTime() - today.getTime()) / (1000 * 60 * 60 * 24),
  );
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Tomorrow";
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(
    date.getDate(),
  )}`;
}

function isDateOnly(value: string): boolean {
  if (isDateOnlyDateTimeValue(value)) return true;
  const d = parseTaskDateValue(value);
  return !!d && d.getHours() === 0 && d.getMinutes() === 0;
}

function hasTime(value: string | null): boolean {
  if (!value) return false;
  if (isDateOnlyDateTimeValue(value)) return false;
  const d = parseTaskDateValue(value);
  return !!d && (d.getHours() !== 0 || d.getMinutes() !== 0);
}

function hasExplicitTimeText(raw: string): boolean {
  return /(^|\s)\d{1,2}:\d{2}\s*$/.test(normalizeDateText(raw));
}

function inferAllDay(
  startValue: string | null,
  endValue: string | null,
  fallback = false,
): boolean {
  if (hasTime(startValue) || hasTime(endValue)) return false;
  if (!startValue && !endValue) return fallback;
  if (fallback) return true;
  return true;
}

function isSameDay(a: string, b: string): boolean {
  const da = parseTaskDateValue(a);
  const db = parseTaskDateValue(b);
  if (!da || !db) return false;
  return (
    da.getFullYear() === db.getFullYear() &&
    da.getMonth() === db.getMonth() &&
    da.getDate() === db.getDate()
  );
}

function isSameDateTime(a: string | null, b: string | null): boolean {
  if (!a || !b) return false;
  const da = parseTaskDateValue(a);
  const db = parseTaskDateValue(b);
  return !!da && !!db && da.getTime() === db.getTime();
}

function adjustEndDateWithStartTime(
  endValue: string,
  startValue: string | null,
): string {
  if (
    !startValue ||
    !hasTime(startValue) ||
    !isDateOnly(endValue) ||
    !isSameDay(endValue, startValue)
  ) {
    return endValue;
  }
  const s = parseTaskDateValue(startValue);
  const e = parseTaskDateValue(endValue);
  if (!s || !e) return endValue;
  e.setHours(s.getHours() + 1, s.getMinutes(), 0, 0);
  return formatDateTimeLocal(e);
}

// ─── Presets ───

interface Preset {
  label: string;
  subLabel: string;
  getDate: () => Date;
}

function getPresets(): Preset[] {
  const now = new Date();
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const fmtShort = (d: Date) => `${d.getMonth() + 1}/${d.getDate()}`;

  const make = (label: string, d: Date, sub?: string): Preset => ({
    label,
    subLabel: sub ?? dayNames[d.getDay()],
    getDate: () => new Date(d),
  });

  const today = new Date(now);
  today.setHours(9, 0, 0, 0);
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(9, 0, 0, 0);

  const thisWeekend = new Date(now);
  const daysUntilSat = (6 - now.getDay() + 7) % 7 || 7;
  thisWeekend.setDate(thisWeekend.getDate() + daysUntilSat);
  thisWeekend.setHours(0, 0, 0, 0);

  const nextMonday = new Date(now);
  nextMonday.setDate(
    nextMonday.getDate() + (now.getDay() === 0 ? 1 : 8 - now.getDay()),
  );
  nextMonday.setHours(9, 0, 0, 0);

  const nextWeekend = new Date(now);
  nextWeekend.setDate(
    nextWeekend.getDate() +
      (daysUntilSat < 7 ? daysUntilSat + 7 : daysUntilSat),
  );
  nextWeekend.setHours(0, 0, 0, 0);

  const twoWeeks = new Date(now);
  twoWeeks.setDate(twoWeeks.getDate() + 14);
  twoWeeks.setHours(9, 0, 0, 0);
  const fourWeeks = new Date(now);
  fourWeeks.setDate(fourWeeks.getDate() + 28);
  fourWeeks.setHours(9, 0, 0, 0);

  return [
    make("Today", today),
    make("Tomorrow", tomorrow),
    make("This weekend", thisWeekend),
    make("Next week", nextMonday, fmtShort(nextMonday)),
    make("Next weekend", nextWeekend, fmtShort(nextWeekend)),
    make("In 2 weeks", twoWeeks, fmtShort(twoWeeks)),
    make("In 4 weeks", fourWeeks, fmtShort(fourWeeks)),
  ];
}

// ─── Suggestions ───

const SUGGEST_KEYWORDS = [
  "Today",
  "Tomorrow",
  "Next week",
  "Next month",
];

function getTimeSuggestions(text: string): string[] {
  const t = text.trim();
  const m = t.match(/^(\d{1,2})(:(\d{0,2}))?$/);
  if (!m) return [];

  const hourStr = m[1];
  const hasColon = !!m[2];
  const minStr = m[3] ?? "";
  const results: string[] = [];

  if (!hasColon) {
    const h = parseInt(hourStr);
    if (hourStr.length === 2) {
      if (h >= 0 && h <= 23) results.push(`${pad(h)}:00`, `${pad(h)}:30`);
    } else {
      if (h >= 0 && h <= 9) results.push(`${pad(h)}:00`, `${pad(h)}:30`);
      const base = h * 10;
      for (let hh = base; hh <= Math.min(base + 9, 23); hh++) {
        results.push(`${pad(hh)}:00`);
      }
    }
  } else {
    const h = parseInt(hourStr);
    if (h >= 0 && h <= 23) {
      for (const min of ["00", "15", "30", "45"]) {
        if (min.startsWith(minStr)) results.push(`${pad(h)}:${min}`);
      }
    }
  }

  return results.filter((r) => r !== t).slice(0, 6);
}

function appendAdjustedTime(
  keyword: string,
  startValue: string | null,
): string {
  if (!startValue || !hasTime(startValue)) return keyword;
  const resolved = parseFlexibleDate(keyword);
  if (!resolved || !isSameDay(resolved, startValue)) return keyword;
  const s = parseTaskDateValue(startValue);
  if (!s) return keyword;
  const adjusted = new Date(s);
  adjusted.setHours(s.getHours() + 1, s.getMinutes(), 0, 0);
  if (adjusted.getDate() !== s.getDate()) {
    adjusted.setHours(23, 59, 0, 0);
  }
  return `${keyword} ${pad(adjusted.getHours())}:${pad(adjusted.getMinutes())}`;
}

function getSuggestions(
  text: string,
  field?: "start" | "end",
  startValue?: string | null,
): string[] {
  const t = text.trim().toLowerCase();
  if (!t) return [];
  if (/^.+\s+\d/.test(t)) return [];
  const keywords = SUGGEST_KEYWORDS.filter(
    (k) => k.toLowerCase().startsWith(t) && k.toLowerCase() !== t,
  );
  if (keywords.length > 0) {
    if (field === "end" && startValue && hasTime(startValue)) {
      return keywords.map((k) => appendAdjustedTime(k, startValue));
    }
    return keywords;
  }
  const timeSugs = getTimeSuggestions(text);
  if (timeSugs.length > 0) {
    const baseLabel =
      field === "end" && startValue
        ? formatParseableDatePrefix(startValue)
        : "Today";
    return timeSugs.map((ts) => `${baseLabel} ${ts}`);
  }
  return [];
}

// ─── Recurrence ───

const STATUS_OPTIONS = [
  { value: "open", label: "未着手" },
  { value: "in_progress", label: "進行中" },
  { value: "on_hold", label: "保留" },
  { value: "review", label: "確認待ち" },
  { value: "closed", label: "完了" },
] as const;

export interface RecurrenceConfig {
  recurrenceRule: RecurrenceRule | null;
  freq: string;
  interval: number;
  byDay: string[];
  triggerStatus: string;
  createNew: boolean;
  recurForever: boolean;
  resetStatusTo: string;
  endCount: number | null;
  endDate: string | null;
  skipWeekend: boolean;
  skipHoliday: boolean;
  saving: boolean;
  onFreqChange: (v: string) => void;
  onIntervalChange: (v: number) => void;
  onToggleWeekday: (dayKey: string) => void;
  onTriggerStatusChange: (v: string) => void;
  onCreateNewChange: (v: boolean) => void;
  onRecurForeverChange: (v: boolean) => void;
  onResetStatusToChange: (v: string) => void;
  onEndCountChange: (v: number | null) => void;
  onEndDateChange: (v: string | null) => void;
  onSkipWeekendChange: (v: boolean) => void;
  onSkipHolidayChange: (v: boolean) => void;
  onSave: () => void;
  onDelete: () => void;
}

function RecurrenceInlinePanel({
  freq,
  interval,
  byDay,
  triggerStatus,
  createNew,
  recurForever,
  endCount,
  endDate,
  skipWeekend,
  skipHoliday,
  saving,
  hasExisting,
  onFreqChange,
  onIntervalChange,
  onToggleWeekday,
  onTriggerStatusChange,
  onCreateNewChange,
  onRecurForeverChange,
  onEndCountChange,
  onEndDateChange,
  onSkipWeekendChange,
  onSkipHolidayChange,
  onSave,
  onDelete,
  onBack,
}: {
  freq: string;
  interval: number;
  byDay: string[];
  triggerStatus: string;
  createNew: boolean;
  recurForever: boolean;
  endCount: number | null;
  endDate: string | null;
  skipWeekend: boolean;
  skipHoliday: boolean;
  saving: boolean;
  hasExisting: boolean;
  onFreqChange: (v: string) => void;
  onIntervalChange: (v: number) => void;
  onToggleWeekday: (dayKey: string) => void;
  onTriggerStatusChange: (v: string) => void;
  onCreateNewChange: (v: boolean) => void;
  onRecurForeverChange: (v: boolean) => void;
  onEndCountChange: (v: number | null) => void;
  onEndDateChange: (v: string | null) => void;
  onSkipWeekendChange: (v: boolean) => void;
  onSkipHolidayChange: (v: boolean) => void;
  onSave: () => void;
  onDelete: () => void;
  onBack: () => void;
}) {
  return (
    <div className="flex w-full min-w-0 flex-col sm:min-w-[200px] sm:max-w-[220px]">
      <div className="flex items-center gap-1 px-2 pt-2 pb-1">
        <button
          type="button"
          className="p-0.5 rounded hover:bg-accent transition-colors"
          onClick={onBack}
        >
          <ChevronLeft className="size-3.5" />
        </button>
        <span className="text-xs font-medium flex items-center gap-1">
          <Repeat className="size-3" />
          繰り返し
        </span>
      </div>

      <div className="px-2 pb-2 space-y-2">
        {/* 頻度 + 間隔 */}
        <div className="flex items-center gap-1.5">
          <select
            value={freq}
            onChange={(e) => onFreqChange(e.target.value)}
            className="h-7 rounded-md border border-input bg-background px-1.5 text-xs outline-none"
          >
            {FREQ_OPTIONS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          <input
            type="number"
            min={1}
            max={99}
            value={interval}
            onChange={(e) => {
              const val = parseInt(e.target.value, 10);
              if (!isNaN(val) && val >= 1) onIntervalChange(val);
            }}
            className="h-7 w-12 rounded-md border border-input bg-background px-1.5 text-xs outline-none"
            title="間隔"
          />
        </div>

        {/* WEEKLY: 曜日選択 */}
        {freq === "WEEKLY" && (
          <div className="flex flex-wrap gap-0.5">
            {WEEKDAYS.map((day) => (
              <button
                key={day.key}
                type="button"
                className={cn(
                  "h-6 w-6 rounded text-xs transition-colors",
                  byDay.includes(day.key)
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted hover:bg-accent",
                )}
                onClick={() => onToggleWeekday(day.key)}
              >
                {day.label}
              </button>
            ))}
          </div>
        )}

        {/* DAILY: 週末スキップ / 全頻度: 祝日スキップ */}
        <div className="space-y-1">
          {freq === "DAILY" && (
            <label className="flex items-center gap-1.5 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={skipWeekend}
                onChange={(e) => onSkipWeekendChange(e.target.checked)}
                className="size-3.5 accent-primary"
              />
              週末をスキップ
            </label>
          )}
          <label className="flex items-center gap-1.5 text-xs cursor-pointer">
            <input
              type="checkbox"
              checked={skipHoliday}
              onChange={(e) => onSkipHolidayChange(e.target.checked)}
              className="size-3.5 accent-primary"
            />
            祝日をスキップ
          </label>
        </div>

        <div className="border-t border-border/60 my-1" />

        {/* トリガー */}
        <div className="space-y-1">
          <label className="text-[10px] text-muted-foreground">トリガー</label>
          <select
            value={triggerStatus}
            onChange={(e) => onTriggerStatusChange(e.target.value)}
            className="h-7 w-full rounded-md border border-input bg-background px-1.5 text-xs outline-none"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}になった時
              </option>
            ))}
          </select>
        </div>

        {/* チェックボックス群 */}
        <label className="flex items-center gap-1.5 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={createNew}
            onChange={(e) => onCreateNewChange(e.target.checked)}
            className="size-3.5 accent-primary"
          />
          新しいタスクを作成
        </label>

        <label className="flex items-center gap-1.5 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={recurForever}
            onChange={(e) => onRecurForeverChange(e.target.checked)}
            className="size-3.5 accent-primary"
          />
          永続的に繰り返す
        </label>

        {!recurForever && (
          <div className="pl-5 space-y-1.5">
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={1}
                max={999}
                value={endCount ?? ""}
                onChange={(e) => {
                  const val = e.target.value
                    ? parseInt(e.target.value, 10)
                    : null;
                  onEndCountChange(val && val >= 1 ? val : null);
                  if (val) onEndDateChange(null);
                }}
                placeholder="∞"
                className="h-6 w-14 rounded border border-input bg-background px-1 text-xs outline-none"
              />
              <span className="text-xs text-muted-foreground">回</span>
            </div>
            <input
              type="date"
              value={endDate ?? ""}
              onChange={(e) => {
                onEndDateChange(e.target.value || null);
                if (e.target.value) onEndCountChange(null);
              }}
              className="h-6 w-full rounded border border-input bg-background px-1 text-xs outline-none"
            />
          </div>
        )}

      </div>

      {/* フッター */}
      <div className="flex items-center justify-between border-t border-border/60 px-2 py-1.5">
        {hasExisting ? (
          <button
            type="button"
            className="text-[11px] text-red-500 hover:text-red-400 transition-colors"
            onClick={onDelete}
            disabled={saving}
          >
            解除
          </button>
        ) : (
          <span />
        )}
        <button
          type="button"
          className="h-6 px-3 rounded-md bg-primary text-primary-foreground text-xs hover:bg-primary/90 transition-colors disabled:opacity-50"
          onClick={onSave}
          disabled={saving}
        >
          {saving ? "..." : "保存"}
        </button>
      </div>
    </div>
  );
}

// ─── Component ───

type ActiveField = "start" | "end";
type DraftValues = {
  startAt: string | null;
  endAt: string | null;
};

interface TaskDatePickerProps {
  startAt: string | null;
  endAt: string | null;
  onStartAtChange: (value: string | null) => void;
  onEndAtChange: (value: string | null) => void;
  onRangeChange?: (values: {
    startAt: string | null;
    endAt: string | null;
  }) => void;
  deferCommitUntilClose?: boolean;
  /** 終日チェックが有効の場合、時刻入力を非表示 */
  allDay?: boolean;
  startPlaceholder?: string;
  endPlaceholder?: string;
  startButtonClassName?: string;
  endButtonClassName?: string;
  recurrence?: RecurrenceConfig;
  onOpenChange?: (open: boolean) => void;
}

export function TaskDatePicker({
  startAt,
  endAt,
  onStartAtChange,
  onEndAtChange,
  onRangeChange,
  deferCommitUntilClose = false,
  allDay,
  startPlaceholder = "Start Date",
  endPlaceholder = "Due Date",
  startButtonClassName,
  endButtonClassName,
  recurrence,
  onOpenChange,
}: TaskDatePickerProps) {
  const [open, setOpen] = useState(false);
  const [activeField, setActiveField] = useState<ActiveField>("start");
  const [showRecurrence, setShowRecurrence] = useState(false);
  const [draftStartAt, setDraftStartAt] = useState<string | null>(startAt);
  const [draftEndAt, setDraftEndAt] = useState<string | null>(endAt);
  const [startText, setStartText] = useState("");
  const [endText, setEndText] = useState("");
  const [editingField, setEditingField] = useState<ActiveField | null>(null);
  const [suggestIndex, setSuggestIndex] = useState(-1);
  const [dropdownPos, setDropdownPos] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const startInputRef = useRef<HTMLInputElement>(null);
  const endInputRef = useRef<HTMLInputElement>(null);
  const isAdvancingToEndRef = useRef(false);
  const endFocusTimeoutRef = useRef<number | null>(null);
  const [dropdownWidth, setDropdownWidth] = useState(520);

  useLayoutEffect(() => {
    if (!open || !containerRef.current) {
      const timer = window.setTimeout(() => setDropdownPos(null), 0);
      return () => window.clearTimeout(timer);
    }
    const updatePos = () => {
      const rect = containerRef.current!.getBoundingClientRect();
      const viewportPadding = 8;
      const nextDropdownWidth = Math.min(
        520,
        Math.max(240, window.innerWidth - viewportPadding * 2),
      );
      setDropdownWidth(nextDropdownWidth);
      const maxLeft = Math.max(
        viewportPadding,
        window.innerWidth - viewportPadding - nextDropdownWidth,
      );
      const left = Math.min(
        Math.max(rect.right - nextDropdownWidth, viewportPadding),
        maxLeft,
      );

      const dropdownHeight = dropdownRef.current?.offsetHeight ?? 420;
      const spaceBelow = window.innerHeight - rect.bottom - 8;
      const spaceAbove = rect.top - 8;
      let top: number;
      if (spaceBelow >= dropdownHeight || spaceBelow >= spaceAbove) {
        top = rect.bottom + 4;
        if (top + dropdownHeight > window.innerHeight - 8) {
          top = Math.max(8, window.innerHeight - 8 - dropdownHeight);
        }
      } else {
        top = Math.max(8, rect.top - dropdownHeight - 4);
      }
      setDropdownPos({ top, left });
    };
    updatePos();
    window.addEventListener("scroll", updatePos, true);
    window.addEventListener("resize", updatePos);
    let ro: ResizeObserver | null = null;
    if (dropdownRef.current && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => updatePos());
      ro.observe(dropdownRef.current);
    }
    return () => {
      window.removeEventListener("scroll", updatePos, true);
      window.removeEventListener("resize", updatePos);
      ro?.disconnect();
    };
  }, [open, showRecurrence]);

  const focusEndInput = useCallback(() => {
    if (endFocusTimeoutRef.current !== null) {
      window.clearTimeout(endFocusTimeoutRef.current);
    }
    endFocusTimeoutRef.current = window.setTimeout(() => {
      endInputRef.current?.focus();
      isAdvancingToEndRef.current = false;
      endFocusTimeoutRef.current = null;
    }, 0);
  }, []);

  useEffect(() => {
    return () => {
      if (endFocusTimeoutRef.current !== null) {
        window.clearTimeout(endFocusTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    onOpenChange?.(open);
  }, [open, onOpenChange]);

  const displayedStartAt = open ? draftStartAt : startAt;
  const displayedEndAt = open ? draftEndAt : endAt;
  const displayAllDay = inferAllDay(
    displayedStartAt,
    displayedEndAt,
    Boolean(allDay),
  );

  const getDisplayText = useCallback(
    (value: string | null, nextValues?: Partial<DraftValues>) =>
      formatDisplay(
        value,
        inferAllDay(
          nextValues?.startAt ?? draftStartAt,
          nextValues?.endAt ?? draftEndAt,
          Boolean(allDay),
        ),
      ),
    [allDay, draftEndAt, draftStartAt],
  );

  const commitDrafts = useCallback(
    (
      nextStartAt: string | null = draftStartAt,
      nextEndAt: string | null = draftEndAt,
    ) => {
      if (nextStartAt === startAt && nextEndAt === endAt) {
        return;
      }
      if (onRangeChange) {
        onRangeChange({ startAt: nextStartAt, endAt: nextEndAt });
        return;
      }
      if (nextStartAt !== startAt) {
        onStartAtChange(nextStartAt);
      }
      if (nextEndAt !== endAt) {
        onEndAtChange(nextEndAt);
      }
    },
    [
      draftEndAt,
      draftStartAt,
      endAt,
      onEndAtChange,
      onRangeChange,
      onStartAtChange,
      startAt,
    ],
  );

  const setOpenWithCommit = useCallback(
    (nextOpen: boolean, nextValues?: Partial<DraftValues>) => {
      if (open && !nextOpen && isAdvancingToEndRef.current) {
        return;
      }
      if (open && !nextOpen && deferCommitUntilClose) {
        commitDrafts(
          nextValues?.startAt ?? draftStartAt,
          nextValues?.endAt ?? draftEndAt,
        );
      }
      setOpen(nextOpen);
    },
    [commitDrafts, deferCommitUntilClose, draftEndAt, draftStartAt, open],
  );

  const activeValue = activeField === "start" ? draftStartAt : draftEndAt;
  const activeOnChange =
    activeField === "start" ? onStartAtChange : onEndAtChange;

  const selectedDate = useMemo(() => {
    if (!activeValue) return undefined;
    return parseTaskDateValue(activeValue) ?? undefined;
  }, [activeValue]);

  // 繰り返しプレビュー: 開始日を起点に次回以降の発生日を算出してカレンダー上でハイライト
  const upcomingOccurrences = useMemo(() => {
    if (!showRecurrence || !recurrence) return [] as Date[];
    if (!draftStartAt) return [] as Date[];
    const base = parseTaskDateValue(draftStartAt);
    if (!base) return [] as Date[];
    return computeUpcomingOccurrences(
      base,
      {
        freq: recurrence.freq,
        interval: recurrence.interval,
        byDay: recurrence.byDay,
        skipWeekend: recurrence.skipWeekend,
        skipHoliday: recurrence.skipHoliday,
        endCount: recurrence.endCount,
        endDate: recurrence.endDate,
      },
      30,
    );
  }, [showRecurrence, recurrence, draftStartAt]);
  const nextOccurrence = upcomingOccurrences[0];

  const presets = useMemo(() => getPresets(), []);

  const activeSuggestions = useMemo(() => {
    if (!editingField) return [];
    const text = editingField === "start" ? startText : endText;
    return getSuggestions(text, editingField, draftStartAt);
  }, [editingField, startText, endText, draftStartAt]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSuggestIndex(activeSuggestions.length > 0 ? 0 : -1);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [activeSuggestions]);

  const acceptSuggestion = useCallback(
    (keyword: string, field: ActiveField) => {
      const hasTimeInSuggestion = /\d{1,2}:\d{2}$/.test(keyword);
      const newText = hasTimeInSuggestion ? keyword : keyword + " ";
      if (field === "start") setStartText(newText);
      else setEndText(newText);
      setSuggestIndex(-1);
    },
    [],
  );

  const handleOpen = useCallback(
    (field: ActiveField) => {
      setDraftStartAt(startAt);
      setDraftEndAt(endAt);
      setStartText(
        formatDisplay(startAt, inferAllDay(startAt, endAt, Boolean(allDay))),
      );
      setEndText(
        formatDisplay(endAt, inferAllDay(startAt, endAt, Boolean(allDay))),
      );
      setEditingField(null);
      setActiveField(field);
      setOpen(true);
      setTimeout(() => {
        const ref = field === "start" ? startInputRef : endInputRef;
        ref.current?.focus();
      }, 0);
    },
    [allDay, endAt, startAt],
  );

  // 開始日時確定後、自動で終了日時に遷移する共通ロジック
  const autoAdvanceOrClose = useCallback(
    (nextValues?: Partial<DraftValues>) => {
      if (activeField === "start") {
        // 開始日時 → 終了日時に自動遷移
        isAdvancingToEndRef.current = true;
        setActiveField("end");
        setEditingField(null);
        setEndText(getDisplayText(nextValues?.endAt ?? draftEndAt, nextValues));
        focusEndInput();
      } else {
        // 終了日時確定 → 閉じる
        setEditingField(null);
        setOpenWithCommit(false, nextValues);
      }
    },
    [activeField, draftEndAt, focusEndInput, getDisplayText, setOpenWithCommit],
  );

  const handleSelectDate = useCallback(
    (date: Date | undefined) => {
      if (!date) return;
      const cur = parseTaskDateValue(activeValue);
      const hh = cur ? cur.getHours() : 0;
      const mm = cur ? cur.getMinutes() : 0;
      date.setHours(hh, mm, 0, 0);
      const raw = formatDateTimeLocal(date);
      const nextValue =
        activeField === "end"
          ? adjustEndDateWithStartTime(raw, draftStartAt)
          : raw;
      if (activeField === "start") {
        setDraftStartAt(nextValue);
      } else {
        setDraftEndAt(nextValue);
      }
      if (!deferCommitUntilClose) {
        activeOnChange(nextValue);
      }
      autoAdvanceOrClose(
        activeField === "start"
          ? { startAt: nextValue, endAt: draftEndAt }
          : { startAt: draftStartAt, endAt: nextValue },
      );
    },
    [
      activeField,
      activeOnChange,
      activeValue,
      autoAdvanceOrClose,
      deferCommitUntilClose,
      draftEndAt,
      draftStartAt,
    ],
  );

  const handlePreset = useCallback(
    (preset: Preset) => {
      const d = preset.getDate();
      if (activeValue) {
        const existing = parseTaskDateValue(activeValue);
        if (existing) {
          d.setHours(existing.getHours(), existing.getMinutes(), 0, 0);
        } else {
          d.setHours(0, 0, 0, 0);
        }
      } else {
        d.setHours(0, 0, 0, 0);
      }
      const raw = formatDateTimeLocal(d);
      const nextValue =
        activeField === "end"
          ? adjustEndDateWithStartTime(raw, draftStartAt)
          : raw;
      if (activeField === "start") {
        setDraftStartAt(nextValue);
      } else {
        setDraftEndAt(nextValue);
      }
      if (!deferCommitUntilClose) {
        activeOnChange(nextValue);
      }
      autoAdvanceOrClose(
        activeField === "start"
          ? { startAt: nextValue, endAt: draftEndAt }
          : { startAt: draftStartAt, endAt: nextValue },
      );
    },
    [
      activeField,
      activeOnChange,
      activeValue,
      autoAdvanceOrClose,
      deferCommitUntilClose,
      draftEndAt,
      draftStartAt,
    ],
  );

  const handleTextCommit = useCallback(
    (field: ActiveField) => {
      const text = field === "start" ? startText : endText;
      const onChange = field === "start" ? onStartAtChange : onEndAtChange;
      const trimmed = text.trim();
      if (!trimmed) {
        setEditingField(null);
        return;
      }
      const parsed =
        applyTimeOnlyToBaseDate(
          trimmed,
          field === "start" ? draftStartAt : (draftEndAt ?? draftStartAt),
        ) ?? parseFlexibleDate(trimmed);
      if (parsed) {
        const adjusted =
          field === "end"
            ? adjustEndDateWithStartTime(parsed, draftStartAt)
            : parsed;
        const dateOnlyInput = !hasExplicitTimeText(trimmed);
        const nextStartAt =
          field === "start"
            ? adjusted
            : dateOnlyInput && isSameDateTime(draftStartAt, activeValue)
              ? adjusted
              : draftStartAt;
        const nextEndAt =
          field === "end"
            ? adjusted
            : dateOnlyInput && isSameDateTime(draftEndAt, activeValue)
              ? adjusted
              : draftEndAt;
        const nextValues = { startAt: nextStartAt, endAt: nextEndAt };
        if (field === "start") {
          setDraftStartAt(nextStartAt);
          setDraftEndAt(nextEndAt);
          setStartText(getDisplayText(nextStartAt, nextValues));
          setEndText(getDisplayText(nextEndAt, nextValues));
        } else {
          setDraftStartAt(nextStartAt);
          setDraftEndAt(nextEndAt);
          setStartText(getDisplayText(nextStartAt, nextValues));
          setEndText(getDisplayText(nextEndAt, nextValues));
        }
        if (!deferCommitUntilClose) {
          if (onRangeChange) {
            onRangeChange(nextValues);
          } else {
            onChange(adjusted);
          }
        }
        return nextValues;
      }
      setEditingField(null);
      return null;
    },
    [
      activeValue,
      draftEndAt,
      draftStartAt,
      deferCommitUntilClose,
      endText,
      getDisplayText,
      onEndAtChange,
      onRangeChange,
      onStartAtChange,
      startText,
    ],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent, field: ActiveField) => {
      if (activeSuggestions.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSuggestIndex((i) => Math.min(i + 1, activeSuggestions.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSuggestIndex((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter" && suggestIndex >= 0) {
          e.preventDefault();
          acceptSuggestion(activeSuggestions[suggestIndex], field);
          return;
        }
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const committedValues = handleTextCommit(field);
        if (field === "start") {
          isAdvancingToEndRef.current = true;
          setActiveField("end");
          focusEndInput();
        } else {
          setOpenWithCommit(false, committedValues ?? undefined);
        }
      }
      if (e.key === "Escape") {
        setEditingField(null);
        setDraftStartAt(startAt);
        setDraftEndAt(endAt);
        setStartText(
          formatDisplay(startAt, inferAllDay(startAt, endAt, Boolean(allDay))),
        );
        setEndText(
          formatDisplay(endAt, inferAllDay(startAt, endAt, Boolean(allDay))),
        );
        setOpen(false);
      }
    },
    [
      acceptSuggestion,
      activeSuggestions,
      allDay,
      endAt,
      focusEndInput,
      handleTextCommit,
      setOpenWithCommit,
      startAt,
      suggestIndex,
    ],
  );

  // Click outside to close
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        containerRef.current &&
        !containerRef.current.contains(target) &&
        (!dropdownRef.current || !dropdownRef.current.contains(target))
      ) {
        const committedValues = editingField
          ? handleTextCommit(editingField)
          : null;
        const nextStartAt = committedValues?.startAt ?? draftStartAt;
        const nextEndAt = committedValues?.endAt ?? draftEndAt;
        setOpenWithCommit(false, committedValues ?? undefined);
        setEditingField(null);
        setStartText(
          getDisplayText(nextStartAt, {
            startAt: nextStartAt,
            endAt: nextEndAt,
          }),
        );
        setEndText(
          getDisplayText(nextEndAt, {
            startAt: nextStartAt,
            endAt: nextEndAt,
          }),
        );
      }
    };
    const t = setTimeout(
      () => document.addEventListener("mousedown", handler),
      0,
    );
    return () => {
      clearTimeout(t);
      document.removeEventListener("mousedown", handler);
    };
  }, [
    draftEndAt,
    draftStartAt,
    editingField,
    getDisplayText,
    handleTextCommit,
    open,
    setOpenWithCommit,
  ]);

  const handleClear = useCallback(
    (e: React.MouseEvent, field: ActiveField) => {
      e.stopPropagation();
      e.preventDefault();
      const nextStart = field === "start" ? null : draftStartAt;
      const nextEnd = field === "end" ? null : draftEndAt;
      if (field === "start") {
        setDraftStartAt(null);
        setStartText("");
      } else {
        setDraftEndAt(null);
        setEndText("");
      }
      if (!deferCommitUntilClose || !open) {
        commitDrafts(nextStart, nextEnd);
      }
    },
    [commitDrafts, deferCommitUntilClose, draftEndAt, draftStartAt, open],
  );

  return (
    <div
      ref={containerRef}
      className="relative w-full"
      onClick={(e) => e.stopPropagation()}
    >
      {/* Two trigger buttons side by side */}
      <div className="grid grid-cols-2 gap-2">
        {/* Start date trigger */}
        <button
          type="button"
          title={
            displayedStartAt
              ? formatDisplay(displayedStartAt, displayAllDay)
              : startPlaceholder
          }
          className={cn(
            "flex h-9 w-full min-w-0 items-center gap-1.5 rounded-md border px-2.5 text-left text-sm transition-colors",
            open && activeField === "start"
              ? "border-primary ring-2 ring-primary/30 bg-background"
              : displayedStartAt
                ? "border-input bg-background hover:bg-accent/50"
                : "border-dashed border-muted-foreground/40 text-muted-foreground hover:border-input hover:bg-accent/30",
            startButtonClassName,
          )}
          onClick={() => handleOpen("start")}
        >
          <CalendarIcon className="size-3.5 shrink-0" />
          <span className="truncate min-w-0 flex-1">
            {displayedStartAt
              ? formatDisplay(displayedStartAt, displayAllDay)
              : startPlaceholder}
          </span>
          {displayedStartAt && (
            <span
              role="button"
              className="shrink-0 p-0.5 rounded-sm hover:bg-accent"
              onClick={(e) => handleClear(e, "start")}
            >
              <X className="size-3 text-muted-foreground" />
            </span>
          )}
        </button>

        {/* End date trigger */}
        <button
          type="button"
          title={
            displayedEndAt
              ? formatDisplay(displayedEndAt, displayAllDay)
              : endPlaceholder
          }
          className={cn(
            "flex h-9 w-full min-w-0 items-center gap-1.5 rounded-md border px-2.5 text-left text-sm transition-colors",
            open && activeField === "end"
              ? "border-primary ring-2 ring-primary/30 bg-background"
              : displayedEndAt
                ? "border-input bg-background hover:bg-accent/50"
                : "border-dashed border-muted-foreground/40 text-muted-foreground hover:border-input hover:bg-accent/30",
            endButtonClassName,
          )}
          onClick={() => handleOpen("end")}
        >
          <CalendarIcon className="size-3.5 shrink-0" />
          <span className="truncate min-w-0 flex-1">
            {displayedEndAt
              ? formatDisplay(displayedEndAt, displayAllDay)
              : endPlaceholder}
          </span>
          {displayedEndAt && (
            <span
              role="button"
              className="shrink-0 p-0.5 rounded-sm hover:bg-accent"
              onClick={(e) => handleClear(e, "end")}
            >
              <X className="size-3 text-muted-foreground" />
            </span>
          )}
        </button>
      </div>

      {/* Dropdown panel (portal to avoid overflow clipping) */}
      {open &&
        dropdownPos &&
        createPortal(
          <div
            ref={dropdownRef}
            style={{
              position: "fixed",
              top: dropdownPos.top,
              left: dropdownPos.left,
              width: dropdownWidth,
              maxHeight: "calc(100vh - 16px)",
              overflowY: "auto",
              zIndex: 200,
            }}
            className="rounded-lg bg-popover text-popover-foreground shadow-lg ring-1 ring-foreground/10"
            onClick={(e) => e.stopPropagation()}
            onMouseDown={(e) => {
              e.stopPropagation();
              const t = e.target;
              const isFormControl =
                t instanceof HTMLInputElement ||
                t instanceof HTMLSelectElement ||
                t instanceof HTMLTextAreaElement ||
                t instanceof HTMLOptionElement;
              if (!isFormControl) e.preventDefault();
            }}
          >
            {/* Top: 2 unified input fields (date + time combined) */}
            <div className="flex flex-col gap-2 border-b p-3 sm:flex-row sm:items-center">
              <div className="relative flex-1 min-w-0">
                <CalendarIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground pointer-events-none z-10" />
                <input
                  ref={startInputRef}
                  type="text"
                  value={
                    editingField === "start"
                      ? startText
                      : formatDisplay(displayedStartAt, displayAllDay)
                  }
                  onChange={(e) => {
                    setEditingField("start");
                    setStartText(e.target.value);
                  }}
                  onFocus={() => {
                    setActiveField("start");
                    setStartText(getDisplayText(draftStartAt));
                    setEditingField("start");
                  }}
                  onKeyDown={(e) => handleKeyDown(e, "start")}
                  placeholder="Start Date"
                  className={`w-full h-8 pl-8 pr-2 text-sm rounded-md border bg-background outline-none transition-colors ${
                    activeField === "start"
                      ? "border-primary ring-2 ring-primary/30"
                      : "border-input"
                  }`}
                />
                {editingField === "start" && activeSuggestions.length > 0 && (
                  <div className="absolute left-0 right-0 top-full mt-1 z-20 rounded-md border bg-popover shadow-md overflow-hidden">
                    {activeSuggestions.map((s, i) => (
                      <button
                        key={s}
                        type="button"
                        className={cn(
                          "w-full px-3 py-1.5 text-sm text-left transition-colors",
                          i === suggestIndex
                            ? "bg-accent text-accent-foreground"
                            : "hover:bg-accent/50",
                        )}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          acceptSuggestion(s, "start");
                          startInputRef.current?.focus();
                        }}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <span className="hidden text-sm text-muted-foreground sm:inline">
                →
              </span>
              <div className="relative flex-1 min-w-0">
                <CalendarIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground pointer-events-none z-10" />
                <input
                  ref={endInputRef}
                  type="text"
                  value={
                    editingField === "end"
                      ? endText
                      : formatDisplay(displayedEndAt, displayAllDay)
                  }
                  onChange={(e) => {
                    setEditingField("end");
                    setEndText(e.target.value);
                  }}
                  onFocus={() => {
                    setActiveField("end");
                    setEndText(getDisplayText(draftEndAt));
                    setEditingField("end");
                  }}
                  onKeyDown={(e) => handleKeyDown(e, "end")}
                  placeholder="Due Date"
                  className={`w-full h-8 pl-8 pr-2 text-sm rounded-md border bg-background outline-none transition-colors ${
                    activeField === "end"
                      ? "border-primary ring-2 ring-primary/30"
                      : "border-input"
                  }`}
                />
                {editingField === "end" && activeSuggestions.length > 0 && (
                  <div className="absolute left-0 right-0 top-full mt-1 z-20 rounded-md border bg-popover shadow-md overflow-hidden">
                    {activeSuggestions.map((s, i) => (
                      <button
                        key={s}
                        type="button"
                        className={cn(
                          "w-full px-3 py-1.5 text-sm text-left transition-colors",
                          i === suggestIndex
                            ? "bg-accent text-accent-foreground"
                            : "hover:bg-accent/50",
                        )}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          acceptSuggestion(s, "end");
                          endInputRef.current?.focus();
                        }}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Body: Presets/Recurrence + Calendar */}
            <div className="flex flex-col sm:flex-row">
              {/* Left: Presets or Recurrence panel */}
              {showRecurrence && recurrence ? (
                <div className="border-b sm:border-r sm:border-b-0">
                  <RecurrenceInlinePanel
                    freq={recurrence.freq}
                    interval={recurrence.interval}
                    byDay={recurrence.byDay}
                    triggerStatus={recurrence.triggerStatus}
                    createNew={recurrence.createNew}
                    recurForever={recurrence.recurForever}
                    endCount={recurrence.endCount}
                    endDate={recurrence.endDate}
                    skipWeekend={recurrence.skipWeekend}
                    skipHoliday={recurrence.skipHoliday}
                    saving={recurrence.saving}
                    hasExisting={!!recurrence.recurrenceRule}
                    onFreqChange={recurrence.onFreqChange}
                    onIntervalChange={recurrence.onIntervalChange}
                    onToggleWeekday={recurrence.onToggleWeekday}
                    onTriggerStatusChange={recurrence.onTriggerStatusChange}
                    onCreateNewChange={recurrence.onCreateNewChange}
                    onRecurForeverChange={recurrence.onRecurForeverChange}
                    onEndCountChange={recurrence.onEndCountChange}
                    onEndDateChange={recurrence.onEndDateChange}
                    onSkipWeekendChange={recurrence.onSkipWeekendChange}
                    onSkipHolidayChange={recurrence.onSkipHolidayChange}
                    onSave={() => {
                      recurrence.onSave();
                      setShowRecurrence(false);
                    }}
                    onDelete={() => {
                      recurrence.onDelete();
                      setShowRecurrence(false);
                    }}
                    onBack={() => setShowRecurrence(false)}
                  />
                </div>
              ) : (
                <div className="flex min-w-0 gap-1 overflow-x-auto border-b p-2 sm:min-w-[148px] sm:flex-col sm:gap-0 sm:overflow-visible sm:border-r sm:border-b-0">
                  {presets.map((preset) => (
                    <button
                      key={preset.label}
                      type="button"
                      className="flex shrink-0 items-center justify-between rounded-md px-2.5 py-1.5 text-left text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                      onClick={(e) => {
                        e.stopPropagation();
                        handlePreset(preset);
                      }}
                    >
                      <span>{preset.label}</span>
                      <span className="text-xs text-muted-foreground ml-3">
                        {preset.subLabel}
                      </span>
                    </button>
                  ))}
                  {recurrence && (
                    <>
                      <div className="border-t border-border/60 my-1" />
                      <button
                        type="button"
                        className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground transition-colors text-left"
                        onClick={() => setShowRecurrence(true)}
                      >
                        <Repeat className="size-3.5" />
                        <span>
                          {recurrence.recurrenceRule
                            ? recurrenceLabel(
                                recurrence.freq,
                                recurrence.interval,
                                recurrence.byDay,
                              )
                            : "繰り返しを設定"}
                        </span>
                      </button>
                    </>
                  )}
                </div>
              )}

              {/* Right: Calendar + Time */}
              <div className="flex flex-1 flex-col items-center min-w-0">
                <Calendar
                  className="w-full [--cell-size:--spacing(9)] [&_.rdp-months]:w-full"
                  mode="single"
                  selected={selectedDate}
                  onSelect={handleSelectDate}
                  modifiers={
                    showRecurrence
                      ? {
                          nextOccurrence: nextOccurrence
                            ? [nextOccurrence]
                            : [],
                          futureOccurrence: upcomingOccurrences.slice(1),
                        }
                      : undefined
                  }
                  modifiersClassNames={
                    showRecurrence
                      ? {
                          nextOccurrence:
                            "[&>button]:ring-2 [&>button]:ring-primary [&>button]:ring-inset [&>button]:bg-primary/25 [&>button]:text-foreground",
                          futureOccurrence:
                            "[&>button]:bg-primary/10 [&>button]:text-foreground",
                        }
                      : undefined
                  }
                />
                {showRecurrence && (
                  <div className="px-3 pb-2 pt-1 text-[11px] text-muted-foreground flex items-center gap-3">
                    <span className="inline-flex items-center gap-1">
                      <span className="inline-block size-2.5 rounded-sm ring-2 ring-primary ring-inset bg-primary/25" />
                      次回作成日
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <span className="inline-block size-2.5 rounded-sm bg-primary/10" />
                      以降の予定
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
