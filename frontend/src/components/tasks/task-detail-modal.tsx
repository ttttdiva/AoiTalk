"use client";

import React, {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
} from "react";
import {
  buildDraftTask,
  buildManualEstimateTaskPatch,
  buildTaskCommandCandidates,
} from "@/components/tasks/task-form-utils";
import {
  TaskDescriptionEditor,
  type TaskDescriptionEditorHandle,
} from "@/components/editor/task-description-editor";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
  type Tag,
  type TimeEntry,
  type TaskAttachment,
  type TaskReference,
} from "@/lib/task-api";
import {
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { useProject } from "@/contexts/project-context";

import { RecurringDeleteDialog } from "@/components/tasks/task-detail/recurring-delete-dialog";
import { SubtaskSection } from "@/components/tasks/task-detail/subtask-section";
import { TaskAttachmentsSection } from "@/components/tasks/task-detail/task-attachments-section";
import { TaskDetailHeader } from "@/components/tasks/task-detail/task-detail-header";
import { TaskDetailTriageCard } from "@/components/tasks/task-detail/task-detail-triage-card";
import { TaskDetailPropertyGrid } from "@/components/tasks/task-detail/task-detail-property-grid";
import { TaskDetailComments } from "@/components/tasks/task-detail/task-detail-comments";
import { useTaskTagManagement } from "@/components/tasks/hooks/use-task-tag-management";
import { useTaskRecurrence } from "@/components/tasks/hooks/use-task-recurrence";
import { useTaskPersistence } from "@/components/tasks/hooks/use-task-persistence";
import { useTaskAgentActions } from "@/components/tasks/hooks/use-task-agent-actions";
import { useTaskDeletion } from "@/components/tasks/hooks/use-task-deletion";
import { useTaskDocsNode } from "@/components/tasks/hooks/use-task-docs-node";
import { useTaskDraftForm } from "@/components/tasks/hooks/use-task-draft-form";
import { useTaskTimer } from "@/components/tasks/hooks/use-task-timer";
import {
  deriveTaskTriageView,
  fetchCurrentOccurrenceContext,
  isEditableTarget,
  isRecord,
} from "@/components/tasks/task-detail/task-detail-utils";

interface TaskDetailModalProps {
  taskId: string | null;
  draftTask?: Partial<Task> | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTaskUpdated: () => void;
  onNewTaskKept?: () => void;
  entryFocus?: TimeEntry | null;
  occurrenceContext?: RecurringOccurrenceContext | null;
}

type FetchTaskOptions = {
  showLoading?: boolean;
};

export function TaskDetailModal({
  taskId,
  draftTask,
  open,
  onOpenChange,
  onTaskUpdated,
  onNewTaskKept,
  entryFocus,
  occurrenceContext,
}: TaskDetailModalProps) {
  const { allProjects, spaces } = useProject();
  const [createdTaskId, setCreatedTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const taskMetadataRef = useRef<Record<string, unknown>>({});
  const [occurrenceDateOverride, setOccurrenceDateOverride] = useState<{
    start_at: string | null;
    end_at: string | null;
  } | null>(null);
  const [occurrenceStatusOverride, setOccurrenceStatusOverride] = useState<
    string | null
  >(null);
  const [inferredOccurrenceContext, setInferredOccurrenceContext] =
    useState<RecurringOccurrenceContext | null>(null);
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);

  // 見積工数
  const [editEstHours, setEditEstHours] = useState("");
  const [estHoursSaving, setEstHoursSaving] = useState(false);

  // 編集状態
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTagIds, setDraftTagIds] = useState<string[]>([]);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const descriptionEditorRef = useRef<TaskDescriptionEditorHandle>(null);

  // コメント
  const [comments, setComments] = useState<
    {
      id: string;
      content: string;
      created_at?: string | null;
      user_id?: string | null;
    }[]
  >([]);
  const [commentText, setCommentText] = useState("");
  const [sendingComment, setSendingComment] = useState(false);
  const [attachments, setAttachments] = useState<TaskAttachment[]>([]);
  const [references, setReferences] = useState<TaskReference[]>([]);

  useEffect(() => {
    taskMetadataRef.current = isRecord(task?.metadata) ? task.metadata : {};
  }, [task?.metadata]);
  const [subtaskInputOpenSignal, setSubtaskInputOpenSignal] = useState(0);
  const [, setStatusSelectOpen] = useState(false);
  const draftSuppressTitleBlurRef = useRef(false);
  const draftSubmitIntentRef = useRef(false);
  const draftLifecycleRef = useRef(0);
  const draftSlashUpdatesRef = useRef<Record<string, unknown>>({});
  const draftSlashUpdatePromiseRef = useRef<Promise<
    Record<string, unknown>
  > | null>(null);

  const focusDescriptionEditor = useCallback(() => {
    descriptionEditorRef.current?.focus();
  }, []);

  const focusTitleEditor = useCallback(() => {
    setEditingTitle(true);
    window.setTimeout(() => titleInputRef.current?.focus(), 0);
  }, []);
  const effectiveTaskId = taskId ?? createdTaskId;
  const activeOccurrenceContext = occurrenceContext ?? inferredOccurrenceContext;

  useEffect(() => {
    setOccurrenceDateOverride(null);
    setInferredOccurrenceContext(null);
    setOccurrenceStatusOverride(occurrenceContext?.status ?? null);
  }, [
    occurrenceContext?.occurrence_id,
    occurrenceContext?.start_at,
    occurrenceContext?.end_at,
    occurrenceContext?.original_start_at,
    occurrenceContext?.status,
  ]);

  // スラッシュコマンド候補（/m: プロジェクト, /t: タグ, /status: ステータス, /priority: 優先度）
  const slashSelectedTagIds = useMemo(
    () =>
      effectiveTaskId ? (task?.tags || []).map((tag) => tag.id) : draftTagIds,
    [draftTagIds, effectiveTaskId, task],
  );
  const slashCandidates = useMemo(() => {
    return buildTaskCommandCandidates({
      projects: allProjects,
      tags,
      selectedTagIds: slashSelectedTagIds,
    });
  }, [allProjects, slashSelectedTagIds, tags]);

  const currentProjectId = task?.project_id || draftTask?.project_id || null;
  const currentSpaceId = useMemo(
    () =>
      allProjects.find((project) => project.id === currentProjectId)
        ?.space_id ?? null,
    [allProjects, currentProjectId],
  );

  const {
    resolveTagUpdates,
    handleRenameTag,
    handleChangeTagColor,
    handleDeleteTag,
    handleCopyTagToSpace,
  } = useTaskTagManagement({
    tags,
    setTags,
    setDraftTagIds,
    setTask,
    slashSelectedTagIds,
    currentProjectId,
    spaces,
    onTaskUpdated,
  });

  // debounce用
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftCreatePromiseRef = useRef<Promise<Task | null> | null>(null);
  const draftCreatedTaskIdRef = useRef<string | null>(null);
  const openRef = useRef(open);

  const {
    applyLocalDraftUpdate,
    createFromDraft,
    ensureTaskId,
    debouncedUpdate,
    immediateUpdate,
    descriptionLinkDisplayModes,
    handleDescriptionLinkDisplayModeChange,
    buildDateTaskUpdate,
    moveOccurrenceDateRange,
  } = useTaskPersistence({
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
  });

  // タスク取得
  const fetchTask = useCallback(async (options: FetchTaskOptions = {}) => {
    if (!effectiveTaskId) return;
    const shouldShowLoading = options.showLoading ?? true;
    if (shouldShowLoading) setLoading(true);
    try {
      const t = await taskApi.getTask(effectiveTaskId);
      let occurrenceForView = activeOccurrenceContext;
      if (!occurrenceForView && t.has_recurrence) {
        occurrenceForView = await fetchCurrentOccurrenceContext(t);
        setInferredOccurrenceContext(occurrenceForView);
      }
      const occurrenceStatus =
        occurrenceStatusOverride ?? occurrenceForView?.status ?? null;
      setTask(
        occurrenceStatus ? { ...t, status: occurrenceStatus } : t,
      );
      setEditTitle(t.title);
      setEditDescription(t.description || "");
      setEditEstHours(
        t.estimated_hours != null ? String(t.estimated_hours) : "",
      );
      setComments(t.comments || []);
      try {
        setAttachments(await taskApi.listAttachments(effectiveTaskId));
      } catch (err) {
        console.error("添付ファイル取得失敗", err);
        setAttachments([]);
      }
      try {
        setReferences(await taskApi.listReferences(effectiveTaskId));
      } catch (err) {
        console.error("References取得失敗", err);
        setReferences([]);
      }
      setDraftTagIds((t.tags || []).map((tag) => tag.id));
      if (t.project_id) {
        const tagList = await taskApi.listTags(t.project_id);
        setTags(tagList);
      }
    } catch (err) {
      console.error("タスク取得失敗:", err);
    } finally {
      if (shouldShowLoading) setLoading(false);
    }
  }, [activeOccurrenceContext, effectiveTaskId, occurrenceStatusOverride]);

  useTaskCompletionRefresh(fetchTask);

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  useEffect(() => {
    if (effectiveTaskId) return;
    setTask((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        tags: draftTagIds
          .map((tagId) => tags.find((tag) => tag.id === tagId))
          .filter((tag): tag is Tag => Boolean(tag)),
      };
    });
  }, [draftTagIds, effectiveTaskId, tags]);

  const {
    recurrenceRule,
    setRecurrenceRule,
    recFreq,
    recInterval,
    recByDay,
    recTriggerStatus,
    recCreateNew,
    recRecurForever,
    recResetStatusTo,
    recEndCount,
    recEndDate,
    recSkipWeekend,
    recSkipHoliday,
    recurrenceSaving,
    setRecInterval,
    setRecTriggerStatus,
    setRecCreateNew,
    setRecRecurForever,
    setRecResetStatusTo,
    setRecEndCount,
    setRecEndDate,
    setRecSkipWeekend,
    setRecSkipHoliday,
    resetRecurrenceState,
    fetchRecurrence,
    toggleWeekday,
    handleFreqChange,
    handleSaveRecurrence,
    handleDeleteRecurrence,
  } = useTaskRecurrence({
    effectiveTaskId,
    onTaskUpdated,
    setTask,
    ensureTaskId,
  });

  const { elapsedSeconds, timerLoading, handleTimer } = useTaskTimer({
    task,
    effectiveTaskId,
    open,
    fetchTask,
    onTaskUpdated,
    setTask,
  });

  const { launchingAgent, triagingAgent, handleRunWithAgent, handleRunAgentTriage } =
    useTaskAgentActions({
      task,
      editTitle,
      editDescription,
      effectiveTaskId,
      onOpenChange,
      setTask,
      fetchTask,
    });

  const {
    showRecurringDeletePrompt,
    setShowRecurringDeletePrompt,
    handleDelete,
    handleDuplicate,
    handleDeleteSingleOccurrence,
    handleDeleteFutureOccurrences,
  } = useTaskDeletion({
    effectiveTaskId,
    task,
    activeOccurrenceContext,
    editTitle,
    editDescription,
    onTaskUpdated,
    onOpenChange,
  });

  useEffect(() => {
    if (open && effectiveTaskId) {
      draftCreatedTaskIdRef.current = null;
      // モーダルが開かれるたびにリセットしてから取得
      setTask(null);
      setComments([]);
      setAttachments([]);
      setReferences([]);
      setCommentText("");
      setEditingTitle(false);
      resetRecurrenceState();
      fetchTask({ showLoading: true });
      fetchRecurrence();
    }
  }, [
    open,
    effectiveTaskId,
    fetchTask,
    fetchRecurrence,
    resetRecurrenceState,
  ]);

  useEffect(() => {
    if (!open || effectiveTaskId || !draftTask) return;
    draftCreatedTaskIdRef.current = null;
    const nextTask = buildDraftTask(draftTask);
    draftSlashUpdatesRef.current = {};
    draftSlashUpdatePromiseRef.current = null;
    setTask(nextTask);
    setTags([]);
    setDraftTagIds((draftTask.tags || []).map((tag) => tag.id));
    setLoading(false);
    setComments([]);
    setAttachments([]);
    setReferences([]);
    setCommentText("");
    setEditTitle(nextTask.title || "");
    setEditDescription(nextTask.description || "");
    setEditEstHours(
      nextTask.estimated_hours != null ? String(nextTask.estimated_hours) : "",
    );
    setDraftTagIds(
      Array.isArray((draftTask as { tag_ids?: unknown[] } | null)?.tag_ids)
        ? (draftTask as { tag_ids: unknown[] }).tag_ids.filter(
            (tagId): tagId is string => typeof tagId === "string",
          )
        : [],
    );
    setEditingTitle(true);
    setRecurrenceRule(null);
    if (nextTask.project_id) {
      void taskApi
        .listTags(nextTask.project_id)
        .then(setTags)
        .catch(() => setTags([]));
    }
  }, [draftTask, effectiveTaskId, open]);

  useEffect(() => {
    if (open) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    draftSuppressTitleBlurRef.current = false;
    draftSubmitIntentRef.current = false;
    draftLifecycleRef.current += 1;
    draftSlashUpdatesRef.current = {};
    draftSlashUpdatePromiseRef.current = null;
    setCreatedTaskId(null);
    setDraftTagIds([]);
  }, [open]);

  // 見積工数の保存
  const handleEstHoursBlur = useCallback(async () => {
    if (!effectiveTaskId || !task) return;
    const newVal = editEstHours ? parseFloat(editEstHours) : null;
    const oldVal = task.estimated_hours ?? null;
    if (newVal === oldVal) return;
    setEstHoursSaving(true);
    try {
      await taskApi.updateTask(
        effectiveTaskId,
        buildManualEstimateTaskPatch({
          estimatedHours: newVal,
          currentMetadata: task.metadata,
        }),
      );
      await fetchTask();
      onTaskUpdated();
    } catch (err) {
      console.error("見積工数更新失敗:", err);
    } finally {
      setEstHoursSaving(false);
    }
  }, [effectiveTaskId, task, editEstHours, fetchTask, onTaskUpdated]);

  useEffect(() => {
    if (!open) return;
    const handleSlashShortcut = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isEditableTarget(e.target)) return;
      e.preventDefault();
      setEditTitle((prev) => {
        if (!prev) return "/";
        return /\s$/.test(prev) ? `${prev}/` : `${prev} /`;
      });
      setEditingTitle(true);
    };
    window.addEventListener("keydown", handleSlashShortcut);
    return () => window.removeEventListener("keydown", handleSlashShortcut);
  }, [open]);

  // コメント送信
  const handleSendComment = useCallback(async () => {
    if (!effectiveTaskId || !commentText.trim()) return;
    setSendingComment(true);
    try {
      await taskApi.addComment(effectiveTaskId, commentText.trim());
      setCommentText("");
      setComments((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          content: commentText.trim(),
          created_at: new Date().toISOString(),
        },
      ]);
    } catch (err) {
      console.error("コメント送信失敗:", err);
    } finally {
      setSendingComment(false);
    }
  }, [commentText, effectiveTaskId]);

  const safeTags = Array.isArray(tags) ? tags : [];
  const displayTaskTags = effectiveTaskId
    ? (task?.tags ?? [])
    : safeTags.filter((tag) => draftTagIds.includes(tag.id));
  const taskWithOccurrenceDate = task
    ? ({
        ...task,
        effective_start_at:
          occurrenceDateOverride?.start_at ??
          activeOccurrenceContext?.start_at ??
          null,
        effective_end_at:
          occurrenceDateOverride?.end_at ??
          activeOccurrenceContext?.end_at ??
          null,
        effective_occurrence_start_at:
          occurrenceDateOverride?.start_at ??
          activeOccurrenceContext?.start_at ??
          null,
        effective_occurrence_end_at:
          occurrenceDateOverride?.end_at ??
          activeOccurrenceContext?.end_at ??
          null,
        effective_occurrence_source_kind:
          activeOccurrenceContext?.source_kind ?? null,
      } satisfies Task)
    : null;
  const displayStartAt = taskWithOccurrenceDate
    ? getTaskDisplayStartAt(taskWithOccurrenceDate)
    : null;
  const displayEndAt = taskWithOccurrenceDate
    ? getTaskDisplayEndAt(taskWithOccurrenceDate)
    : null;

  const {
    handleDialogOpenChange,
    handleSubmitAndCloseIntent,
    handleTitleChange,
    handleTitleBlur,
    handleTitleSubmitIntent,
    handleParseSlashCommands,
  } = useTaskDraftForm({
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
  });

  const { docsNodeLoading, handleOpenDocsNode, handleOpenMeetingNote } =
    useTaskDocsNode({
      effectiveTaskId,
      task,
      handleDialogOpenChange,
      onTaskUpdated,
      setTask,
    });

  const {
    triageStatus,
    triageSummary,
    triageHasSummary,
    triageQuestions,
    shouldShowTriageCard,
  } = deriveTaskTriageView(task);

  return (
    <>
      <Dialog open={open} onOpenChange={handleDialogOpenChange}>
        <DialogContent
          className="sm:max-w-6xl max-h-[85vh] overflow-y-auto p-0"
          showCloseButton={true}
        >
          <DialogHeader className="sr-only">
            <DialogTitle>タスク詳細</DialogTitle>
            <DialogDescription>
              タスクの詳細情報を表示・編集します
            </DialogDescription>
          </DialogHeader>

          {loading ? (
            <div className="p-4 space-y-4">
              <Skeleton className="h-8 w-48" />
              <Skeleton className="h-64 w-full" />
            </div>
          ) : !task ? (
            <div className="flex items-center justify-center p-16 text-muted-foreground">
              タスクが見つかりません
            </div>
          ) : (
            <div className="flex flex-col h-full">
              {/* メインコンテンツ */}
              <div className="flex-1 overflow-auto p-4 space-y-6">
                {/* タイトル */}
                <TaskDetailHeader
                  task={task}
                  effectiveTaskId={effectiveTaskId}
                  editTitle={editTitle}
                  editingTitle={editingTitle}
                  setEditingTitle={setEditingTitle}
                  titleInputRef={titleInputRef}
                  slashCandidates={slashCandidates}
                  entryFocus={entryFocus}
                  allProjects={allProjects}
                  spaces={spaces}
                  launchingAgent={launchingAgent}
                  triagingAgent={triagingAgent}
                  onTitleChange={handleTitleChange}
                  onTitleBlur={handleTitleBlur}
                  onTitleSubmitIntent={handleTitleSubmitIntent}
                  onParseSlashCommands={handleParseSlashCommands}
                  focusDescriptionEditor={focusDescriptionEditor}
                  immediateUpdate={immediateUpdate}
                  handleRunWithAgent={handleRunWithAgent}
                  handleRunAgentTriage={handleRunAgentTriage}
                  handleDuplicate={handleDuplicate}
                  handleDelete={handleDelete}
                  handleDialogOpenChange={handleDialogOpenChange}
                />

                {shouldShowTriageCard ? (
                  <TaskDetailTriageCard
                    triageStatus={triageStatus}
                    triageSummary={triageSummary}
                    triageHasSummary={triageHasSummary}
                    triageQuestions={triageQuestions}
                  />
                ) : null}

                {/* ClickUp風 プロパティグリッド */}
                <TaskDetailPropertyGrid
                  task={task}
                  effectiveTaskId={effectiveTaskId}
                  tags={tags}
                  spaces={spaces}
                  currentSpaceId={currentSpaceId}
                  displayTaskTags={displayTaskTags}
                  displayStartAt={displayStartAt}
                  displayEndAt={displayEndAt}
                  activeOccurrenceContext={activeOccurrenceContext}
                  editEstHours={editEstHours}
                  setEditEstHours={setEditEstHours}
                  estHoursSaving={estHoursSaving}
                  handleEstHoursBlur={handleEstHoursBlur}
                  elapsedSeconds={elapsedSeconds}
                  timerLoading={timerLoading}
                  handleTimer={handleTimer}
                  docsNodeLoading={docsNodeLoading}
                  draftTagIds={draftTagIds}
                  setStatusSelectOpen={setStatusSelectOpen}
                  immediateUpdate={immediateUpdate}
                  applyLocalDraftUpdate={applyLocalDraftUpdate}
                  buildDateTaskUpdate={buildDateTaskUpdate}
                  moveOccurrenceDateRange={moveOccurrenceDateRange}
                  resolveTagUpdates={resolveTagUpdates}
                  handleRenameTag={handleRenameTag}
                  handleChangeTagColor={handleChangeTagColor}
                  handleDeleteTag={handleDeleteTag}
                  handleCopyTagToSpace={handleCopyTagToSpace}
                  handleOpenDocsNode={handleOpenDocsNode}
                  handleOpenMeetingNote={handleOpenMeetingNote}
                  recurrenceRule={recurrenceRule}
                  recFreq={recFreq}
                  recInterval={recInterval}
                  recByDay={recByDay}
                  recTriggerStatus={recTriggerStatus}
                  recCreateNew={recCreateNew}
                  recRecurForever={recRecurForever}
                  recResetStatusTo={recResetStatusTo}
                  recEndCount={recEndCount}
                  recEndDate={recEndDate}
                  recSkipWeekend={recSkipWeekend}
                  recSkipHoliday={recSkipHoliday}
                  recurrenceSaving={recurrenceSaving}
                  handleFreqChange={handleFreqChange}
                  setRecInterval={setRecInterval}
                  toggleWeekday={toggleWeekday}
                  setRecTriggerStatus={setRecTriggerStatus}
                  setRecCreateNew={setRecCreateNew}
                  setRecRecurForever={setRecRecurForever}
                  setRecResetStatusTo={setRecResetStatusTo}
                  setRecEndCount={setRecEndCount}
                  setRecEndDate={setRecEndDate}
                  setRecSkipWeekend={setRecSkipWeekend}
                  setRecSkipHoliday={setRecSkipHoliday}
                  handleSaveRecurrence={handleSaveRecurrence}
                  handleDeleteRecurrence={handleDeleteRecurrence}
                />

                {/* 説明 */}
                <div className="space-y-2">
                  <Label>説明</Label>
                  <TaskDescriptionEditor
                    ref={descriptionEditorRef}
                    value={editDescription}
                    onChange={(val) => {
                      setEditDescription(val);
                      debouncedUpdate({ description: val });
                    }}
                    placeholder="説明を追加..."
                    minHeight={80}
                    linkDisplayModes={descriptionLinkDisplayModes}
                    onLinkDisplayModeChange={
                      handleDescriptionLinkDisplayModeChange
                    }
                    onSubmitIntent={(value) => {
                      void handleSubmitAndCloseIntent(value);
                    }}
                    onArrowUpFromStart={focusTitleEditor}
                  />
                </div>

                <Separator />

                {/* サブタスク */}
                <SubtaskSection
                  task={task!}
                  onEnsureTask={!effectiveTaskId ? createFromDraft : undefined}
                  openInputSignal={subtaskInputOpenSignal}
                  onSubtaskAdded={(parentTask, subtask) => {
                    setTask((prev) => {
                      const baseTask =
                        prev && prev.id === parentTask.id ? prev : parentTask;
                      const subtasks = baseTask.subtasks || [];
                      if (subtasks.some((item) => item.id === subtask.id)) {
                        return baseTask;
                      }
                      return {
                        ...baseTask,
                        subtasks: [...subtasks, subtask],
                      };
                    });
                    setSubtaskInputOpenSignal((value) => value + 1);
                    void fetchTask({ showLoading: false });
                    onTaskUpdated();
                  }}
                  onSubtaskUpdated={(updatedSubtask) => {
                    setTask((prev) =>
                      prev
                        ? {
                            ...prev,
                            subtasks: (prev.subtasks || []).map((subtask) =>
                              subtask.id === updatedSubtask.id
                                ? { ...subtask, ...updatedSubtask }
                                : subtask,
                            ),
                          }
                        : prev,
                    );
                  }}
                  onSubtaskDeleted={(subtaskId) => {
                    setTask((prev) =>
                      prev
                        ? {
                            ...prev,
                            subtasks: (prev.subtasks || []).filter(
                              (subtask) => subtask.id !== subtaskId,
                            ),
                          }
                        : prev,
                    );
                  }}
                  onUpdated={() => {
                    void fetchTask({ showLoading: false });
                    onTaskUpdated();
                  }}
                />

                <TaskAttachmentsSection
                  effectiveTaskId={effectiveTaskId}
                  attachments={attachments}
                  setAttachments={setAttachments}
                  references={references}
                  setReferences={setReferences}
                />

                {/* コメント */}
                <TaskDetailComments
                  comments={comments}
                  commentText={commentText}
                  setCommentText={setCommentText}
                  sendingComment={sendingComment}
                  onSendComment={handleSendComment}
                />
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
      <RecurringDeleteDialog
        open={showRecurringDeletePrompt}
        onOpenChange={setShowRecurringDeletePrompt}
        onDeleteSingle={handleDeleteSingleOccurrence}
        onDeleteFuture={handleDeleteFutureOccurrences}
      />
    </>
  );
}
