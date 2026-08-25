"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import {
  createTaskDependency,
  deleteTaskDependency,
  listTaskDependencies,
  type CreateTaskDependencyInput,
  type TaskDependency,
} from "@/lib/task-dependency-api";
import {
  projectTaskDependencyCacheKey,
  taskDependencyCacheKey,
} from "@/components/tasks/hooks/task-dependency-cache-keys";

const EMPTY_DEPENDENCIES: TaskDependency[] = [];
let optimisticDependencySequence = 0;

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "依存関係の更新に失敗しました";
}

export function useTaskDependencies({
  taskId,
  projectId,
  enabled,
}: {
  taskId: string | null;
  projectId?: string | null;
  enabled: boolean;
}) {
  const { cache, mutate: mutateCache } = useSWRConfig();
  const key = enabled && taskId ? taskDependencyCacheKey(taskId) : null;
  const {
    data,
    error: loadError,
    isLoading,
    mutate,
  } = useSWR<TaskDependency[]>(
    key,
    () => listTaskDependencies({ taskId: taskId! }),
    {
      keepPreviousData: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
    },
  );
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const mutationInFlightRef = useRef(false);

  const updateOtherIncidentCaches = useCallback(
    async (
      incidentTaskIds: readonly string[],
      update: (current: TaskDependency[]) => TaskDependency[],
    ) => {
      if (!taskId) return;
      await Promise.all(
        [...new Set(incidentTaskIds)]
          .filter((incidentTaskId) => incidentTaskId !== taskId)
          .map(async (incidentTaskId) => {
            const incidentKey = taskDependencyCacheKey(incidentTaskId);
            const cached = cache.get(incidentKey) as
              | { data?: TaskDependency[] }
              | undefined;
            // 未取得endpointへ部分データだけを注入すると、初回GETが抑止される。
            // すでに完全な一覧を取得済みのcacheだけを同期する。
            if (!Array.isArray(cached?.data)) return;
            await mutateCache<TaskDependency[]>(
              incidentKey,
              (current = EMPTY_DEPENDENCIES) => update(current),
              { revalidate: false },
            );
          }),
      );
    },
    [cache, mutateCache, taskId],
  );

  const updateFetchedProjectCache = useCallback(
    async (update: (current: TaskDependency[]) => TaskDependency[]) => {
      if (!projectId) return;
      const projectKey = projectTaskDependencyCacheKey(projectId);
      const cached = cache.get(projectKey) as
        | { data?: TaskDependency[] }
        | undefined;
      // task endpointはproject全体の一部なので、未取得project cacheへは注入しない。
      if (!Array.isArray(cached?.data)) return;
      await mutateCache<TaskDependency[]>(
        projectKey,
        (current = EMPTY_DEPENDENCIES) => update(current),
        { revalidate: false },
      );
    },
    [cache, mutateCache, projectId],
  );

  useEffect(() => {
    setMutationError(null);
    setAdding(false);
    setDeletingIds(new Set());
    mutationInFlightRef.current = false;
  }, [taskId]);

  const addDependency = useCallback(
    async (input: CreateTaskDependencyInput): Promise<boolean> => {
      if (!key || mutationInFlightRef.current) return false;
      mutationInFlightRef.current = true;
      setAdding(true);
      setMutationError(null);
      optimisticDependencySequence += 1;
      const optimisticId = `optimistic:${optimisticDependencySequence}`;
      const optimisticDependency: TaskDependency = {
        id: optimisticId,
        task_id: input.task_id,
        depends_on_task_id: input.depends_on_task_id,
        created_at: null,
      };

      try {
        await mutate(
          async (current = EMPTY_DEPENDENCIES) => {
            const created = await createTaskDependency(input);
            return [
              ...current.filter((item) => item.id !== optimisticId),
              created,
            ];
          },
          {
            optimisticData: (current = EMPTY_DEPENDENCIES) => [
              ...current,
              optimisticDependency,
            ],
            populateCache: true,
            revalidate: false,
            rollbackOnError: true,
          },
        );
        const current = cache.get(key) as
          | { data?: TaskDependency[] }
          | undefined;
        const created = current?.data?.find(
          (item) =>
            item.task_id === input.task_id &&
            item.depends_on_task_id === input.depends_on_task_id,
        );
        if (created) {
          const addCreated = (dependencies: TaskDependency[]) =>
            dependencies.some((item) => item.id === created.id)
              ? dependencies
              : [...dependencies, created];
          await Promise.all([
            updateOtherIncidentCaches(
              [created.task_id, created.depends_on_task_id],
              addCreated,
            ),
            updateFetchedProjectCache(addCreated),
          ]);
        }
        return true;
      } catch (error) {
        setMutationError(errorMessage(error));
        return false;
      } finally {
        mutationInFlightRef.current = false;
        setAdding(false);
      }
    },
    [
      cache,
      key,
      mutate,
      updateFetchedProjectCache,
      updateOtherIncidentCaches,
    ],
  );

  const removeDependency = useCallback(
    async (dependencyId: string): Promise<boolean> => {
      if (!key || mutationInFlightRef.current) return false;
      mutationInFlightRef.current = true;
      setDeletingIds(new Set([dependencyId]));
      setMutationError(null);
      const removingDependency = data?.find((item) => item.id === dependencyId);

      try {
        await mutate(
          async (current = EMPTY_DEPENDENCIES) => {
            await deleteTaskDependency(dependencyId);
            return current.filter((item) => item.id !== dependencyId);
          },
          {
            optimisticData: (current = EMPTY_DEPENDENCIES) =>
              current.filter((item) => item.id !== dependencyId),
            populateCache: true,
            revalidate: false,
            rollbackOnError: true,
          },
        );
        if (removingDependency) {
          const removeDeleted = (dependencies: TaskDependency[]) =>
            dependencies.filter((item) => item.id !== dependencyId);
          await Promise.all([
            updateOtherIncidentCaches(
              [
                removingDependency.task_id,
                removingDependency.depends_on_task_id,
              ],
              removeDeleted,
            ),
            updateFetchedProjectCache(removeDeleted),
          ]);
        }
        return true;
      } catch (error) {
        setMutationError(errorMessage(error));
        return false;
      } finally {
        mutationInFlightRef.current = false;
        setDeletingIds(new Set());
      }
    },
    [
      data,
      key,
      mutate,
      updateFetchedProjectCache,
      updateOtherIncidentCaches,
    ],
  );

  const retry = useCallback(async () => {
    setMutationError(null);
    await mutate();
  }, [mutate]);

  return {
    dependencies: data ?? EMPTY_DEPENDENCIES,
    hasLoadedData: data !== undefined,
    isLoading,
    error: mutationError ?? (loadError ? errorMessage(loadError) : null),
    adding,
    deletingIds,
    addDependency,
    removeDependency,
    retry,
  };
}
