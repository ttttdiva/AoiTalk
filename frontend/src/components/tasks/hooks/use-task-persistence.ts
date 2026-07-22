"use client";

import { useCallback, useMemo } from "react";

import {
  taskApi,
  type RecurringOccurrenceContext,
  type Tag,
  type Task,
} from "@/lib/task-api";
import { type LinkDisplayMode } from "@/components/editor/task-description-editor";
import {
  buildAutoEstimateTaskPatch,
  hasNonMidnightTime,
  normalizeTaskTitle,
} from "@/components/tasks/task-form-utils";
import { toTaskDatePayloadValue } from "@/lib/date-time";
import {
  createTaskCompletionUndoEntry,
  dispatchTaskCompletionUndoBatch,
  isTaskCompletionTransition,
} from "@/lib/task-completion-undo";
import {
  buildTaskDescriptionLinkDisplayModeMetadata,
  getTaskDescriptionLinkDisplayModes,
} from "@/components/tasks/task-detail/task-detail-utils";

/**
 * タスク詳細モーダルの作成・更新（書き込み）エンジンをまとめた hook。
 * state と ref は呼び出し側が所有し、setter / ref を受け取る。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskPersistence({
  taskId,
  effectiveTaskId,
  task,
  draftTask,
  activeOccurrenceContext,
  editTitle,
  draftTagIds,
  setTask,
  setEditTitle,
  setEditDescription,
  setEditEstHours,
  setDraftTagIds,
  setCreatedTaskId,
  setTags,
  setOccurrenceStatusOverride,
  setOccurrenceDateOverride,
  onTaskUpdated,
  onNewTaskKept,
  debounceRef,
  draftCreatePromiseRef,
  draftCreatedTaskIdRef,
  draftLifecycleRef,
  taskMetadataRef,
}: {
  taskId: string | null;
  effectiveTaskId: string | null;
  task: Task | null;
  draftTask?: Partial<Task> | null;
  activeOccurrenceContext: RecurringOccurrenceContext | null;
  editTitle: string;
  draftTagIds: string[];
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  setEditTitle: React.Dispatch<React.SetStateAction<string>>;
  setEditDescription: React.Dispatch<React.SetStateAction<string>>;
  setEditEstHours: React.Dispatch<React.SetStateAction<string>>;
  setDraftTagIds: React.Dispatch<React.SetStateAction<string[]>>;
  setCreatedTaskId: React.Dispatch<React.SetStateAction<string | null>>;
  setTags: React.Dispatch<React.SetStateAction<Tag[]>>;
  setOccurrenceStatusOverride: React.Dispatch<React.SetStateAction<string | null>>;
  setOccurrenceDateOverride: React.Dispatch<
    React.SetStateAction<{ start_at: string | null; end_at: string | null } | null>
  >;
  onTaskUpdated: () => void;
  onNewTaskKept?: () => void;
  debounceRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>;
  draftCreatePromiseRef: React.MutableRefObject<Promise<Task | null> | null>;
  draftCreatedTaskIdRef: React.MutableRefObject<string | null>;
  draftLifecycleRef: React.MutableRefObject<number>;
  taskMetadataRef: React.MutableRefObject<Record<string, unknown>>;
}) {
  const applyLocalDraftUpdate = useCallback((data: Record<string, unknown>) => {
    setTask((prev) => (prev ? ({ ...prev, ...data } as Task) : prev));
    if (typeof data.title === "string") setEditTitle(data.title);
    if (typeof data.description === "string")
      setEditDescription(data.description);
    if (data.description === null) setEditDescription("");
    if ("estimated_hours" in data) {
      const hours = data.estimated_hours;
      setEditEstHours(
        typeof hours === "number" && Number.isFinite(hours)
          ? String(hours)
          : "",
      );
    }
    if (Array.isArray(data.tag_ids)) {
      setDraftTagIds(
        data.tag_ids.filter(
          (value): value is string => typeof value === "string",
        ),
      );
    }
  }, [setDraftTagIds, setEditDescription, setEditEstHours, setEditTitle, setTask]);

  const saveTaskUpdate = useCallback(
    (
      taskId: string,
      data: Record<string, unknown>,
      currentProjectId?: string | null,
    ) => {
      const nextProjectId =
        typeof data.project_id === "string" ? data.project_id : null;
      return nextProjectId && nextProjectId !== currentProjectId
        ? taskApi.moveTask(taskId, data)
        : taskApi.updateTask(taskId, data);
    },
    [],
  );

  const createFromDraft = useCallback(
    async (overrides: Record<string, unknown> = {}) => {
      if (draftCreatePromiseRef.current) {
        return draftCreatePromiseRef.current;
      }
      if (draftCreatedTaskIdRef.current) {
        if (Object.keys(overrides).length === 0) return null;
        const updatePayload = { ...overrides };
        if (typeof updatePayload.title === "string") {
          const normalizedOverrideTitle = normalizeTaskTitle(
            updatePayload.title,
          );
          if (!normalizedOverrideTitle) return null;
          updatePayload.title = normalizedOverrideTitle;
        }
        return saveTaskUpdate(
          draftCreatedTaskIdRef.current,
          updatePayload,
          task?.project_id || draftTask?.project_id || null,
        );
      }

      const titleSource =
        typeof overrides.title === "string"
          ? overrides.title
          : editTitle || task?.title || "";
      const normalizedTitle = normalizeTaskTitle(titleSource);
      const projectId =
        (typeof overrides.project_id === "string"
          ? overrides.project_id
          : task?.project_id || draftTask?.project_id) || "";
      if (!normalizedTitle || !projectId) return null;
      const rawStartAt =
        overrides.start_at !== undefined
          ? (overrides.start_at as string | null)
          : task?.start_at || null;
      const rawEndAt =
        overrides.end_at !== undefined
          ? (overrides.end_at as string | null)
          : task?.end_at || null;
      const payloadAllDay =
        overrides.all_day !== undefined
          ? Boolean(overrides.all_day)
          : task?.all_day === true;

      const payload: Record<string, unknown> = {
        project_id: projectId,
        title: normalizedTitle,
        description:
          overrides.description !== undefined
            ? overrides.description
            : task?.description || null,
        status:
          typeof overrides.status === "string"
            ? overrides.status
            : task?.status || "open",
        priority:
          typeof overrides.priority === "string"
            ? overrides.priority
            : task?.priority || "medium",
        start_at: toTaskDatePayloadValue(rawStartAt, { allDay: payloadAllDay }),
        end_at: toTaskDatePayloadValue(rawEndAt, { allDay: payloadAllDay }),
        all_day: payloadAllDay,
        reminder_offsets:
          overrides.reminder_offsets !== undefined
            ? overrides.reminder_offsets
            : task?.reminder_offsets || [],
        notifications_enabled:
          overrides.notifications_enabled !== undefined
            ? Boolean(overrides.notifications_enabled)
            : task?.notifications_enabled !== false,
        tag_ids:
          overrides.tag_ids !== undefined ? overrides.tag_ids : draftTagIds,
        parent_task_id:
          overrides.parent_task_id !== undefined
            ? overrides.parent_task_id
            : task?.parent_task_id || null,
      };
      Object.assign(
        payload,
        buildAutoEstimateTaskPatch({
          startAt: (payload.start_at as string | null | undefined) ?? null,
          endAt: (payload.end_at as string | null | undefined) ?? null,
          currentEstimatedHours:
            task?.estimated_hours ?? draftTask?.estimated_hours ?? null,
          currentMetadata:
            task?.metadata ??
            (draftTask?.metadata as Record<string, unknown> | undefined) ??
            {},
          forceAuto: !task && draftTask?.estimated_hours == null,
        }),
      );

      const createPromise = (async () => {
        const draftLifecycleToken = draftLifecycleRef.current;
        const created = await taskApi.createTask(payload);
        if (!taskId && draftLifecycleToken !== draftLifecycleRef.current) {
          return created;
        }
        draftCreatedTaskIdRef.current = created.id;
        setCreatedTaskId(created.id);
        setTask(created);
        setEditTitle(created.title);
        onNewTaskKept?.();
        onTaskUpdated();
        if (created.project_id) {
          const tagList = await taskApi.listTags(created.project_id);
          setTags(tagList);
        }
        return created;
      })();
      draftCreatePromiseRef.current = createPromise;
      try {
        return await createPromise;
      } finally {
        draftCreatePromiseRef.current = null;
      }
    },
    [
      draftTagIds,
      draftTask,
      editTitle,
      onNewTaskKept,
      onTaskUpdated,
      saveTaskUpdate,
      task,
      taskId,
      draftCreatePromiseRef,
      draftCreatedTaskIdRef,
      draftLifecycleRef,
      setCreatedTaskId,
      setEditTitle,
      setTags,
      setTask,
    ],
  );

  const ensureTaskId = useCallback(
    async () => (await createFromDraft())?.id ?? draftCreatedTaskIdRef.current,
    [createFromDraft, draftCreatedTaskIdRef],
  );

  const debouncedUpdate = useCallback(
    (data: Record<string, unknown>) => {
      if (!effectiveTaskId) {
        applyLocalDraftUpdate(data);
        return;
      }
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(async () => {
        try {
          const updated = await saveTaskUpdate(
            effectiveTaskId,
            data,
            task?.project_id ?? null,
          );
          setTask(updated);
          onTaskUpdated();
        } catch (err) {
          console.error("更新失敗:", err);
        }
      }, 500);
    },
    [
      applyLocalDraftUpdate,
      effectiveTaskId,
      onTaskUpdated,
      saveTaskUpdate,
      task?.project_id,
      debounceRef,
      setTask,
    ],
  );

  const immediateUpdate = useCallback(
    async (data: Record<string, unknown>) => {
      if (!effectiveTaskId) {
        applyLocalDraftUpdate(data);
        return null;
      }
      try {
        const previousTask = task;
        if (
          task?.has_recurrence &&
          activeOccurrenceContext?.start_at &&
          typeof data.status === "string"
        ) {
          const result = await taskApi.updateOccurrenceStatus(effectiveTaskId, {
            occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
            occurrence_start_at: activeOccurrenceContext.start_at,
            occurrence_end_at: activeOccurrenceContext.end_at ?? null,
            original_start_at:
              activeOccurrenceContext.original_start_at ?? null,
            status: data.status,
          });
          const nextStatus = String(result.occurrence?.status ?? data.status);
          setOccurrenceStatusOverride(nextStatus);
          setTask((prev) =>
            prev ? { ...prev, status: nextStatus } : prev,
          );
          onTaskUpdated();
          return task ? { ...task, status: nextStatus } : null;
        }

        const updated = await saveTaskUpdate(
          effectiveTaskId,
          data,
          task?.project_id ?? null,
        );
        setTask(updated);
        onTaskUpdated();
        if (
          previousTask &&
          typeof data.status === "string" &&
          isTaskCompletionTransition(previousTask.status, data.status)
        ) {
          dispatchTaskCompletionUndoBatch({
            entries: [createTaskCompletionUndoEntry(previousTask)],
          });
        }
        return updated;
      } catch (err) {
        console.error("更新失敗:", err);
        return null;
      }
    },
    [
      applyLocalDraftUpdate,
      activeOccurrenceContext,
      effectiveTaskId,
      onTaskUpdated,
      saveTaskUpdate,
      task,
      setOccurrenceStatusOverride,
      setTask,
    ],
  );

  const descriptionLinkDisplayModes = useMemo(
    () => getTaskDescriptionLinkDisplayModes(task?.metadata),
    [task?.metadata],
  );

  const handleDescriptionLinkDisplayModeChange = useCallback(
    (url: string, mode: LinkDisplayMode) => {
      const metadata = buildTaskDescriptionLinkDisplayModeMetadata({
        metadata: taskMetadataRef.current,
        url,
        mode,
      });
      taskMetadataRef.current = metadata;

      if (!effectiveTaskId) {
        applyLocalDraftUpdate({ metadata });
        return;
      }

      void (async () => {
        try {
          const updated = await saveTaskUpdate(
            effectiveTaskId,
            { metadata },
            task?.project_id ?? null,
          );
          setTask({ ...updated, metadata: taskMetadataRef.current });
          onTaskUpdated();
        } catch (err) {
          console.error("URL表示方式の保存に失敗:", err);
        }
      })();
    },
    [
      applyLocalDraftUpdate,
      effectiveTaskId,
      onTaskUpdated,
      saveTaskUpdate,
      task?.project_id,
      setTask,
      taskMetadataRef,
    ],
  );

  const buildDateTaskUpdate = useCallback(
    (partial: {
      start_at?: string | null;
      end_at?: string | null;
      all_day?: boolean;
    }) => {
      const nextStartAt =
        partial.start_at !== undefined
          ? partial.start_at
          : (task?.start_at ?? null);
      const nextEndAt =
        partial.end_at !== undefined ? partial.end_at : (task?.end_at ?? null);
      const nextAllDay =
        partial.all_day !== undefined
          ? partial.all_day
          : (!!nextStartAt || !!nextEndAt) &&
            !hasNonMidnightTime(nextStartAt) &&
            !hasNonMidnightTime(nextEndAt);
      const dateUpdate: Record<string, string | null | boolean> = {};
      const hasDateChange =
        partial.start_at !== undefined || partial.end_at !== undefined;
      if (partial.start_at !== undefined)
        dateUpdate.start_at = toTaskDatePayloadValue(partial.start_at, {
          allDay: nextAllDay,
        });
      if (partial.end_at !== undefined)
        dateUpdate.end_at = toTaskDatePayloadValue(partial.end_at, {
          allDay: nextAllDay,
        });
      if (partial.all_day !== undefined || hasDateChange) {
        dateUpdate.all_day = nextAllDay;
      }
      return {
        ...dateUpdate,
        ...buildAutoEstimateTaskPatch({
          startAt: nextStartAt,
          endAt: nextEndAt,
          allDay: nextAllDay,
          currentEstimatedHours: task?.estimated_hours ?? null,
          currentMetadata: task?.metadata,
        }),
      };
    },
    [task],
  );

  const moveOccurrenceDateRange = useCallback(
    async (values: { startAt: string | null; endAt: string | null }) => {
      if (!effectiveTaskId || !activeOccurrenceContext?.start_at) return;
      const nextStartAt =
        toTaskDatePayloadValue(values.startAt, { allDay: task?.all_day }) ??
        activeOccurrenceContext.start_at;
      const nextEndAt = toTaskDatePayloadValue(values.endAt, {
        allDay: task?.all_day,
      });
      try {
        const result = await taskApi.moveOccurrence(effectiveTaskId, {
          occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
          occurrence_start_at: activeOccurrenceContext.start_at,
          occurrence_end_at: activeOccurrenceContext.end_at ?? null,
          original_start_at: activeOccurrenceContext.original_start_at ?? null,
          next_start_at: nextStartAt,
          next_end_at: nextEndAt,
          status: activeOccurrenceContext.status ?? task?.status ?? null,
          all_day: task?.all_day,
        });
        setOccurrenceDateOverride({
          start_at: result.occurrence?.start_at ?? nextStartAt,
          end_at: result.occurrence?.end_at ?? nextEndAt,
        });
        onTaskUpdated();
      } catch (err) {
        console.error("繰り返し発生日時の更新に失敗:", err);
      }
    },
    [
      activeOccurrenceContext,
      effectiveTaskId,
      onTaskUpdated,
      task,
      setOccurrenceDateOverride,
    ],
  );

  return {
    applyLocalDraftUpdate,
    saveTaskUpdate,
    createFromDraft,
    ensureTaskId,
    debouncedUpdate,
    immediateUpdate,
    descriptionLinkDisplayModes,
    handleDescriptionLinkDisplayModeChange,
    buildDateTaskUpdate,
    moveOccurrenceDateRange,
  };
}
