"use client";

import { useCallback, useEffect, useRef } from "react";
import useSWR from "swr";

export type ProjectsData<S, P> = {
  spaces: S[];
  projects: P[];
};

export type ProjectsDataState = {
  spacesError: Error | null;
  projectsError: Error | null;
  spacesLoading: boolean;
  projectsLoading: boolean;
  spacesLoaded: boolean;
  projectsLoaded: boolean;
};

const SPACES_SWR_KEY = "projects-page/spaces";
const PROJECTS_SWR_KEY = "projects-page/projects";

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    const message =
      typeof detail?.detail === "string" && detail.detail.trim()
        ? detail.detail
        : res.statusText || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return res.json();
}

function toError(value: unknown): Error | null {
  if (value == null) return null;
  if (value instanceof Error) return value;
  return new Error(String(value));
}

const SWR_OPTIONS = {
  // 取得タイミングを従来実装（呼び出し側の fetchAll）に一致させるため、
  // SWR の自動 revalidation は無効化し、全ての取得を refresh 経由にする。
  revalidateOnMount: false,
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  revalidateIfStale: false,
  keepPreviousData: true,
  // refresh ごとに実取得する（従来は fetchAll で毎回 fetch）。
  dedupingInterval: 0,
} as const;

/**
 * プロジェクト管理ページのスペース一覧・プロジェクト一覧を独立して取得する。
 *
 * 2 API を一つの Promise.all にまとめると、一方の失敗で成功したレスポンスまで
 * 破棄され、画面が「0件」と誤表示される。SWR のキャッシュも API ごとに分離し、
 * 成功済みデータは他方のエラーや再取得中も保持する。
 */
export function useProjectsData<S, P>() {
  const {
    data: spacesData,
    error: spacesSWRerror,
    isValidating: spacesIsValidating,
    mutate: mutateSpaces,
  } = useSWR<{ spaces: S[] }>(
    SPACES_SWR_KEY,
    () => fetchJson<{ spaces: S[] }>("/api/spaces"),
    SWR_OPTIONS,
  );
  const {
    data: projectsData,
    error: projectsSWRerror,
    isValidating: projectsIsValidating,
    mutate: mutateProjects,
  } = useSWR<{ projects: P[] }>(
    PROJECTS_SWR_KEY,
    () => fetchJson<{ projects: P[] }>("/api/projects"),
    SWR_OPTIONS,
  );
  const spacesDataRef = useRef<S[] | undefined>(spacesData?.spaces);
  const projectsDataRef = useRef<P[] | undefined>(projectsData?.projects);
  useEffect(() => {
    spacesDataRef.current = spacesData?.spaces;
  }, [spacesData?.spaces]);
  useEffect(() => {
    projectsDataRef.current = projectsData?.projects;
  }, [projectsData?.projects]);

  const refreshSpaces = useCallback(async () => {
    try {
      const result = await mutateSpaces();
      return result?.spaces ?? spacesDataRef.current ?? [];
    } catch {
      // SWR stores the error on `spacesError`; retain the previous data
      // so a temporary spaces failure cannot hide usable project data.
      return spacesDataRef.current ?? [];
    }
  }, [mutateSpaces]);

  const refreshProjects = useCallback(async () => {
    try {
      const result = await mutateProjects();
      return result?.projects ?? projectsDataRef.current ?? [];
    } catch {
      return projectsDataRef.current ?? [];
    }
  }, [mutateProjects]);

  const refresh = useCallback(async () => {
    const [spaces, projects] = await Promise.all([
      refreshSpaces(),
      refreshProjects(),
    ]);
    return { spaces, projects };
  }, [refreshProjects, refreshSpaces]);

  const spacesLoaded = spacesData !== undefined || spacesSWRerror != null;
  const projectsLoaded =
    projectsData !== undefined || projectsSWRerror != null;

  return {
    spaces: spacesData?.spaces,
    projects: projectsData?.projects,
    spacesError: toError(spacesSWRerror),
    projectsError: toError(projectsSWRerror),
    spacesLoading: spacesIsValidating || (!spacesLoaded && !spacesSWRerror),
    projectsLoading:
      projectsIsValidating || (!projectsLoaded && !projectsSWRerror),
    spacesLoaded,
    projectsLoaded,
    refresh,
    refreshSpaces,
    refreshProjects,
    // Keep the old mutate escape hatch as an aggregate refresh. No caller in
    // the page relies on SWR's raw mutate return value.
    mutate: refresh,
  };
}
