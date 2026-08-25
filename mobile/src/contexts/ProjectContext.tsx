/**
 * Backwards-compatible Project context shim.
 *
 * Historical callers import `useProject()` / `<ProjectProvider>`. To avoid
 * touching every screen at M1 we keep this module but delegate state to the
 * new zustand store in `src/stores/project.ts`. Subsequent milestones will
 * switch call sites directly to the store.
 */

import React, { useEffect } from "react";
import type { Project, Space } from "../types/api";
import { useProjectStore, type RefreshProjectsOptions } from "../stores/project";
import { useAuth } from "./AuthContext";

interface ProjectContextValue {
  spaces: Space[];
  projects: Project[];
  selectedSpaceId: string | null;
  selectedSpace: Space | null;
  selectedProjectId: string | null;
  selectedProject: Project | null;
  setSelectedSpaceId: (id: string) => void;
  setSelectedProjectId: (id: string | null) => void;
  refreshProjects: (options?: RefreshProjectsOptions) => Promise<void>;
}

export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const { canUseApp, isAuthenticated, isAnonymous, user } = useAuth();
  const refreshProjects = useProjectStore((s) => s.refreshProjects);
  const resetProjects = useProjectStore((s) => s.reset);
  const authScope = isAuthenticated
    ? `auth:${user?.user_id ?? "unknown"}`
    : isAnonymous
      ? "anonymous"
      : "signed_out";

  useEffect(() => {
    let cancelled = false;

    const loadProjects = async () => {
      resetProjects();
      // The project shell is local-first. Remote sync is owned by the root
      // scheduler so it cannot become a mount/auth-scope completion gate.
      await refreshProjects({ localOnly: true });
      if (cancelled) return;
    };

    if (canUseApp) {
      void loadProjects();
    } else {
      resetProjects();
    }

    return () => {
      cancelled = true;
    };
  }, [authScope, canUseApp, refreshProjects, resetProjects]);

  return <>{children}</>;
}

export function useProject(): ProjectContextValue {
  const spaces = useProjectStore((s) => s.spaces);
  const projects = useProjectStore((s) => s.projects);
  const selectedSpaceId = useProjectStore((s) => s.selectedSpaceId);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);
  const setSelectedSpaceId = useProjectStore((s) => s.setSelectedSpaceId);
  const setSelectedProjectId = useProjectStore((s) => s.setSelectedProjectId);
  const refreshProjects = useProjectStore((s) => s.refreshProjects);

  const selectedSpace =
    (selectedSpaceId && spaces.find((s) => s.id === selectedSpaceId)) || null;
  const selectedProject =
    (selectedProjectId && projects.find((p) => p.id === selectedProjectId)) ||
    null;

  return {
    spaces,
    projects,
    selectedSpaceId,
    selectedSpace,
    selectedProjectId,
    selectedProject,
    setSelectedSpaceId,
    setSelectedProjectId,
    refreshProjects,
  };
}
