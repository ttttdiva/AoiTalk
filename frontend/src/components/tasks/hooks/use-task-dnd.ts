"use client";

import { useCallback, useRef, useState } from "react";
import type React from "react";

import { taskApi, type Task } from "@/lib/task-api";
import {
  buildAllTopLevelReorderIds,
  buildTopLevelReorderIds,
  resolveTaskDropMode,
  type DropMode,
} from "@/lib/task-reorder";
import {
  saveTaskUpdate,
  TASK_DND_MIME,
  TASK_PROJECT_TAB_ALL,
} from "@/lib/tasks-page-utils";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";

// ドラッグ&ドロップハンドラ
// 入力系（input/textarea/select/contenteditable）と明示的な `[data-no-drag="true"]`
// の内側だけドラッグ開始を抑制し、それ以外の行領域（タイトル/タグ/日時ボタン/タイマーボタン等）
// はどこでも掴んで並び替えできるようにする。click と drag は同じ mousedown から
// 分岐するので、クリック機能はそのまま動く。
const NO_DRAG_SELECTOR =
  'input, textarea, select, [contenteditable="true"], [data-no-drag="true"]';

/**
 * タスク一覧のドラッグ&ドロップ（並び替え / サブタスク化 / プロジェクト間移動）をまとめたフック。
 *
 * ClickUp 準拠のドロップモード：
 *   reorder-before: 行の上境界に挿入（前行と現在行の間）
 *   reorder-after:  行の下境界に挿入（現在行と次行の間）
 *   subtask-before: 上境界上の右側ドロップ → 前行のサブタスク化
 *   subtask-after:  下境界上の右側ドロップ → 現在行のサブタスク化
 */
export function useTaskDnd({
  tasks,
  projectIds,
  projectTab,
  fetchData,
  applyTaskPatchesLocally,
  applyTopLevelReorderLocally,
  applyAllTopLevelReorderLocally,
  setExpandedTasks,
  filteredTasksRef,
  selectedIdsRef,
}: {
  tasks: Task[];
  projectIds: Set<string>;
  projectTab: string;
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  applyTaskPatchesLocally: (patches: Map<string, Partial<Task>>) => void;
  applyTopLevelReorderLocally: (args: {
    projectId: string;
    newIds: string[];
    movingIds: string[];
    patches?: Map<string, Partial<Task>>;
  }) => void;
  applyAllTopLevelReorderLocally: (args: {
    newIds: string[];
    movingIds: string[];
  }) => void;
  setExpandedTasks: React.Dispatch<React.SetStateAction<Set<string>>>;
  filteredTasksRef: React.RefObject<Task[]>;
  selectedIdsRef: React.RefObject<Set<string>>;
}) {
  const [, setDragId] = useState<string | null>(null);
  const [draggingIds, setDraggingIds] = useState<string[]>([]);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [dropMode, setDropMode] = useState<DropMode>("reorder-after");
  const dragIdRef = useRef<string | null>(null);
  const dragIdsRef = useRef<string[]>([]);

  const readDragPayload = useCallback((dataTransfer: DataTransfer) => {
    try {
      const raw = dataTransfer.getData(TASK_DND_MIME);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as unknown;
      if (!parsed || typeof parsed !== "object") return null;
      const payload = parsed as { draggedId?: unknown; draggedIds?: unknown };
      const draggedId =
        typeof payload.draggedId === "string" ? payload.draggedId : null;
      const draggedIds = Array.isArray(payload.draggedIds)
        ? payload.draggedIds.filter(
            (id): id is string => typeof id === "string",
          )
        : [];
      if (!draggedId || draggedIds.length === 0) return null;
      return { draggedId, draggedIds };
    } catch {
      return null;
    }
  }, []);

  const handleDragStart = useCallback(
    (e: React.DragEvent, taskId: string) => {
      const target = e.target as HTMLElement | null;
      if (target && target.closest(NO_DRAG_SELECTOR)) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      const currentSelectedIds = selectedIdsRef.current;
      const visibleSelectedIds = filteredTasksRef.current
        .filter((task) => currentSelectedIds.has(task.id))
        .map((task) => task.id);
      const nextDraggingIds =
        currentSelectedIds.has(taskId) && visibleSelectedIds.length > 1
          ? visibleSelectedIds
          : [taskId];
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", taskId);
      e.dataTransfer.setData(
        TASK_DND_MIME,
        JSON.stringify({ draggedId: taskId, draggedIds: nextDraggingIds }),
      );
      dragIdRef.current = taskId;
      dragIdsRef.current = nextDraggingIds;
      setDragId(taskId);
      setDraggingIds(nextDraggingIds);
    },
    [filteredTasksRef, selectedIdsRef],
  );

  const handleDragOver = useCallback(
    (e: React.DragEvent, taskId: string) => {
      if (!dragIdsRef.current.length) {
        const payload = readDragPayload(e.dataTransfer);
        if (!payload) return;
        dragIdRef.current = payload.draggedId;
        dragIdsRef.current = payload.draggedIds;
        setDragId(payload.draggedId);
        setDraggingIds(payload.draggedIds);
      }
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (dropTargetId !== taskId) setDropTargetId(taskId);

      // ClickUp 流の境界ベース判定:
      //   relY < 0.5: 前の境界（前行と現在行の間）
      //   relY >= 0.5: 後の境界（現在行と次行の間）
      //   relX > 0.25: 右にインデント = subtask化
      //     - 前の境界 + 右 → 前行のサブタスク
      //     - 後の境界 + 右 → 現在行のサブタスク
      const nextMode = resolveTaskDropMode(
        { clientX: e.clientX, clientY: e.clientY },
        (e.currentTarget as HTMLElement).getBoundingClientRect(),
      );
      if (dropMode !== nextMode) setDropMode(nextMode);
    },
    [dropTargetId, dropMode, readDragPayload],
  );

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    // 子要素間の遷移では dragLeave が誤発火するので、行の外側に出た時だけクリア
    const tr = e.currentTarget as HTMLElement;
    const related = e.relatedTarget as Node | null;
    if (related && tr.contains(related)) return;
    setDropTargetId(null);
    setDropMode("reorder-after");
  }, []);

  const handleDrop = useCallback(
    async (e: React.DragEvent, dropOnTaskId: string) => {
      e.preventDefault();
      const payload = readDragPayload(e.dataTransfer);
      const draggedId = dragIdRef.current ?? payload?.draggedId ?? null;
      const draggedIds =
        dragIdsRef.current.length > 0
          ? dragIdsRef.current
          : (payload?.draggedIds ?? []);
      // drop 時点のマウス座標から最終的な dropMode を再判定する
      let currentDropMode = resolveTaskDropMode(
        { clientX: e.clientX, clientY: e.clientY },
        (e.currentTarget as HTMLElement).getBoundingClientRect(),
      );

      let dropTask = tasks.find((t) => t.id === dropOnTaskId);
      const dragTask = tasks.find((t) => t.id === draggedId);
      const draggedTasks = tasks.filter((t) => draggedIds.includes(t.id));

      setDragId(null);
      setDraggingIds([]);
      setDropTargetId(null);
      setDropMode("reorder-after");
      dragIdRef.current = null;
      dragIdsRef.current = [];

      if (!draggedId || draggedIds.length === 0) return;
      if (!dropTask || !dragTask || draggedTasks.length === 0) return;
      if (draggedIds.includes(dropOnTaskId)) return;
      const draggedProjectIds = new Set(
        draggedTasks.map((task) => task.project_id),
      );
      const isCrossProjectDrop =
        draggedProjectIds.size > 1 ||
        !draggedProjectIds.has(dropTask.project_id);
      if (
        projectTab === TASK_PROJECT_TAB_ALL &&
        (isCrossProjectDrop ||
          currentDropMode === "reorder-before" ||
          currentDropMode === "reorder-after")
      ) {
        const globalDropMode: DropMode =
          currentDropMode === "reorder-before" ||
          currentDropMode === "subtask-before"
            ? "reorder-before"
            : "reorder-after";
        const newIds = buildAllTopLevelReorderIds({
          taskList: tasks,
          projectIds,
          movingIds: draggedIds,
          dropTask,
          dropMode: globalDropMode,
        });
        if (!newIds) {
          await fetchData();
          return;
        }

        applyAllTopLevelReorderLocally({
          newIds,
          movingIds: draggedIds,
        });

        try {
          await Promise.all(
            draggedTasks.map((task) =>
              task.parent_task_id
                ? saveTaskUpdate(
                    task.id,
                    { parent_task_id: null },
                    task.project_id,
                  )
                : Promise.resolve(task),
            ),
          );
          await taskApi.reorderAllTasks(newIds);
          void fetchData({ notifySidebar: false });
        } catch (err) {
          console.error("ALL表示の並び替え保存失敗:", err);
          await fetchData();
        }
        return;
      }
      const initialDropProjectId = dropTask.project_id;
      if (
        draggedTasks.some((task) => task.project_id !== initialDropProjectId)
      ) {
        const sourceProjectId = draggedTasks[0]?.project_id;
        const canReorderWithinSourceProject =
          !!sourceProjectId &&
          draggedTasks.every((task) => task.project_id === sourceProjectId);

        if (!canReorderWithinSourceProject) {
          await fetchData({ notifySidebar: false });
          return;
        }

        const movingSet = new Set(draggedIds);
        const visibleTasks = filteredTasksRef.current;
        const visualAnchorId = dropTask.parent_task_id || dropOnTaskId;
        const visualIndex = visibleTasks.findIndex(
          (task) => task.id === visualAnchorId,
        );
        if (visualIndex === -1) {
          await fetchData({ notifySidebar: false });
          return;
        }

        const isBeforeDrop =
          currentDropMode === "reorder-before" ||
          currentDropMode === "subtask-before";
        let fallbackAnchor: Task | null = null;
        let fallbackDropMode: DropMode = "reorder-after";

        if (isBeforeDrop) {
          for (let index = visualIndex - 1; index >= 0; index -= 1) {
            const candidate = visibleTasks[index];
            if (
              candidate.project_id === sourceProjectId &&
              !movingSet.has(candidate.id)
            ) {
              fallbackAnchor = candidate;
              fallbackDropMode = "reorder-after";
              break;
            }
          }
          if (!fallbackAnchor) {
            for (
              let index = visualIndex;
              index < visibleTasks.length;
              index += 1
            ) {
              const candidate = visibleTasks[index];
              if (
                candidate.project_id === sourceProjectId &&
                !movingSet.has(candidate.id)
              ) {
                fallbackAnchor = candidate;
                fallbackDropMode = "reorder-before";
                break;
              }
            }
          }
        } else {
          for (
            let index = visualIndex + 1;
            index < visibleTasks.length;
            index += 1
          ) {
            const candidate = visibleTasks[index];
            if (
              candidate.project_id === sourceProjectId &&
              !movingSet.has(candidate.id)
            ) {
              fallbackAnchor = candidate;
              fallbackDropMode = "reorder-before";
              break;
            }
          }
          if (!fallbackAnchor) {
            for (let index = visualIndex; index >= 0; index -= 1) {
              const candidate = visibleTasks[index];
              if (
                candidate.project_id === sourceProjectId &&
                !movingSet.has(candidate.id)
              ) {
                fallbackAnchor = candidate;
                fallbackDropMode = "reorder-after";
                break;
              }
            }
          }
        }

        if (!fallbackAnchor) {
          await fetchData({ notifySidebar: false });
          return;
        }
        dropTask = fallbackAnchor;
        currentDropMode = fallbackDropMode;
      }

      if (draggedIds.length > 1) {
        if (
          currentDropMode === "subtask-before" ||
          currentDropMode === "subtask-after"
        ) {
          let newParentId: string | null = null;
          if (currentDropMode === "subtask-after") {
            newParentId = dropTask.parent_task_id || dropOnTaskId;
          } else {
            const sameLevel = tasks.filter(
              (t) => !t.parent_task_id && t.project_id === dropTask.project_id,
            );
            const idx = sameLevel.findIndex((t) => t.id === dropOnTaskId);
            const prev = idx > 0 ? sameLevel[idx - 1] : null;
            if (prev && !draggedIds.includes(prev.id)) {
              newParentId = prev.id;
            } else {
              currentDropMode = "reorder-before";
            }
          }

          if (
            newParentId &&
            (currentDropMode === "subtask-before" ||
              currentDropMode === "subtask-after")
          ) {
            const parentTask = tasks.find((t) => t.id === newParentId);
            const optimisticPatches = new Map<string, Partial<Task>>();
            for (const task of draggedTasks) {
              optimisticPatches.set(task.id, {
                parent_task_id: newParentId,
                ...(parentTask && task.project_id !== parentTask.project_id
                  ? { project_id: parentTask.project_id }
                  : {}),
              });
            }
            applyTaskPatchesLocally(optimisticPatches);
            setExpandedTasks((prev) => new Set([...prev, newParentId!]));
            try {
              await Promise.all(
                draggedTasks.map((task) => {
                  const patch: Record<string, unknown> = {
                    parent_task_id: newParentId,
                  };
                  if (parentTask && task.project_id !== parentTask.project_id) {
                    patch.project_id = parentTask.project_id;
                  }
                  return saveTaskUpdate(task.id, patch, task.project_id);
                }),
              );
              void fetchData({ notifySidebar: false });
            } catch (err) {
              console.error("複数タスクのサブタスク化失敗:", err);
              await fetchData();
            }
            return;
          }
        }

        const projectId = dropTask.project_id;
        const optimisticPatches = new Map<string, Partial<Task>>();
        for (const task of draggedTasks) {
          const patch: Partial<Task> = {};
          if (task.parent_task_id) patch.parent_task_id = null;
          if (task.project_id !== projectId) patch.project_id = projectId;
          if (Object.keys(patch).length > 0) {
            optimisticPatches.set(task.id, patch);
          }
        }
        const newIds = buildTopLevelReorderIds({
          taskList: tasks,
          projectId,
          movingIds: draggedIds,
          dropTask,
          dropMode: currentDropMode,
        });
        if (!newIds) {
          await fetchData();
          return;
        }
        applyTopLevelReorderLocally({
          projectId,
          newIds,
          movingIds: draggedIds,
          patches: optimisticPatches,
        });
        try {
          await Promise.all(
            draggedTasks.map((task) => {
              const patch: Record<string, unknown> = {};
              if (task.parent_task_id) patch.parent_task_id = null;
              if (task.project_id !== projectId) patch.project_id = projectId;
              if (Object.keys(patch).length === 0) return Promise.resolve(task);
              return saveTaskUpdate(task.id, patch, task.project_id);
            }),
          );
          await taskApi.reorderTasks(projectId, newIds);
          void fetchData({ notifySidebar: false });
        } catch (err) {
          console.error("複数タスクの移動失敗:", err);
          await fetchData();
        }
        return;
      }

      // --- サブタスク化モード ---
      // subtask-before: 前行のサブタスクになる
      // subtask-after:  現在行のサブタスクになる
      if (
        currentDropMode === "subtask-before" ||
        currentDropMode === "subtask-after"
      ) {
        // 対象親を特定
        let newParentId: string | null = null;
        if (currentDropMode === "subtask-after") {
          // 現在行がサブタスクならその親、そうでなければ現在行そのもの
          newParentId = dropTask.parent_task_id || dropOnTaskId;
        } else {
          // 前の境界: 同プロジェクトの親タスク一覧から現在行の前の同階層タスクを探す
          const sameLevel = tasks.filter(
            (t) => !t.parent_task_id && t.project_id === dropTask.project_id,
          );
          const idx = sameLevel.findIndex((t) => t.id === dropOnTaskId);
          const prev = idx > 0 ? sameLevel[idx - 1] : null;
          if (prev && !draggedIds.includes(prev.id)) {
            newParentId = prev.id;
          } else {
            // 前がない / 自分自身 → サブタスク化不可なので reorder-before にフォールバック
            currentDropMode = "reorder-before";
          }
        }

        if (
          newParentId &&
          (currentDropMode === "subtask-before" ||
            currentDropMode === "subtask-after")
        ) {
          if (draggedIds.includes(newParentId)) return;
          const parentTask = tasks.find((t) => t.id === newParentId);
          const optimisticPatches = new Map<string, Partial<Task>>();
          for (const movedTask of draggedTasks) {
            optimisticPatches.set(movedTask.id, {
              parent_task_id: newParentId,
              ...(parentTask && movedTask.project_id !== parentTask.project_id
                ? { project_id: parentTask.project_id }
                : {}),
            });
          }
          applyTaskPatchesLocally(optimisticPatches);
          setExpandedTasks((prev) => new Set([...prev, newParentId!]));
          try {
            await Promise.all(
              draggedTasks.map((dragTask) => {
                const patch: Record<string, unknown> = {
                  parent_task_id: newParentId,
                };
                if (
                  parentTask &&
                  dragTask.project_id !== parentTask.project_id
                ) {
                  patch.project_id = parentTask.project_id;
                }
                return saveTaskUpdate(dragTask.id, patch, dragTask.project_id);
              }),
            );
            void fetchData({ notifySidebar: false });
          } catch (err) {
            console.error("サブタスク化失敗:", err);
            await fetchData();
          }
          return;
        }
      }

      // --- 並び替えモード ---
      // 基準プロジェクト = ドロップ先のプロジェクト（別プロジェクト間の移動も許容）
      const projectId = dropTask.project_id;

      // サブタスクだった場合は親解除、別プロジェクトなら合わせる
      const needsPatch: Record<string, unknown> = {};
      if (dragTask.parent_task_id) needsPatch.parent_task_id = null;
      if (dragTask.project_id !== projectId) needsPatch.project_id = projectId;
      if (Object.keys(needsPatch).length > 0) {
        try {
          await saveTaskUpdate(draggedId, needsPatch, dragTask.project_id);
        } catch (err) {
          console.error("タスクの親/プロジェクト更新失敗:", err);
          await fetchData();
          return;
        }
      }

      const newIds = buildTopLevelReorderIds({
        taskList: tasks,
        projectId,
        movingIds: [draggedId],
        dropTask,
        dropMode: currentDropMode,
      });
      if (!newIds) {
        await fetchData();
        return;
      }

      applyTopLevelReorderLocally({
        projectId,
        newIds,
        movingIds: [draggedId],
        patches: new Map([[draggedId, needsPatch as Partial<Task>]]),
      });

      try {
        await taskApi.reorderTasks(projectId, newIds);
        void fetchData({ notifySidebar: false });
      } catch (err) {
        console.error("並び替え保存失敗:", err);
        await fetchData();
      }
      return;
    },
    [
      applyTaskPatchesLocally,
      applyAllTopLevelReorderLocally,
      applyTopLevelReorderLocally,
      tasks,
      fetchData,
      filteredTasksRef,
      projectIds,
      projectTab,
      readDragPayload,
      setExpandedTasks,
    ],
  );

  const handleDragEnd = useCallback(() => {
    setDragId(null);
    setDraggingIds([]);
    setDropTargetId(null);
    setDropMode("reorder-after");
    dragIdRef.current = null;
    dragIdsRef.current = [];
  }, []);

  return {
    draggingIds,
    dropTargetId,
    dropMode,
    handleDragStart,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    handleDragEnd,
  };
}
