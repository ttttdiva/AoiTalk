"use client";

import { Eye, RotateCcw } from "lucide-react";

import type { Project, Space } from "@/lib/task-api";
import type { TaskBrowseScope } from "@/lib/task-browse-scope";
import { AppSelect } from "@/components/ui/app-select";
import { cn } from "@/lib/utils";

type CatalogResource = {
  id: string;
  name: string;
  source?: string;
  deleted_at?: string | null;
  metadata?: Record<string, unknown> | null;
};

function isInboxProject(project: Project): boolean {
  const metadata = project.metadata;
  return (
    project.slug === `inbox-project-${project.owner_id ?? project.owner_user_id}` ||
    metadata?.isInboxDefault === true
  );
}

function isInboxSpace(space: Space): boolean {
  return space.slug === `inbox-${space.owner_id}`;
}

function isBrowseableResource(resource: CatalogResource): boolean {
  if (
    !resource.id ||
    !resource.name ||
    resource.source === "remote" ||
    resource.deleted_at
  )
    return false;
  const metadata = resource.metadata;
  return !(
    metadata?.isInboxDefault === true ||
    metadata?.is_inbox === true ||
    metadata?.is_default_inbox === true ||
    metadata?.is_personal === true
  );
}

function scopeValue(scope: TaskBrowseScope | null): string {
  return scope ? `${scope.kind}:${scope.id}` : "";
}

function parseScopeValue(value: string): TaskBrowseScope | null {
  const separator = value.indexOf(":");
  if (separator <= 0 || separator === value.length - 1) return null;
  const kind = value.slice(0, separator);
  if (kind !== "project" && kind !== "space") return null;
  return { kind, id: value.slice(separator + 1) };
}

export type TaskBrowseScopePickerProps = {
  projects?: Project[];
  spaces?: Space[];
  participatingProjects?: Project[];
  participatingSpaces?: Space[];
  browseScope: TaskBrowseScope | null;
  onBrowseScopeChange: (scope: TaskBrowseScope | null) => void;
  disabled?: boolean;
  className?: string;
};

/** Compact chooser for the route-local, read-only browse scope. */
export function TaskBrowseScopePicker({
  projects = [],
  spaces = [],
  participatingProjects = [],
  participatingSpaces = [],
  browseScope,
  onBrowseScopeChange,
  disabled = false,
  className,
}: TaskBrowseScopePickerProps) {
  const participatingProjectIds = new Set(
    participatingProjects.map((project) => project.id),
  );
  const participatingSpaceIds = new Set(
    participatingSpaces.map((space) => space.id),
  );
  const hasParticipationFlags = projects.some(
    (project) => typeof project.is_participating === "boolean",
  );
  const browseableProjects = projects.filter((project) => {
    if (!isBrowseableResource(project as Project & CatalogResource)) return false;
    if (isInboxProject(project)) return false;
    return hasParticipationFlags
      ? project.is_participating === false
      : !participatingProjectIds.has(project.id);
  });
  const browseableSpaces = spaces.filter(
    (space) =>
      isBrowseableResource(space as Space & CatalogResource) &&
      !isInboxSpace(space) &&
      !participatingSpaceIds.has(space.id),
  );
  const selectedValue = scopeValue(browseScope);
  const selectedResource = browseScope
    ? browseScope.kind === "project"
      ? browseableProjects.find((project) => project.id === browseScope.id)
      : browseableSpaces.find((space) => space.id === browseScope.id)
    : null;

  return (
    <div
      className={cn("space-y-1.5", className)}
      data-testid="task-browse-scope-picker"
      data-browse-scope={selectedValue || "normal"}
    >
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-sidebar-foreground/70">
        <Eye className="size-3.5" aria-hidden="true" />
        <span>他のSpace / Projectを参照</span>
      </div>
      <AppSelect
        aria-label="他のSpace / Projectを参照"
        value={selectedValue}
        disabled={disabled}
        onValueChange={(value) => onBrowseScopeChange(parseScopeValue(value))}
        className="h-8 w-full min-w-0 rounded-md border border-sidebar-border bg-sidebar px-2 text-xs text-sidebar-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <option value="">通常の参加範囲</option>
        {browseableSpaces.length > 0 && (
          <optgroup label="Spaceを参照">
            {browseableSpaces.map((space) => (
              <option key={`space:${space.id}`} value={`space:${space.id}`}>
                {space.name}
              </option>
            ))}
          </optgroup>
        )}
        {browseableProjects.length > 0 && (
          <optgroup label="Projectを参照">
            {browseableProjects.map((project) => (
              <option
                key={`project:${project.id}`}
                value={`project:${project.id}`}
              >
                {project.name}
              </option>
            ))}
          </optgroup>
        )}
      </AppSelect>
      {browseScope && (
        <div
          role="status"
          className="rounded-md border border-primary/35 bg-primary/10 px-2.5 py-2 text-[11px] leading-relaxed text-sidebar-foreground"
        >
          <p className="font-medium">
            {selectedResource?.name ?? "選択した範囲"} を参照中
          </p>
          <p className="text-sidebar-foreground/70">明示参照・読み取り専用</p>
          <button
            type="button"
            onClick={() => onBrowseScopeChange(null)}
            disabled={disabled}
            className="mt-1 inline-flex items-center gap-1 rounded px-1.5 py-1 text-[11px] font-medium text-primary hover:bg-primary/10 disabled:pointer-events-none disabled:opacity-50"
          >
            <RotateCcw className="size-3" aria-hidden="true" />
            通常の参加範囲に戻る
          </button>
        </div>
      )}
    </div>
  );
}
