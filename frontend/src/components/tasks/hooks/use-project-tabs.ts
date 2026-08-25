"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type React from "react";

import type { FilterConfig } from "@/components/tasks/task-filter-builder";
import {
  getSavedProjectTab,
  getSavedProjectTabsCollapsed,
  persistProjectTabSelection,
  persistProjectTabsCollapsed,
  readTaskSidebarViewState,
  TASK_PROJECT_TAB_ALL,
  TASK_PROJECT_TAB_STATE_VERSION,
  TASK_SIDEBAR_VIEW_STATE_KEY,
  type FilterTab,
} from "@/lib/tasks-page-utils";

type ProjectLike = { id: string; name: string; space_id?: string | null };

export type ProjectTabSelectionOptions = {
  /** A Space row click intentionally selects the whole Space. */
  source?: "space-wide";
};

/**
 * プロジェクト横タブの選択状態・折りたたみ状態と localStorage 永続化をまとめたフック。
 */
export function useProjectTabs({
  projects,
  activeProjects,
  selectedSpaceId,
  selectedProjectId,
  setSelectedProjectId,
  spaceSelectionSourceRef,
  hasLoadedTasksRef,
  filter,
  showClosed,
  showFuture,
  customFilter,
}: {
  projects: ProjectLike[];
  activeProjects: ProjectLike[];
  selectedSpaceId: string | null;
  selectedProjectId?: string | null;
  setSelectedProjectId: (projectId: string) => void;
  /** Tasks のスペースナビ経由の切替と、ヘッダーからの切替を区別するための印。 */
  spaceSelectionSourceRef?: React.MutableRefObject<
    "task-space-navigation" | "task-space-wide-navigation" | null
  >;
  hasLoadedTasksRef: React.RefObject<boolean>;
  filter: FilterTab;
  showClosed: boolean;
  showFuture: boolean;
  customFilter: FilterConfig;
}) {
  const [projectTab, setProjectTab] = useState<string>(() =>
    getSavedProjectTab(null),
  );
  const skipProjectTabPersistRef = useRef(false);
  const previousSpaceIdRef = useRef<string | null | undefined>(undefined);
  const previousSelectedProjectIdRef = useRef<string | null | undefined>(
    undefined,
  );
  const internalProjectSelectionRef = useRef<string | null>(null);
  const restoringProjectTabRef = useRef<string | null>(null);
  const pendingSpaceRestoreRef = useRef<string | null | undefined>(undefined);
  // A Space-wide request is consumed by the first render in the target Space.
  // Keeping the target in the marker prevents a same-Space no-op from leaving
  // a stale intent that could swallow a later header Project selection.
  const pendingSpaceWideSelectionRef = useRef<string | null>(null);
  // A cross-Space Project click updates Context and this hook in one user
  // intent.  Do not let the intermediate old Space render persist the new
  // Project tab under the wrong key.
  const pendingTargetSpaceRef = useRef<string | null>(null);
  const projectTabRef = useRef(projectTab);
  const [projectTabsCollapsed, setProjectTabsCollapsed] = useState(false);
  const projectTabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const projectTabsToggleRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    projectTabRef.current = projectTab;
  }, [projectTab]);

  useEffect(() => {
    // SSR とのハイドレーション不一致を避けるため、マウント後に localStorage から復元する
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProjectTabsCollapsed(getSavedProjectTabsCollapsed());
  }, []);

  useEffect(() => {
    // まだスペース/プロジェクト一覧がない初期レンダーでは判定を保留する。
    // 初期ロード後に保存済みのスペース別タブを正しく復元できるようにする。
    if (selectedSpaceId === null && projects.length === 0) return;

    const previousSpaceId = previousSpaceIdRef.current;
    const previousSelectedProjectId = previousSelectedProjectIdRef.current;
    const isInitialScope = previousSpaceId === undefined;
    const isSpaceChanged =
      !isInitialScope && previousSpaceId !== selectedSpaceId;
    const selectedProjectIsValid =
      selectedProjectId !== null &&
      projects.some((project) => project.id === selectedProjectId);
    const selectedProjectChanged =
      previousSelectedProjectId !== undefined &&
      previousSelectedProjectId !== selectedProjectId;
    const taskSpaceNavigation =
      isSpaceChanged &&
      spaceSelectionSourceRef?.current === "task-space-navigation";
    // Clicking a Space row is one explicit intent: show the whole Space.  Do
    // not restore that Space's remembered project tab, including when the
    // user clicks the already-selected Space again.
    const explicitSpaceWideSelection =
      pendingSpaceWideSelectionRef.current === selectedSpaceId ||
      spaceSelectionSourceRef?.current === "task-space-wide-navigation" ||
      spaceSelectionSourceRef?.current === "task-space-navigation";
    const deferSpaceRestore = selectedSpaceId !== null && projects.length === 0;
    if (deferSpaceRestore) {
      pendingSpaceRestoreRef.current = selectedSpaceId;
      return;
    }
    const pendingSpaceRestore =
      pendingSpaceRestoreRef.current === selectedSpaceId;

    const savedTab = getSavedProjectTab(selectedSpaceId);
    const savedTabIsValid =
      savedTab === TASK_PROJECT_TAB_ALL ||
      projects.some((project) => project.id === savedTab);
    const restoredTab = savedTabIsValid ? savedTab : TASK_PROJECT_TAB_ALL;

    // スペースナビからの切替では、保存済みのスペース別タブを優先する。
    // ヘッダーのプロジェクト選択は同時にスペースも動かすため、ナビ印が
    // ないスペース切替では Context の選択を優先して意図を上書きしない。
    const shouldHonorExternalSelection =
      selectedProjectIsValid &&
      !taskSpaceNavigation &&
      !pendingSpaceRestore &&
      ((isSpaceChanged && !isInitialScope) ||
        (!isSpaceChanged &&
          !isInitialScope &&
          selectedProjectChanged &&
          internalProjectSelectionRef.current !== selectedProjectId));
    const nextTab = explicitSpaceWideSelection
      ? TASK_PROJECT_TAB_ALL
      : shouldHonorExternalSelection
      ? selectedProjectId!
      : restoredTab;

    if (pendingSpaceWideSelectionRef.current === selectedSpaceId) {
      pendingSpaceWideSelectionRef.current = null;
    }

    if (pendingSpaceRestore && projects.length > 0) {
      pendingSpaceRestoreRef.current = undefined;
    }

    // localStorage に保存されたタブ選択へスペース切替時に同期する。
    // 実際にタブが変わるときだけ永続化を一度スキップし、復元値で古い
    // スペースの選択を上書きしない。
    setProjectTab((currentTab) => {
      if (currentTab === nextTab) return currentTab;
      if (!shouldHonorExternalSelection) {
        skipProjectTabPersistRef.current = true;
      }
      return nextTab;
    });
    if (nextTab !== projectTabRef.current) {
      restoringProjectTabRef.current = nextTab;
    }

    if (nextTab !== TASK_PROJECT_TAB_ALL) {
      if (selectedProjectId !== nextTab) {
        internalProjectSelectionRef.current = nextTab;
        setSelectedProjectId(nextTab);
      } else {
        internalProjectSelectionRef.current = null;
      }
    } else {
      internalProjectSelectionRef.current = null;
    }

    previousSpaceIdRef.current = selectedSpaceId;
    previousSelectedProjectIdRef.current = selectedProjectId;
    if (spaceSelectionSourceRef) {
      spaceSelectionSourceRef.current = null;
    }
  }, [
    projects,
    selectedProjectId,
    selectedSpaceId,
    setSelectedProjectId,
    spaceSelectionSourceRef,
  ]);

  useEffect(() => {
    if (restoringProjectTabRef.current === projectTab) {
      restoringProjectTabRef.current = null;
    }
  }, [projectTab]);

  useEffect(() => {
    if (skipProjectTabPersistRef.current) {
      skipProjectTabPersistRef.current = false;
      return;
    }

    if (
      pendingTargetSpaceRef.current !== null &&
      selectedSpaceId !== pendingTargetSpaceRef.current
    ) {
      return;
    }
    pendingTargetSpaceRef.current = null;

    try {
      const saved = readTaskSidebarViewState();
      const projectTabsBySpace = { ...(saved.projectTabsBySpace ?? {}) };
      if (selectedSpaceId) {
        projectTabsBySpace[selectedSpaceId] = projectTab;
      }

      localStorage.setItem(
        TASK_SIDEBAR_VIEW_STATE_KEY,
        JSON.stringify({
          ...saved,
          filter,
          projectTab,
          projectTabStateVersion: TASK_PROJECT_TAB_STATE_VERSION,
          projectTabsBySpace,
          showClosed,
          showFuture,
          customFilter,
        }),
      );
    } catch {
      // ignore
    }
    window.dispatchEvent(new Event("task-sidebar-refresh"));
  }, [
    customFilter,
    filter,
    projectTab,
    selectedSpaceId,
    showClosed,
    showFuture,
  ]);

  useEffect(() => {
    if (!hasLoadedTasksRef.current || projectTab === TASK_PROJECT_TAB_ALL) {
      return;
    }
    // スペース復元と同じコミットでは、復元前の古い tab を見て
    // フォールバックしない。次のレンダーで復元値が反映されたら通常判定に戻る。
    if (
      restoringProjectTabRef.current !== null &&
      restoringProjectTabRef.current !== projectTab
    ) {
      return;
    }
    if (restoringProjectTabRef.current === projectTab) {
      restoringProjectTabRef.current = null;
    }
    if (activeProjects.some((project) => project.id === projectTab)) {
      return;
    }
    // 選択中プロジェクトがタスク読込後に消えた場合のフォールバック
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProjectTab(TASK_PROJECT_TAB_ALL);
  }, [activeProjects, hasLoadedTasksRef, projectTab]);

  const projectTabOrder = useMemo(
    () => ["all", ...activeProjects.map((project) => project.id)],
    [activeProjects],
  );

  const setProjectTabAndSelection = useCallback(
    (
      nextTab: string,
      targetSpaceId: string | null = selectedSpaceId,
      options?: ProjectTabSelectionOptions,
    ) => {
      // Project rows can move Context to another Space in the same event.  A
      // caller may provide that target so the old Space is never persisted.
      if (targetSpaceId && targetSpaceId !== selectedSpaceId) {
        pendingTargetSpaceRef.current = targetSpaceId;
      }
      if (nextTab === TASK_PROJECT_TAB_ALL && targetSpaceId === selectedSpaceId) {
        const isMarkedSpaceWide =
          options?.source === "space-wide" ||
          spaceSelectionSourceRef?.current === "task-space-wide-navigation" ||
          spaceSelectionSourceRef?.current === "task-space-navigation";
        if (isMarkedSpaceWide) {
          // No Space state transition means the scope effect will not run.
          // Consume any legacy external marker now so a subsequent header
          // Project selection remains authoritative.
          pendingSpaceWideSelectionRef.current = null;
          if (
            spaceSelectionSourceRef?.current === "task-space-wide-navigation" ||
            spaceSelectionSourceRef?.current === "task-space-navigation"
          ) {
            spaceSelectionSourceRef.current = null;
          }
        }
      } else if (
        options?.source === "space-wide" &&
        nextTab === TASK_PROJECT_TAB_ALL
      ) {
        pendingSpaceWideSelectionRef.current = targetSpaceId;
      }
      persistProjectTabSelection(nextTab, targetSpaceId);
      setProjectTab(nextTab);
      if (nextTab !== "all") {
        internalProjectSelectionRef.current = nextTab;
        setSelectedProjectId(nextTab);
      } else {
        internalProjectSelectionRef.current = null;
      }
    },
    [selectedSpaceId, setSelectedProjectId, spaceSelectionSourceRef],
  );

  const cycleProjectTab = useCallback(
    (direction: 1 | -1) => {
      if (projectTabOrder.length === 0) return;
      const currentIndex = Math.max(0, projectTabOrder.indexOf(projectTab));
      const nextIndex =
        (currentIndex + direction + projectTabOrder.length) %
        projectTabOrder.length;
      const nextTab = projectTabOrder[nextIndex];
      setProjectTabAndSelection(nextTab);
      requestAnimationFrame(() => {
        if (projectTabsCollapsed) {
          projectTabsToggleRef.current?.focus({ preventScroll: true });
        } else {
          projectTabRefs.current[nextTab]?.focus({ preventScroll: true });
        }
      });
    },
    [
      projectTab,
      projectTabOrder,
      projectTabsCollapsed,
      setProjectTabAndSelection,
    ],
  );

  const toggleProjectTabsCollapsed = useCallback(() => {
    setProjectTabsCollapsed((collapsed) => {
      const next = !collapsed;
      persistProjectTabsCollapsed(next);
      return next;
    });
  }, []);

  return {
    projectTab,
    projectTabsCollapsed,
    projectTabRefs,
    projectTabsToggleRef,
    setProjectTabAndSelection,
    cycleProjectTab,
    toggleProjectTabsCollapsed,
  };
}
