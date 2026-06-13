"use client";

import type React from "react";

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
    <div className="flex min-h-8 items-center border-b">
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
            "shrink-0 px-3 py-1.5 text-sm font-medium border-b-2 transition-colors",
            projectTab === "all"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          全て
          <span className="ml-1.5 text-xs tabular-nums text-muted-foreground">
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
              "shrink-0 px-3 py-1.5 text-sm font-medium border-b-2 transition-colors",
              projectTab === p.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {p.name}
            <span className="ml-1.5 text-xs tabular-nums text-muted-foreground">
              {projectTaskCounts.get(p.id) || 0}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
