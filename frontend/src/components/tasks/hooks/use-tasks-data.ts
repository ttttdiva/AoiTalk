"use client";

import { useCallback, useRef, useState } from "react";

import { taskApi, type Task, type Tag } from "@/lib/task-api";

export type FetchDataOptions = {
  forceLoading?: boolean;
  notifySidebar?: boolean;
};

/**
 * タスク一覧ページのデータ取得とローカル更新（楽観的更新）をまとめたフック。
 */
export function useTasksData(selectedProjectId: string | null) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);
  const hasLoadedTasksRef = useRef(false);

  // タスク・タグ取得
  const fetchData = useCallback(
    async (options: FetchDataOptions = {}) => {
      const shouldShowLoading =
        options.forceLoading ?? !hasLoadedTasksRef.current;
      if (shouldShowLoading) setLoading(true);
      try {
        const taskList = await taskApi.listTasks();
        setTasks(taskList);

        // タグはselectedProjectIdがある場合のみ取得
        if (selectedProjectId) {
          const tagList = await taskApi.listTags(selectedProjectId);
          setTags(tagList);
        }
        if (options.notifySidebar !== false) {
          window.dispatchEvent(new Event("task-sidebar-refresh"));
        }
      } catch (err) {
        console.error("データ取得失敗:", err);
      } finally {
        hasLoadedTasksRef.current = true;
        setLoading(false);
      }
    },
    [selectedProjectId],
  );

  const upsertTaskLocally = useCallback((task: Task) => {
    setTasks((prev) => {
      const index = prev.findIndex((item) => item.id === task.id);
      if (index === -1) return [task, ...prev];
      const next = [...prev];
      next[index] = task;
      return next;
    });
  }, []);

  const removeTaskLocally = useCallback((taskId: string) => {
    setTasks((prev) => prev.filter((item) => item.id !== taskId));
  }, []);

  const applyTaskPatchLocally = useCallback(
    (taskId: string, patch: Partial<Task>) => {
      setTasks((prev) =>
        prev.map((item) => (item.id === taskId ? { ...item, ...patch } : item)),
      );
    },
    [],
  );

  const applyTaskPatchesLocally = useCallback(
    (patches: Map<string, Partial<Task>>) => {
      if (patches.size === 0) return;
      setTasks((prev) =>
        prev.map((item) => {
          const patch = patches.get(item.id);
          return patch ? { ...item, ...patch } : item;
        }),
      );
    },
    [],
  );

  const applyTopLevelReorderLocally = useCallback(
    ({
      projectId,
      newIds,
      movingIds,
      patches,
    }: {
      projectId: string;
      newIds: string[];
      movingIds: string[];
      patches?: Map<string, Partial<Task>>;
    }) => {
      const movingSet = new Set(movingIds);
      const orderedIdSet = new Set(newIds);

      setTasks((prev) => {
        const patched = prev.map((item) => {
          const patch = patches?.get(item.id);
          const sortIndex = newIds.indexOf(item.id);
          if (!patch && !movingSet.has(item.id) && sortIndex === -1) {
            return item;
          }
          return {
            ...item,
            ...(patch || {}),
            ...(movingSet.has(item.id)
              ? { project_id: projectId, parent_task_id: null }
              : {}),
            ...(sortIndex >= 0 ? { sort_order: sortIndex } : {}),
          };
        });
        const taskById = new Map(patched.map((item) => [item.id, item]));
        const reordered = newIds
          .map((id) => taskById.get(id))
          .filter((item): item is Task => !!item);
        if (reordered.length === 0) return patched;

        const firstAffectedIndex = patched.findIndex(
          (item) =>
            (item.project_id === projectId && !item.parent_task_id) ||
            movingSet.has(item.id),
        );
        const withoutReordered = patched.filter(
          (item) =>
            !(
              item.project_id === projectId &&
              !item.parent_task_id &&
              orderedIdSet.has(item.id)
            ),
        );
        const insertIndex =
          firstAffectedIndex === -1
            ? withoutReordered.length
            : Math.min(firstAffectedIndex, withoutReordered.length);
        const next = [...withoutReordered];
        next.splice(insertIndex, 0, ...reordered);
        return next;
      });
    },
    [],
  );

  const applyAllTopLevelReorderLocally = useCallback(
    ({ newIds, movingIds }: { newIds: string[]; movingIds: string[] }) => {
      const movingSet = new Set(movingIds);
      const orderedIdSet = new Set(newIds);

      setTasks((prev) => {
        const patched = prev.map((item) => {
          const sortIndex = newIds.indexOf(item.id);
          if (!movingSet.has(item.id) && sortIndex === -1) return item;
          return {
            ...item,
            ...(movingSet.has(item.id) ? { parent_task_id: null } : {}),
            ...(sortIndex >= 0 ? { sort_order: sortIndex } : {}),
          };
        });
        const taskById = new Map(patched.map((item) => [item.id, item]));
        const reordered = newIds
          .map((id) => taskById.get(id))
          .filter((item): item is Task => !!item);
        if (reordered.length === 0) return patched;

        const firstAffectedIndex = patched.findIndex(
          (item) =>
            (!item.parent_task_id && orderedIdSet.has(item.id)) ||
            movingSet.has(item.id),
        );
        const withoutReordered = patched.filter(
          (item) => !(orderedIdSet.has(item.id) && !item.parent_task_id),
        );
        const insertIndex =
          firstAffectedIndex === -1
            ? withoutReordered.length
            : Math.min(firstAffectedIndex, withoutReordered.length);
        const next = [...withoutReordered];
        next.splice(insertIndex, 0, ...reordered);
        return next;
      });
    },
    [],
  );

  return {
    tasks,
    setTasks,
    tags,
    setTags,
    loading,
    fetchData,
    hasLoadedTasksRef,
    upsertTaskLocally,
    removeTaskLocally,
    applyTaskPatchLocally,
    applyTaskPatchesLocally,
    applyTopLevelReorderLocally,
    applyAllTopLevelReorderLocally,
  };
}
