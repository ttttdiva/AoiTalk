"use client";

import { useEffect, useState } from "react";
import { FolderOpen, Layers, ListChecks, MoreHorizontal } from "lucide-react";

import type { Project, Space } from "@/lib/task-api";
import { cn } from "@/lib/utils";
import type { TaskViewMode } from "@/components/tasks/hooks/use-task-view-preferences";
import { TaskViewSwitcher } from "@/components/tasks/task-view-switcher";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ResourceColorPicker } from "@/components/projects/resource-color-picker";

type ColorUpdate = (resourceId: string, color: string) => Promise<void>;

function ResourceColorAction({
  resourceLabel,
  color,
  onSave,
  resourceId,
}: {
  resourceLabel: string;
  color?: string | null;
  onSave?: ColorUpdate;
  resourceId: string;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(color || "#3b82f6");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setDraft(color || "#3b82f6");
    setError(null);
  }, [color, open]);

  if (!onSave) return null;

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) setError(null);
      }}
    >
      <PopoverTrigger
        render={
          <button
            type="button"
            aria-label={`${resourceLabel}の色を変更`}
            title={`${resourceLabel}の色を変更`}
            className="relative z-10 grid size-6 shrink-0 place-items-center rounded text-sidebar-foreground/45 opacity-0 transition hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-hover:opacity-100"
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
          >
            <MoreHorizontal className="size-3.5" aria-hidden="true" />
          </button>
        }
      />
      <PopoverContent
        className="w-64 p-3"
        align="end"
        side="right"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="space-y-3">
          <p className="text-xs font-medium">{resourceLabel}の色</p>
          <ResourceColorPicker
            value={draft}
            onChange={setDraft}
            inputClassName="h-7"
            compact
          />
          {error ? (
            <p role="alert" className="text-[11px] text-destructive">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="rounded border border-input px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
              onClick={() => setOpen(false)}
              disabled={saving}
            >
              キャンセル
            </button>
            <button
              type="button"
              className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              disabled={saving}
              onClick={async () => {
                setSaving(true);
                setError(null);
                try {
                  await onSave(resourceId, draft);
                  setOpen(false);
                } catch (saveError) {
                  setError(
                    saveError instanceof Error
                      ? saveError.message
                      : "色の保存に失敗しました",
                  );
                } finally {
                  setSaving(false);
                }
              }}
            >
              {saving ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Tasks の route-scoped workspace navigation。
 *
 * Space/Project は ProjectContext の参加可能リソースを正本として描画し、
 * タスク件数がまだない Project もツリーから消さない。選択状態・mutation は
 * TasksPage が所有し、ナビは描画とイベント転送だけを担当する。
 */
export function TasksWorkspaceNavigation({
  viewMode,
  onViewModeChange,
  spaces,
  projects,
  // Keep the old prop as a source-compatible fallback for route-local callers.
  activeProjects,
  selectedSpaceId,
  onSpaceChange,
  projectTab,
  projectTaskCounts,
  onProjectChange,
  onSpaceColorChange,
  onProjectColorChange,
}: {
  viewMode: TaskViewMode;
  onViewModeChange: (mode: TaskViewMode) => void;
  spaces: Space[];
  projects?: Project[];
  activeProjects?: Project[];
  selectedSpaceId: string | null;
  onSpaceChange: (spaceId: string) => void;
  projectTab: string;
  /** Deprecated: the visible all row was removed, kept for caller compatibility. */
  allCount?: number;
  /** Counts are only rendered when known from the current task response. */
  projectTaskCounts: Map<string, number>;
  onProjectChange: (projectId: string, spaceId?: string | null) => void;
  onSpaceColorChange?: ColorUpdate;
  onProjectColorChange?: ColorUpdate;
}) {
  const treeProjects = (projects ?? activeProjects ?? []).filter(
    (project) => !project.is_completed,
  );

  return (
    <nav
      className="ao-tasks-navigation ao-workspace-nav-panel flex h-full min-h-0 flex-col gap-5 overflow-y-auto px-4 py-4"
      aria-label="タスクワークスペース"
      data-shell-workspace="tasks"
      data-shell-region="tasks-workspace-navigation"
    >
      <div className="flex items-center gap-2 border-b border-sidebar-border/70 px-1 pb-4">
        <span className="grid size-8 shrink-0 place-items-center rounded border border-sidebar-border bg-sidebar-accent text-primary">
          <ListChecks className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold tracking-tight">Tasks</p>
          <p className="truncate text-[11px] text-sidebar-foreground/60">
            List / Schedule
          </p>
        </div>
      </div>

      <section className="space-y-2" aria-labelledby="tasks-view-heading">
        <h2
          id="tasks-view-heading"
          className="px-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-sidebar-foreground/55"
        >
          表示
        </h2>
        <TaskViewSwitcher value={viewMode} onChange={onViewModeChange} />
      </section>

      <section className="min-h-0 space-y-2" aria-labelledby="tasks-scope-heading">
        <h2
          id="tasks-scope-heading"
          className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/55"
        >
          スペースとプロジェクト
        </h2>
        <div
          className="space-y-1"
          role="listbox"
          aria-label="タスクのスペースとプロジェクト"
        >
          {spaces.map((space) => {
            const spaceProjects = treeProjects.filter(
              (project) => project.space_id === space.id,
            );
            const selected = selectedSpaceId === space.id && projectTab === "all";
            const canEdit =
              space.source !== "remote" && space.can_write === true;

            return (
              <div key={space.id} className="group min-w-0">
                <div className="relative flex min-w-0 items-center">
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected}
                    data-testid={`task-space-${space.id}`}
                    onClick={() => onSpaceChange(space.id)}
                    className={cn(
                      "relative flex min-w-0 flex-1 items-center gap-2 rounded px-2.5 py-1.5 pr-8 text-left text-xs transition-colors",
                      selected
                        ? "bg-sidebar-accent text-sidebar-foreground before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-primary"
                        : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                    )}
                  >
                    <Layers
                      className="size-3.5 shrink-0"
                      aria-hidden="true"
                      style={
                        space.color ? { color: space.color } : undefined
                      }
                    />
                    <span className="truncate">
                      {space.source === "remote" ? "[EP] " : ""}
                      {space.name}
                    </span>
                  </button>
                  {canEdit ? (
                    <ResourceColorAction
                      resourceId={space.id}
                      resourceLabel={space.name}
                      color={space.color}
                      onSave={onSpaceColorChange}
                    />
                  ) : null}
                </div>

                {spaceProjects.length > 0 ? (
                  <div className="ml-4 space-y-0.5 border-l border-sidebar-border/60 pl-2">
                    {spaceProjects.map((project) => {
                      const projectSelected = projectTab === project.id;
                      const count = projectTaskCounts.get(project.id);
                      const projectCanEdit =
                        project.source !== "remote" &&
                        project.can_manage_settings === true;

                      return (
                        <div key={project.id} className="group relative flex min-w-0 items-center">
                          <button
                            type="button"
                            role="option"
                            aria-selected={projectSelected}
                            data-testid={`task-project-${project.id}`}
                            onClick={() =>
                              onProjectChange(project.id, project.space_id)
                            }
                            className={cn(
                              "relative flex min-w-0 flex-1 items-center gap-2 rounded px-2.5 py-1.5 pr-8 text-left text-xs transition-colors",
                              projectSelected
                                ? "bg-sidebar-accent text-sidebar-foreground before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-primary"
                                : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                            )}
                          >
                            <FolderOpen
                              className="size-3.5 shrink-0"
                              aria-hidden="true"
                              style={
                                project.color
                                  ? { color: project.color }
                                  : undefined
                              }
                            />
                            <span className="min-w-0 flex-1 truncate">
                              {project.source === "remote" ? "[EP] " : ""}
                              {project.name}
                            </span>
                            {count !== undefined ? (
                              <span className="shrink-0 tabular-nums text-[10px] text-sidebar-foreground/55">
                                {count}
                              </span>
                            ) : null}
                          </button>
                          {projectCanEdit ? (
                            <ResourceColorAction
                              resourceId={project.id}
                              resourceLabel={project.name}
                              color={project.color}
                              onSave={onProjectColorChange}
                            />
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </section>
    </nav>
  );
}
