"use client";

import { useMemo } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, Check, FileText } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";

export type CalendarWorkspaceScope = "project" | "space" | "all";

type MiniCalendarDay = {
  date: Date;
  key: string;
  inMonth: boolean;
};

function formatDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseDate(value: string | null | undefined): Date {
  if (value) {
    const parsed = new Date(`${value.slice(0, 10)}T00:00:00`);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return today;
}

function buildMiniCalendarDays(anchor: Date): MiniCalendarDay[] {
  const monthStart = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const gridStart = new Date(monthStart);
  gridStart.setDate(monthStart.getDate() - monthStart.getDay());

  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart);
    date.setDate(gridStart.getDate() + index);
    return {
      date,
      key: formatDateKey(date),
      inMonth: date.getMonth() === anchor.getMonth(),
    };
  });
}

/** Calendar のローカルスコープ、ミニカレンダー、既存フィルターを表示するナビ。 */
export function CalendarWorkspaceNavigation({
  scope,
  scopeLabel,
  readOnly,
  showDocsLayer,
  hideRecurring,
  showClosed,
  currentDate,
  onDateChange,
  onPreviousMonth,
  onNextMonth,
  onScopeChange,
  onShowDocsLayerChange,
  onHideRecurringChange,
  onShowClosedChange,
}: {
  scope: CalendarWorkspaceScope;
  scopeLabel: string;
  readOnly: boolean;
  showDocsLayer: boolean;
  hideRecurring: boolean;
  showClosed: boolean;
  currentDate?: string | null;
  onDateChange?: (date: string) => void;
  onPreviousMonth?: () => void;
  onNextMonth?: () => void;
  onScopeChange: (scope: CalendarWorkspaceScope) => void;
  onShowDocsLayerChange: (checked: boolean) => void;
  onHideRecurringChange: (checked: boolean) => void;
  onShowClosedChange: (checked: boolean) => void;
}) {
  const anchor = useMemo(() => parseDate(currentDate), [currentDate]);
  const days = useMemo(() => buildMiniCalendarDays(anchor), [anchor]);
  const todayKey = formatDateKey(new Date());
  const selectedKey = currentDate?.slice(0, 10) ?? todayKey;
  const monthLabel = anchor.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
  });
  const weekLabels = ["S", "M", "T", "W", "T", "F", "S"];

  return (
    <nav
      className="ao-workspace-nav-panel ao-calendar-navigation"
      aria-label="Calendar workspace"
      data-shell-workspace="calendar"
      data-shell-region="calendar-workspace-navigation"
    >
      <section className="ao-calendar-mini" aria-label="Mini calendar">
        <div className="ao-calendar-mini-header">
          <div>
            <p className="ao-calendar-nav-kicker">Calendar</p>
            <h1 className="ao-calendar-mini-title">{monthLabel}</h1>
          </div>
          <div className="ao-calendar-mini-actions">
            <button
              type="button"
              className="ao-calendar-icon-button"
              aria-label="Previous month"
              title="Previous month"
              onClick={onPreviousMonth}
            >
              <ChevronLeft className="size-4" aria-hidden="true" />
            </button>
            <button
              type="button"
              className="ao-calendar-icon-button"
              aria-label="Next month"
              title="Next month"
              onClick={onNextMonth}
            >
              <ChevronRight className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className="ao-calendar-mini-weekdays" aria-hidden="true">
          {weekLabels.map((label, index) => (
            <span key={`${label}-${index}`}>{label}</span>
          ))}
        </div>
        <div className="ao-calendar-mini-grid">
          {days.map(({ date, key, inMonth }) => {
            const isSelected = key === selectedKey;
            const isToday = key === todayKey;
            return (
              <button
                key={key}
                type="button"
                className={cn(
                  "ao-calendar-mini-day",
                  !inMonth && "is-outside",
                  isToday && "is-today",
                  isSelected && "is-selected",
                )}
                aria-label={date.toLocaleDateString("en-US", {
                  year: "numeric",
                  month: "long",
                  day: "numeric",
                })}
                aria-current={isToday ? "date" : undefined}
                onClick={() => onDateChange?.(key)}
              >
                {date.getDate()}
              </button>
            );
          })}
        </div>
      </section>

      <section className="ao-calendar-nav-section" aria-labelledby="calendar-scope-heading">
        <h2 id="calendar-scope-heading" className="ao-calendar-nav-heading">
          Scope
        </h2>
        <p className="ao-calendar-scope-label" title={scopeLabel}>
          {scopeLabel}
        </p>
        <div className="ao-calendar-scope-options" role="group" aria-label="Calendar scope">
          {(
            [
              ["project", "Project"],
              ["space", "Space"],
              ["all", "All"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={scope === value}
              onClick={() => onScopeChange(value)}
              className={cn("ao-calendar-scope-option", scope === value && "is-active")}
            >
              <span className="ao-calendar-radio" aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
      </section>

      <section className="ao-calendar-nav-section" aria-labelledby="calendar-filter-heading">
        <h2 id="calendar-filter-heading" className="ao-calendar-nav-heading">
          Filters
        </h2>
        <div className="ao-calendar-filter-options">
          <label className="ao-calendar-filter-option">
            <Checkbox
              checked={showDocsLayer}
              onCheckedChange={(checked) => onShowDocsLayerChange(checked === true)}
            />
            <FileText className="size-3.5 text-sky-300/80" aria-hidden="true" />
            <span>Docs Items</span>
          </label>
          <label className="ao-calendar-filter-option">
            <Checkbox
              checked={hideRecurring}
              onCheckedChange={(checked) => onHideRecurringChange(checked === true)}
            />
            <CalendarDays className="size-3.5 text-primary/85" aria-hidden="true" />
            <span>Hide Recurring</span>
          </label>
          <label className="ao-calendar-filter-option">
            <Checkbox
              checked={showClosed}
              onCheckedChange={(checked) => onShowClosedChange(checked === true)}
            />
            <Check className="size-3.5 text-sidebar-foreground/55" aria-hidden="true" />
            <span>Show Completed</span>
          </label>
        </div>
      </section>

      {readOnly && (
        <div role="status" className="ao-calendar-readonly-note">
          Remote data (read-only)
        </div>
      )}

      <p className="ao-calendar-nav-hint">
        Click a date cell or time slot to create a task in this scope.
      </p>

    </nav>
  );
}
