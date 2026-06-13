"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  type ReactNode,
} from "react";
import { taskApi, type Project, type Space } from "@/lib/task-api";

type ProjectContextValue = {
  spaces: Space[];
  selectedSpaceId: string | null;
  selectedSpace: Space | null;
  setSelectedSpaceId: (id: string) => void;
  refreshSpaces: () => Promise<Space[]>;
  projects: Project[];
  allProjects: Project[];
  selectedProjectId: string | null;
  selectedProject: Project | null;
  setSelectedProjectId: (id: string) => void;
  refreshProjects: () => Promise<Project[] | never[]>;
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [allProjects, setAllProjects] = useState<Project[]>([]);
  const [selectedSpaceIdState, setSelectedSpaceIdRaw] = useState<string | null>(
    null,
  );
  const [selectedProjectIdState, setSelectedProjectIdRaw] = useState<
    string | null
  >(null);

  const persistSelectedSpaceId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem("selectedSpaceId", id);
      return;
    }
    localStorage.removeItem("selectedSpaceId");
  }, []);

  const persistSelectedProjectId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem("selectedProjectId", id);
      return;
    }
    localStorage.removeItem("selectedProjectId");
  }, []);

  const getProjectsForSpace = useCallback(
    (spaceId: string | null) => {
      const activeProjects = allProjects.filter((project) => !project.is_completed);
      if (!spaceId) return activeProjects;
      return activeProjects.filter((project) => project.space_id === spaceId);
    },
    [allProjects],
  );

  const setSelectedSpaceId = useCallback(
    (id: string) => {
      setSelectedSpaceIdRaw(id);
      persistSelectedSpaceId(id);

      const scopedProjects = getProjectsForSpace(id);
      if (scopedProjects.some((project) => project.id === selectedProjectIdState)) {
        return;
      }

      const nextProjectId = scopedProjects[0]?.id ?? null;
      setSelectedProjectIdRaw(nextProjectId);
      persistSelectedProjectId(nextProjectId);
    },
    [
      getProjectsForSpace,
      persistSelectedProjectId,
      persistSelectedSpaceId,
      selectedProjectIdState,
    ],
  );

  const setSelectedProjectId = useCallback(
    (id: string) => {
      setSelectedProjectIdRaw(id);
      persistSelectedProjectId(id);

      const project = allProjects.find((item) => item.id === id);
      if (!project?.space_id || project.space_id === selectedSpaceIdState) {
        return;
      }

      setSelectedSpaceIdRaw(project.space_id);
      persistSelectedSpaceId(project.space_id);
    },
    [
      allProjects,
      persistSelectedProjectId,
      persistSelectedSpaceId,
      selectedSpaceIdState,
    ],
  );

  const refreshSpaces = useCallback(async () => {
    try {
      const res = await taskApi.listSpaces();
      setSpaces(res.spaces);
      return res.spaces;
    } catch {
      return [];
    }
  }, []);

  const refreshProjects = useCallback(async () => {
    try {
      const res = await taskApi.listProjects();
      setAllProjects(res.projects);
      if (selectedProjectIdState) {
        const selectedProject = res.projects.find(
          (project) => project.id === selectedProjectIdState,
        );
        if (
          selectedProject?.space_id &&
          selectedProject.space_id !== selectedSpaceIdState
        ) {
          setSelectedSpaceIdRaw(selectedProject.space_id);
          persistSelectedSpaceId(selectedProject.space_id);
        }
      }
      return res.projects;
    } catch {
      return [];
    }
  }, [
    persistSelectedSpaceId,
    selectedProjectIdState,
    selectedSpaceIdState,
  ]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void (async () => {
        const spaceList = await refreshSpaces();
        const projectList = await refreshProjects();

        if (spaceList.length > 0) {
          const savedSpace = localStorage.getItem("selectedSpaceId");
          if (savedSpace && spaceList.some((space) => space.id === savedSpace)) {
            setSelectedSpaceIdRaw(savedSpace);
          } else {
            setSelectedSpaceIdRaw(spaceList[0].id);
            persistSelectedSpaceId(spaceList[0].id);
          }
        }

        if (projectList.length > 0) {
          const savedProject = localStorage.getItem("selectedProjectId");
          let nextProjectId: string | null = null;
          if (
            savedProject &&
            projectList.some((project) => project.id === savedProject)
          ) {
            nextProjectId = savedProject;
          } else {
            nextProjectId = projectList[0].id;
            persistSelectedProjectId(projectList[0].id);
          }

          if (nextProjectId) {
            setSelectedProjectIdRaw(nextProjectId);
            const selectedProject = projectList.find(
              (project) => project.id === nextProjectId,
            );
            if (selectedProject?.space_id) {
              setSelectedSpaceIdRaw(selectedProject.space_id);
              persistSelectedSpaceId(selectedProject.space_id);
            }
          }
        }
      })();
    }, 0);

    return () => clearTimeout(timer);
  }, [
    persistSelectedProjectId,
    persistSelectedSpaceId,
    refreshProjects,
    refreshSpaces,
  ]);

  const selectedSpaceId = useMemo(() => {
    if (spaces.length === 0) return null;
    if (
      selectedSpaceIdState &&
      spaces.some((space) => space.id === selectedSpaceIdState)
    ) {
      return selectedSpaceIdState;
    }
    return spaces[0].id;
  }, [selectedSpaceIdState, spaces]);

  const projects = useMemo(() => {
    const active = allProjects.filter((p) => !p.is_completed);
    if (!selectedSpaceId) return active;
    return active.filter((project) => project.space_id === selectedSpaceId);
  }, [allProjects, selectedSpaceId]);

  const selectedProjectId = useMemo(() => {
    if (projects.length === 0) return null;
    if (
      selectedProjectIdState &&
      projects.some((project) => project.id === selectedProjectIdState)
    ) {
      return selectedProjectIdState;
    }
    return projects[0].id;
  }, [projects, selectedProjectIdState]);

  useEffect(() => {
    function handleSwitchSpace(e: Event) {
      const index = (e as CustomEvent<number>).detail;
      if (index >= 0 && index < spaces.length) {
        setSelectedSpaceId(spaces[index].id);
      }
    }
    window.addEventListener("global-switch-space", handleSwitchSpace);
    return () =>
      window.removeEventListener("global-switch-space", handleSwitchSpace);
  }, [spaces, setSelectedSpaceId]);

  const selectedSpace =
    spaces.find((space) => space.id === selectedSpaceId) ?? null;
  const selectedProject =
    allProjects.find((project) => project.id === selectedProjectId) ?? null;

  return (
    <ProjectContext.Provider
      value={{
        spaces,
        selectedSpaceId,
        selectedSpace,
        setSelectedSpaceId,
        refreshSpaces,
        projects,
        allProjects,
        selectedProjectId,
        selectedProject,
        setSelectedProjectId,
        refreshProjects,
      }}
    >
      {children}
    </ProjectContext.Provider>
  );
}

export function useProject() {
  const ctx = useContext(ProjectContext);
  if (!ctx) {
    throw new Error("useProject must be used within ProjectProvider");
  }
  return ctx;
}
