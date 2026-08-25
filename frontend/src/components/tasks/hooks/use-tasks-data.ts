"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import useSWR from "swr";

import {
  taskApi,
  type Project,
  type Space,
  type Task,
  type Tag,
} from "@/lib/task-api";
import { listRemoteTasks, toRemoteTask } from "@/lib/remote-tasks";
import { resourceId } from "@/lib/remote-resource";

export type FetchDataOptions = {
  forceLoading?: boolean;
  notifySidebar?: boolean;
};

const EMPTY_TASKS: Task[] = [];
const EMPTY_TAGS: Tag[] = [];
const TASKS_CACHE_VERSION = "v2";

export type TasksCacheScopeProject = Pick<Project, "id" | "space_id" | "source">;

/**
 * ローカル一覧キャッシュは Space 単位（SWR キー `local/{selectedSpaceId ?? "all-spaces"}`）。
 * Project タブは見ない。
 */
export function taskMatchesLocalTasksCacheScope(
  task: Pick<Task, "project_id" | "source">,
  selectedSpaceId: string | null | undefined,
  projects: ReadonlyArray<TasksCacheScopeProject>,
): boolean {
  if (task.source === "remote") return false;
  if (selectedSpaceId != null && selectedSpaceId.startsWith("remote:")) {
    return false;
  }
  const project = projects.find((item) => item.id === task.project_id);
  if (!project || project.source === "remote") return false;
  if (selectedSpaceId == null) return true;
  return project.space_id === selectedSpaceId;
}

export function isRemoteTasksCacheScope(
  selectedSpaceId?: string | null,
  selectedSpace?: Pick<Space, "source"> | null,
  selectedProject?: Pick<Project, "source"> | null,
): boolean {
  return (
    selectedSpace?.source === "remote" ||
    selectedProject?.source === "remote" ||
    Boolean(selectedSpaceId?.startsWith("remote:"))
  );
}

export function shouldApplyTaskMutationToCurrentCache({
  task,
  removedTaskId,
  selectedSpaceId,
  projects,
  cachedTasks,
  draftTask,
  isRemoteCache = false,
}: {
  task?: Pick<Task, "id" | "project_id" | "source" | "parent_task_id"> | null;
  removedTaskId?: string;
  selectedSpaceId: string | null | undefined;
  projects: ReadonlyArray<TasksCacheScopeProject>;
  cachedTasks: ReadonlyArray<Pick<Task, "id">>;
  draftTask?: Pick<Partial<Task>, "project_id"> | null;
  isRemoteCache?: boolean;
}): boolean {
  const cachedIds = new Set(cachedTasks.map((item) => item.id));
  if (removedTaskId) {
    return cachedIds.has(removedTaskId);
  }
  if (!task) return false;
  if (cachedIds.has(task.id)) return true;
  if (isRemoteCache && task.source !== "remote") return false;
  if (task.parent_task_id) {
    if (cachedIds.has(task.parent_task_id)) return true;
    return (
      Boolean(draftTask) &&
      draftTask?.project_id === task.project_id &&
      taskMatchesLocalTasksCacheScope(task, selectedSpaceId, projects)
    );
  }
  return taskMatchesLocalTasksCacheScope(task, selectedSpaceId, projects);
}

export function removeTaskSubtrees(
  tasks: Task[],
  rootTaskIds: Iterable<string>,
): Task[] {
  const removedIds = new Set(rootTaskIds);
  let added = true;
  while (added) {
    added = false;
    for (const task of tasks) {
      if (
        task.parent_task_id &&
        removedIds.has(task.parent_task_id) &&
        !removedIds.has(task.id)
      ) {
        removedIds.add(task.id);
        added = true;
      }
    }
  }
  return tasks.filter((task) => !removedIds.has(task.id));
}

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
  selectedSpace?: Space | null,
) {
  const [loading, setLoading] = useState(true);
  const hasLoadedTasksRef = useRef(false);
  const remoteProject =
    selectedProject?.source === "remote" ? selectedProject : null;
  const parsedSelectedSpace = selectedSpaceId?.match(/^remote:([^:]+):(.+)$/);
  const parsedSelectedProject = selectedProjectId?.match(/^remote:([^:]+):(.+)$/);

  // Remote IDs are decorated as remote:{profile}:{resource}.  Prefer the
  // resource metadata supplied by ProjectContext, but safely recover the raw
  // ID for an empty remote space where no project is selected yet.
  const remoteSpaceProfileId =
    selectedSpace?.source === "remote"
      ? selectedSpace.remote_server_id ?? parsedSelectedSpace?.[1]
      : parsedSelectedSpace?.[1];
  const remoteSpaceResourceId =
    selectedSpace?.source === "remote"
      ? selectedSpace.resource_id ??
        (selectedSpaceId ? resourceId(selectedSpaceId) : null)
      : parsedSelectedSpace
        ? resourceId(selectedSpaceId)
        : null;
  const hasRemoteSpaceScope = Boolean(
    remoteSpaceProfileId && remoteSpaceResourceId,
  );

  const remoteProjectProfileId =
    remoteProject?.remote_server_id ?? parsedSelectedProject?.[1];
  const remoteProjectResourceId =
    remoteProject?.resource_id ??
    (selectedProjectId ? resourceId(selectedProjectId) : null);
  const hasRemoteProjectScope = Boolean(
    (remoteProject?.source === "remote" || parsedSelectedProject) &&
      remoteProjectProfileId &&
      remoteProjectResourceId,
  );

  // A remote space must win over the currently selected project so that
  // selecting 「全て」 fetches every project in that space.  During the
  // transient render where the project belongs to another profile, fall back
  // to that project scope instead of crossing profiles.
  const useRemoteSpaceScope = Boolean(
    hasRemoteSpaceScope &&
      (!remoteProject || remoteProjectProfileId === remoteSpaceProfileId),
  );
  const remoteServerId = useRemoteSpaceScope
    ? remoteSpaceProfileId
    : remoteProjectProfileId;
  const remoteResourceId = useRemoteSpaceScope
    ? remoteSpaceResourceId
    : remoteProjectResourceId;
  const isRemote = Boolean(
    remoteServerId &&
      remoteResourceId &&
      (useRemoteSpaceScope || hasRemoteProjectScope),
  );
  const remoteProfileSource =
    useRemoteSpaceScope && selectedSpace?.source === "remote"
      ? selectedSpace
      : remoteProject;
  const remoteServerName =
    remoteProfileSource?.remote_server_name ?? "Remote";
  const remoteServerColor = remoteProfileSource?.remote_server_color;
  const remoteServerBaseUrl = remoteProfileSource?.remote_server_base_url;
  const remoteScopeKind = useRemoteSpaceScope ? "space" : "project";
  const remoteScopeId = remoteResourceId ?? "invalid";
  const selectedSpaceLooksRemote = Boolean(
    selectedSpace?.source === "remote" || selectedSpaceId?.startsWith("remote:"),
  );
  const invalidRemoteSpaceScope =
    selectedSpaceLooksRemote && !hasRemoteSpaceScope && !hasRemoteProjectScope;
  // タスクのキャッシュ境界を実際の取得スコープと一致させる。
  // local は space 単位、remote は接続先の space/project 単位であり、
  // ヘッダーの selectedProjectId だけが変わっても同じ space のタスク配列を共有する。
  const tasksSwrKey = isRemote
    ? [
        "tasks-page",
        TASKS_CACHE_VERSION,
        "remote",
        remoteServerId,
        remoteScopeKind,
        remoteScopeId,
      ].join("/")
    : invalidRemoteSpaceScope
      ? [
          "tasks-page",
          TASKS_CACHE_VERSION,
          "remote-invalid",
          selectedSpaceId ?? "unknown",
        ].join("/")
      : [
        "tasks-page",
        TASKS_CACHE_VERSION,
        "local",
        selectedSpaceId ?? "all-spaces",
        ].join("/");
  // タグだけが project 単位のデータなので、タスク配列とは別キーで管理する。
  const tagsSwrKey =
    selectedProjectId && !isRemote
      ? ["tasks-page", TASKS_CACHE_VERSION, "tags", selectedProjectId].join("/")
      : null;

  // SWR fetcher。呼び出し時点の最新スコープを閉じ込む（revalidate は最新レンダーの
  // fetcher を使うため、スコープ変更後の fetchData で最新パラメータが反映される）。
  const tasksFetcher = useCallback(async (): Promise<Task[]> => {
    return isRemote
      ? (
          await listRemoteTasks(
            remoteServerId!,
            useRemoteSpaceScope
              ? { space_id: remoteResourceId! }
              : { project_id: remoteResourceId! },
          )
        ).map((task) =>
          toRemoteTask(
            {
              id: remoteServerId!,
              name: remoteServerName ?? "Remote",
              display_color: remoteServerColor,
              base_url: remoteServerBaseUrl,
            },
            task,
          ),
        )
      : invalidRemoteSpaceScope
        ? EMPTY_TASKS
      : await taskApi.listTasks(
          selectedSpaceId ? { space_id: selectedSpaceId } : undefined,
        );
  }, [
    invalidRemoteSpaceScope,
    isRemote,
    remoteResourceId,
    remoteServerBaseUrl,
    remoteServerColor,
    remoteServerId,
    remoteServerName,
    selectedSpaceId,
    useRemoteSpaceScope,
  ]);

  const tagsFetcher = useCallback(
    async (): Promise<Tag[]> =>
      selectedProjectId && !isRemote
        ? taskApi.listTags(selectedProjectId)
        : EMPTY_TAGS,
    [isRemote, selectedProjectId],
  );

  const {
    data: tasksData,
    error: tasksError,
    mutate: mutateTasks,
  } = useSWR<Task[]>(tasksSwrKey, tasksFetcher, {
    // 取得タイミングは呼び出し側の fetchData に委ねる（自動 revalidation は使わない）。
    // ただし低帯域配慮でキャッシュ（永続化含む）は有効化し、再訪時は前回データを即描画する。
    revalidateOnMount: false,
    revalidateOnFocus: false,
    // 再接続時のみ最新化（オフライン復帰時の取りこぼし防止・通信量は軽微）。
    revalidateOnReconnect: true,
    revalidateIfStale: false,
    // 別scopeの前回値は表示しない。同一scopeの永続キャッシュはキー別に復元される。
    keepPreviousData: false,
    // fetchData（= 手動 mutate）連打を重複排除する。timer-changed / task-list-refresh
    // などのイベントが短時間に重なっても実 fetch は 1 回に集約される。
    dedupingInterval: 5000,
  });
  const {
    data: tagsData,
    error: tagsError,
    mutate: mutateTags,
  } = useSWR<Tag[]>(tagsSwrKey, tagsFetcher, {
    revalidateOnMount: false,
    revalidateOnFocus: false,
    revalidateOnReconnect: true,
    revalidateIfStale: false,
    keepPreviousData: false,
    dedupingInterval: 5000,
  });

  const tasks = tasksData ?? EMPTY_TASKS;
  const embeddedTags = useMemo(() => {
    const tagsById = new Map<string, Tag>();
    tasks.forEach((task) =>
      task.tags?.forEach((tag) => tagsById.set(tag.id, tag)),
    );
    return Array.from(tagsById.values());
  }, [tasks]);
  const tags = tagsSwrKey ? (tagsData ?? EMPTY_TAGS) : embeddedTags;
  // fetchData が「キャッシュ済みデータがあるのに skeleton を出す」のを防ぐための参照。
  // render 中に ref を書かず、commit 後に同期する（値は fetchData 呼び出し時点で十分新しい）。
  const hasDataRef = useRef(false);
  useEffect(() => {
    hasDataRef.current = tasksData !== undefined;
  }, [tasksData]);
  useEffect(() => {
    hasLoadedTasksRef.current = false;
  }, [tasksSwrKey]);
  // 取得失敗時は SWR が直前の data を保持しつつ error を立てる。
  // 従来の loadError（成功で null / 失敗でメッセージ）と同義。
  const loadError =
    tasksError || tagsError
      ? tasksError instanceof Error
        ? tasksError.message
        : tagsError instanceof Error
          ? tagsError.message
          : "タスクを取得できませんでした"
      : null;

  // useState の setTasks と互換の署名（関数アップデータ対応）。
  // revalidate:false でローカルキャッシュのみ更新（楽観的更新）。
  const setTasks = useCallback<Dispatch<SetStateAction<Task[]>>>(
    (action) => {
      void mutateTasks(
        (current = EMPTY_TASKS) => {
          const nextTasks =
            typeof action === "function"
              ? (action as (prev: Task[]) => Task[])(current)
              : action;
          return nextTasks;
        },
        { revalidate: false },
      );
    },
    [mutateTasks],
  );

  const setTags = useCallback<Dispatch<SetStateAction<Tag[]>>>(
    (action) => {
      if (!tagsSwrKey) return;
      void mutateTags(
        (current = EMPTY_TAGS) => {
          const nextTags =
            typeof action === "function"
              ? (action as (prev: Tag[]) => Tag[])(current)
              : action;
          return nextTags;
        },
        { revalidate: false },
      );
    },
    [mutateTags, tagsSwrKey],
  );

  // タスク・タグ取得（従来の loading / loadError / sidebar 通知の挙動を維持）。
  const fetchData = useCallback(
    async (options: FetchDataOptions = {}) => {
      // キャッシュ済みデータが既にある場合は skeleton を出さず即描画→裏で再検証する。
      const shouldShowLoading =
        (options.forceLoading ?? !hasLoadedTasksRef.current) &&
        !hasDataRef.current;
      if (shouldShowLoading) setLoading(true);
      // SWR に revalidate を依頼。並行呼び出しは SWR が dedup / stale 破棄し、
      // 失敗時は error フィールドに反映される（bound mutate は reject しない）。
      const [taskResult, tagResult] = await Promise.all([
        mutateTasks(),
        tagsSwrKey ? mutateTags() : Promise.resolve(EMPTY_TAGS),
      ]);
      // 取得成功時のみサイドバーへ通知（従来挙動）。失敗時は result が undefined。
      if (
        taskResult !== undefined &&
        tagResult !== undefined &&
        options.notifySidebar !== false
      ) {
        window.dispatchEvent(new Event("task-sidebar-refresh"));
      }
      hasLoadedTasksRef.current = true;
      setLoading(false);
    },
    [mutateTags, mutateTasks, tagsSwrKey],
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
      // backendの再帰削除と揃え、子孫も永続SWRキャッシュから同時に除去する。
      setTasks((prev) => removeTaskSubtrees(prev, [taskId]));
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
