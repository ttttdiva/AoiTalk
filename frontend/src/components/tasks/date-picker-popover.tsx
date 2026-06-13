"use client";

import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Calendar } from "@/components/ui/calendar";
import { Input } from "@/components/ui/input";
import { CalendarIcon, X, Clock } from "lucide-react";
import { parseLocalDateTime } from "@/lib/date-time";
import { formatTaskDateLabel } from "@/lib/task-date-label";

function pad(n: number) {
  return n.toString().padStart(2, "0");
}

function formatDateTimeLocal(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function parseDateValue(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = parseLocalDateTime(value) ?? new Date(value);
  return isNaN(date.getTime()) ? null : date;
}

function formatDisplayDate(value: string | null, allDay = false): string {
  return formatTaskDateLabel(value, {
    allDay,
    absoluteStyle: "long",
  });
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
  const lower = raw
    .toLowerCase()
    .trim()
    .replace(/,\s*/g, " ")
    .replace(/\s+/g, " ");
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
    const d = new Date(now.getFullYear(), now.getMonth() + 1, 1, 0, 0, 0);
    return formatDateTimeLocal(d);
  }

  // "today 16:00" パターン
  const todayTimeMatch = lower.match(
    /^(?:today|tod)\s+(\d{1,2}):(\d{2})$/,
  );
  if (todayTimeMatch) {
    const d = new Date(now);
    d.setHours(parseInt(todayTimeMatch[1]), parseInt(todayTimeMatch[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  // "tomo 16:00" パターン
  const tomoTimeMatch = lower.match(
    /^(?:tomorrow|tomo|tom)\s+(\d{1,2}):(\d{2})$/,
  );
  if (tomoTimeMatch) {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(parseInt(tomoTimeMatch[1]), parseInt(tomoTimeMatch[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  // "monday" / "friday 13:00" / "next monday 09:30" パターン
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
      weekdayMatch[3] ? parseInt(weekdayMatch[3]) : 0,
      weekdayMatch[4] ? parseInt(weekdayMatch[4]) : 0,
      0,
      0,
    );
    return formatDateTimeLocal(d);
  }

  // "3/25" or "3/25 10:00" パターン
  const dateMatch = lower.match(
    /^(\d{1,2})\/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$/,
  );
  if (dateMatch) {
    const month = parseInt(dateMatch[1]) - 1;
    const day = parseInt(dateMatch[2]);
    const hour = dateMatch[3] ? parseInt(dateMatch[3]) : 0;
    const min = dateMatch[4] ? parseInt(dateMatch[4]) : 0;
    const d = new Date(now.getFullYear(), month, day, hour, min, 0);
    return formatDateTimeLocal(d);
  }

  // "16:00" only applies to today.
  const timeOnly = lower.match(/^(\d{1,2}):(\d{2})$/);
  if (timeOnly) {
    const d = new Date(now);
    d.setHours(parseInt(timeOnly[1]), parseInt(timeOnly[2]), 0, 0);
    return formatDateTimeLocal(d);
  }

  // フリーフォームの Date.parse を試行
  const parsed = parseDateValue(raw.trim());
  if (parsed) {
    return formatDateTimeLocal(parsed);
  }

  return null;
}

interface Preset {
  label: string;
  subLabel: string;
  getDate: () => Date;
}

function getPresets(): Preset[] {
  const now = new Date();
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  const today = new Date(now);
  today.setHours(0, 0, 0, 0);

  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(0, 0, 0, 0);

  const thisWeekend = new Date(now);
  const daysUntilSat = (6 - now.getDay() + 7) % 7 || 7;
  thisWeekend.setDate(thisWeekend.getDate() + daysUntilSat);
  thisWeekend.setHours(0, 0, 0, 0);

  const nextMonday = new Date(now);
  const daysUntilMon = now.getDay() === 0 ? 1 : 8 - now.getDay();
  nextMonday.setDate(nextMonday.getDate() + daysUntilMon);
  nextMonday.setHours(0, 0, 0, 0);

  const nextWeekend = new Date(now);
  const daysUntilNextSat = daysUntilSat < 7 ? daysUntilSat + 7 : daysUntilSat;
  nextWeekend.setDate(nextWeekend.getDate() + daysUntilNextSat);
  nextWeekend.setHours(0, 0, 0, 0);

  const twoWeeks = new Date(now);
  twoWeeks.setDate(twoWeeks.getDate() + 14);
  twoWeeks.setHours(0, 0, 0, 0);

  const fourWeeks = new Date(now);
  fourWeeks.setDate(fourWeeks.getDate() + 28);
  fourWeeks.setHours(0, 0, 0, 0);

  const fmtShort = (d: Date) => `${d.getMonth() + 1}/${d.getDate()}`;

  return [
    {
      label: "Today",
      subLabel: dayNames[today.getDay()],
      getDate: () => new Date(today),
    },
    {
      label: "Tomorrow",
      subLabel: dayNames[tomorrow.getDay()],
      getDate: () => new Date(tomorrow),
    },
    {
      label: "This weekend",
      subLabel: dayNames[thisWeekend.getDay()],
      getDate: () => new Date(thisWeekend),
    },
    {
      label: "Next week",
      subLabel: `${fmtShort(nextMonday)}`,
      getDate: () => new Date(nextMonday),
    },
    {
      label: "Next weekend",
      subLabel: `${fmtShort(nextWeekend)}`,
      getDate: () => new Date(nextWeekend),
    },
    {
      label: "In 2 weeks",
      subLabel: `${fmtShort(twoWeeks)}`,
      getDate: () => new Date(twoWeeks),
    },
    {
      label: "In 4 weeks",
      subLabel: `${fmtShort(fourWeeks)}`,
      getDate: () => new Date(fourWeeks),
    },
  ];
}

interface DatePickerPopoverProps {
  value: string | null;
  onChange?: (value: string | null) => void;
  label?: string;
  placeholder?: string;
  /** 値が確定された（プリセットクリック、Enter、カレンダー選択）時に値付きで呼ばれるコールバック */
  onCommit?: (value: string | null) => void;
  /** 外部からinputにrefを持たせたい場合 */
  inputId?: string;
  /** ボタンに適用するクラス名（インライン表示モード用） */
  buttonClassName?: string;
  /** 日付だけ/終日タスクの場合は時刻を表示しない */
  allDay?: boolean;
}

export function DatePickerPopover({
  value,
  onChange,
  label,
  placeholder,
  onCommit,
  inputId,
  buttonClassName,
  allDay,
}: DatePickerPopoverProps) {
  const [open, setOpen] = useState(false);
  const [textInput, setTextInput] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // テキスト入力を値と同期（編集中でない時のみ）
  useEffect(() => {
    if (!isEditing) {
      return;
    }
  }, [value, isEditing]);

  // クリック外で閉じる
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
        setIsEditing(false);
        setTextInput(formatDisplayDate(value, Boolean(allDay)));
      }
    };
    // 遅延して追加（開いた直後のクリックで閉じないように）
    const timer = setTimeout(() => {
      document.addEventListener("mousedown", handleClickOutside);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [allDay, open, value]);

  const selectedDate = useMemo(() => {
    return parseDateValue(value) ?? undefined;
  }, [value]);

  const timeValue = useMemo(() => {
    const d = parseDateValue(value);
    if (!d) return "00:00";
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }, [value]);

  const presets = useMemo(() => getPresets(), []);

  const handleSelectDate = useCallback(
    (date: Date | undefined) => {
      if (!date) return;
      const currentTime = parseDateValue(value);
      const hh = currentTime ? currentTime.getHours() : 0;
      const mm = currentTime ? currentTime.getMinutes() : 0;
      date.setHours(hh, mm, 0, 0);
      const v = formatDateTimeLocal(date);
      onChange?.(v);
      onCommit?.(v);
      setOpen(false);
      setIsEditing(false);
    },
    [value, onChange, onCommit],
  );

  const handlePreset = useCallback(
    (preset: Preset) => {
      const d = preset.getDate();
      if (value) {
        const existing = parseDateValue(value);
        if (existing) {
          d.setHours(existing.getHours(), existing.getMinutes(), 0, 0);
        }
      }
      const v = formatDateTimeLocal(d);
      onChange?.(v);
      onCommit?.(v);
      setIsEditing(false);
      setOpen(false);
    },
    [value, onChange, onCommit],
  );

  const handleTimeChange = useCallback(
    (newTime: string) => {
      if (!newTime) return;
      const [hh, mm] = newTime.split(":").map(Number);
      const base = selectedDate ? new Date(selectedDate) : new Date();
      if (!selectedDate) {
        base.setHours(0, 0, 0, 0);
      }
      base.setHours(hh, mm, 0, 0);
      const v = formatDateTimeLocal(base);
      onChange?.(v);
      onCommit?.(v);
    },
    [selectedDate, onChange, onCommit],
  );

  const handleTextInputKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const trimmed = textInput.trim();
        const parsed = trimmed ? parseFlexibleDate(trimmed) : null;
        if (parsed) {
          onChange?.(parsed);
          onCommit?.(parsed);
          setTextInput(formatDisplayDate(parsed, Boolean(allDay)));
        }
        setIsEditing(false);
        setOpen(false);
      }
      if (e.key === "Escape") {
        setIsEditing(false);
        setTextInput(formatDisplayDate(value, Boolean(allDay)));
        setOpen(false);
      }
    },
    [allDay, textInput, value, onChange, onCommit],
  );

  const handleClear = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      e.preventDefault();
      onChange?.(null);
      onCommit?.(null);
      setTextInput("");
      setIsEditing(false);
    },
    [onChange, onCommit],
  );

  const handleInputFocus = useCallback(() => {
    setTextInput(formatDisplayDate(value, Boolean(allDay)));
    setIsEditing(true);
    setOpen(true);
  }, [allDay, value]);

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setIsEditing(true);
      setTextInput(e.target.value);
    },
    [],
  );

  const isButtonMode = buttonClassName !== undefined;

  return (
    <div ref={containerRef} className="relative w-full">
      {/* トリガー */}
      {isButtonMode ? (
        <button
          type="button"
          className={`flex items-center gap-1 text-xs hover:underline cursor-pointer ${buttonClassName || "text-muted-foreground"}`}
          onClick={() => {
            setOpen(!open);
            if (!open) handleInputFocus();
          }}
        >
          <CalendarIcon className="size-3 shrink-0" />
          <span className="truncate">
            {value
              ? formatDisplayDate(value, Boolean(allDay))
              : placeholder || label || ""}
          </span>
          {value && (
            <span
              role="button"
              className="shrink-0 p-0.5 rounded-sm hover:bg-accent"
              onClick={handleClear}
            >
              <X className="size-3 text-muted-foreground" />
            </span>
          )}
        </button>
      ) : (
        <div className="relative w-full">
          <CalendarIcon className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground pointer-events-none" />
          <input
            ref={inputRef}
            id={inputId}
            type="text"
            value={
              isEditing ? textInput : formatDisplayDate(value, Boolean(allDay))
            }
            onChange={handleInputChange}
            onKeyDown={handleTextInputKeyDown}
            onFocus={handleInputFocus}
            placeholder={placeholder || label}
            className="w-full h-9 pl-9 pr-8 text-sm rounded-md border border-input bg-background ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          />
          {value && (
            <button
              type="button"
              onClick={handleClear}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded-sm hover:bg-accent"
            >
              <X className="size-3.5 text-muted-foreground hover:text-foreground" />
            </button>
          )}
        </div>
      )}

      {/* ドロップダウン */}
      {open && (
        <div
          ref={dropdownRef}
          className="absolute z-[100] mt-1 left-0 rounded-lg bg-popover text-popover-foreground shadow-lg ring-1 ring-foreground/10"
          onMouseDown={(e) => {
            e.stopPropagation();
            // ドロップダウン内のクリックでinputのblurを防止
            if (!(e.target instanceof HTMLInputElement)) {
              e.preventDefault();
            }
          }}
        >
          <div className="flex flex-row">
            {/* 左パネル: プリセット */}
            <div className="flex flex-col border-r p-2 min-w-[150px]">
              {presets.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground transition-colors text-left"
                  onClick={() => handlePreset(preset)}
                >
                  <span>{preset.label}</span>
                  <span className="text-xs text-muted-foreground ml-3">
                    {preset.subLabel}
                  </span>
                </button>
              ))}
            </div>

            {/* 右パネル: カレンダー + 時刻 */}
            <div className="flex flex-col">
              <Calendar
                mode="single"
                selected={selectedDate}
                onSelect={handleSelectDate}
              />
              {/* 時刻入力 */}
              <div className="border-t px-3 py-2 flex items-center gap-2">
                <Clock className="size-3.5 text-muted-foreground" />
                <label className="text-xs text-muted-foreground shrink-0">
                  時刻
                </label>
                <Input
                  type="time"
                  value={timeValue}
                  onChange={(e) => handleTimeChange(e.target.value)}
                  className="h-7 text-sm w-auto"
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
