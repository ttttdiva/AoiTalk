"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import type React from "react";
import { toast } from "sonner";

import {
  taskApi,
  type Project,
  type Space,
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

// ダイアログのクローズ（終了アニメーション）が完了するまでの猶予。
// base-ui のダイアログは終了アニメーション中に一覧（tasks prop）が再レンダリング
// されるとアニメーションが再起動し、窓がアンマウントされず閉じない不具合が起きる。
// そのため、楽観的更新・保存・再取得といった一覧を書き換える処理は、この時間だけ
// 遅らせてクローズを妨げないようにする。dialog.tsx の duration-100 より十分長く取る。
const TASK_COMMAND_CLOSE_DEFER_MS = 220;

/**
 * フォーカス行に対するタスクコマンドダイアログ（`/` ショートカット）をまとめたフック。
 */
export function useTaskCommandDialog({
  tasks,
  tags,
  setTags,
  projects,
  spaces,
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
  spaces: Space[];
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
  // バックグラウンド保存の同時実行数。成功時は更新レスポンスを局所反映し、
  // いずれかが失敗した時だけ最後に全量再取得してロールバックする。
  const pendingSavesRef = useRef(0);
  const failedSaveRef = useRef(false);
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
      projectSpaceNames: new Map(spaces.map((space) => [space.id, space.name])),
      tags: Array.from(mergedTags.values()),
      selectedTagIds: (taskCommandTask?.tags || []).map((tag) => tag.id),
    });
  }, [projects, spaces, tags, taskCommandTask]);

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
    async (raw: string, selectedTargetProjectId?: string) => {
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
      const targetProjectId = selectedTargetProjectId || patch.targetProjectId;
      if (targetProjectId) {
        updates.project_id = targetProjectId;
        previous.project_id = task.project_id;
      }

      const tagNames =
        patch.tagNames && patch.tagNames.length > 0 ? patch.tagNames : null;

      // タグ以外の更新もタグ変更も無ければ有効なコマンドではない。
      // ここまでの検証はクライアント側で完結する（サーバー往復なし）。
      if (Object.keys(updates).length === 0 && !tagNames) {
        setTaskCommandError("有効なスラッシュコマンドを入力してください。");
        return raw;
      }

      const willComplete =
        typeof updates.status === "string" &&
        isTaskCompletionTransition(task.status, updates.status);

      // Undo登録・クローズ・フォーカス復帰までを同期的に済ませる。
      // ここではタスク一覧（tasks）は書き換えない。クローズと同一コミットで
      // 一覧が変わると base-ui の終了アニメーションが再起動し窓が閉じないため。
      setTaskCommandError(null);
      if (tagNames) {
        previous.tag_ids = (task.tags || []).map((tag) => tag.id);
      }
      if (!willComplete) {
        pushUndo({
          type: "update",
          taskId: task.id,
          previous,
        });
      }

      const targetTask = task;
      const targetTaskId = task.id;
      // サーバー応答を待たずに即座に窓を閉じる（高レイテンシ環境でも連発可能に）。
      closeTaskCommandDialog();
      focusTaskById(targetTaskId);

      // 楽観的更新・タグ解決・保存・再取得は、窓のクローズアニメーションが
      // 終わってから実行する。ユーザー要件どおり処理は後回しにして、
      // 窓の即時クローズと連続コマンド入力を最優先する。
      pendingSavesRef.current += 1;
      window.setTimeout(() => {
        void (async () => {
          try {
            if (Object.keys(updates).length > 0) {
              applyTaskPatchLocally(targetTaskId, updates as Partial<Task>);
            }
            if (tagNames) {
              const tagProjectId =
                typeof updates.project_id === "string"
                  ? updates.project_id
                  : targetTask.project_id;
              let availableTags = [
                ...tags,
                ...(targetTask.tags || []).filter(
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
                tagNames,
                currentTagIds:
                  tagProjectId === targetTask.project_id
                    ? (targetTask.tags || []).map((tag) => tag.id)
                    : [],
                availableTags,
                createTag: (name) => taskApi.createTag(tagProjectId, { name }),
              });
              updates.tag_ids = tagIds;
              if (tagProjectId === selectedProjectId && createdTags.length > 0) {
                setTags((prev) => [...prev, ...createdTags]);
              }
              applyTaskPatchLocally(targetTaskId, {
                tag_ids: tagIds,
              } as Partial<Task>);
            }

            const updated = await saveTaskUpdate(
              targetTaskId,
              updates,
              targetTask.project_id,
            );
            upsertTaskLocally(updated);
            // 完了遷移の Undo は保存成功後にだけ提示する（従来と同じタイミング）。
            // 失敗時に「完了しました」トーストを誤表示しないため。
            if (willComplete) {
              queueTaskCompletionUndo([targetTask]);
            }
          } catch (err) {
            failedSaveRef.current = true;
            console.error("タスクコマンド実行失敗:", err);
            toast.error("タスクコマンドの実行に失敗しました。");
          } finally {
            pendingSavesRef.current -= 1;
            // 成功時は upsert 済みなので再取得しない。失敗時だけ楽観更新を戻す。
            if (pendingSavesRef.current === 0 && failedSaveRef.current) {
              failedSaveRef.current = false;
              await fetchData();
            }
          }
        })();
      }, TASK_COMMAND_CLOSE_DEFER_MS);

      return "";
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
