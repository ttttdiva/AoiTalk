"use client";

import { useCallback, useEffect, useMemo } from "react";

import { toast } from "sonner";

import { type Project, type Task } from "@/lib/task-api";
import type { CommandCandidateSelection } from "@/components/tasks/slash-command-input";
import {
  buildTaskSlashCommandFormPatch,
  normalizeTaskTitle,
} from "@/components/tasks/task-form-utils";
import { toLocalDateTimeInputValue } from "@/lib/date-time";

/**
 * タスク詳細モーダルのタイトル編集・スラッシュコマンド解析・ドラフト送信・
 * ダイアログ開閉ロジックをまとめた hook。
 * state と ref は呼び出し側が所有し、setter / ref / 書き込みハンドラを受け取る。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskDraftForm({
  open,
  effectiveTaskId,
  task,
  draftTask,
  allProjects,
  editTitle,
  editDescription,
  draftTagIds,
  setEditTitle,
  setEditingTitle,
  setCreatedTaskId,
  onOpenChange,
  createFromDraft,
  immediateUpdate,
  applyLocalDraftUpdate,
  buildDateTaskUpdate,
  debouncedUpdate,
  resolveTagUpdates,
  debounceRef,
  draftSuppressTitleBlurRef,
  draftSubmitIntentRef,
  draftLifecycleRef,
  draftSlashUpdatesRef,
  draftSlashUpdatePromiseRef,
}: {
  open: boolean;
  effectiveTaskId: string | null;
  task: Task | null;
  draftTask?: Partial<Task> | null;
  allProjects: Project[];
  editTitle: string;
  editDescription: string;
  draftTagIds: string[];
  setEditTitle: React.Dispatch<React.SetStateAction<string>>;
  setEditingTitle: React.Dispatch<React.SetStateAction<boolean>>;
  setCreatedTaskId: React.Dispatch<React.SetStateAction<string | null>>;
  onOpenChange: (open: boolean) => void;
  createFromDraft: (
    overrides?: Record<string, unknown>,
  ) => Promise<Task | null>;
  immediateUpdate: (data: Record<string, unknown>) => Promise<Task | null>;
  applyLocalDraftUpdate: (data: Record<string, unknown>) => void;
  buildDateTaskUpdate: (partial: {
    start_at?: string | null;
    end_at?: string | null;
    all_day?: boolean;
  }) => Record<string, unknown>;
  debouncedUpdate: (data: Record<string, unknown>) => void;
  resolveTagUpdates: (
    tagNames: string[],
    targetProjectId?: string | null,
  ) => Promise<{ tag_ids: string[] }>;
  debounceRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>;
  draftSuppressTitleBlurRef: React.MutableRefObject<boolean>;
  draftSubmitIntentRef: React.MutableRefObject<boolean>;
  draftLifecycleRef: React.MutableRefObject<number>;
  draftSlashUpdatesRef: React.MutableRefObject<Record<string, unknown>>;
  draftSlashUpdatePromiseRef: React.MutableRefObject<Promise<
    Record<string, unknown>
  > | null>;
}) {
  const hasUnsavedDraft = useMemo(() => {
    if (effectiveTaskId || !task) return false;
    return Boolean(
      normalizeTaskTitle(editTitle) ||
      editDescription.trim() ||
      draftTagIds.length > 0 ||
      task.start_at ||
      task.end_at ||
      task.status !== "open" ||
      task.priority !== "medium" ||
      task.parent_task_id ||
      task.reminder_offsets.length > 0 ||
      task.notifications_enabled === false,
    );
  }, [draftTagIds.length, editDescription, editTitle, effectiveTaskId, task]);

  const handleDialogOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (nextOpen) {
        onOpenChange(true);
        return;
      }

      if (!effectiveTaskId) {
        if (hasUnsavedDraft && !draftSubmitIntentRef.current) {
          const shouldClose = window.confirm(
            "入力中の新規タスクを保存せずに閉じますか？",
          );
          if (!shouldClose) {
            draftSuppressTitleBlurRef.current = false;
            return;
          }
        }

        draftSuppressTitleBlurRef.current = true;
        draftSubmitIntentRef.current = false;
        draftLifecycleRef.current += 1;
        setCreatedTaskId(null);
      }

      onOpenChange(false);
    },
    [
      effectiveTaskId,
      hasUnsavedDraft,
      onOpenChange,
      draftSubmitIntentRef,
      draftSuppressTitleBlurRef,
      draftLifecycleRef,
      setCreatedTaskId,
    ],
  );

  const handleDraftSubmitIntent = useCallback(
    async (submitOverrides: Record<string, unknown> = {}) => {
      if (effectiveTaskId) return;
      const overrides: Record<string, unknown> = {};
      const hasInlineSlash = editTitle.includes("/");
      if (!hasInlineSlash) {
        const pendingSlashUpdates = draftSlashUpdatePromiseRef.current
          ? await draftSlashUpdatePromiseRef.current
          : null;
        Object.assign(
          overrides,
          pendingSlashUpdates ?? draftSlashUpdatesRef.current,
        );
        delete overrides.title;
      }

      let finalTitle = editTitle;
      if (hasInlineSlash) {
        const patch = buildTaskSlashCommandFormPatch({
          text: editTitle,
          currentStartAt:
            toLocalDateTimeInputValue(task?.start_at, {
              allDay: task?.all_day === true,
            }) ?? null,
          currentEndAt:
            toLocalDateTimeInputValue(task?.end_at, {
              allDay: task?.all_day === true,
            }) ?? null,
          projects: allProjects,
        });
        finalTitle = patch.title;
        if (patch.title !== editTitle) {
          setEditTitle(patch.title);
          overrides.title = patch.title;
        }
        if (patch.startAt !== undefined || patch.endAt !== undefined) {
          Object.assign(
            overrides,
            buildDateTaskUpdate({
              start_at: patch.startAt ?? undefined,
              end_at: patch.endAt ?? undefined,
            }),
          );
        }
        if (patch.allDay !== undefined) overrides.all_day = patch.allDay;
        if (patch.status) overrides.status = patch.status;
        if (patch.priority) overrides.priority = patch.priority;
        if (patch.targetProjectId) overrides.project_id = patch.targetProjectId;
        if (patch.tagNames && patch.tagNames.length > 0) {
          const tagProjectId =
            typeof overrides.project_id === "string"
              ? overrides.project_id
              : task?.project_id || draftTask?.project_id || null;
          Object.assign(
            overrides,
            await resolveTagUpdates(patch.tagNames, tagProjectId),
          );
        }
      }
      Object.assign(overrides, submitOverrides);
      if (Object.keys(overrides).length > 0) {
        draftSlashUpdatesRef.current = {
          ...draftSlashUpdatesRef.current,
          ...overrides,
        };
      }

      const normalizedTitle = normalizeTaskTitle(finalTitle);
      if (!normalizedTitle) return;
      draftSubmitIntentRef.current = true;
      const created = await createFromDraft({
        ...overrides,
        title: normalizedTitle,
      });
      if (!created) {
        draftSubmitIntentRef.current = false;
        return;
      }
      setEditingTitle(false);
      handleDialogOpenChange(false);
    },
    [
      allProjects,
      buildDateTaskUpdate,
      createFromDraft,
      editTitle,
      effectiveTaskId,
      handleDialogOpenChange,
      resolveTagUpdates,
      task?.all_day,
      task?.end_at,
      task?.project_id,
      task?.start_at,
      draftTask?.project_id,
      setEditTitle,
      setEditingTitle,
      draftSlashUpdatePromiseRef,
      draftSlashUpdatesRef,
      draftSubmitIntentRef,
    ],
  );

  useEffect(() => {
    if (open && !effectiveTaskId) {
      draftSuppressTitleBlurRef.current = false;
      draftSubmitIntentRef.current = false;
    }
    if (!open || effectiveTaskId) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (
        target.closest('[data-slot="dialog-close"]') ||
        target.closest('[data-slot="dialog-overlay"]')
      ) {
        draftSuppressTitleBlurRef.current = true;
      }
    };

    const handleEscapeKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== "Escape") return;
      draftSuppressTitleBlurRef.current = true;
    };

    document.addEventListener("pointerdown", handlePointerDown, true);
    window.addEventListener("keydown", handleEscapeKey, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      window.removeEventListener("keydown", handleEscapeKey, true);
    };
  }, [effectiveTaskId, open, draftSubmitIntentRef, draftSuppressTitleBlurRef]);

  const buildSlashFormPatch = useCallback(
    (
      text: string,
      preserveTrailingSpace = false,
      selection?: CommandCandidateSelection,
    ) =>
      buildTaskSlashCommandFormPatch({
        text,
        currentStartAt:
          toLocalDateTimeInputValue(task?.start_at, {
            allDay: task?.all_day === true,
          }) ?? null,
        currentEndAt:
          toLocalDateTimeInputValue(task?.end_at, {
            allDay: task?.all_day === true,
          }) ?? null,
        projects: allProjects,
        preserveTrailingSpace,
        selection,
      }),
    [allProjects, task?.all_day, task?.end_at, task?.start_at],
  );

  const buildSlashUpdates = useCallback(
    async (
      patch: ReturnType<typeof buildSlashFormPatch>,
      originalText: string,
    ): Promise<Record<string, unknown>> => {
      const updates: Record<string, unknown> = {};
      if (patch.title !== originalText) updates.title = patch.title;
      if (patch.startAt !== undefined || patch.endAt !== undefined) {
        Object.assign(
          updates,
          buildDateTaskUpdate({
            start_at: patch.startAt ?? undefined,
            end_at: patch.endAt ?? undefined,
          }),
        );
      }
      if (patch.allDay !== undefined) updates.all_day = patch.allDay;
      if (patch.status) updates.status = patch.status;
      if (patch.targetProjectId) updates.project_id = patch.targetProjectId;
      if (patch.tagNames && patch.tagNames.length > 0) {
        const tagProjectId =
          typeof updates.project_id === "string"
            ? updates.project_id
            : task?.project_id || draftTask?.project_id || null;
        Object.assign(
          updates,
          await resolveTagUpdates(patch.tagNames, tagProjectId),
        );
      }
      return updates;
    },
    [
      buildDateTaskUpdate,
      draftTask?.project_id,
      resolveTagUpdates,
      task?.project_id,
    ],
  );

  const handleSubmitAndCloseIntent = useCallback(
    async (descriptionOverride?: string) => {
      if (!task) return;

      if (!effectiveTaskId) {
        if (descriptionOverride !== undefined) {
          applyLocalDraftUpdate({ description: descriptionOverride });
        }
        await handleDraftSubmitIntent(
          descriptionOverride !== undefined
            ? { description: descriptionOverride }
            : {},
        );
        return;
      }

      const updates: Record<string, unknown> = {};
      let finalTitle = editTitle || task.title;
      if (editTitle.includes("/")) {
        const patch = buildSlashFormPatch(editTitle);
        finalTitle = patch.title;
        Object.assign(updates, await buildSlashUpdates(patch, editTitle));
      }

      const normalizedTitle = normalizeTaskTitle(finalTitle);
      if (!normalizedTitle) {
        toast.error("Task title is required.");
        return;
      }

      updates.title = normalizedTitle;
      updates.description =
        descriptionOverride !== undefined
          ? descriptionOverride
          : editDescription;

      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }

      const updated = await immediateUpdate(updates);
      if (!updated) return;
      setEditingTitle(false);
      handleDialogOpenChange(false);
    },
    [
      applyLocalDraftUpdate,
      buildSlashFormPatch,
      buildSlashUpdates,
      editDescription,
      editTitle,
      effectiveTaskId,
      handleDialogOpenChange,
      handleDraftSubmitIntent,
      immediateUpdate,
      task,
      debounceRef,
      setEditingTitle,
    ],
  );

  const handleTitleChange = useCallback(
    (val: string) => {
      setEditTitle(val);
      if (effectiveTaskId) debouncedUpdate({ title: val });
      else applyLocalDraftUpdate({ title: val });
    },
    [applyLocalDraftUpdate, debouncedUpdate, effectiveTaskId, setEditTitle],
  );

  const handleTitleBlur = useCallback(() => {
    void (async () => {
      if (!effectiveTaskId && draftSuppressTitleBlurRef.current) {
        draftSuppressTitleBlurRef.current = false;
        setEditingTitle(false);
        return;
      }

      const updates: Record<string, unknown> = {};
      if (editTitle.includes("/")) {
        const patch = buildSlashFormPatch(editTitle);
        if (patch.title !== editTitle) {
          setEditTitle(patch.title);
        }
        Object.assign(updates, await buildSlashUpdates(patch, editTitle));
      }

      if (!effectiveTaskId) {
        if (Object.keys(updates).length > 0) {
          draftSlashUpdatesRef.current = {
            ...draftSlashUpdatesRef.current,
            ...updates,
          };
          applyLocalDraftUpdate(updates);
        }
      } else if (Object.keys(updates).length > 0) {
        await immediateUpdate(updates);
      }
      setEditingTitle(false);
    })();
  }, [
    applyLocalDraftUpdate,
    buildSlashFormPatch,
    buildSlashUpdates,
    editTitle,
    effectiveTaskId,
    immediateUpdate,
    setEditTitle,
    setEditingTitle,
    draftSuppressTitleBlurRef,
    draftSlashUpdatesRef,
  ]);

  const handleTitleSubmitIntent = useCallback(() => {
    void handleSubmitAndCloseIntent();
  }, [handleSubmitAndCloseIntent]);

  const handleParseSlashCommands = useCallback(
    (text: string, selection?: CommandCandidateSelection) => {
      if (!text.includes("/")) return text;
      const patch = buildSlashFormPatch(text, true, selection);
      const pendingSlashUpdates = (async () => {
        const updates = await buildSlashUpdates(patch, text);
        if (!effectiveTaskId) {
          draftSlashUpdatesRef.current = {
            ...draftSlashUpdatesRef.current,
            ...updates,
          };
        }
        if (Object.keys(updates).length > 0) {
          if (effectiveTaskId) {
            await immediateUpdate(updates);
          } else {
            applyLocalDraftUpdate(updates);
          }
        }
        return updates;
      })();
      if (!effectiveTaskId) {
        let trackedSlashUpdates: Promise<Record<string, unknown>> | null = null;
        trackedSlashUpdates = pendingSlashUpdates.finally(() => {
          if (draftSlashUpdatePromiseRef.current === trackedSlashUpdates) {
            draftSlashUpdatePromiseRef.current = null;
          }
        });
        draftSlashUpdatePromiseRef.current = trackedSlashUpdates;
      }
      return patch.title;
    },
    [
      applyLocalDraftUpdate,
      buildSlashFormPatch,
      buildSlashUpdates,
      effectiveTaskId,
      immediateUpdate,
      draftSlashUpdatePromiseRef,
      draftSlashUpdatesRef,
    ],
  );

  return {
    handleDialogOpenChange,
    handleSubmitAndCloseIntent,
    handleTitleChange,
    handleTitleBlur,
    handleTitleSubmitIntent,
    handleParseSlashCommands,
  };
}
