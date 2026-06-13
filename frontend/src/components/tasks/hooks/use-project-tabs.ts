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

type ProjectLike = { id: string; name: string };

/**
 * プロジェクト横タブの選択状態・折りたたみ状態と localStorage 永続化をまとめたフック。
 */
export function useProjectTabs({
  projects,
  activeProjects,
  selectedSpaceId,
  setSelectedProjectId,
  hasLoadedTasksRef,
  filter,
  showClosed,
  showFuture,
  customFilter,
}: {
  projects: ProjectLike[];
  activeProjects: ProjectLike[];
  selectedSpaceId: string | null;
  setSelectedProjectId: (projectId: string) => void;
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
  const [projectTabsCollapsed, setProjectTabsCollapsed] = useState(false);
  const projectTabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const projectTabsToggleRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    // SSR とのハイドレーション不一致を避けるため、マウント後に localStorage から復元する
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProjectTabsCollapsed(getSavedProjectTabsCollapsed());
  }, []);

  useEffect(() => {
    const savedTab = getSavedProjectTab(selectedSpaceId);
    const nextTab =
      savedTab === TASK_PROJECT_TAB_ALL ||
      projects.some((project) => project.id === savedTab)
        ? savedTab
        : TASK_PROJECT_TAB_ALL;

    skipProjectTabPersistRef.current = true;
    // localStorage に保存されたタブ選択へスペース切替時に同期する(永続化スキップとセット)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setProjectTab((currentTab) =>
      currentTab === nextTab ? currentTab : nextTab,
    );
  }, [projects, selectedSpaceId]);

  useEffect(() => {
    if (skipProjectTabPersistRef.current) {
      skipProjectTabPersistRef.current = false;
      return;
    }

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
    (nextTab: string) => {
      persistProjectTabSelection(nextTab, selectedSpaceId);
      setProjectTab(nextTab);
      if (nextTab !== "all") {
        setSelectedProjectId(nextTab);
      }
    },
    [selectedSpaceId, setSelectedProjectId],
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
