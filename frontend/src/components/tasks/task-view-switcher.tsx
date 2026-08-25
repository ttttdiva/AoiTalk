"use client";

import { CalendarRange, List } from "lucide-react";

import type { TaskViewMode } from "@/components/tasks/hooks/use-task-view-preferences";
import { cn } from "@/lib/utils";

const VIEW_OPTIONS: Array<{
  mode: TaskViewMode;
  label: string;
  icon: typeof List;
}> = [
  { mode: "list", label: "リスト", icon: List },
  { mode: "schedule", label: "スケジュール", icon: CalendarRange },
];

export function TaskViewSwitcher({
  value,
  onChange,
}: {
  value: TaskViewMode;
  onChange: (mode: TaskViewMode) => void;
}) {
  return (
    <div className="overflow-x-auto" data-testid="task-view-switcher">
      <div
        role="group"
        aria-label="タスクの表示形式"
        className="inline-flex min-w-max items-center gap-0.5 rounded border border-sidebar-border bg-sidebar-accent/40 p-0.5"
      >
        {VIEW_OPTIONS.map(({ mode, label, icon: Icon }) => {
          const selected = value === mode;
          return (
            <button
              key={mode}
              type="button"
              data-testid={`task-view-${mode}`}
              aria-pressed={selected}
              onClick={() => onChange(mode)}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded px-2.5 text-xs font-medium text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring",
                selected && "bg-background text-foreground shadow-none",
              )}
            >
              <Icon className="size-3.5" aria-hidden="true" />
              <span>{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
