"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { taskApi, type Project, type Space } from "@/lib/task-api";
import {
  listRemoteProjects,
  listRemoteServers,
  listRemoteSpaces,
  type RemoteServerProfile,
} from "@/lib/remote-servers";
import {
  decorateRemoteProject,
  decorateRemoteSpace,
  localResourceKey,
} from "@/lib/remote-resource";
import { useUserSettings } from "@/contexts/user-settings-context";
import { getRemoteServerConnectionEnabled } from "@/lib/user-settings";

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
  projectsLoading: boolean;
  projectsLoadError: string | null;
  initialLoadComplete: boolean;
  remoteErrors: string[];
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const { settings } = useUserSettings();
  const remoteConnectionEnabled = getRemoteServerConnectionEnabled(settings);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [allProjects, setAllProjects] = useState<Project[]>([]);
  const [selectedSpaceIdState, setSelectedSpaceIdRaw] = useState<string | null>(
    null,
  );
  const [selectedProjectIdState, setSelectedProjectIdRaw] = useState<
    string | null
  >(null);
  const [remoteErrors, setRemoteErrors] = useState<string[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsLoadError, setProjectsLoadError] = useState<string | null>(
    null,
  );
  const [initialLoadComplete, setInitialLoadComplete] = useState(false);
  const remoteConnectionEnabledRef = useRef(remoteConnectionEnabled);

  const visibleSpaces = useMemo(
    () =>
      remoteConnectionEnabled
        ? spaces
        : spaces.filter((space) => space.source !== "remote"),
    [remoteConnectionEnabled, spaces],
  );
  const visibleAllProjects = useMemo(
    () =>
      remoteConnectionEnabled
        ? allProjects
        : allProjects.filter((project) => project.source !== "remote"),
    [allProjects, remoteConnectionEnabled],
  );

  useEffect(() => {
    remoteConnectionEnabledRef.current = remoteConnectionEnabled;
  }, [remoteConnectionEnabled]);

  const persistSelectedSpaceId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem(
        "selectedSpaceId",
        id.startsWith("remote:") ? id : localResourceKey(id),
      );
      return;
    }
    localStorage.removeItem("selectedSpaceId");
  }, []);

  const persistSelectedProjectId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem(
        "selectedProjectId",
        id.startsWith("remote:") ? id : localResourceKey(id),
      );
      return;
    }
    localStorage.removeItem("selectedProjectId");
  }, []);

  const getProjectsForSpace = useCallback(
    (spaceId: string | null) => {
      const activeProjects = visibleAllProjects.filter(
        (project) => !project.is_completed,
      );
      if (!spaceId) return activeProjects;
      return activeProjects.filter((project) => project.space_id === spaceId);
    },
    [visibleAllProjects],
  );

  const setSelectedSpaceId = useCallback(
    (id: string) => {
      setSelectedSpaceIdRaw(id);
      persistSelectedSpaceId(id);

      const scopedProjects = getProjectsForSpace(id);
      if (
        scopedProjects.some((project) => project.id === selectedProjectIdState)
      ) {
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

      const project = visibleAllProjects.find((item) => item.id === id);
      if (!project?.space_id || project.space_id === selectedSpaceIdState) {
        return;
      }

      setSelectedSpaceIdRaw(project.space_id);
      persistSelectedSpaceId(project.space_id);
    },
    [
      persistSelectedProjectId,
      persistSelectedSpaceId,
      selectedSpaceIdState,
      visibleAllProjects,
    ],
  );

  const refreshSpaces = useCallback(async () => {
    try {
      const res = await taskApi.listSpaces();
      const localSpaces = res.spaces.map((space) => ({
        ...space,
        source: "local" as const,
        resource_id: space.id,
      }));
      const errors: string[] = [];
      let profiles: RemoteServerProfile[] = [];
      if (remoteConnectionEnabled) {
        try {
          profiles = (await listRemoteServers()).filter(
            (profile) => profile.enabled,
          );
        } catch (error) {
          errors.push(
            error instanceof Error
              ? error.message
              : "リモート接続先を取得できません",
          );
        }
      }
      const remoteResults = await Promise.all(
        profiles.map(async (profile) => {
          try {
            const remoteSpaces = await listRemoteSpaces(profile.id);
            return remoteSpaces.map((space) =>
              decorateRemoteSpace(
                profile.id,
                profile.name,
                profile.display_color,
                profile.base_url,
                space,
              ),
            );
          } catch (error) {
            errors.push(
              `${profile.name}: ${error instanceof Error ? error.message : "接続失敗"}`,
            );
            return [] as Space[];
          }
        }),
      );
      if (!remoteConnectionEnabledRef.current) {
        setRemoteErrors([]);
        setSpaces(localSpaces);
        return localSpaces;
      }
      setRemoteErrors(remoteConnectionEnabled ? errors : []);
      const merged = [localSpaces, ...remoteResults].flat();
      setSpaces(merged);
      return merged;
    } catch {
      return [];
    }
  }, [remoteConnectionEnabled]);

  const refreshProjects = useCallback(async () => {
    setProjectsLoading(true);
    setProjectsLoadError(null);
    try {
      const res = await taskApi.listProjects();
      const localProjects = res.projects.map((project) => ({
        ...project,
        source: "local" as const,
        resource_id: project.id,
      }));
      const errors: string[] = [];
      let profiles: RemoteServerProfile[] = [];
      if (remoteConnectionEnabled) {
        try {
          profiles = (await listRemoteServers()).filter(
            (profile) => profile.enabled,
          );
        } catch (error) {
          errors.push(
            error instanceof Error
              ? error.message
              : "リモート接続先を取得できません",
          );
        }
      }
      const remoteResults = await Promise.all(
        profiles.map(async (profile) => {
          try {
            const remoteProjects = await listRemoteProjects(profile.id);
            return remoteProjects.map((project) =>
              decorateRemoteProject(
                profile.id,
                profile.name,
                profile.display_color,
                profile.base_url,
                project,
              ),
            );
          } catch (error) {
            errors.push(
              `${profile.name}: ${error instanceof Error ? error.message : "接続失敗"}`,
            );
            return [] as Project[];
          }
        }),
      );
      if (!remoteConnectionEnabledRef.current) {
        setRemoteErrors([]);
        setAllProjects(localProjects);
        return localProjects;
      }
      setRemoteErrors((previous) =>
        remoteConnectionEnabled
          ? errors.length > 0
            ? Array.from(new Set([...previous, ...errors]))
            : previous
          : [],
      );
      const merged = [localProjects, ...remoteResults].flat();
      setAllProjects(merged);
      if (selectedProjectIdState) {
        const selectedProject = merged.find(
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
      return merged;
    } catch (error) {
      setProjectsLoadError(
        error instanceof Error
          ? error.message
          : "プロジェクトを取得できませんでした",
      );
      return [];
    } finally {
      setProjectsLoading(false);
    }
  }, [
    persistSelectedSpaceId,
    selectedProjectIdState,
    selectedSpaceIdState,
    remoteConnectionEnabled,
  ]);

  useEffect(() => {
    if (remoteConnectionEnabled) return;
    if (selectedSpaceIdState?.startsWith("remote:")) {
      persistSelectedSpaceId(null);
    }
    if (selectedProjectIdState?.startsWith("remote:")) {
      persistSelectedProjectId(null);
    }
  }, [
    persistSelectedProjectId,
    persistSelectedSpaceId,
    remoteConnectionEnabled,
    selectedProjectIdState,
    selectedSpaceIdState,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setRemoteErrors([]);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [remoteConnectionEnabled]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void (async () => {
        const spaceList = await refreshSpaces();
        const projectList = await refreshProjects();

        if (spaceList.length > 0) {
          const savedSpace = localStorage.getItem("selectedSpaceId");
          const savedSpaceId = savedSpace?.replace(/^local:/, "");
          if (
            savedSpaceId &&
            spaceList.some((space) => space.id === savedSpaceId)
          ) {
            setSelectedSpaceIdRaw(savedSpaceId);
          } else {
            setSelectedSpaceIdRaw(spaceList[0].id);
            persistSelectedSpaceId(spaceList[0].id);
          }
        }

        if (projectList.length > 0) {
          const savedProject = localStorage.getItem("selectedProjectId");
          let nextProjectId: string | null = null;
          const savedProjectId = savedProject?.replace(/^local:/, "");
          if (
            savedProjectId &&
            projectList.some((project) => project.id === savedProjectId)
          ) {
            nextProjectId = savedProjectId;
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
        setInitialLoadComplete(true);
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
    if (visibleSpaces.length === 0) return null;
    if (
      selectedSpaceIdState &&
      visibleSpaces.some((space) => space.id === selectedSpaceIdState)
    ) {
      return selectedSpaceIdState;
    }
    return visibleSpaces[0].id;
  }, [selectedSpaceIdState, visibleSpaces]);

  const projects = useMemo(() => {
    const active = visibleAllProjects.filter((p) => !p.is_completed);
    if (!selectedSpaceId) return active;
    return active.filter((project) => project.space_id === selectedSpaceId);
  }, [selectedSpaceId, visibleAllProjects]);

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
      if (index >= 0 && index < visibleSpaces.length) {
        setSelectedSpaceId(visibleSpaces[index].id);
      }
    }
    window.addEventListener("global-switch-space", handleSwitchSpace);
    return () =>
      window.removeEventListener("global-switch-space", handleSwitchSpace);
  }, [setSelectedSpaceId, visibleSpaces]);

  const selectedSpace =
    visibleSpaces.find((space) => space.id === selectedSpaceId) ?? null;
  const selectedProject =
    visibleAllProjects.find((project) => project.id === selectedProjectId) ??
    null;

  return (
    <ProjectContext.Provider
      value={{
        spaces: visibleSpaces,
        selectedSpaceId,
        selectedSpace,
        setSelectedSpaceId,
        refreshSpaces,
        projects,
        allProjects: visibleAllProjects,
        selectedProjectId,
        selectedProject,
        setSelectedProjectId,
        refreshProjects,
        projectsLoading,
        projectsLoadError,
        initialLoadComplete,
        remoteErrors: remoteConnectionEnabled ? remoteErrors : [],
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
