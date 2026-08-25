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
  /** Accessible resources (including global-admin-only Projects). */
  accessibleSpaces: Space[];
  accessibleProjects: Project[];
  /** Normal operational scope (owner/explicit member read only). */
  spaces: Space[];
  participatingSpaces: Space[];
  selectedSpaceId: string | null;
  selectedSpace: Space | null;
  setSelectedSpaceId: (id: string) => void;
  refreshSpaces: () => Promise<Space[]>;
  projects: Project[];
  participatingProjects: Project[];
  allProjects: Project[];
  selectedProjectId: string | null;
  selectedProject: Project | null;
  setSelectedProjectId: (id: string) => void;
  refreshProjects: () => Promise<Project[] | never[]>;
  spacesLoading: boolean;
  spacesLoadError: string | null;
  projectsLoading: boolean;
  projectsLoadError: string | null;
  initialLoadComplete: boolean;
  remoteErrors: string[];
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

/**
 * The header project switcher is a Space-scoped navigation control.  Local
 * projects without a `space_id` are still returned by `/api/projects` for
 * project-level pages (for example, records created by a Docs acceptance
 * flow), but they do not belong to any selectable Space and must not leak into
 * this selector.  Remote projects keep their own resource semantics and may
 * legitimately omit a local Space id.  Such remote projects are only a
 * global fallback when no Space is selected; they must not be attached to a
 * selected remote Space.  The task loader treats a selected remote Space as
 * a space-wide scope, so presenting an unscoped remote Project there would
 * make a project selection silently load every project in that Space.
 */
function isGloballySelectableProject(project: Project): boolean {
  return project.source !== "local" || Boolean(project.space_id);
}

/**
 * Normal Project selection is intentionally narrower than the server's
 * accessible Project projection.  A global administrator may receive a
 * Space-scoped Project without a membership row (`can_manage_settings` can be
 * true while `is_participating` is false), but that row is an internal Files
 * target rather than a Project the user participates in.
 */
function isOperationalProject(project: Project): boolean {
  return project.source !== "local" || project.is_participating !== false;
}

function isProjectInSelectedSpace(
  project: Project,
  spaceId: string | null,
): boolean {
  return spaceId
    ? project.space_id === spaceId
    : isGloballySelectableProject(project);
}

const LAST_PROJECT_ID_BY_SPACE_KEY = "lastProjectIdBySpace";

function persistResourceId(id: string): string {
  return id.startsWith("remote:") ? id : localResourceKey(id);
}

function readPersistedResourceId(value: string): string {
  return value.replace(/^local:/, "");
}

function loadLastProjectIdBySpace(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(LAST_PROJECT_ID_BY_SPACE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const result: Record<string, string> = {};
    for (const [spaceId, projectId] of Object.entries(
      parsed as Record<string, unknown>,
    )) {
      if (typeof projectId !== "string" || !spaceId || !projectId) continue;
      result[readPersistedResourceId(spaceId)] =
        readPersistedResourceId(projectId);
    }
    return result;
  } catch {
    return {};
  }
}

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
  const [spacesLoading, setSpacesLoading] = useState(true);
  const [spacesLoadError, setSpacesLoadError] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsLoadError, setProjectsLoadError] = useState<string | null>(
    null,
  );
  const [initialLoadComplete, setInitialLoadComplete] = useState(false);
  // A retry can start before the previous request settles. Track generations
  // per resource so an older success/error/finally cannot overwrite the latest
  // data or status. Projects additionally use one counter per connection mode;
  // the mode generation check below invalidates requests after a mode switch.
  const spacesRequestGenerationRef = useRef(0);
  const projectsRequestGenerationRef = useRef({ local: 0, remote: 0 });
  const spacesDataGenerationRef = useRef(0);
  const projectsDataGenerationRef = useRef(0);
  const modeStateRef = useRef({
    remoteConnectionEnabled,
    generation: 0,
  });
  if (
    modeStateRef.current.remoteConnectionEnabled !== remoteConnectionEnabled
  ) {
    modeStateRef.current = {
      remoteConnectionEnabled,
      generation: modeStateRef.current.generation + 1,
    };
  }
  // 初期ロードは「リモート接続の有無」ごとに一度だけ走らせる。
  // このeffectはrefreshSpaces/refreshProjectsを依存に持ち、それらの識別子は
  // 初期ロード自身が更新するselectedSpaceId/selectedProjectIdに依存するため、
  // ガードがないと /api/spaces と /api/projects を毎回二重取得してしまう。
  const bootstrapModeRef = useRef<string | null>(null);

  const accessibleSpaces = useMemo(
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
  const participatingAllProjects = useMemo(
    () =>
      visibleAllProjects.filter(isOperationalProject),
    [visibleAllProjects],
  );
  const participatingSpaces = useMemo(
    () => {
      // Spaces and Projects are fetched sequentially during bootstrap.  Keep
      // the server-authorized Space list visible while the Project request is
      // still pending (or failed), otherwise the intermediate empty Project
      // list makes every local Space disappear from the header and Tasks tree.
      // Once the Project request succeeds, restore the normal participation
      // filter so unrelated/empty local Spaces remain hidden as before.
      if (projectsLoading || projectsLoadError) return accessibleSpaces;
      return accessibleSpaces.filter(
        (space) =>
          space.source !== "local" ||
          participatingAllProjects.some((project) => project.space_id === space.id),
      );
    },
    [
      accessibleSpaces,
      participatingAllProjects,
      projectsLoadError,
      projectsLoading,
    ],
  );

  const persistSelectedSpaceId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem("selectedSpaceId", persistResourceId(id));
      return;
    }
    localStorage.removeItem("selectedSpaceId");
  }, []);

  const persistSelectedProjectId = useCallback((id: string | null) => {
    if (id) {
      localStorage.setItem("selectedProjectId", persistResourceId(id));
      return;
    }
    localStorage.removeItem("selectedProjectId");
  }, []);

  const lastProjectIdBySpaceRef = useRef<Record<string, string>>(
    loadLastProjectIdBySpace(),
  );
  const selectedSpaceIdStateRef = useRef<string | null>(selectedSpaceIdState);
  const selectedProjectIdStateRef = useRef<string | null>(
    selectedProjectIdState,
  );
  selectedSpaceIdStateRef.current = selectedSpaceIdState;
  selectedProjectIdStateRef.current = selectedProjectIdState;

  const persistLastProjectIdBySpace = useCallback(
    (map: Record<string, string>) => {
      lastProjectIdBySpaceRef.current = map;
      const persisted: Record<string, string> = {};
      for (const [spaceId, projectId] of Object.entries(map)) {
        persisted[persistResourceId(spaceId)] = persistResourceId(projectId);
      }
      localStorage.setItem(
        LAST_PROJECT_ID_BY_SPACE_KEY,
        JSON.stringify(persisted),
      );
    },
    [],
  );

  /**
   * Drop stale/non-participating Project ids from the per-Space restore map.
   * This is deliberately run only after a successful Project response; while
   * the request is pending an empty list must not erase a valid persisted
   * selection before bootstrap has a chance to resolve it.
   */
  const normalizeLastProjectIdBySpace = useCallback(
    (projectList: Project[]) => {
      const selectableById = new Map(
        projectList
          .filter(isOperationalProject)
          .filter((project) => !project.is_completed)
          .filter((project) => Boolean(project.space_id))
          .map((project) => [project.id, project] as const),
      );
      const current = lastProjectIdBySpaceRef.current;
      const next: Record<string, string> = {};
      let changed = false;
      for (const [spaceId, projectId] of Object.entries(current)) {
        const project = selectableById.get(projectId);
        if (!project || project.space_id !== spaceId) {
          changed = true;
          continue;
        }
        next[spaceId] = projectId;
      }
      if (changed) persistLastProjectIdBySpace(next);
    },
    [persistLastProjectIdBySpace],
  );

  const rememberProjectForSpace = useCallback(
    (spaceId: string, projectId: string) => {
      persistLastProjectIdBySpace({
        ...lastProjectIdBySpaceRef.current,
        [spaceId]: projectId,
      });
    },
    [persistLastProjectIdBySpace],
  );

  const getProjectsForSpace = useCallback(
    (spaceId: string | null) => {
      const activeProjects = participatingAllProjects.filter(
        (project) => !project.is_completed,
      );
      if (!spaceId) {
        return activeProjects.filter((project) =>
          isProjectInSelectedSpace(project, null),
        );
      }
      return activeProjects.filter((project) =>
        isProjectInSelectedSpace(project, spaceId),
      );
    },
    [participatingAllProjects],
  );

  const setSelectedSpaceId = useCallback(
    (id: string) => {
      const space = accessibleSpaces.find((item) => item.id === id);
      if (!space) return;
      // Once the Project response has settled, a local Space with no
      // owner/read-membership Project is not a normal operational Space.
      // During the request we retain the server-authorized Space list so the
      // header does not flicker; accepting it here is safe because the
      // post-refresh normalization below will replace it if necessary.
      if (
        !projectsLoading &&
        !projectsLoadError &&
        space.source === "local" &&
        !participatingAllProjects.some((project) => project.space_id === id)
      ) {
        return;
      }
      selectedSpaceIdStateRef.current = id;
      setSelectedSpaceIdRaw(id);
      persistSelectedSpaceId(id);

      const scopedProjects = getProjectsForSpace(id);
      const rememberedId = lastProjectIdBySpaceRef.current[id];
      const rememberedProject = scopedProjects.find(
        (project) => project.id === rememberedId,
      );
      // Restore in this same update. A later selectedProjectId write is
      // treated by Tasks tabs as an explicit header Project selection.
      let nextProjectId: string | null;
      if (rememberedProject) {
        nextProjectId = rememberedProject.id;
      } else if (
        scopedProjects.some(
          (project) => project.id === selectedProjectIdStateRef.current,
        )
      ) {
        nextProjectId = selectedProjectIdStateRef.current;
      } else {
        nextProjectId = scopedProjects[0]?.id ?? null;
      }

      if (nextProjectId === selectedProjectIdStateRef.current) {
        return;
      }

      selectedProjectIdStateRef.current = nextProjectId;
      setSelectedProjectIdRaw(nextProjectId);
      persistSelectedProjectId(nextProjectId);
    },
    [
      accessibleSpaces,
      getProjectsForSpace,
      participatingAllProjects,
      persistSelectedProjectId,
      persistSelectedSpaceId,
      projectsLoadError,
      projectsLoading,
    ],
  );

  const setSelectedProjectId = useCallback(
    (id: string) => {
      const project = participatingAllProjects.find((item) => item.id === id);
      if (!project) return;
      selectedProjectIdStateRef.current = id;
      setSelectedProjectIdRaw(id);
      persistSelectedProjectId(id);

      if (project?.space_id) {
        rememberProjectForSpace(project.space_id, id);
      }
      if (
        !project?.space_id ||
        project.space_id === selectedSpaceIdStateRef.current
      ) {
        return;
      }

      selectedSpaceIdStateRef.current = project.space_id;
      setSelectedSpaceIdRaw(project.space_id);
      persistSelectedSpaceId(project.space_id);
    },
    [
      persistSelectedProjectId,
      persistSelectedSpaceId,
      rememberProjectForSpace,
      participatingAllProjects,
    ],
  );

  const refreshSpaces = useCallback(async () => {
    const modeGeneration = modeStateRef.current.generation;
    const isCurrentMode = () =>
      modeStateRef.current.generation === modeGeneration &&
      modeStateRef.current.remoteConnectionEnabled === remoteConnectionEnabled;
    if (!isCurrentMode()) return spaces;
    const requestGeneration = ++spacesRequestGenerationRef.current;
    const isCurrentRequest = () =>
      isCurrentMode() &&
      spacesRequestGenerationRef.current === requestGeneration;
    setSpacesLoading(true);
    setSpacesLoadError(null);
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
      if (!isCurrentRequest()) return spaces;
      setRemoteErrors(remoteConnectionEnabled ? errors : []);
      const merged = [localSpaces, ...remoteResults].flat();
      setSpaces(merged);
      spacesDataGenerationRef.current = modeGeneration;
      return merged;
    } catch (error) {
      if (!isCurrentRequest()) return spaces;
      setSpacesLoadError(
        error instanceof Error
          ? error.message
          : "スペースを取得できませんでした",
      );
      // Keep the last successful list. An unavailable spaces endpoint must
      // not turn existing data into a misleading empty state.
      return spacesDataGenerationRef.current === modeGeneration ? spaces : [];
    } finally {
      if (isCurrentRequest()) setSpacesLoading(false);
    }
  }, [remoteConnectionEnabled, spaces]);

  const refreshProjects = useCallback(async () => {
    const modeGeneration = modeStateRef.current.generation;
    const isCurrentMode = () =>
      modeStateRef.current.generation === modeGeneration &&
      modeStateRef.current.remoteConnectionEnabled === remoteConnectionEnabled;
    if (!isCurrentMode()) return [];
    const modeKey = remoteConnectionEnabled ? "remote" : "local";
    const requestGeneration = ++projectsRequestGenerationRef.current[modeKey];
    const isCurrentRequest = () =>
      isCurrentMode() &&
      projectsRequestGenerationRef.current[modeKey] === requestGeneration;
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
      if (!isCurrentRequest()) return allProjects;
      setRemoteErrors((previous) =>
        remoteConnectionEnabled
          ? errors.length > 0
            ? Array.from(new Set([...previous, ...errors]))
            : previous
          : [],
      );
      const merged = [localProjects, ...remoteResults].flat();
      setAllProjects(merged);
      normalizeLastProjectIdBySpace(merged);
      projectsDataGenerationRef.current = modeGeneration;
      if (selectedProjectIdState) {
        const selectedProject = merged.find(
          (project) =>
          project.id === selectedProjectIdState &&
            isOperationalProject(project),
        );
        if (
          selectedProject?.space_id &&
          selectedProject.space_id !== selectedSpaceIdState
        ) {
          selectedSpaceIdStateRef.current = selectedProject.space_id;
          setSelectedSpaceIdRaw(selectedProject.space_id);
          persistSelectedSpaceId(selectedProject.space_id);
        }
      }
      return merged;
    } catch (error) {
      if (!isCurrentRequest()) return allProjects;
      setProjectsLoadError(
        error instanceof Error
          ? error.message
          : "プロジェクトを取得できませんでした",
      );
      // Keep the last successful list. The caller can still render it while
      // exposing projectsLoadError and offering a retry.
      return projectsDataGenerationRef.current === modeGeneration
        ? allProjects
        : [];
    } finally {
      if (isCurrentRequest()) setProjectsLoading(false);
    }
  }, [
    persistSelectedSpaceId,
    normalizeLastProjectIdBySpace,
    selectedProjectIdState,
    selectedSpaceIdState,
    remoteConnectionEnabled,
    allProjects,
  ]);

  useEffect(() => {
    if (remoteConnectionEnabled) return;
    if (selectedSpaceIdState?.startsWith("remote:")) {
      persistSelectedSpaceId(null);
    }
    if (selectedProjectIdState?.startsWith("remote:")) {
      persistSelectedProjectId(null);
    }
    const map = lastProjectIdBySpaceRef.current;
    const next: Record<string, string> = {};
    let changed = false;
    for (const [spaceId, projectId] of Object.entries(map)) {
      if (spaceId.startsWith("remote:") || projectId.startsWith("remote:")) {
        changed = true;
        continue;
      }
      next[spaceId] = projectId;
    }
    if (changed) {
      persistLastProjectIdBySpace(next);
    }
  }, [
    persistLastProjectIdBySpace,
    persistSelectedProjectId,
    persistSelectedSpaceId,
    remoteConnectionEnabled,
    selectedProjectIdState,
    selectedSpaceIdState,
  ]);

  // A saved admin-only/removed Project must not survive a successful refresh
  // as the canonical header selection.  Keep the server-authorized
  // accessible list separate from this normal operational state.
  useEffect(() => {
    if (spacesLoading || projectsLoading || projectsLoadError) return;

    const nextSpaceId =
      selectedSpaceIdState &&
      participatingSpaces.some((space) => space.id === selectedSpaceIdState)
        ? selectedSpaceIdState
        : participatingSpaces[0]?.id ?? null;
    if (nextSpaceId !== selectedSpaceIdState) {
      selectedSpaceIdStateRef.current = nextSpaceId;
      setSelectedSpaceIdRaw(nextSpaceId);
      persistSelectedSpaceId(nextSpaceId);
    }

    const scopedProjects = getProjectsForSpace(nextSpaceId);
    const nextProjectId =
      selectedProjectIdState &&
      scopedProjects.some((project) => project.id === selectedProjectIdState)
        ? selectedProjectIdState
        : scopedProjects[0]?.id ?? null;
    if (nextProjectId !== selectedProjectIdState) {
      selectedProjectIdStateRef.current = nextProjectId;
      setSelectedProjectIdRaw(nextProjectId);
      persistSelectedProjectId(nextProjectId);
    }
  }, [
    getProjectsForSpace,
    participatingSpaces,
    persistSelectedProjectId,
    persistSelectedSpaceId,
    projectsLoadError,
    projectsLoading,
    spacesLoading,
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
      // StrictMode の再マウントで初期ロードが飛ばないよう、判定はタイマー発火時に行う
      // （マウント直後にクリアされたタイマーではガードを立てない）。
      const bootstrapGeneration = modeStateRef.current.generation;
      const bootstrapMode = remoteConnectionEnabled ? "remote" : "local";
      if (bootstrapModeRef.current === bootstrapMode) return;
      bootstrapModeRef.current = bootstrapMode;
      void (async () => {
        const spaceList = await refreshSpaces();
        if (modeStateRef.current.generation !== bootstrapGeneration) return;
        const projectList = await refreshProjects();
        if (modeStateRef.current.generation !== bootstrapGeneration) return;

        normalizeLastProjectIdBySpace(projectList);

        const participatingProjectList = projectList.filter(
          (project) =>
            isOperationalProject(project) &&
            isGloballySelectableProject(project),
        );
        const participatingSpaceList = spaceList.filter(
          (space) =>
            space.source !== "local" ||
            participatingProjectList.some((project) => project.space_id === space.id),
        );

        let bootstrapSpaceId: string | null = null;
        if (participatingSpaceList.length > 0) {
          const savedSpace = localStorage.getItem("selectedSpaceId");
          const savedSpaceId = savedSpace
            ? readPersistedResourceId(savedSpace)
            : undefined;
          if (
            savedSpaceId &&
            participatingSpaceList.some((space) => space.id === savedSpaceId)
          ) {
            bootstrapSpaceId = savedSpaceId;
          } else {
            bootstrapSpaceId = participatingSpaceList[0].id;
            persistSelectedSpaceId(bootstrapSpaceId);
          }
          selectedSpaceIdStateRef.current = bootstrapSpaceId;
          setSelectedSpaceIdRaw(bootstrapSpaceId);
        } else {
          selectedSpaceIdStateRef.current = null;
          setSelectedSpaceIdRaw(null);
          persistSelectedSpaceId(null);
          selectedProjectIdStateRef.current = null;
          setSelectedProjectIdRaw(null);
          persistSelectedProjectId(null);
        }

        const bootstrapProjectList = participatingProjectList.filter((project) =>
          isProjectInSelectedSpace(project, bootstrapSpaceId),
        );
        if (bootstrapProjectList.length > 0) {
          const savedProject = localStorage.getItem("selectedProjectId");
          const savedProjectId = savedProject
            ? readPersistedResourceId(savedProject)
            : undefined;
          const rememberedProjectId = bootstrapSpaceId
            ? lastProjectIdBySpaceRef.current[bootstrapSpaceId]
            : undefined;
          let nextProjectId: string | null = null;
          if (
            rememberedProjectId &&
            bootstrapProjectList.some(
              (project) => project.id === rememberedProjectId,
            )
          ) {
            nextProjectId = rememberedProjectId;
          } else if (
            savedProjectId &&
            bootstrapProjectList.some((project) => project.id === savedProjectId)
          ) {
            nextProjectId = savedProjectId;
          } else {
            nextProjectId = bootstrapProjectList[0].id;
          }

          if (nextProjectId && nextProjectId !== savedProjectId) {
            persistSelectedProjectId(nextProjectId);
          }

          if (
            bootstrapSpaceId &&
            nextProjectId &&
            Object.keys(lastProjectIdBySpaceRef.current).length === 0 &&
            savedProjectId &&
            bootstrapProjectList.some((project) => project.id === savedProjectId)
          ) {
            persistLastProjectIdBySpace({
              [bootstrapSpaceId]: savedProjectId,
            });
          }

          if (nextProjectId) {
            selectedProjectIdStateRef.current = nextProjectId;
            setSelectedProjectIdRaw(nextProjectId);
            const selectedProject = projectList.find(
              (project) => project.id === nextProjectId,
            );
            if (selectedProject?.space_id) {
              selectedSpaceIdStateRef.current = selectedProject.space_id;
              setSelectedSpaceIdRaw(selectedProject.space_id);
              persistSelectedSpaceId(selectedProject.space_id);
            }
          }
        } else {
          selectedProjectIdStateRef.current = null;
          setSelectedProjectIdRaw(null);
          persistSelectedProjectId(null);
        }
        setInitialLoadComplete(true);
      })();
    }, 0);

    return () => clearTimeout(timer);
  }, [
    persistLastProjectIdBySpace,
    normalizeLastProjectIdBySpace,
    persistSelectedProjectId,
    persistSelectedSpaceId,
    refreshProjects,
    refreshSpaces,
    remoteConnectionEnabled,
  ]);

  const selectedSpaceId = useMemo(() => {
    if (participatingSpaces.length === 0) return null;
    if (
      selectedSpaceIdState &&
      participatingSpaces.some((space) => space.id === selectedSpaceIdState)
    ) {
      return selectedSpaceIdState;
    }
    return participatingSpaces[0].id;
  }, [participatingSpaces, selectedSpaceIdState]);

  const projects = useMemo(() => {
    return getProjectsForSpace(selectedSpaceId);
  }, [getProjectsForSpace, selectedSpaceId]);

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
      if (index >= 0 && index < participatingSpaces.length) {
        setSelectedSpaceId(participatingSpaces[index].id);
      }
    }
    window.addEventListener("global-switch-space", handleSwitchSpace);
    return () =>
      window.removeEventListener("global-switch-space", handleSwitchSpace);
  }, [participatingSpaces, setSelectedSpaceId]);

  const selectedSpace =
    participatingSpaces.find((space) => space.id === selectedSpaceId) ?? null;
  const selectedProject =
    participatingAllProjects.find((project) => project.id === selectedProjectId) ??
    null;

  return (
    <ProjectContext.Provider
      value={{
        accessibleSpaces,
        accessibleProjects: visibleAllProjects,
        participatingSpaces,
        spaces: participatingSpaces,
        selectedSpaceId,
        selectedSpace,
        setSelectedSpaceId,
        refreshSpaces,
        spacesLoading,
        spacesLoadError,
        projects,
        participatingProjects: participatingAllProjects,
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
