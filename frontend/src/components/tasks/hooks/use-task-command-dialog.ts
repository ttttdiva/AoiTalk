"use client";

import { useCallback, useMemo, useState } from "react";
import type React from "react";

import {
  taskApi,
  type Project,
  type Tag,
  type Task,
} from "@/lib/task-api";
import {
  buildTaskCommandCandidates,
  buildTaskSlashCommandFormPatch,
  resolveTaskTagIds,
} from "@/components/tasks/task-form-utils";
import {
  toLocalDateTimeInputValue,
  toTaskDatePayloadValue,
} from "@/lib/date-time";
import { isTaskCompletionTransition } from "@/lib/task-completion-undo";
import {
  PRIORITY_LABELS,
  saveTaskUpdate,
  STATUS_LABELS,
  TASK_LIST_COMMAND_INITIAL_VALUE,
} from "@/lib/tasks-page-utils";
import type { FetchDataOptions } from "@/components/tasks/hooks/use-tasks-data";
import type { UndoEntry } from "@/components/tasks/hooks/use-task-undo";

/**
 * フォーカス行に対するタスクコマンドダイアログ（`/` ショートカット）をまとめたフック。
 */
export function useTaskCommandDialog({
  tasks,
  tags,
  setTags,
  projects,
  selectedProjectId,
  fetchData,
  focusTaskById,
  pushUndo,
  queueTaskCompletionUndo,
  applyTaskPatchLocally,
  upsertTaskLocally,
}: {
  tasks: Task[];
  tags: Tag[];
  setTags: React.Dispatch<React.SetStateAction<Tag[]>>;
  projects: Project[];
  selectedProjectId: string | null;
  fetchData: (options?: FetchDataOptions) => Promise<void>;
  focusTaskById: (taskId: string | null) => void;
  pushUndo: (entry: UndoEntry) => void;
  queueTaskCompletionUndo: (entries: Task[]) => void;
  applyTaskPatchLocally: (taskId: string, patch: Partial<Task>) => void;
  upsertTaskLocally: (task: Task) => void;
}) {
  const [taskCommandOpen, setTaskCommandOpen] = useState(false);
  const [taskCommandTaskId, setTaskCommandTaskId] = useState<string | null>(
    null,
  );
  const [taskCommandValue, setTaskCommandValue] = useState(
    TASK_LIST_COMMAND_INITIAL_VALUE,
  );
  const [taskCommandError, setTaskCommandError] = useState<string | null>(null);
  const [taskCommandLoading, setTaskCommandLoading] = useState(false);
  const taskCommandTask = useMemo(
    () => tasks.find((item) => item.id === taskCommandTaskId) ?? null,
    [taskCommandTaskId, tasks],
  );
  const taskCommandCandidates = useMemo(() => {
    const mergedTags = new Map<string, Tag>();
    for (const tag of tags) mergedTags.set(tag.id, tag);
    for (const tag of taskCommandTask?.tags || []) mergedTags.set(tag.id, tag);
    return buildTaskCommandCandidates({
      projects,
      tags: Array.from(mergedTags.values()),
      selectedTagIds: (taskCommandTask?.tags || []).map((tag) => tag.id),
    });
  }, [projects, tags, taskCommandTask]);

  const closeTaskCommandDialog = useCallback(() => {
    setTaskCommandOpen(false);
    setTaskCommandTaskId(null);
    setTaskCommandValue(TASK_LIST_COMMAND_INITIAL_VALUE);
    setTaskCommandError(null);
    setTaskCommandLoading(false);
  }, []);

  const openTaskCommandDialog = useCallback((taskId: string) => {
    setTaskCommandTaskId(taskId);
    setTaskCommandValue(TASK_LIST_COMMAND_INITIAL_VALUE);
    setTaskCommandError(null);
    setTaskCommandOpen(true);
  }, []);

  const handleTaskCommandSubmit = useCallback(
    async (raw: string) => {
      const task = tasks.find((item) => item.id === taskCommandTaskId);
      if (!task) {
        setTaskCommandError("対象タスクが見つかりません。");
        return raw;
      }

      const patch = buildTaskSlashCommandFormPatch({
        text: raw,
        currentStartAt: toLocalDateTimeInputValue(task.start_at, {
          allDay: task.all_day,
        }),
        currentEndAt: toLocalDateTimeInputValue(task.end_at, {
          allDay: task.all_day,
        }),
        projects,
      });
      const updates: Record<string, unknown> = {};
      const previous: Record<string, unknown> = {};

      if (patch.status) {
        if (
          !Object.prototype.hasOwnProperty.call(STATUS_LABELS, patch.status)
        ) {
          setTaskCommandError("`/status` の値が不正です。");
          return raw;
        }
        updates.status = patch.status;
        previous.status = task.status;
        previous.completed_at = task.completed_at ?? null;
      }
      if (patch.priority) {
        if (
          !Object.prototype.hasOwnProperty.call(PRIORITY_LABELS, patch.priority)
        ) {
          setTaskCommandError("`/priority` の値が不正です。");
          return raw;
        }
        updates.priority = patch.priority;
        previous.priority = task.priority;
      }
      if (patch.startAt !== undefined) {
        updates.start_at = toTaskDatePayloadValue(patch.startAt, {
          allDay: patch.allDay ?? task.all_day,
        });
        previous.start_at = task.start_at ?? null;
      }
      if (patch.endAt !== undefined) {
        updates.end_at = toTaskDatePayloadValue(patch.endAt, {
          allDay: patch.allDay ?? task.all_day,
        });
        previous.end_at = task.end_at ?? null;
      }
      if (patch.allDay !== undefined) {
        updates.all_day = patch.allDay;
        previous.all_day = task.all_day;
      }
      if (patch.targetProjectId) {
        updates.project_id = patch.targetProjectId;
        previous.project_id = task.project_id;
      }
      if (patch.tagNames && patch.tagNames.length > 0) {
        const tagProjectId =
          typeof updates.project_id === "string"
            ? updates.project_id
            : task.project_id;
        let availableTags = [
          ...tags,
          ...(task.tags || []).filter(
            (tag) => !tags.some((current) => current.id === tag.id),
          ),
        ];
        if (tagProjectId !== selectedProjectId) {
          try {
            availableTags = await taskApi.listTags(tagProjectId);
          } catch (err) {
            console.error("タスクコマンド用タグ取得に失敗しました", err);
          }
        }
        const { tagIds, createdTags } = await resolveTaskTagIds({
          tagNames: patch.tagNames,
          currentTagIds:
            tagProjectId === task.project_id
              ? (task.tags || []).map((tag) => tag.id)
              : [],
          availableTags,
          createTag: (name) => taskApi.createTag(tagProjectId, { name }),
        });
        updates.tag_ids = tagIds;
        previous.tag_ids = (task.tags || []).map((tag) => tag.id);
        if (tagProjectId === selectedProjectId && createdTags.length > 0) {
          setTags((prev) => [...prev, ...createdTags]);
        }
      }

      if (Object.keys(updates).length === 0) {
        setTaskCommandError("有効なスラッシュコマンドを入力してください。");
        return raw;
      }

      const willComplete =
        typeof updates.status === "string" &&
        isTaskCompletionTransition(task.status, updates.status);

      setTaskCommandLoading(true);
      setTaskCommandError(null);
      try {
        if (!willComplete) {
          pushUndo({
            type: "update",
            taskId: task.id,
            previous,
          });
        }
        applyTaskPatchLocally(task.id, updates as Partial<Task>);
        const updated = await saveTaskUpdate(task.id, updates, task.project_id);
        upsertTaskLocally(updated);
        await fetchData();
        if (willComplete) {
          queueTaskCompletionUndo([task]);
        }
        closeTaskCommandDialog();
        focusTaskById(task.id);
        return "";
      } catch (err) {
        console.error("タスクコマンド実行失敗:", err);
        setTaskCommandError("タスクコマンドの実行に失敗しました。");
        await fetchData();
        return raw;
      } finally {
        setTaskCommandLoading(false);
      }
    },
    [
      applyTaskPatchLocally,
      closeTaskCommandDialog,
      fetchData,
      focusTaskById,
      projects,
      pushUndo,
      queueTaskCompletionUndo,
      selectedProjectId,
      setTags,
      taskCommandTaskId,
      tasks,
      tags,
      upsertTaskLocally,
    ],
  );

  return {
    taskCommandOpen,
    taskCommandTaskId,
    taskCommandValue,
    setTaskCommandValue,
    taskCommandError,
    setTaskCommandError,
    taskCommandLoading,
    taskCommandCandidates,
    closeTaskCommandDialog,
    openTaskCommandDialog,
    handleTaskCommandSubmit,
  };
}
