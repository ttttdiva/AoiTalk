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

export function useProjectTaskDependencies({
  projectId,
  enabled,
}: {
  projectId: string | null;
  enabled: boolean;
}) {
  const { cache, mutate: mutateCache } = useSWRConfig();
  const key =
    enabled && projectId ? projectTaskDependencyCacheKey(projectId) : null;
  const {
    data,
    error: loadError,
    isLoading,
    mutate,
  } = useSWR<TaskDependency[]>(
    key,
    () => listTaskDependencies({ projectId: projectId! }),
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
  const mutationScopeRef = useRef({ projectId, generation: 0 });
  if (mutationScopeRef.current.projectId !== projectId) {
    mutationScopeRef.current = {
      projectId,
      generation: mutationScopeRef.current.generation + 1,
    };
  }

  useEffect(() => {
    setMutationError(null);
    setAdding(false);
    setDeletingIds(new Set());
    mutationInFlightRef.current = false;
  }, [projectId]);

  const updateFetchedTaskCaches = useCallback(
    async (
      taskIds: readonly string[],
      update: (current: TaskDependency[]) => TaskDependency[],
    ) => {
      await Promise.all(
        [...new Set(taskIds)].map(async (taskId) => {
          const taskKey = taskDependencyCacheKey(taskId);
          const cached = cache.get(taskKey) as
            | { data?: TaskDependency[] }
            | undefined;
          // 未取得cacheへproject一覧の一部だけを注入すると初回GETが抑止される。
          if (!Array.isArray(cached?.data)) return;
          await mutateCache<TaskDependency[]>(
            taskKey,
            (current = EMPTY_DEPENDENCIES) => update(current),
            { revalidate: false },
          );
        }),
      );
    },
    [cache, mutateCache],
  );

  const addDependency = useCallback(
    async (input: CreateTaskDependencyInput): Promise<boolean> => {
      if (!key || mutationInFlightRef.current) return false;
      const operationGeneration = mutationScopeRef.current.generation;
      mutationInFlightRef.current = true;
      setAdding(true);
      setMutationError(null);
      optimisticDependencySequence += 1;
      const optimisticId = `project-optimistic:${optimisticDependencySequence}`;
      const optimisticDependency: TaskDependency = {
        id: optimisticId,
        task_id: input.task_id,
        depends_on_task_id: input.depends_on_task_id,
        created_at: null,
      };
      let createdDependency: TaskDependency | null = null;

      try {
        await mutate(
          async (current = EMPTY_DEPENDENCIES) => {
            createdDependency = await createTaskDependency(input);
            return [
              ...current.filter((item) => item.id !== optimisticId),
              createdDependency,
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
        if (createdDependency) {
          const created = createdDependency as TaskDependency;
          await updateFetchedTaskCaches(
            [created.task_id, created.depends_on_task_id],
            (dependencies) =>
              dependencies.some((item) => item.id === created.id)
                ? dependencies
                : [...dependencies, created],
          );
        }
        return true;
      } catch (error) {
        if (mutationScopeRef.current.generation === operationGeneration) {
          setMutationError(errorMessage(error));
        }
        return false;
      } finally {
        if (mutationScopeRef.current.generation === operationGeneration) {
          mutationInFlightRef.current = false;
          setAdding(false);
        }
      }
    },
    [key, mutate, updateFetchedTaskCaches],
  );

  const removeDependency = useCallback(
    async (dependencyId: string): Promise<boolean> => {
      if (!key || mutationInFlightRef.current) return false;
      const removingDependency = data?.find((item) => item.id === dependencyId);
      if (
        !removingDependency ||
        dependencyId.startsWith("project-optimistic:")
      ) {
        return false;
      }
      const operationGeneration = mutationScopeRef.current.generation;
      mutationInFlightRef.current = true;
      setDeletingIds(new Set([dependencyId]));
      setMutationError(null);

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
        await updateFetchedTaskCaches(
          [removingDependency.task_id, removingDependency.depends_on_task_id],
          (dependencies) =>
            dependencies.filter((item) => item.id !== dependencyId),
        );
        return true;
      } catch (error) {
        if (mutationScopeRef.current.generation === operationGeneration) {
          setMutationError(errorMessage(error));
        }
        return false;
      } finally {
        if (mutationScopeRef.current.generation === operationGeneration) {
          mutationInFlightRef.current = false;
          setDeletingIds(new Set());
        }
      }
    },
    [data, key, mutate, updateFetchedTaskCaches],
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
