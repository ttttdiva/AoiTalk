"use client";

import {
  useCallback,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import useSWR from "swr";

import { taskApi, type Project, type Task, type Tag } from "@/lib/task-api";
import { listRemoteTasks, toRemoteTask } from "@/lib/remote-tasks";

export type FetchDataOptions = {
  forceLoading?: boolean;
  notifySidebar?: boolean;
};

type TasksData = {
  tasks: Task[];
  tags: Tag[];
};

// SWR キャッシュキー。タスク一覧ページで一意なので固定文字列を使う。
// スコープ（project/space）変更時の再取得は従来どおり呼び出し側の
// fetchData（= 手動 revalidate）で駆動し、キー変更による二重 fetch を避ける。
const TASKS_SWR_KEY = "tasks-page/tasks";

const EMPTY_TASKS: Task[] = [];
const EMPTY_TAGS: Tag[] = [];

/**
 * タスク一覧ページのデータ取得とローカル更新（楽観的更新）をまとめたフック。
 *
 * 取得・キャッシュ・リクエスト重複排除・競合（stale response 破棄）は SWR に委譲する。
 * 公開 API（tasks/setTasks/tags/setTags/loading/loadError/fetchData/各ローカル更新関数）は
 * 従来の useState 実装と完全互換で、表示挙動は不変。
 */
export function useTasksData(
  selectedProjectId: string | null,
  selectedProject?: Project | null,
  selectedSpaceId?: string | null,
) {
  const [loading, setLoading] = useState(true);
  const hasLoadedTasksRef = useRef(false);
  const remoteProject =
    selectedProject?.source === "remote" ? selectedProject : null;
  const remoteServerId = remoteProject?.remote_server_id;
  const remoteResourceId = remoteProject?.resource_id;
  const remoteServerName = remoteProject?.remote_server_name;
  const remoteServerColor = remoteProject?.remote_server_color;
  const remoteServerBaseUrl = remoteProject?.remote_server_base_url;

  // SWR fetcher。呼び出し時点の最新スコープを閉じ込む（revalidate は最新レンダーの
  // fetcher を使うため、スコープ変更後の fetchData で最新パラメータが反映される）。
  const fetcher = useCallback(async (): Promise<TasksData> => {
    const isRemote = remoteServerId && remoteResourceId;
    const taskList = isRemote
      ? (
          await listRemoteTasks(remoteServerId, {
            project_id: remoteResourceId,
          })
        ).map((task) =>
          toRemoteTask(
            {
              id: remoteServerId,
              name: remoteServerName ?? "Remote",
              display_color: remoteServerColor,
              base_url: remoteServerBaseUrl,
            },
            task,
          ),
        )
      : await taskApi.listTasks(
          selectedSpaceId ? { space_id: selectedSpaceId } : undefined,
        );

    // タグは selectedProjectId がある場合のみ取得
    if (selectedProjectId && !isRemote) {
      const tagList = await taskApi.listTags(selectedProjectId);
      return { tasks: taskList, tags: tagList };
    }
    const tagsById = new Map<string, Tag>();
    taskList.forEach((task) =>
      task.tags?.forEach((tag) => tagsById.set(tag.id, tag)),
    );
    return { tasks: taskList, tags: Array.from(tagsById.values()) };
  }, [
    remoteResourceId,
    remoteServerBaseUrl,
    remoteServerColor,
    remoteServerId,
    remoteServerName,
    selectedProjectId,
    selectedSpaceId,
  ]);

  const { data, error, mutate } = useSWR<TasksData>(TASKS_SWR_KEY, fetcher, {
    // 取得タイミングは従来実装（呼び出し側の fetchData）に完全一致させるため、
    // SWR の自動 revalidation は全て無効化する。全ての取得は fetchData 経由。
    revalidateOnMount: false,
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    revalidateIfStale: false,
    keepPreviousData: true,
    // fetchData ごとに実取得する（従来は毎回 fetch）。並行時は SWR が
    // 最新リクエストの結果のみ採用し、古い結果を破棄する。
    dedupingInterval: 0,
  });

  const tasks = data?.tasks ?? EMPTY_TASKS;
  const tags = data?.tags ?? EMPTY_TAGS;
  // 取得失敗時は SWR が直前の data を保持しつつ error を立てる。
  // 従来の loadError（成功で null / 失敗でメッセージ）と同義。
  const loadError = error
    ? error instanceof Error
      ? error.message
      : "タスクを取得できませんでした"
    : null;

  // useState の setTasks と互換の署名（関数アップデータ対応）。
  // revalidate:false でローカルキャッシュのみ更新（楽観的更新）。
  const setTasks = useCallback<Dispatch<SetStateAction<Task[]>>>(
    (action) => {
      void mutate(
        (current) => {
          const cur = current?.tasks ?? EMPTY_TASKS;
          const nextTasks =
            typeof action === "function"
              ? (action as (prev: Task[]) => Task[])(cur)
              : action;
          return { tasks: nextTasks, tags: current?.tags ?? EMPTY_TAGS };
        },
        { revalidate: false },
      );
    },
    [mutate],
  );

  const setTags = useCallback<Dispatch<SetStateAction<Tag[]>>>(
    (action) => {
      void mutate(
        (current) => {
          const cur = current?.tags ?? EMPTY_TAGS;
          const nextTags =
            typeof action === "function"
              ? (action as (prev: Tag[]) => Tag[])(cur)
              : action;
          return { tasks: current?.tasks ?? EMPTY_TASKS, tags: nextTags };
        },
        { revalidate: false },
      );
    },
    [mutate],
  );

  // タスク・タグ取得（従来の loading / loadError / sidebar 通知の挙動を維持）。
  const fetchData = useCallback(
    async (options: FetchDataOptions = {}) => {
      const shouldShowLoading =
        options.forceLoading ?? !hasLoadedTasksRef.current;
      if (shouldShowLoading) setLoading(true);
      // SWR に revalidate を依頼。並行呼び出しは SWR が dedup / stale 破棄し、
      // 失敗時は error フィールドに反映される（bound mutate は reject しない）。
      const result = await mutate();
      // 取得成功時のみサイドバーへ通知（従来挙動）。失敗時は result が undefined。
      if (result !== undefined && options.notifySidebar !== false) {
        window.dispatchEvent(new Event("task-sidebar-refresh"));
      }
      hasLoadedTasksRef.current = true;
      setLoading(false);
    },
    [mutate],
  );

  const upsertTaskLocally = useCallback(
    (task: Task) => {
      setTasks((prev) => {
        const index = prev.findIndex((item) => item.id === task.id);
        if (index === -1) return [task, ...prev];
        const next = [...prev];
        next[index] = task;
        return next;
      });
    },
    [setTasks],
  );

  const removeTaskLocally = useCallback(
    (taskId: string) => {
      setTasks((prev) => prev.filter((item) => item.id !== taskId));
    },
    [setTasks],
  );

  const applyTaskPatchLocally = useCallback(
    (taskId: string, patch: Partial<Task>) => {
      setTasks((prev) =>
        prev.map((item) => (item.id === taskId ? { ...item, ...patch } : item)),
      );
    },
    [setTasks],
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
    [setTasks],
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
    [setTasks],
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
    [setTasks],
  );

  return {
    tasks,
    setTasks,
    tags,
    setTags,
    loading,
    loadError,
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
