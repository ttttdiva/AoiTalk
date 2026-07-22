"use client";

import { useCallback } from "react";
import useSWR from "swr";

export type ProjectsData<S, P> = {
  spaces: S[];
  projects: P[];
};

// SWR キャッシュキー。プロジェクト管理ページで一意なので固定文字列を使う。
// 取得タイミングは従来どおり呼び出し側の refresh（= 手動 revalidate）で駆動し、
// 選択復元・スペース展開などの副作用を伴う初期化ロジックは呼び出し側に残す。
const PROJECTS_SWR_KEY = "projects-page/spaces-projects";

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

/**
 * プロジェクト管理ページのスペース一覧・プロジェクト一覧の取得を SWR で管理するフック。
 *
 * 取得・キャッシュ・重複排除・競合破棄は SWR に委譲する。ローディング表示や
 * 選択スコープの復元などの副作用は表示挙動を不変に保つため呼び出し側に残し、
 * このフックはデータと再取得手段（refresh）のみを提供する。
 */
export function useProjectsData<S, P>() {
  const fetcher = useCallback(async (): Promise<ProjectsData<S, P>> => {
    const [spacesData, projectsData] = await Promise.all([
      fetchJson<{ spaces: S[] }>("/api/spaces"),
      fetchJson<{ projects: P[] }>("/api/projects"),
    ]);
    return { spaces: spacesData.spaces, projects: projectsData.projects };
  }, []);

  const { data, mutate } = useSWR<ProjectsData<S, P>>(
    PROJECTS_SWR_KEY,
    fetcher,
    {
      // 取得タイミングを従来実装（呼び出し側の fetchAll）に一致させるため、
      // SWR の自動 revalidation は無効化し、全ての取得を refresh 経由にする。
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      // refresh ごとに実取得する（従来は fetchAll で毎回 fetch）。
      dedupingInterval: 0,
    },
  );

  // revalidate を実行し、最新の { spaces, projects } を返す。
  const refresh = useCallback(() => mutate(), [mutate]);

  return {
    spaces: data?.spaces,
    projects: data?.projects,
    refresh,
    mutate,
  };
}
