"use client";

import { FolderKanban, Layers, ListChecks, Tags } from "lucide-react";
import { ProjectDashboard } from "@/components/project-dashboard";

export type SpaceOverviewSpace = {
  id: string;
  name: string;
  description?: string | null;
  color?: string | null;
};

export type SpaceOverviewProject = {
  id: string;
  name: string;
  description?: string | null;
  estimated_hours?: number | null;
  is_completed?: boolean;
  color?: string | null;
};

type SpaceOverviewPanelProps = {
  space: SpaceOverviewSpace;
  projects: SpaceOverviewProject[];
  onSelectProject: (projectId: string) => void;
  onOpenTags: () => void;
};

/**
 * Space dashboard view. The project cards intentionally contain only fields
 * returned by the projects endpoint; health, progress and analytics are not
 * inferred here because those are not part of the Space API contract.
 */
export function SpaceOverviewPanel({
  space,
  projects,
  onSelectProject,
  onOpenTags,
}: SpaceOverviewPanelProps) {
  const activeProjects = projects.filter((project) => !project.is_completed);
  const closedProjects = projects.filter((project) => project.is_completed);

  return (
    <div className="space-y-6 px-1 pb-8">
      <header className="border-b border-border pb-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              <Layers className="size-4 text-primary" />
              Space overview
            </div>
            <h1 className="mt-2 truncate text-2xl font-semibold tracking-tight text-foreground">
              {space.name}
            </h1>
            {space.description ? (
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
                {space.description}
              </p>
            ) : null}
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="size-2 rounded-full bg-primary" aria-hidden="true" />
            {activeProjects.length} active project{activeProjects.length === 1 ? "" : "s"}
          </div>
        </div>
      </header>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div className="min-w-0 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <FolderKanban className="size-4 text-primary" />
              Active projects
            </h2>
            <span className="text-xs text-muted-foreground">
              {activeProjects.length} project{activeProjects.length === 1 ? "" : "s"}
            </span>
          </div>

          {activeProjects.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2">
              {activeProjects.map((project) => (
                <button
                  key={project.id}
                  type="button"
                  onClick={() => onSelectProject(project.id)}
                  className="group min-h-32 rounded-md border border-border bg-card p-4 text-left transition-colors hover:border-primary/60 hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <div className="flex items-start justify-between gap-3">
                    <span className="flex min-w-0 items-center gap-2 text-sm font-semibold">
                      <span
                        className="size-2 shrink-0 rounded-full"
                        style={{ backgroundColor: project.color || "var(--primary)" }}
                        aria-hidden="true"
                      />
                      <span className="truncate">{project.name}</span>
                    </span>
                    <span className="shrink-0 rounded border border-primary/30 px-1.5 py-0.5 text-[11px] text-primary">
                      Active
                    </span>
                  </div>
                  {project.description ? (
                    <p className="mt-3 line-clamp-2 text-sm leading-5 text-muted-foreground">
                      {project.description}
                    </p>
                  ) : (
                    <p className="mt-3 text-sm text-muted-foreground">No description</p>
                  )}
                  {project.estimated_hours != null ? (
                    <div className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
                      <ListChecks className="size-3.5" />
                      Estimate {project.estimated_hours}h
                    </div>
                  ) : null}
                </button>
              ))}
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
              No active projects in this Space.
            </div>
          )}

          {closedProjects.length > 0 ? (
            <details className="rounded-md border border-border bg-card">
              <summary className="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
                Closed projects ({closedProjects.length})
              </summary>
              <div className="grid gap-2 border-t border-border p-3 sm:grid-cols-2">
                {closedProjects.map((project) => (
                  <button
                    key={project.id}
                    type="button"
                    onClick={() => onSelectProject(project.id)}
                    className="rounded border border-border px-3 py-2 text-left text-sm text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
                  >
                    <span className="truncate">{project.name}</span>
                  </button>
                ))}
              </div>
            </details>
          ) : null}
        </div>

        <aside className="space-y-3">
          <section className="rounded-md border border-border bg-card p-4">
            <div className="flex items-center justify-between gap-2">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Tags className="size-4 text-primary" />
                Space tags
              </h2>
              <button
                type="button"
                onClick={onOpenTags}
                className="rounded border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary"
              >
                Manage
              </button>
            </div>
            <p className="mt-3 text-sm leading-5 text-muted-foreground">
              Create, edit, and copy tags from the Tags tab. Tag assignments remain scoped to this Space.
            </p>
          </section>

          <section className="rounded-md border border-border bg-card p-4">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <Layers className="size-4 text-muted-foreground" />
              Space scope
            </h2>
            <dl className="mt-3 space-y-2 text-sm">
              <div className="flex items-center justify-between gap-3 border-b border-border pb-2">
                <dt className="text-muted-foreground">Projects</dt>
                <dd className="tabular-nums text-foreground">{projects.length}</dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt className="text-muted-foreground">Closed</dt>
                <dd className="tabular-nums text-foreground">{closedProjects.length}</dd>
              </div>
            </dl>
          </section>
        </aside>
      </section>

      <section className="border-t border-border pt-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Space dashboard</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Task, time, priority, and member statistics for this Space.
            </p>
          </div>
        </div>
        <ProjectDashboard scope={{ type: "space", id: space.id }} />
      </section>
    </div>
  );
}
