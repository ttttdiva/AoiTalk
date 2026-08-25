"use client";

import type React from "react";

import { FolderOpen } from "lucide-react";

import type { Project } from "@/lib/task-api";
import { cn } from "@/lib/utils";

/**
 * タスク一覧のプロジェクト横タブ。
 */
export function TaskProjectTabs({
  projectTab,
  activeProjects,
  allCount,
  projectTaskCounts,
  projectTabRefs,
  onSelectTab,
}: {
  projectTab: string;
  activeProjects: Project[];
  allCount: number;
  projectTaskCounts: Map<string, number>;
  projectTabRefs: React.RefObject<Record<string, HTMLButtonElement | null>>;
  onSelectTab: (tab: string) => void;
}) {
  return (
    <div className="flex min-h-9 items-center border-b border-border">
      <div
        data-testid="task-project-tabs"
        className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto overflow-y-hidden whitespace-nowrap [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        <button
          ref={(node) => {
            // projectTabRefs は ref オブジェクトの prop なので current への代入は正当
            // eslint-disable-next-line react-hooks/immutability
            projectTabRefs.current.all = node;
          }}
          onClick={() => onSelectTab("all")}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors",
            projectTab === "all"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          <FolderOpen
            className="size-3.5 shrink-0"
            aria-hidden="true"
          />
          全て
          <span className="text-xs tabular-nums text-muted-foreground">
            {allCount}
          </span>
        </button>
        {activeProjects.map((p) => (
          <button
            key={p.id}
            ref={(node) => {
              projectTabRefs.current[p.id] = node;
            }}
            onClick={() => onSelectTab(p.id)}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors",
              projectTab === p.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <FolderOpen
              className="size-3.5 shrink-0"
              aria-hidden="true"
              style={p.color ? { color: p.color } : undefined}
            />
            {p.name}
            <span className="text-xs tabular-nums text-muted-foreground">
              {projectTaskCounts.get(p.id) || 0}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
