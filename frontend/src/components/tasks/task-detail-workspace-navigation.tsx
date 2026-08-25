"use client";

import { ArrowLeft, CheckSquare } from "lucide-react";

export function TaskDetailWorkspaceNavigation({
  title,
  status,
  onBack,
}: {
  title: string;
  status?: string | null;
  onBack: () => void;
}) {
  return (
    <nav
      className="ao-workspace-nav-panel flex h-full min-h-0 flex-col gap-4 overflow-y-auto px-3 py-4"
      aria-label="タスク詳細ワークスペース"
      data-shell-workspace="task-detail"
      data-shell-region="task-detail-workspace-navigation"
    >
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground"
      >
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        タスク一覧
      </button>
      <div className="flex items-start gap-2 px-1">
        <span className="grid size-7 shrink-0 place-items-center rounded-md bg-primary/12 text-primary">
          <CheckSquare className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="break-words text-sm font-semibold leading-5">
            {title || "タスク"}
          </p>
          {status && (
            <p className="mt-1 text-[11px] text-sidebar-foreground/60">
              {status}
            </p>
          )}
        </div>
      </div>
    </nav>
  );
}
