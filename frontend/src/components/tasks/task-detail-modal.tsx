"use client";

import React, {
  useState,
  useEffect,
  useLayoutEffect,
  useCallback,
  useRef,
  useMemo,
} from "react";
import { Upload } from "lucide-react";
import { toast } from "sonner";
import {
  buildDraftTask,
  buildManualEstimateTaskPatch,
  buildTaskCommandCandidates,
} from "@/components/tasks/task-form-utils";
import {
  TaskDescriptionEditor,
  type TaskDescriptionEditorHandle,
  type EditorImageInsertHandler,
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
  TaskApiError,
  type RecurringOccurrenceContext,
  type Task,
  type Tag,
  type TimeEntry,
  type TaskAttachment,
  type TaskReference,
} from "@/lib/task-api";
import { uploadFailureToastOptions } from "@/lib/upload-failure";
import {
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import { useProject } from "@/contexts/project-context";
import { cn } from "@/lib/utils";

import { RecurringDeleteDialog } from "@/components/tasks/task-detail/recurring-delete-dialog";
import { SubtaskSection } from "@/components/tasks/task-detail/subtask-section";
import { TaskAttachmentsSection } from "@/components/tasks/task-detail/task-attachments-section";
import { TaskDetailHeader } from "@/components/tasks/task-detail/task-detail-header";
import { TaskDetailTriageCard } from "@/components/tasks/task-detail/task-detail-triage-card";
import { TaskDetailPropertyGrid } from "@/components/tasks/task-detail/task-detail-property-grid";
import { TaskDetailComments } from "@/components/tasks/task-detail/task-detail-comments";
import { TaskDependencySection } from "@/components/tasks/task-detail/task-dependency-section";
import { useTaskTagManagement } from "@/components/tasks/hooks/use-task-tag-management";
import { useTaskRecurrence } from "@/components/tasks/hooks/use-task-recurrence";
import { useTaskPersistence } from "@/components/tasks/hooks/use-task-persistence";
import { useTaskAgentActions } from "@/components/tasks/hooks/use-task-agent-actions";
import { useTaskDeletion } from "@/components/tasks/hooks/use-task-deletion";
import { useTaskDocsNode } from "@/components/tasks/hooks/use-task-docs-node";
import { useTaskDraftForm } from "@/components/tasks/hooks/use-task-draft-form";
import { useTaskTimer } from "@/components/tasks/hooks/use-task-timer";
import { isImageFile } from "@/lib/editor-image-files";
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
  onTaskUpdated: (
    task?: Task | null,
    options?: { removedTaskId?: string },
  ) => void;
  onTaskLoaded?: (task: Task) => void;
  onOpenTask?: (taskId: string) => void;
  onNewTaskKept?: () => void;
  entryFocus?: TimeEntry | null;
  occurrenceContext?: RecurringOccurrenceContext | null;
  readOnly?: boolean;
}

type FetchTaskOptions = {
  showLoading?: boolean;
};

type ReferenceMutation = {
  deletedId?: string;
  deletedTargetId?: string;
  deletionFailed?: boolean;
};

type Tombstones = ReadonlySet<string> | ReadonlyMap<string, number>;

type DraftInitialReloadState = {
  taskId: string;
  attachmentsPending: boolean;
  referencesPending: boolean;
};

function mergeTaskItems<T extends { id: string }>(
  fetched: T[],
  previous: T[],
  tombstones: Tombstones,
  getTombstoneKeys: (item: T) => readonly string[] = (item) => [item.id],
): T[] {
  const fetchedIds = new Set(fetched.flatMap((item) => getTombstoneKeys(item)));
  return [
    ...previous.filter(
      (item) =>
        getTombstoneKeys(item).every((key) => !fetchedIds.has(key)) &&
        getTombstoneKeys(item).every((key) => !tombstones.has(key)),
    ),
    ...fetched.filter((item) =>
      getTombstoneKeys(item).every((key) => !tombstones.has(key)),
    ),
  ];
}

function reconcileTombstones(
  tombstones: Map<string, number>,
  fetchedIds: ReadonlySet<string>,
  requestedGeneration: number,
  currentGeneration: number,
) {
  // GET開始後に削除された場合は、そのGETが古い一覧なのでtombstoneを
  // 消さない。削除後に開始したGETが削除済みIDを返さなかった時だけ、
  // サーバー側でも削除済みと確定したものとして解放する。
  if (requestedGeneration !== currentGeneration) return;
  for (const id of tombstones.keys()) {
    if (!fetchedIds.has(id)) tombstones.delete(id);
  }
}

function referenceTombstoneKeys(reference: TaskReference): string[] {
  const targetId =
    reference.target_id ??
    (reference.reference_type === "task_attachment"
      ? reference.attachment?.id
      : null);
  return [reference.id, ...(targetId ? [`target:${targetId}`] : [])];
}

export function TaskDetailModal({
  taskId,
  draftTask,
  open,
  onOpenChange,
  onTaskUpdated,
  onTaskLoaded,
  onOpenTask,
  onNewTaskKept,
  entryFocus,
  occurrenceContext,
  readOnly = false,
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
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [fileDragActive, setFileDragActive] = useState(false);
  const fileDragDepthRef = useRef(0);
  const attachmentUploadInFlightRef = useRef(false);
  const attachmentUploadGenerationRef = useRef(0);
  const attachmentMutationGenerationRef = useRef(0);
  const referenceMutationGenerationRef = useRef(0);
  const attachmentTombstonesRef = useRef(new Map<string, number>());
  const referenceTombstonesRef = useRef(new Map<string, number>());
  const draftInitialReloadRef = useRef<DraftInitialReloadState | null>(null);
  const taskFetchGenerationRef = useRef(0);

  useEffect(() => {
    taskMetadataRef.current = isRecord(task?.metadata) ? task.metadata : {};
  }, [task?.metadata]);
  const [subtaskInputOpenSignal, setSubtaskInputOpenSignal] = useState(0);
  const [, setStatusSelectOpen] = useState(false);
  const draftSuppressTitleBlurRef = useRef(false);
  const draftSubmitIntentRef = useRef(false);
  const draftLifecycleRef = useRef(0);
  const draftLifecycleIdentityRef = useRef({ open, draftTask });
  if (
    draftLifecycleIdentityRef.current.open !== open ||
    draftLifecycleIdentityRef.current.draftTask !== draftTask
  ) {
    draftLifecycleRef.current += 1;
    draftLifecycleIdentityRef.current = { open, draftTask };
  }
  const draftSlashUpdatesRef = useRef<Record<string, unknown>>({});
  const draftSlashUpdatePromiseRef = useRef<Promise<
    Record<string, unknown>
  > | null>(null);

  const focusDescriptionEditor = useCallback(() => {
    if (readOnly) return;
    descriptionEditorRef.current?.focus();
  }, [readOnly]);

  const focusTitleEditor = useCallback(() => {
    if (readOnly) return;
    setEditingTitle(true);
    window.setTimeout(() => titleInputRef.current?.focus(), 0);
  }, [readOnly]);
  const effectiveTaskId = taskId ?? createdTaskId;
  const activeOccurrenceContext =
    occurrenceContext ?? inferredOccurrenceContext;

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
  const draftCreateGenerationRef = useRef<number | null>(null);
  const draftCreatedTaskIdRef = useRef<string | null>(null);
  const persistenceLifecycleRef = useRef(0);
  const persistenceLifecycleIdentityRef = useRef({ open, taskId, draftTask });
  if (
    persistenceLifecycleIdentityRef.current.open !== open ||
    persistenceLifecycleIdentityRef.current.taskId !== taskId ||
    persistenceLifecycleIdentityRef.current.draftTask !== draftTask
  ) {
    persistenceLifecycleRef.current += 1;
    persistenceLifecycleIdentityRef.current = { open, taskId, draftTask };
  }
  const openRef = useRef(open);
  const attachmentTaskIdRef = useRef(effectiveTaskId);

  useLayoutEffect(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
  }, [draftTask, open, taskId]);

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
    draftCreateGenerationRef,
    draftCreatedTaskIdRef,
    draftLifecycleRef,
    lifecycleGenerationRef: persistenceLifecycleRef,
    taskMetadataRef,
  });

  const ensureRecurrenceTaskId = useCallback(async () => {
    const draftLifecycle = draftLifecycleRef.current;
    const ensuredTaskId = await ensureTaskId();
    return draftLifecycleRef.current === draftLifecycle ? ensuredTaskId : null;
  }, [ensureTaskId]);

  const uploadAttachmentFiles = useCallback(
    async (
      files: FileList | readonly File[],
      purpose: "reference" | "description-image" = "reference",
    ): Promise<TaskAttachment[]> => {
      const pendingFiles = Array.from(files);
      if (
        readOnly ||
        pendingFiles.length === 0 ||
        attachmentUploadInFlightRef.current
      ) {
        return [];
      }

      const uploadGeneration = attachmentUploadGenerationRef.current + 1;
      attachmentUploadGenerationRef.current = uploadGeneration;
      attachmentUploadInFlightRef.current = true;
      setUploadingAttachments(true);
      attachmentMutationGenerationRef.current += 1;

      // 新規タスクはタイトル編集だけではまだ保存されていないため、
      // 参照ファイルを追加する操作を起点にドラフトを先に保存する。
      try {
        const uploadTaskId = effectiveTaskId ?? (await ensureTaskId());
        if (!uploadTaskId) {
          toast.error("タイトルを入力してからファイルを追加してください");
          return [];
        }
        // ensureTaskId() がReactのstate/effect反映より先に返る場合でも、
        // 同じドラフトから続くアップロード結果を有効なものとして扱う。
        // ensureTaskId側でlifecycleを検証済みなので、別タスクのIDはここへ到達しない。
        attachmentTaskIdRef.current = uploadTaskId;
        const results = await Promise.allSettled(
          pendingFiles.map((file) =>
            taskApi.uploadAttachment(uploadTaskId, file),
          ),
        );
        // GET開始後にuploadが完了したことを観測できるよう、完了時にも
        // mutation世代を進める。初期reload/古いGETは結果をmergeする。
        attachmentMutationGenerationRef.current += 1;
        if (
          attachmentUploadGenerationRef.current !== uploadGeneration ||
          attachmentTaskIdRef.current !== uploadTaskId ||
          !openRef.current
        ) {
          return [];
        }
        const uploaded = results.flatMap((result) =>
          result.status === "fulfilled" ? [result.value] : [],
        );
        const failed = results.flatMap((result, index) =>
          result.status === "rejected"
            ? [{ file: pendingFiles[index], reason: result.reason }]
            : [],
        );
        const failureDetails = failed.map(({ file, reason }) => ({
          name: file.name,
          status: reason instanceof TaskApiError ? reason.status : undefined,
          message: reason instanceof Error ? reason.message : undefined,
        }));

        if (uploaded.length > 0) {
          setAttachments((prev) => [...uploaded, ...prev]);
        }
        if (failed.length === 0) {
          toast.success(
            purpose === "description-image"
              ? uploaded.length === 1
                ? "画像を説明に挿入しました"
                : `${uploaded.length}件の画像を説明に挿入しました`
              : uploaded.length === 1
                ? "ファイルをリファレンスに追加しました"
                : `${uploaded.length}件のファイルをリファレンスに追加しました`,
          );
        } else if (uploaded.length > 0) {
          toast.warning(
            `${uploaded.length}件を追加し、${failed.length}件は追加できませんでした`,
            uploadFailureToastOptions(failureDetails),
          );
        } else {
          toast.error(
            "ファイルのアップロードに失敗しました",
            uploadFailureToastOptions(failureDetails),
          );
        }
        return uploaded;
      } catch (err) {
        if (attachmentUploadGenerationRef.current === uploadGeneration) {
          toast.error(
            err instanceof Error ? err.message : "ファイルの追加に失敗しました",
          );
        }
        return [];
      } finally {
        if (attachmentUploadGenerationRef.current === uploadGeneration) {
          attachmentUploadInFlightRef.current = false;
          setUploadingAttachments(false);
        }
      }
    },
    [effectiveTaskId, ensureTaskId, readOnly],
  );

  const handleDescriptionImageInsert = useCallback<EditorImageInsertHandler>(
    async (files) => {
      if (readOnly) return null;
      const uploaded = await uploadAttachmentFiles(files, "description-image");
      const markdown = uploaded
        .map((attachment) => {
          const displayName =
            attachment.display_name ||
            attachment.file_path.split(/[\\/]/).pop() ||
            "image";
          const url =
            attachment.url ||
            (attachment.task_id && attachment.id
              ? `/api/tasks/${attachment.task_id}/attachments/${attachment.id}`
              : null);
          if (!url) return null;
          const alt = displayName
            .replace(/[\r\n]/g, " ")
            .replace(/\]/g, "\\]");
          return `![${alt}](${url})`;
        })
        .filter((reference): reference is string => Boolean(reference))
        .join("\n");
      return markdown || null;
    },
    [readOnly, uploadAttachmentFiles],
  );

  const markAttachmentMutation = useCallback((mutation?: ReferenceMutation) => {
    attachmentMutationGenerationRef.current += 1;
    if (!mutation?.deletedId) return;
    if (mutation.deletionFailed) {
      attachmentTombstonesRef.current.delete(mutation.deletedId);
    } else {
      attachmentTombstonesRef.current.set(
        mutation.deletedId,
        attachmentMutationGenerationRef.current,
      );
    }
  }, []);

  const markReferenceMutation = useCallback((mutation?: ReferenceMutation) => {
    referenceMutationGenerationRef.current += 1;
    if (!mutation?.deletedId && !mutation?.deletedTargetId) return;
    if (mutation.deletionFailed) {
      if (mutation.deletedId) {
        referenceTombstonesRef.current.delete(mutation.deletedId);
      }
      if (mutation.deletedTargetId) {
        referenceTombstonesRef.current.delete(
          `target:${mutation.deletedTargetId}`,
        );
      }
    } else {
      if (mutation.deletedId) {
        referenceTombstonesRef.current.set(
          mutation.deletedId,
          referenceMutationGenerationRef.current,
        );
      }
      if (mutation.deletedTargetId) {
        referenceTombstonesRef.current.set(
          `target:${mutation.deletedTargetId}`,
          referenceMutationGenerationRef.current,
        );
      }
    }
  }, []);

  const hasDraggedFiles = useCallback(
    (dataTransfer: DataTransfer | null) =>
      Boolean(dataTransfer && Array.from(dataTransfer.types).includes("Files")),
    [],
  );

  const clearFileDragState = useCallback(() => {
    fileDragDepthRef.current = 0;
    setFileDragActive(false);
  }, []);

  const isEditorImageDragEvent = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest?.(".cm-content")) return false;
      const files = Array.from(event.dataTransfer?.files ?? []);
      if (files.some((file) => isImageFile(file))) return true;
      return Array.from(event.dataTransfer?.items ?? []).some(
        (item) =>
          item.kind === "file" && item.type.toLowerCase().startsWith("image/"),
      );
    },
    [],
  );

  const handleFileDragEnter = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      if (isEditorImageDragEvent(event)) {
        clearFileDragState();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect =
        !readOnly && !uploadingAttachments ? "copy" : "none";
      fileDragDepthRef.current += 1;
      if (
        fileDragDepthRef.current === 1 &&
        !readOnly &&
        !uploadingAttachments
      ) {
        setFileDragActive(true);
      }
    },
    [clearFileDragState, hasDraggedFiles, isEditorImageDragEvent, readOnly, uploadingAttachments],
  );

  const handleFileDragOver = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      if (isEditorImageDragEvent(event)) {
        clearFileDragState();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect =
        !readOnly && !uploadingAttachments ? "copy" : "none";
    },
    [clearFileDragState, hasDraggedFiles, isEditorImageDragEvent, readOnly, uploadingAttachments],
  );

  const handleFileDragLeave = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (fileDragDepthRef.current === 0) return;
      if (isEditorImageDragEvent(event)) {
        clearFileDragState();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      fileDragDepthRef.current = Math.max(0, fileDragDepthRef.current - 1);
      if (fileDragDepthRef.current === 0) setFileDragActive(false);
    },
    [clearFileDragState, isEditorImageDragEvent],
  );

  const handleFileDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      if (isEditorImageDragEvent(event)) {
        clearFileDragState();
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      clearFileDragState();
      if (readOnly || uploadingAttachments) return;
      void uploadAttachmentFiles(event.dataTransfer.files);
    },
    [
      hasDraggedFiles,
      clearFileDragState,
      isEditorImageDragEvent,
      readOnly,
      uploadAttachmentFiles,
      uploadingAttachments,
    ],
  );

  // CodeMirror stops handled image drops before the Dialog bubble handlers
  // run. Capture the event at the modal boundary so an earlier non-image drag
  // cannot leave the References overlay active after the editor consumes it.
  const handleEditorImageDragCapture = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (hasDraggedFiles(event.dataTransfer) && isEditorImageDragEvent(event)) {
        clearFileDragState();
      }
    },
    [clearFileDragState, hasDraggedFiles, isEditorImageDragEvent],
  );

  // タスク取得
  const fetchTask = useCallback(
    async (options: FetchTaskOptions = {}) => {
      if (!effectiveTaskId) return;
      const requestedTaskId = effectiveTaskId;
      const requestGeneration = taskFetchGenerationRef.current + 1;
      taskFetchGenerationRef.current = requestGeneration;
      const requestedAttachmentMutationGeneration =
        attachmentMutationGenerationRef.current;
      const requestedReferenceMutationGeneration =
        referenceMutationGenerationRef.current;
      const draftInitialReloadTaskId =
        draftInitialReloadRef.current?.taskId === requestedTaskId
          ? requestedTaskId
          : null;
      const attachmentTombstones = attachmentTombstonesRef.current;
      const referenceTombstones = referenceTombstonesRef.current;
      const completeDraftInitialReload = (
        key: "attachmentsPending" | "referencesPending",
      ) => {
        const current = draftInitialReloadRef.current;
        if (current?.taskId !== requestedTaskId) return;
        current[key] = false;
        if (!current.attachmentsPending && !current.referencesPending) {
          draftInitialReloadRef.current = null;
        }
      };
      const isCurrentRequest = () =>
        openRef.current &&
        attachmentTaskIdRef.current === requestedTaskId &&
        taskFetchGenerationRef.current === requestGeneration;
      const shouldShowLoading = options.showLoading ?? true;
      if (shouldShowLoading) setLoading(true);
      try {
        const t = await taskApi.getTask(requestedTaskId);
        if (!isCurrentRequest()) return;
        let occurrenceForView = activeOccurrenceContext;
        if (!occurrenceForView && t.has_recurrence) {
          occurrenceForView = await fetchCurrentOccurrenceContext(t);
          if (!isCurrentRequest()) return;
          setInferredOccurrenceContext(occurrenceForView);
        }
        const occurrenceStatus =
          occurrenceStatusOverride ?? occurrenceForView?.status ?? null;
        setTask(occurrenceStatus ? { ...t, status: occurrenceStatus } : t);
        // 詳細の単体GETで得た正本を、呼び出し側の一覧キャッシュにも同期できるようにする。
        // occurrence表示用の上書きではなくDB上のタスク本体を渡す。
        onTaskLoaded?.(t);
        setEditTitle(t.title);
        setEditDescription(t.description || "");
        setEditEstHours(
          t.estimated_hours != null ? String(t.estimated_hours) : "",
        );
        setComments(t.comments || []);
        try {
          const nextAttachments =
            await taskApi.listAttachments(requestedTaskId);
          if (!isCurrentRequest()) return;
          const nextAttachmentIds = new Set(
            nextAttachments.map((attachment) => attachment.id),
          );
          reconcileTombstones(
            attachmentTombstones,
            nextAttachmentIds,
            requestedAttachmentMutationGeneration,
            attachmentMutationGenerationRef.current,
          );
          const shouldMerge =
            requestedAttachmentMutationGeneration !==
              attachmentMutationGenerationRef.current ||
            draftInitialReloadTaskId === requestedTaskId;
          setAttachments((previous) =>
            shouldMerge
              ? mergeTaskItems(nextAttachments, previous, attachmentTombstones)
              : nextAttachments.filter(
                  (attachment) => !attachmentTombstones.has(attachment.id),
                ),
          );
          completeDraftInitialReload("attachmentsPending");
        } catch (err) {
          if (!isCurrentRequest()) return;
          console.error("添付ファイル取得失敗", err);
          const shouldPreserve =
            requestedAttachmentMutationGeneration !==
              attachmentMutationGenerationRef.current ||
            draftInitialReloadTaskId === requestedTaskId;
          if (!shouldPreserve && attachmentTombstones.size === 0) {
            setAttachments([]);
          }
          completeDraftInitialReload("attachmentsPending");
        }
        try {
          const nextReferences = await taskApi.listReferences(requestedTaskId);
          if (!isCurrentRequest()) return;
          const nextReferenceIds = new Set(
            nextReferences.flatMap(referenceTombstoneKeys),
          );
          reconcileTombstones(
            referenceTombstones,
            nextReferenceIds,
            requestedReferenceMutationGeneration,
            referenceMutationGenerationRef.current,
          );
          const shouldMerge =
            requestedReferenceMutationGeneration !==
              referenceMutationGenerationRef.current ||
            draftInitialReloadTaskId === requestedTaskId;
          setReferences((previous) =>
            shouldMerge
              ? mergeTaskItems(
                  nextReferences,
                  previous,
                  referenceTombstones,
                  referenceTombstoneKeys,
                )
              : nextReferences.filter((reference) =>
                  referenceTombstoneKeys(reference).every(
                    (key) => !referenceTombstones.has(key),
                  ),
                ),
          );
          completeDraftInitialReload("referencesPending");
        } catch (err) {
          if (!isCurrentRequest()) return;
          console.error("References取得失敗", err);
          const shouldPreserve =
            requestedReferenceMutationGeneration !==
              referenceMutationGenerationRef.current ||
            draftInitialReloadTaskId === requestedTaskId;
          if (!shouldPreserve && referenceTombstones.size === 0) {
            setReferences([]);
          }
          completeDraftInitialReload("referencesPending");
        }
        if (!isCurrentRequest()) return;
        setDraftTagIds((t.tags || []).map((tag) => tag.id));
        if (t.project_id) {
          const tagList = await taskApi.listTags(t.project_id);
          if (!isCurrentRequest()) return;
          setTags(tagList);
        }
      } catch (err) {
        if (isCurrentRequest()) console.error("タスク取得失敗:", err);
      } finally {
        if (shouldShowLoading && isCurrentRequest()) setLoading(false);
      }
    },
    [
      activeOccurrenceContext,
      effectiveTaskId,
      occurrenceStatusOverride,
      onTaskLoaded,
    ],
  );

  useTaskCompletionRefresh(fetchTask);

  useLayoutEffect(() => {
    const previousTaskId = attachmentTaskIdRef.current;
    // ensureTaskId() はドラフトを保存した直後にファイルアップロードを
    // 継続するため、null -> createdTaskId の切り替えで進行中のアップロードを
    // 無効化しない。それ以外のタスク切り替え・開閉では従来どおり破棄する。
    const materializingDraft =
      (!previousTaskId && Boolean(effectiveTaskId) && open) ||
      (attachmentUploadInFlightRef.current &&
        previousTaskId === effectiveTaskId &&
        open);
    openRef.current = open;
    attachmentTaskIdRef.current = effectiveTaskId;
    if (previousTaskId !== effectiveTaskId) {
      attachmentTombstonesRef.current.clear();
      referenceTombstonesRef.current.clear();
    }
    if (!materializingDraft) {
      taskFetchGenerationRef.current += 1;
      attachmentUploadGenerationRef.current += 1;
      attachmentUploadInFlightRef.current = false;
      setUploadingAttachments(false);
    }
    fileDragDepthRef.current = 0;
    setFileDragActive(false);
  }, [effectiveTaskId, open]);

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
    recSkipMode,
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
    setRecSkipMode,
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
    ensureTaskId: ensureRecurrenceTaskId,
    lifecycleGeneration: draftLifecycleRef.current,
  });

  const { elapsedSeconds, timerLoading, handleTimer } = useTaskTimer({
    task,
    effectiveTaskId,
    open,
    fetchTask,
    onTaskUpdated,
    setTask,
  });

  const { openingChat, triagingAgent, handleOpenInChat, handleRunAgentTriage } =
    useTaskAgentActions({
      task,
      editTitle,
      editDescription,
      effectiveTaskId,
      onOpenChange,
      setTask,
      fetchTask,
      ensureTaskId,
    });

  const {
    showRecurringDeletePrompt,
    setShowRecurringDeletePrompt,
    handleDelete,
    handleDuplicate,
    handleDeleteSingleOccurrence,
    handleDeleteFutureOccurrences,
    handleDeleteRecurringSeries,
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
      const materializingDraft =
        draftCreatedTaskIdRef.current === effectiveTaskId;
      draftCreatedTaskIdRef.current = null;
      // ドラフトから初めてIDを得た直後は、作成処理の戻り値を表示したまま
      // 最新データを裏で取得する。ここでローディング表示へ戻すと参照欄が
      // 一度アンマウントされ、追加中のURL／ファイル操作が失われる。
      if (!materializingDraft) {
        // モーダルが開かれるたびにリセットしてから取得
        draftInitialReloadRef.current = null;
        setTask(null);
        setComments([]);
        setAttachments([]);
        setReferences([]);
        setCommentText("");
        setEditingTitle(false);
      } else {
        // 作成直後の単体GETは、作成中に返ってきた空の一覧でローカルの
        // 追加成功状態を上書きしないよう、2つの初期reload完了までmergeする。
        draftInitialReloadRef.current = {
          taskId: effectiveTaskId,
          attachmentsPending: true,
          referencesPending: true,
        };
      }
      resetRecurrenceState();
      fetchTask({ showLoading: !materializingDraft });
      fetchRecurrence();
    }
  }, [open, effectiveTaskId, fetchTask, fetchRecurrence, resetRecurrenceState]);

  useEffect(() => {
    if (!open || effectiveTaskId || !draftTask) return;
    const requestGeneration = taskFetchGenerationRef.current + 1;
    taskFetchGenerationRef.current = requestGeneration;
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
    resetRecurrenceState();
    if (nextTask.project_id) {
      const requestedProjectId = nextTask.project_id;
      void taskApi
        .listTags(requestedProjectId)
        .then((nextTags) => {
          if (
            openRef.current &&
            !attachmentTaskIdRef.current &&
            taskFetchGenerationRef.current === requestGeneration
          ) {
            setTags(nextTags);
          }
        })
        .catch(() => {
          if (
            openRef.current &&
            !attachmentTaskIdRef.current &&
            taskFetchGenerationRef.current === requestGeneration
          ) {
            setTags([]);
          }
        });
    }
  }, [draftTask, effectiveTaskId, open, resetRecurrenceState]);

  useEffect(() => {
    if (open) return;
    fileDragDepthRef.current = 0;
    setFileDragActive(false);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    draftSuppressTitleBlurRef.current = false;
    draftSubmitIntentRef.current = false;
    draftSlashUpdatesRef.current = {};
    draftSlashUpdatePromiseRef.current = null;
    setCreatedTaskId(null);
    setDraftTagIds([]);
    resetRecurrenceState();
  }, [open, resetRecurrenceState]);

  useEffect(() => {
    if (!readOnly) return;
    fileDragDepthRef.current = 0;
    setFileDragActive(false);
  }, [readOnly]);

  // 見積工数の保存
  const handleEstHoursBlur = useCallback(async () => {
    if (readOnly || !effectiveTaskId || !task) return;
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
  }, [effectiveTaskId, task, editEstHours, fetchTask, onTaskUpdated, readOnly]);

  useEffect(() => {
    if (!open || readOnly) return;
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
  }, [open, readOnly]);

  // コメント送信
  const handleSendComment = useCallback(async () => {
    if (readOnly || !effectiveTaskId || !commentText.trim()) return;
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
  }, [commentText, effectiveTaskId, readOnly]);

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

  const guardedImmediateUpdate = useCallback(
    (data: Record<string, unknown>) => {
      if (readOnly) return;
      return immediateUpdate(data);
    },
    [immediateUpdate, readOnly],
  );

  const handleReadOnlyAwareDialogOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (readOnly) {
        onOpenChange(nextOpen);
        return;
      }
      handleDialogOpenChange(nextOpen);
    },
    [handleDialogOpenChange, onOpenChange, readOnly],
  );

  const {
    triageStatus,
    triageSummary,
    triageHasSummary,
    triageQuestions,
    shouldShowTriageCard,
  } = deriveTaskTriageView(task);

  return (
    <>
      <Dialog open={open} onOpenChange={handleReadOnlyAwareDialogOpenChange}>
        <DialogContent
          className={cn(
            "sm:max-w-6xl max-h-[85vh] overflow-y-auto rounded border-border bg-background p-0",
            fileDragActive && "ring-2 ring-primary",
          )}
          showCloseButton={true}
          onDragEnterCapture={handleEditorImageDragCapture}
          onDragOverCapture={handleEditorImageDragCapture}
          onDragLeaveCapture={handleEditorImageDragCapture}
          onDropCapture={handleEditorImageDragCapture}
          onDragEnter={handleFileDragEnter}
          onDragOver={handleFileDragOver}
          onDragLeave={handleFileDragLeave}
          onDrop={handleFileDrop}
        >
          <DialogHeader className="sr-only">
            <DialogTitle>タスク詳細</DialogTitle>
            <DialogDescription>
              タスクの詳細情報を表示・編集します
            </DialogDescription>
          </DialogHeader>

          {loading ? (
            <div className="space-y-4 p-6">
              <Skeleton className="h-8 w-48" />
              <Skeleton className="h-64 w-full" />
            </div>
          ) : !task ? (
            <div className="flex items-center justify-center p-16 text-muted-foreground">
              タスクが見つかりません
            </div>
          ) : (
            <div className="flex min-h-0 flex-col space-y-6 p-4">
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
                openingChat={openingChat}
                triagingAgent={triagingAgent}
                onTitleChange={handleTitleChange}
                onTitleBlur={handleTitleBlur}
                onTitleSubmitIntent={handleTitleSubmitIntent}
                onParseSlashCommands={handleParseSlashCommands}
                focusDescriptionEditor={focusDescriptionEditor}
                immediateUpdate={guardedImmediateUpdate}
                handleOpenInChat={handleOpenInChat}
                handleRunAgentTriage={handleRunAgentTriage}
                handleDuplicate={handleDuplicate}
                handleDelete={handleDelete}
                handleDialogOpenChange={handleReadOnlyAwareDialogOpenChange}
                readOnly={readOnly}
              />

              {shouldShowTriageCard ? (
                <TaskDetailTriageCard
                  triageStatus={triageStatus}
                  triageSummary={triageSummary}
                  triageHasSummary={triageHasSummary}
                  triageQuestions={triageQuestions}
                />
              ) : null}

              <TaskDetailPropertyGrid
                task={task}
                effectiveTaskId={effectiveTaskId}
                assigneeSelectorKey={`${effectiveTaskId ?? "draft"}:${persistenceLifecycleRef.current}`}
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
                immediateUpdate={guardedImmediateUpdate}
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
                recSkipMode={recSkipMode}
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
                setRecSkipMode={setRecSkipMode}
                handleSaveRecurrence={handleSaveRecurrence}
                handleDeleteRecurrence={handleDeleteRecurrence}
                readOnly={readOnly}
              />

              {/* 説明 */}
              <div className="space-y-2">
                <Label>説明</Label>
                <TaskDescriptionEditor
                  ref={descriptionEditorRef}
                  value={editDescription}
                  onChange={(val) => {
                    if (readOnly) return;
                    setEditDescription(val);
                    debouncedUpdate({ description: val });
                  }}
                  placeholder="説明を追加..."
                  minHeight={80}
                  linkDisplayModes={descriptionLinkDisplayModes}
                  onLinkDisplayModeChange={
                    readOnly ? undefined : handleDescriptionLinkDisplayModeChange
                  }
                  onSubmitIntent={(value) => {
                    if (readOnly) return;
                    void handleSubmitAndCloseIntent(value);
                  }}
                  onArrowUpFromStart={focusTitleEditor}
                  onImageInsert={handleDescriptionImageInsert}
                  readOnly={readOnly}
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
                  onTaskUpdated(subtask);
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
                  onTaskUpdated(updatedSubtask);
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
                  onTaskUpdated(undefined, { removedTaskId: subtaskId });
                }}
                onUpdated={() => {
                  void fetchTask({ showLoading: false });
                }}
                readOnly={readOnly}
              />

              {effectiveTaskId ? (
                <TaskDependencySection
                  key={task.id}
                  task={task}
                  readOnly={readOnly}
                  onOpenTask={onOpenTask}
                />
              ) : null}

              <TaskAttachmentsSection
                effectiveTaskId={effectiveTaskId}
                projectId={currentProjectId}
                attachments={attachments}
                setAttachments={setAttachments}
                references={references}
                setReferences={setReferences}
                uploading={uploadingAttachments}
                onFilesSelected={async (files) => {
                  await uploadAttachmentFiles(files);
                }}
                onEnsureTask={!effectiveTaskId ? ensureTaskId : undefined}
                onAttachmentMutation={markAttachmentMutation}
                onReferenceMutation={markReferenceMutation}
                onOpenTask={onOpenTask}
                readOnly={readOnly}
              />

              {/* コメント */}
              <TaskDetailComments
                comments={comments}
                commentText={commentText}
                setCommentText={setCommentText}
                sendingComment={sendingComment}
                onSendComment={handleSendComment}
                readOnly={readOnly}
              />
            </div>
          )}
          {fileDragActive ? (
            <div
              role="status"
              className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-background/90"
            >
              <div className="flex flex-col items-center gap-2 text-primary">
                <Upload className="size-10" />
                <span className="text-sm font-medium">
                  ファイルをドロップしてリファレンスに追加
                </span>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
      <RecurringDeleteDialog
        open={!readOnly && showRecurringDeletePrompt}
        onOpenChange={setShowRecurringDeletePrompt}
        onDeleteSingle={handleDeleteSingleOccurrence}
        onDeleteFuture={handleDeleteFutureOccurrences}
        onDeleteSeries={handleDeleteRecurringSeries}
      />
    </>
  );
}
