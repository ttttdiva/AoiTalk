"use client";

import { BarChart3, Clock3, LineChart } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";
import type { PeriodPreset, ReportsViewMode, ScopeMode } from "./reports-utils";

/** Reports の scope / 期間設定を SharedAppShell の左ナビへ表示する。 */
export function ReportsWorkspaceNavigation({
  scope,
  activeView,
  period,
  customFrom,
  customTo,
  scopeLabel,
  readOnly,
  weekOffset,
  showScheduleFrames,
  canShowScheduleFrames,
  onScopeChange,
  onActiveViewChange,
  onPeriodChange,
  onCustomFromChange,
  onCustomToChange,
  onWeekOffsetChange,
  onShowScheduleFramesChange,
}: {
  scope: ScopeMode;
  activeView: ReportsViewMode;
  period: PeriodPreset;
  customFrom: string;
  customTo: string;
  scopeLabel: string;
  readOnly: boolean;
  weekOffset: number;
  showScheduleFrames: boolean;
  canShowScheduleFrames: boolean;
  onScopeChange: (scope: ScopeMode) => void;
  onActiveViewChange: (view: ReportsViewMode) => void;
  onPeriodChange: (period: PeriodPreset) => void;
  onCustomFromChange: (value: string) => void;
  onCustomToChange: (value: string) => void;
  onWeekOffsetChange: (offset: number) => void;
  onShowScheduleFramesChange: (checked: boolean) => void;
}) {
  const scopeOptions: Array<[ScopeMode, string]> = [
    ["project", "プロジェクト単位"],
    ["space", "スペース単位"],
    ["all", "全表示"],
  ];
  const viewOptions: Array<[ReportsViewMode, string, typeof BarChart3]> = [
    ["summary", "サマリー", BarChart3],
    ["timeline", "タイムライン", LineChart],
  ];
  const periodOptions: Array<[PeriodPreset, string]> = [
    ["this_week", "今週"],
    ["this_month", "今月"],
    ["custom", "カスタム"],
  ];

  return (
    <nav
      className="ao-workspace-nav-panel flex h-full min-h-0 flex-col gap-4 overflow-y-auto px-3 py-4"
      aria-label="レポートワークスペース"
      data-shell-workspace="reports"
      data-shell-region="reports-workspace-navigation"
    >
      <div className="flex items-center gap-2 px-1">
        <span className="grid size-7 shrink-0 place-items-center rounded-md bg-primary/12 text-primary">
          <BarChart3 className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">レポート</p>
          <p className="truncate text-[11px] text-sidebar-foreground/60">
            {scopeLabel}
          </p>
        </div>
      </div>

      {readOnly && (
        <div
          role="status"
          className="rounded-md border border-primary/35 bg-primary/10 px-3 py-2 text-[11px] leading-relaxed text-sidebar-foreground"
        >
          リモートレポート（読み取り専用）
        </div>
      )}

      <section className="space-y-2" aria-labelledby="reports-scope-heading">
        <h2
          id="reports-scope-heading"
          className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/55"
        >
          範囲
        </h2>
        <div className="space-y-0.5" role="group" aria-label="レポートの範囲">
          {scopeOptions.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={scope === value}
              onClick={() => onScopeChange(value)}
              className={cn(
                "flex w-full items-center rounded-md px-2.5 py-2 text-left text-xs transition-colors",
                scope === value
                  ? "bg-sidebar-accent text-sidebar-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </section>

      <section className="space-y-2" aria-labelledby="reports-view-heading">
        <h2
          id="reports-view-heading"
          className="px-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-sidebar-foreground/55"
        >
          ビュー
        </h2>
        <div className="space-y-0.5" role="group" aria-label="レポートのビュー">
          {viewOptions.map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              aria-pressed={activeView === value}
              onClick={() => onActiveViewChange(value)}
              className={cn(
                "relative flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs transition-colors",
                activeView === value
                  ? "bg-sidebar-accent text-primary before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-primary"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              <Icon className="size-3.5" aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
      </section>

      <section className="space-y-2" aria-labelledby="reports-period-heading">
        <h2
          id="reports-period-heading"
          className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/55"
        >
          期間
        </h2>
        <div className="space-y-0.5" role="group" aria-label="レポートの期間">
          {periodOptions.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={period === value}
              onClick={() => onPeriodChange(value)}
              className={cn(
                "flex w-full items-center rounded-md px-2.5 py-2 text-left text-xs transition-colors",
                period === value
                  ? "bg-sidebar-accent text-sidebar-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </section>

      {period === "custom" && (
        <div className="space-y-2 px-1">
          <Input
            type="date"
            value={customFrom}
            onChange={(event) => onCustomFromChange(event.target.value)}
            aria-label="レポート開始日"
            className="h-8 bg-sidebar text-xs"
          />
          <Input
            type="date"
            value={customTo}
            onChange={(event) => onCustomToChange(event.target.value)}
            aria-label="レポート終了日"
            className="h-8 bg-sidebar text-xs"
          />
        </div>
      )}

      {period === "this_week" && (
        <div className="space-y-2 px-1">
          <div className="flex items-center justify-between gap-1">
            <button
              type="button"
              onClick={() => onWeekOffsetChange(weekOffset - 1)}
              className="rounded border border-sidebar-border px-2 py-1 text-[11px] text-sidebar-foreground/75 hover:bg-sidebar-accent"
            >
              前の週
            </button>
            <button
              type="button"
              onClick={() => onWeekOffsetChange(weekOffset + 1)}
              disabled={weekOffset >= 0}
              className="rounded border border-sidebar-border px-2 py-1 text-[11px] text-sidebar-foreground/75 hover:bg-sidebar-accent disabled:pointer-events-none disabled:opacity-50"
            >
              次の週
            </button>
          </div>
          {weekOffset !== 0 && (
            <button
              type="button"
              onClick={() => onWeekOffsetChange(0)}
              className="w-full rounded px-2 py-1 text-[11px] text-sidebar-foreground/70 hover:bg-sidebar-accent"
            >
              今週に戻す
            </button>
          )}
          <label
            className={cn(
              "flex items-center gap-2 text-xs",
              canShowScheduleFrames
                ? "cursor-pointer text-sidebar-foreground/75"
                : "text-sidebar-foreground/40",
            )}
          >
            <Checkbox
              checked={showScheduleFrames}
              disabled={!canShowScheduleFrames}
              onCheckedChange={(checked) =>
                onShowScheduleFramesChange(checked === true)
              }
            />
            予定時間の枠を表示
          </label>
        </div>
      )}

      <div className="mt-auto border-t border-sidebar-border pt-4 text-[11px] leading-relaxed text-sidebar-foreground/50">
        <div className="flex items-center gap-1.5 font-medium text-sidebar-foreground/70">
          <Clock3 className="size-3.5" aria-hidden="true" />
          実績データ
        </div>
        <p className="mt-1">作業時間とタイムエントリを実データから集計します。</p>
      </div>
    </nav>
  );
}
