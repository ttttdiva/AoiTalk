"use client";

import React, {
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
} from "react";
import { useRouter } from "next/navigation";
import { Input } from "@/components/ui/input";
import { SlashCommandInput } from "@/components/tasks/slash-command-input";
import {
  buildAutoEstimateTaskPatch,
  buildDraftTask,
  buildManualEstimateTaskPatch,
  buildTaskCommandCandidates,
  buildTaskSlashCommandFormPatch,
  hasNonMidnightTime,
  normalizeTaskTitle,
  resolveTaskTagIds,
  taskValueCompletion,
  taskValuePreview,
} from "@/components/tasks/task-form-utils";
import { Textarea } from "@/components/ui/textarea";
import {
  TaskDescriptionEditor,
  type LinkDisplayMode,
  type TaskDescriptionEditorHandle,
} from "@/components/editor/task-description-editor";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Play,
  Square,
  Send,
  Trash2,
  Plus,
  MoreHorizontal,
  CircleDot,
  Users2,
  Calendar,
  Flag,
  Tag as TagIcon,
  Timer,
  Bell,
  Hourglass,
  CheckCircle,
  Repeat,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  taskApi,
  type RecurringOccurrenceContext,
  type Task,
  type Tag,
  type RecurrenceRule,
  type TimeEntry,
  type TaskAttachment,
} from "@/lib/task-api";
import {
  getTaskDisplayEndAt,
  getTaskDisplayStartAt,
} from "@/lib/task-effective-date";
import {
  createTaskCompletionUndoEntry,
  dispatchTaskCompletionUndoBatch,
  isTaskCompletionTransition,
} from "@/lib/task-completion-undo";
import { chatApi } from "@/lib/chat-api";
import { useTaskCompletionRefresh } from "@/hooks/use-task-completion-refresh";
import {
  buildTaskAgentPrompt,
  buildTaskAgentSessionTitle,
} from "@/lib/task-agent";
import { useProject } from "@/contexts/project-context";
import {
  toLocalDateTimeInputValue,
  toTaskDatePayloadValue,
} from "@/lib/date-time";
import { cn } from "@/lib/utils";
import { formatTimerClock, getElapsedTimerSeconds } from "@/lib/task-time";
import { toast } from "sonner";
import {
  TaskDatePicker,
  buildRrule,
  parseRrule,
  recurrenceEndDateInputValue,
} from "@/components/tasks/task-date-picker";

import { PropertyRow } from "@/components/tasks/task-detail/property-row";
import { RecurringDeleteDialog } from "@/components/tasks/task-detail/recurring-delete-dialog";
import { SubtaskSection } from "@/components/tasks/task-detail/subtask-section";
import { TagSelector } from "@/components/tasks/task-detail/tag-selector";
import { TaskAttachmentsSection } from "@/components/tasks/task-detail/task-attachments-section";
import {
  STATUS_DOT_COLORS,
  buildTaskDescriptionLinkDisplayModeMetadata,
  fetchCurrentOccurrenceContext,
  formatDateTime,
  formatDuration,
  formatTimeRange,
  getTaskDescriptionLinkDisplayModes,
  isEditableTarget,
  isRecord,
  shouldPrepareTaskForAgent,
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
  const router = useRouter();
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
  const [timerLoading, setTimerLoading] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

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
  const [showRecurringDeletePrompt, setShowRecurringDeletePrompt] =
    useState(false);

  useEffect(() => {
    taskMetadataRef.current = isRecord(task?.metadata) ? task.metadata : {};
  }, [task?.metadata]);
  const [subtaskInputOpenSignal, setSubtaskInputOpenSignal] = useState(0);
  const [launchingAgent, setLaunchingAgent] = useState(false);
  const [triagingAgent, setTriagingAgent] = useState(false);
  const [, setStatusSelectOpen] = useState(false);
  const draftSuppressTitleBlurRef = useRef(false);
  const draftSubmitIntentRef = useRef(false);
  const draftLifecycleRef = useRef(0);
  const draftSlashUpdatesRef = useRef<Record<string, unknown>>({});
  const draftSlashUpdatePromiseRef = useRef<Promise<
    Record<string, unknown>
  > | null>(null);

  // 繰り返し設定
  const [recurrenceRule, setRecurrenceRule] = useState<RecurrenceRule | null>(
    null,
  );
  const [recFreq, setRecFreq] = useState("WEEKLY");
  const [recInterval, setRecInterval] = useState(1);
  const [recByDay, setRecByDay] = useState<string[]>([]);
  const [recTriggerStatus, setRecTriggerStatus] = useState("closed");
  const [recCreateNew, setRecCreateNew] = useState(false);
  const [recRecurForever, setRecRecurForever] = useState(true);
  const [recResetStatusTo, setRecResetStatusTo] = useState("open");
  const [recEndCount, setRecEndCount] = useState<number | null>(null);
  const [recEndDate, setRecEndDate] = useState<string | null>(null);
  const [recSkipWeekend, setRecSkipWeekend] = useState(false);
  const [recSkipHoliday, setRecSkipHoliday] = useState(false);
  const [recurrenceSaving, setRecurrenceSaving] = useState(false);

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

  const resolveTagUpdates = useCallback(
    async (tagNames: string[], targetProjectId?: string | null) => {
      const currentProjectId =
        task?.project_id || draftTask?.project_id || null;
      const projectId = targetProjectId || currentProjectId;
      let availableTags = tags;
      if (projectId && projectId !== currentProjectId) {
        try {
          availableTags = await taskApi.listTags(projectId);
        } catch (err) {
          console.error("移動先プロジェクトのタグ取得に失敗しました", err);
          availableTags = [];
        }
      }
      const { tagIds, createdTags } = await resolveTaskTagIds({
        tagNames,
        currentTagIds:
          projectId && projectId === currentProjectId
            ? slashSelectedTagIds
            : [],
        availableTags,
        createTag: async (name) => {
          if (!projectId) return null;
          return taskApi.createTag(projectId, { name });
        },
      });
      if (createdTags.length > 0 && projectId === currentProjectId) {
        setTags((prev) => {
          const existingIds = new Set(prev.map((tag) => tag.id));
          const nextCreated = createdTags.filter(
            (tag) => !existingIds.has(tag.id),
          );
          return nextCreated.length > 0 ? [...prev, ...nextCreated] : prev;
        });
      }
      return { tag_ids: tagIds };
    },
    [draftTask?.project_id, slashSelectedTagIds, tags, task?.project_id],
  );

  const currentProjectId = task?.project_id || draftTask?.project_id || null;
  const currentSpaceId = useMemo(
    () =>
      allProjects.find((project) => project.id === currentProjectId)
        ?.space_id ?? null,
    [allProjects, currentProjectId],
  );

  const syncManagedTag = useCallback(
    (tagId: string, updater: (tag: Tag) => Tag) => {
      setTags((prev) =>
        prev.map((tag) => (tag.id === tagId ? updater(tag) : tag)),
      );
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              tags: prev.tags.map((tag) =>
                tag.id === tagId ? updater(tag) : tag,
              ),
            } as Task)
          : prev,
      );
    },
    [],
  );

  const announceTagChange = useCallback(() => {
    onTaskUpdated();
  }, [onTaskUpdated]);

  const handleRenameTag = useCallback(
    async (tagId: string, name: string) => {
      const updated = await taskApi.updateTag(tagId, { name });
      syncManagedTag(tagId, (tag) => ({ ...tag, name: updated.name }));
      announceTagChange();
    },
    [announceTagChange, syncManagedTag],
  );

  const handleChangeTagColor = useCallback(
    async (tagId: string, color: string) => {
      const updated = await taskApi.updateTag(tagId, { color });
      syncManagedTag(tagId, (tag) => ({ ...tag, color: updated.color }));
      announceTagChange();
    },
    [announceTagChange, syncManagedTag],
  );

  const handleDeleteTag = useCallback(
    async (tagId: string) => {
      await taskApi.deleteTag(tagId);
      setTags((prev) => prev.filter((tag) => tag.id !== tagId));
      setDraftTagIds((prev) => prev.filter((id) => id !== tagId));
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              tags: prev.tags.filter((tag) => tag.id !== tagId),
            } as Task)
          : prev,
      );
      announceTagChange();
      toast.success("Tag deleted");
    },
    [announceTagChange],
  );

  const handleCopyTagToSpace = useCallback(
    async (tagId: string, spaceId: string) => {
      const copied = await taskApi.copyTagToSpace(tagId, spaceId);
      const targetSpace = spaces.find((space) => space.id === copied.space_id);
      toast.success(
        targetSpace
          ? `Copied to ${targetSpace.name}`
          : "Copied to another space",
      );
    },
    [spaces],
  );

  // debounce用
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftCreatePromiseRef = useRef<Promise<Task | null> | null>(null);
  const draftCreatedTaskIdRef = useRef<string | null>(null);
  const openRef = useRef(open);

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
  }, []);

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
    ],
  );

  // タスク取得
  const fetchTask = useCallback(async () => {
    if (!effectiveTaskId) return;
    setLoading(true);
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
      setDraftTagIds((t.tags || []).map((tag) => tag.id));
      if (t.project_id) {
        const tagList = await taskApi.listTags(t.project_id);
        setTags(tagList);
      }
    } catch (err) {
      console.error("タスク取得失敗:", err);
    } finally {
      setLoading(false);
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

  // 繰り返し設定取得
  const fetchRecurrence = useCallback(async () => {
    if (!effectiveTaskId) return;
    try {
      const rule = await taskApi.getRecurrence(effectiveTaskId);
      setRecurrenceRule(rule);
      if (rule) {
        const parsed = parseRrule(rule.rrule);
        setRecFreq(parsed.freq);
        setRecInterval(parsed.interval);
        setRecByDay(parsed.byDay);
        setRecEndCount(
          rule.end_count !== undefined
            ? (rule.end_count ?? null)
            : parsed.count,
        );
        setRecEndDate(
          rule.end_date !== undefined
            ? recurrenceEndDateInputValue(rule.end_date)
            : parsed.until,
        );
        setRecTriggerStatus(rule.trigger_status || "closed");
        setRecCreateNew(rule.create_new ?? false);
        setRecRecurForever(rule.recur_forever ?? true);
        setRecResetStatusTo(rule.reset_status_to || "open");
        setRecSkipWeekend(rule.skip_weekend ?? false);
        setRecSkipHoliday(rule.skip_holiday ?? false);
      }
    } catch (err) {
      console.error("繰り返し設定取得失敗:", err);
    }
  }, [effectiveTaskId]);

  useEffect(() => {
    if (open && effectiveTaskId) {
      draftCreatedTaskIdRef.current = null;
      // モーダルが開かれるたびにリセットしてから取得
      setTask(null);
      setComments([]);
      setAttachments([]);
      setCommentText("");
      setEditingTitle(false);
      setRecurrenceRule(null);

      setRecFreq("WEEKLY");
      setRecInterval(1);
      setRecByDay([]);
      setRecTriggerStatus("closed");
      setRecCreateNew(false);
      setRecRecurForever(true);
      setRecResetStatusTo("open");
      setRecEndCount(null);
      setRecEndDate(null);
      setRecSkipWeekend(false);
      setRecSkipHoliday(false);
      fetchTask();
      fetchRecurrence();
    }
  }, [open, effectiveTaskId, fetchTask, fetchRecurrence]);

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

  // タイマー表示更新
  useEffect(() => {
    if (!task?.active_time_entry?.started_at) {
      setElapsedSeconds(0);
      return;
    }
    const updateElapsed = () => {
      setElapsedSeconds(
        getElapsedTimerSeconds(task.active_time_entry?.started_at),
      );
    };
    updateElapsed();
    const interval = setInterval(updateElapsed, 1000);
    return () => clearInterval(interval);
  }, [task?.active_time_entry?.started_at]);

  // debounce更新
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
    ],
  );

  // 即時更新（select変更用）
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
    [activeOccurrenceContext, effectiveTaskId, onTaskUpdated, task],
  );

  // タイマー操作
  const handleTimer = useCallback(async () => {
    if (!effectiveTaskId) return;
    setTimerLoading(true);
    try {
      if (task?.active_time_entry) {
        await taskApi.stopTimer(task.active_time_entry.id);
        setTask((prev) => (prev ? { ...prev, active_time_entry: null } : prev));
      } else {
        const started = await taskApi.startTimer(effectiveTaskId);
        setElapsedSeconds(0);
        setTask((prev) =>
          prev ? { ...prev, active_time_entry: started } : prev,
        );
      }
      window.dispatchEvent(new Event("timer-changed"));
      await fetchTask();
      onTaskUpdated();
    } catch (err) {
      console.error("タイマー操作失敗:", err);
    } finally {
      setTimerLoading(false);
    }
  }, [effectiveTaskId, fetchTask, onTaskUpdated, task]);

  // ヘッダー等でタイマーが変わったらタスク情報を再取得
  useEffect(() => {
    if (!open || !effectiveTaskId) return;
    const onTimerChanged = () => {
      fetchTask();
    };
    window.addEventListener("timer-changed", onTimerChanged);
    return () => window.removeEventListener("timer-changed", onTimerChanged);
  }, [effectiveTaskId, fetchTask, open]);

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

  // Alt+S でタイマー開始/停止
  useEffect(() => {
    if (!open) return;
    const handleKeydown = (e: KeyboardEvent) => {
      if (e.altKey && (e.key === "s" || e.key === "S")) {
        e.preventDefault();
        handleTimer();
      }
    };
    window.addEventListener("keydown", handleKeydown);
    return () => window.removeEventListener("keydown", handleKeydown);
  }, [open, handleTimer]);

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

  // タスク削除
  const handleDelete = useCallback(async () => {
    if (!effectiveTaskId) return;
    if (task?.has_recurrence && activeOccurrenceContext?.start_at) {
      setShowRecurringDeletePrompt(true);
      return;
    }
    try {
      await taskApi.deleteTask(effectiveTaskId);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("削除失敗:", err);
    }
  }, [
    activeOccurrenceContext,
    effectiveTaskId,
    onOpenChange,
    onTaskUpdated,
    task,
  ]);

  const handleDuplicate = useCallback(async () => {
    if (!task) return;
    try {
      await taskApi.createTask({
        project_id: task.project_id,
        title: `コピー: ${normalizeTaskTitle(editTitle || task.title) || task.title}`,
        description: editDescription.trim() || task.description || "",
        status: task.status,
        priority: task.priority,
        start_at: task.start_at ?? null,
        end_at: task.end_at ?? null,
        all_day: task.all_day,
        notifications_enabled: task.notifications_enabled,
        reminder_offsets: task.reminder_offsets || [],
        parent_task_id: task.parent_task_id ?? null,
        tag_ids: (task.tags || []).map((tag) => tag.id),
      });
      onTaskUpdated();
    } catch (err) {
      console.error("隍・｣ｽ螟ｱ謨・", err);
    }
  }, [editDescription, editTitle, onTaskUpdated, task]);

  const handleDeleteSingleOccurrence = useCallback(async () => {
    if (!effectiveTaskId || !activeOccurrenceContext?.start_at) return;
    try {
      await taskApi.deleteOccurrence(effectiveTaskId, {
        mode: "single",
        occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
        occurrence_start_at: activeOccurrenceContext.start_at,
        occurrence_end_at: activeOccurrenceContext.end_at ?? null,
        original_start_at: activeOccurrenceContext.original_start_at ?? null,
      });
      setShowRecurringDeletePrompt(false);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("今回分の削除失敗:", err);
    }
  }, [activeOccurrenceContext, effectiveTaskId, onOpenChange, onTaskUpdated]);

  const handleDeleteFutureOccurrences = useCallback(async () => {
    if (!effectiveTaskId || !activeOccurrenceContext?.start_at) return;
    try {
      await taskApi.deleteOccurrence(effectiveTaskId, {
        mode: "future",
        occurrence_id: activeOccurrenceContext.occurrence_id ?? null,
        occurrence_start_at: activeOccurrenceContext.start_at,
        occurrence_end_at: activeOccurrenceContext.end_at ?? null,
        original_start_at: activeOccurrenceContext.original_start_at ?? null,
      });
      setShowRecurringDeletePrompt(false);
      onTaskUpdated();
      onOpenChange(false);
    } catch (err) {
      console.error("今後分の削除失敗:", err);
    }
  }, [activeOccurrenceContext, effectiveTaskId, onOpenChange, onTaskUpdated]);

  const handleRunWithAgent = useCallback(async () => {
    if (!task) return;

    const normalizedTitle = normalizeTaskTitle(editTitle || task.title);
    if (!normalizedTitle) {
      toast.error("Task title is required.");
      return;
    }

    const taskSnapshot: Task = {
      ...task,
      title: normalizedTitle,
      description: editDescription.trim() || null,
    };

    setLaunchingAgent(true);
    try {
      let launchTask = taskSnapshot;
      if (effectiveTaskId && shouldPrepareTaskForAgent(task.metadata)) {
        setTriagingAgent(true);
        try {
          const result = await taskApi.runAgentTriage(effectiveTaskId);
          const metadata = {
            ...(launchTask.metadata || {}),
            ...result.metadata,
          };
          launchTask = { ...launchTask, metadata };
          setTask((prev) =>
            prev
              ? ({
                  ...prev,
                  metadata,
                } as Task)
              : prev,
          );
        } finally {
          setTriagingAgent(false);
        }
      }

      const created = await chatApi.createSession(
        "aoi",
        launchTask.project_id || undefined,
      );
      const sessionId = created.session.id;

      await chatApi.updateSessionTitle(
        sessionId,
        buildTaskAgentSessionTitle(normalizedTitle),
      );
      await chatApi.dispatchMessage(sessionId, {
        message: buildTaskAgentPrompt(launchTask),
        project_id: launchTask.project_id || undefined,
        generation_profile: "assisted_work",
      });

      onOpenChange(false);
      router.push(`/chat?s=${sessionId}`);
    } catch (err) {
      console.error("Failed to start task agent", err);
      toast.error("Failed to start the task agent.");
    } finally {
      setLaunchingAgent(false);
    }
  }, [editDescription, editTitle, effectiveTaskId, onOpenChange, router, task]);

  const handleRunAgentTriage = useCallback(async () => {
    if (!effectiveTaskId) return;
    setTriagingAgent(true);
    try {
      const result = await taskApi.runAgentTriage(effectiveTaskId);
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              metadata: {
                ...(prev.metadata || {}),
                ...result.metadata,
              },
            } as Task)
          : prev,
      );
      await fetchTask();
      toast.success("Agent triage updated");
    } catch (err) {
      console.error("Agent triage failed", err);
      toast.error(err instanceof Error ? err.message : "Agent triage failed");
    } finally {
      setTriagingAgent(false);
    }
  }, [effectiveTaskId, fetchTask]);

  // 曜日トグル
  const toggleWeekday = useCallback((dayKey: string) => {
    setRecByDay((prev) =>
      prev.includes(dayKey)
        ? prev.filter((d) => d !== dayKey)
        : [...prev, dayKey],
    );
  }, []);

  // 頻度変更（DAILYへ切替時はスキップ・新規作成の既定値を適用）
  const handleFreqChange = useCallback((newFreq: string) => {
    setRecFreq((prev) => {
      if (newFreq === "DAILY" && prev !== "DAILY") {
        setRecSkipWeekend(true);
        setRecSkipHoliday(true);
        setRecCreateNew(true);
      }
      return newFreq;
    });
  }, []);

  // 繰り返し設定の保存
  const handleSaveRecurrence = useCallback(async () => {
    setRecurrenceSaving(true);
    try {
      const targetTaskId =
        effectiveTaskId ??
        (await createFromDraft())?.id ??
        draftCreatedTaskIdRef.current;
      if (!targetTaskId) return;

      const rrule = buildRrule(
        recFreq,
        recInterval,
        recByDay,
        recRecurForever ? null : recEndCount,
        recRecurForever ? null : recEndDate,
      );
      const rule = await taskApi.saveRecurrence(targetTaskId, {
        rrule,
        trigger_status: recTriggerStatus,
        create_new: recCreateNew,
        recur_forever: recRecurForever,
        reset_status_to: recResetStatusTo,
        end_count: recRecurForever ? null : recEndCount,
        end_date: recRecurForever ? null : recEndDate,
        skip_weekend: recFreq === "DAILY" ? recSkipWeekend : false,
        skip_holiday: recSkipHoliday,
      });
      setRecurrenceRule(rule);
      setTask((prev) => (prev ? { ...prev, has_recurrence: true } : prev));
      onTaskUpdated();
    } catch (err) {
      console.error("繰り返し設定の保存に失敗:", err);
    } finally {
      setRecurrenceSaving(false);
    }
  }, [
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
    effectiveTaskId,
    createFromDraft,
    onTaskUpdated,
  ]);

  // 繰り返し設定の削除
  const handleDeleteRecurrence = useCallback(async () => {
    if (!effectiveTaskId) return;
    setRecurrenceSaving(true);
    try {
      await taskApi.deleteRecurrence(effectiveTaskId);
      setRecurrenceRule(null);
      setTask((prev) => (prev ? { ...prev, has_recurrence: false } : prev));

      // リセット
      setRecFreq("WEEKLY");
      setRecInterval(1);
      setRecByDay([]);
      setRecTriggerStatus("closed");
      setRecCreateNew(false);
      setRecRecurForever(true);
      setRecResetStatusTo("open");
      setRecEndCount(null);
      setRecEndDate(null);
      setRecSkipWeekend(false);
      setRecSkipHoliday(false);
      onTaskUpdated();
    } catch (err) {
      console.error("繰り返し設定の削除に失敗:", err);
    } finally {
      setRecurrenceSaving(false);
    }
  }, [effectiveTaskId, onTaskUpdated]);

  const displayTaskTags = effectiveTaskId
    ? (task?.tags ?? [])
    : tags.filter((tag) => draftTagIds.includes(tag.id));
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
    [effectiveTaskId, hasUnsavedDraft, onOpenChange],
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
  }, [effectiveTaskId, open]);

  const buildSlashFormPatch = useCallback(
    (text: string, preserveTrailingSpace = false) =>
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
    ],
  );

  const triageMetadata =
    task?.metadata && typeof task.metadata === "object" ? task.metadata : {};
  const triageStatus =
    typeof triageMetadata.agent_triage_status === "string"
      ? triageMetadata.agent_triage_status
      : "pending";
  const triageSummary =
    typeof triageMetadata.agent_triage_summary === "string"
      ? triageMetadata.agent_triage_summary
      : "";
  const triageHasSummary = triageSummary.trim().length > 0;
  const triageQuestions = Array.isArray(triageMetadata.agent_triage_questions)
    ? triageMetadata.agent_triage_questions.filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const shouldShowTriageCard =
    triageHasSummary ||
    triageQuestions.length > 0 ||
    triageStatus === "needs_user" ||
    triageStatus === "failed";

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
                <div className="pt-2">
                  {editingTitle ? (
                    <SlashCommandInput
                      inputRef={titleInputRef}
                      value={editTitle}
                      getValuePreview={taskValuePreview}
                      getValueCompletion={taskValueCompletion}
                      commandCandidates={slashCandidates}
                      onChange={(val) => {
                        setEditTitle(val);
                        if (effectiveTaskId) debouncedUpdate({ title: val });
                        else applyLocalDraftUpdate({ title: val });
                      }}
                      onBlur={() => {
                        void (async () => {
                          if (
                            !effectiveTaskId &&
                            draftSuppressTitleBlurRef.current
                          ) {
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
                            Object.assign(
                              updates,
                              await buildSlashUpdates(patch, editTitle),
                            );
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
                      }}
                      onSubmitIntent={() => {
                        void handleSubmitAndCloseIntent();
                      }}
                      submitOnEnter={!effectiveTaskId}
                      onParseSlashCommands={(text) => {
                        if (!text.includes("/")) return text;
                        const patch = buildSlashFormPatch(text, true);
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
                          let trackedSlashUpdates: Promise<
                            Record<string, unknown>
                          > | null = null;
                          trackedSlashUpdates = pendingSlashUpdates.finally(
                            () => {
                              if (
                                draftSlashUpdatePromiseRef.current ===
                                trackedSlashUpdates
                              ) {
                                draftSlashUpdatePromiseRef.current = null;
                              }
                            },
                          );
                          draftSlashUpdatePromiseRef.current =
                            trackedSlashUpdates;
                        }
                        return patch.title;
                      }}
                      className="text-2xl md:text-2xl font-bold h-auto border-none shadow-none px-0 py-0 focus-visible:ring-0"
                      autoFocus
                      onNavigateDown={focusDescriptionEditor}
                    />
                  ) : (
                    <h1
                      className="text-2xl md:text-2xl font-bold cursor-pointer hover:text-primary/80 transition-colors"
                      onClick={() => setEditingTitle(true)}
                    >
                      {editTitle || task.title}
                    </h1>
                  )}
                  {entryFocus && (
                    <div className="mt-3 rounded-xl border border-border/60 bg-muted/30 p-4">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                            Time Entry
                          </p>
                          <p className="mt-1 truncate text-sm text-muted-foreground">
                            {entryFocus.project_name || "プロジェクト未設定"}
                          </p>
                          <p className="truncate text-base font-semibold">
                            {entryFocus.task_title || task.title}
                          </p>
                        </div>
                        <div className="shrink-0 text-right">
                          <p className="text-lg font-semibold tabular-nums">
                            {formatDuration(entryFocus.duration_seconds || 0)}
                          </p>
                          <p className="text-xs text-muted-foreground tabular-nums">
                            {formatTimeRange(
                              entryFocus.started_at,
                              entryFocus.ended_at,
                            )}
                          </p>
                        </div>
                      </div>
                    </div>
                  )}
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    {/* プロジェクト表示 */}
                    {task.project_id && allProjects.length > 1 && (
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="text-xs text-muted-foreground">
                          📁
                        </span>
                        <Select
                          value={task.project_id}
                          onValueChange={(v) =>
                            v && immediateUpdate({ project_id: v })
                          }
                        >
                          <SelectTrigger className="h-6 w-auto max-w-full border-none px-1 text-xs text-muted-foreground shadow-none hover:text-foreground">
                            <span className="truncate">
                              {allProjects.find((p) => p.id === task.project_id)
                                ?.name || "不明"}
                            </span>
                          </SelectTrigger>
                          <SelectContent>
                            {spaces.map((s) => {
                              const group = allProjects.filter(
                                (p) => p.space_id === s.id,
                              );
                              if (group.length === 0) return null;
                              return (
                                <SelectGroup key={s.id}>
                                  <SelectLabel>{s.name}</SelectLabel>
                                  {group.map((p) => (
                                    <SelectItem key={p.id} value={p.id}>
                                      {p.name}
                                    </SelectItem>
                                  ))}
                                </SelectGroup>
                              );
                            })}
                            {allProjects.some((p) => !p.space_id) && (
                              <SelectGroup>
                                <SelectLabel>(スペースなし)</SelectLabel>
                                {allProjects
                                  .filter((p) => !p.space_id)
                                  .map((p) => (
                                    <SelectItem key={p.id} value={p.id}>
                                      {p.name}
                                    </SelectItem>
                                  ))}
                              </SelectGroup>
                            )}
                          </SelectContent>
                        </Select>
                      </div>
                    )}

                    <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8 gap-2"
                        onClick={() => void handleRunWithAgent()}
                        disabled={
                          launchingAgent ||
                          !normalizeTaskTitle(editTitle || task.title)
                        }
                      >
                        <Send className="size-3.5" />
                        {launchingAgent ? "Starting..." : "Run with agent"}
                      </Button>
                      <DropdownMenu>
                        <DropdownMenuTrigger className="inline-flex size-8 items-center justify-center rounded-md border bg-background text-muted-foreground shadow-xs transition-colors hover:bg-accent hover:text-accent-foreground">
                          <MoreHorizontal className="size-4" />
                        </DropdownMenuTrigger>
                        <DropdownMenuContent
                          align="end"
                          className="w-56 min-w-56"
                        >
                          <DropdownMenuLabel>Task</DropdownMenuLabel>
                          {effectiveTaskId ? (
                            <>
                              <DropdownMenuItem
                                disabled={triagingAgent}
                                onClick={() => void handleRunAgentTriage()}
                              >
                                <CircleDot className="size-4" />
                                {triagingAgent
                                  ? "Preparing..."
                                  : "Prepare for Agent"}
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                                作成: {formatDateTime(task.created_at)}
                              </div>
                              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                                更新: {formatDateTime(task.updated_at)}
                              </div>
                              {task.completed_at && (
                                <div className="px-2 py-1.5 text-xs text-muted-foreground">
                                  完了: {formatDateTime(task.completed_at)}
                                </div>
                              )}
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                onClick={() => void handleDuplicate()}
                              >
                                <Plus className="size-4" />
                                複製
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                variant="destructive"
                                onClick={() => void handleDelete()}
                              >
                                <Trash2 className="size-4" />
                                削除
                              </DropdownMenuItem>
                            </>
                          ) : (
                            <DropdownMenuItem
                              variant="destructive"
                              onClick={() => handleDialogOpenChange(false)}
                            >
                              <Trash2 className="size-4" />
                              下書きを破棄
                            </DropdownMenuItem>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </div>
                </div>

                {shouldShowTriageCard ? (
                  <div className="rounded-lg border bg-muted/20 p-3">
                    <div className="mb-2 flex items-center gap-2">
                      <Badge
                        variant={
                          triageStatus === "needs_user"
                            ? "destructive"
                            : "secondary"
                        }
                      >
                        {triageStatus}
                      </Badge>
                      <span className="text-sm font-medium">Agent triage</span>
                    </div>
                    {triageHasSummary ? (
                      <p className="whitespace-pre-wrap text-sm text-muted-foreground">
                        {triageSummary}
                      </p>
                    ) : null}
                    {triageQuestions.length > 0 ? (
                      <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                        {triageQuestions.map((question) => (
                          <li key={question}>{question}</li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}

                {/* ClickUp風 プロパティグリッド */}

                <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,26.25rem)] gap-x-6 border-y divide-y [&>*:nth-child(odd)]:border-r [&>*:nth-child(odd)]:pr-6 [&>*:nth-child(even)]:pl-6">
                  {/* Status */}
                  <PropertyRow
                    icon={<CircleDot className="size-3.5" />}
                    label="ステータス"
                  >
                    <div className="flex items-center gap-1">
                      <Select
                        value={task.status}
                        onOpenChange={setStatusSelectOpen}
                        onValueChange={(v) =>
                          v && immediateUpdate({ status: v })
                        }
                      >
                        <SelectTrigger className="h-7 w-auto border-none shadow-none px-1.5 text-xs font-medium gap-1">
                          <span className="flex items-center gap-1.5">
                            <span
                              className={cn(
                                "size-2 rounded-full border-2",
                                STATUS_DOT_COLORS[task.status] ||
                                  STATUS_DOT_COLORS.open,
                              )}
                            />
                            {{
                              todo: "未着手",
                              open: "未着手",
                              in_progress: "進行中",
                              on_hold: "保留",
                              review: "確認待ち",
                              closed: "完了",
                            }[task.status] || task.status}
                          </span>
                        </SelectTrigger>
                        <SelectContent>
                          {(
                            [
                              "open",
                              "in_progress",
                              "on_hold",
                              "review",
                              "closed",
                            ] as const
                          ).map((s) => (
                            <SelectItem key={s} value={s}>
                              <span className="flex items-center gap-1.5">
                                <span
                                  className={cn(
                                    "size-2 rounded-full border-2",
                                    STATUS_DOT_COLORS[s],
                                  )}
                                />
                                {
                                  {
                                    open: "未着手",
                                    in_progress: "進行中",
                                    on_hold: "保留",
                                    review: "確認待ち",
                                    closed: "完了",
                                  }[s]
                                }
                              </span>
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <button
                        type="button"
                        title={
                          task.status === "closed"
                            ? "未着手に戻す"
                            : "完了にする"
                        }
                        className={cn(
                          "flex items-center justify-center size-6 rounded transition-colors",
                          task.status === "closed"
                            ? "bg-green-500/20 text-green-500 hover:bg-green-500/30"
                            : "bg-muted/50 text-muted-foreground hover:bg-green-500/20 hover:text-green-500",
                        )}
                        onClick={() =>
                          immediateUpdate({
                            status:
                              task.status === "closed" ? "open" : "closed",
                          })
                        }
                      >
                        <CheckCircle
                          className={cn(
                            "size-3.5",
                            task.status === "closed" &&
                              "fill-green-500 text-green-50",
                          )}
                        />
                      </button>
                    </div>
                  </PropertyRow>

                  {/* Assignees */}
                  <PropertyRow
                    icon={<Users2 className="size-3.5" />}
                    label="担当者"
                  >
                    {task.assignees && task.assignees.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {task.assignees.map((a) => (
                          <span key={a.id} className="text-xs">
                            {a.display_name || a.username || a.user_id}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">
                        Empty
                      </span>
                    )}
                  </PropertyRow>

                  {/* Dates */}
                  <PropertyRow
                    icon={<Calendar className="size-3.5" />}
                    label="日時"
                  >
                    <div className="flex items-center gap-1">
                      {task.has_recurrence && (
                        <Repeat
                          className="size-3 shrink-0 text-muted-foreground"
                          aria-label="繰り返しタスク"
                        />
                      )}
                      <TaskDatePicker
                        startAt={toLocalDateTimeInputValue(displayStartAt, {
                          allDay: task.all_day,
                        })}
                        endAt={toLocalDateTimeInputValue(displayEndAt, {
                          allDay: task.all_day,
                        })}
                        allDay={task.all_day}
                        deferCommitUntilClose={
                          !!activeOccurrenceContext?.start_at
                        }
                        onRangeChange={
                          activeOccurrenceContext?.start_at
                            ? moveOccurrenceDateRange
                            : ({ startAt, endAt }) =>
                                immediateUpdate(
                                  buildDateTaskUpdate({
                                    start_at: startAt,
                                    end_at: endAt,
                                  }),
                                )
                        }
                        onStartAtChange={(v) =>
                          immediateUpdate(
                            buildDateTaskUpdate({
                              start_at: v,
                            }),
                          )
                        }
                        onEndAtChange={(v) =>
                          immediateUpdate(
                            buildDateTaskUpdate({
                              end_at: v,
                            }),
                          )
                        }
                        recurrence={{
                          recurrenceRule,
                          freq: recFreq,
                          interval: recInterval,
                          byDay: recByDay,
                          triggerStatus: recTriggerStatus,
                          createNew: recCreateNew,
                          recurForever: recRecurForever,
                          resetStatusTo: recResetStatusTo,
                          endCount: recEndCount,
                          endDate: recEndDate,
                          skipWeekend: recSkipWeekend,
                          skipHoliday: recSkipHoliday,
                          saving: recurrenceSaving,
                          onFreqChange: handleFreqChange,
                          onIntervalChange: setRecInterval,
                          onToggleWeekday: toggleWeekday,
                          onTriggerStatusChange: setRecTriggerStatus,
                          onCreateNewChange: setRecCreateNew,
                          onRecurForeverChange: setRecRecurForever,
                          onResetStatusToChange: setRecResetStatusTo,
                          onEndCountChange: setRecEndCount,
                          onEndDateChange: setRecEndDate,
                          onSkipWeekendChange: setRecSkipWeekend,
                          onSkipHolidayChange: setRecSkipHoliday,
                          onSave: handleSaveRecurrence,
                          onDelete: handleDeleteRecurrence,
                        }}
                      />
                    </div>
                  </PropertyRow>

                  {/* Priority */}
                  <PropertyRow
                    icon={<Flag className="size-3.5" />}
                    label="優先度"
                  >
                    <Select
                      value={task.priority}
                      onValueChange={(v) =>
                        v && immediateUpdate({ priority: v })
                      }
                    >
                      <SelectTrigger className="h-7 w-auto border-none shadow-none px-1.5 text-xs">
                        <span>
                          {{
                            urgent: "Urgent",
                            high: "High",
                            medium: "Medium",
                            low: "Low",
                            none: "None",
                          }[task.priority] || task.priority}
                        </span>
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="urgent">Urgent</SelectItem>
                        <SelectItem value="high">High</SelectItem>
                        <SelectItem value="medium">Medium</SelectItem>
                        <SelectItem value="low">Low</SelectItem>
                        <SelectItem value="none">None</SelectItem>
                      </SelectContent>
                    </Select>
                  </PropertyRow>

                  {/* Time estimate */}
                  <PropertyRow
                    icon={<Hourglass className="size-3.5" />}
                    label="見積工数"
                  >
                    <div className="flex items-center gap-1">
                      <Input
                        type="number"
                        value={editEstHours}
                        onChange={(e) => setEditEstHours(e.target.value)}
                        onBlur={handleEstHoursBlur}
                        placeholder="-"
                        className="h-6 w-16 text-xs border-none shadow-none px-1"
                        min="0"
                        step="0.5"
                        disabled={estHoursSaving}
                      />
                      {editEstHours && (
                        <span className="text-[10px] text-muted-foreground">
                          h
                        </span>
                      )}
                    </div>
                  </PropertyRow>

                  {/* Track time */}
                  <PropertyRow
                    icon={<Timer className="size-3.5" />}
                    label="時間計測"
                  >
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant={
                          task.active_time_entry ? "destructive" : "outline"
                        }
                        className="h-6 text-xs px-2 gap-1"
                        onClick={handleTimer}
                        disabled={timerLoading}
                      >
                        {task.active_time_entry ? (
                          <>
                            <Square className="size-3" />
                            Stop
                          </>
                        ) : (
                          <>
                            <Play className="size-3" />
                            Start
                          </>
                        )}
                      </Button>
                      {(() => {
                        const completedSec = (task.activities || []).reduce(
                          (
                            sum: number,
                            a: { duration_seconds?: number | null },
                          ) => sum + (a.duration_seconds || 0),
                          0,
                        );
                        if (completedSec <= 0 && !task.active_time_entry)
                          return null;
                        const isActive = !!task.active_time_entry;
                        return (
                          <span
                            className={`text-xs font-mono tabular-nums ${
                              isActive
                                ? "text-green-600 dark:text-green-400"
                                : "text-muted-foreground"
                            }`}
                          >
                            {isActive
                              ? formatTimerClock(elapsedSeconds)
                              : formatDuration(completedSec)}
                          </span>
                        );
                      })()}
                    </div>
                  </PropertyRow>

                  {/* Tags — ClickUp風: エリアクリックでタグ選択ドロップダウン */}
                  <PropertyRow
                    icon={<TagIcon className="size-3.5" />}
                    label="タグ"
                  >
                    <TagSelector
                      taskTags={displayTaskTags}
                      allTags={tags}
                      spaces={spaces}
                      currentSpaceId={currentSpaceId}
                      onToggle={(tagId) => {
                        const current = effectiveTaskId
                          ? (task.tags || []).map((t) => t.id)
                          : draftTagIds;
                        const newTagIds = current.includes(tagId)
                          ? current.filter((id) => id !== tagId)
                          : [...current, tagId];
                        if (effectiveTaskId) {
                          void immediateUpdate({ tag_ids: newTagIds });
                          return;
                        }
                        applyLocalDraftUpdate({ tag_ids: newTagIds });
                      }}
                      onClear={() => {
                        if (effectiveTaskId) {
                          void immediateUpdate({ tag_ids: [] });
                          return;
                        }
                        applyLocalDraftUpdate({ tag_ids: [] });
                      }}
                      onCreate={async (name) => {
                        const updates = await resolveTagUpdates([name]);
                        if (effectiveTaskId) {
                          await immediateUpdate(updates);
                          return;
                        }
                        applyLocalDraftUpdate(updates);
                      }}
                      onRenameTag={handleRenameTag}
                      onChangeTagColor={handleChangeTagColor}
                      onDeleteTag={handleDeleteTag}
                      onCopyTagToSpace={handleCopyTagToSpace}
                    />
                  </PropertyRow>

                  {/* Reminders */}
                  <PropertyRow
                    icon={<Bell className="size-3.5" />}
                    label="リマインダー"
                  >
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center gap-2">
                        <Button
                          type="button"
                          variant={
                            task.notifications_enabled ? "outline" : "default"
                          }
                          size="sm"
                          className="h-6 px-2 text-[10px]"
                          onClick={() =>
                            immediateUpdate({
                              notifications_enabled:
                                !task.notifications_enabled,
                            })
                          }
                        >
                          {task.notifications_enabled
                            ? "このタスクは通知しない"
                            : "通知を再開"}
                        </Button>
                        <span className="text-[10px] text-muted-foreground">
                          {task.notifications_enabled
                            ? "通知中"
                            : "このタスクの通知は無効"}
                        </span>
                      </div>
                      <div
                        className={cn(
                          "flex flex-wrap gap-1",
                          !task.notifications_enabled && "opacity-50",
                        )}
                      >
                        {[
                          { label: "5分前", value: 5 },
                          { label: "15分前", value: 15 },
                          { label: "30分前", value: 30 },
                          { label: "1時間前", value: 60 },
                          { label: "1日前", value: 1440 },
                        ].map((preset) => {
                          const offsets = task.reminder_offsets || [];
                          const isActive = offsets.includes(preset.value);
                          return (
                            <Badge
                              key={preset.value}
                              variant={isActive ? "default" : "outline"}
                              className={cn(
                                "text-[10px] px-1.5 h-5",
                                task.notifications_enabled &&
                                  "cursor-pointer hover:opacity-80",
                              )}
                              onClick={() => {
                                if (!task.notifications_enabled) return;
                                const newOffsets = isActive
                                  ? offsets.filter((o) => o !== preset.value)
                                  : [...offsets, preset.value].sort(
                                      (a, b) => a - b,
                                    );
                                immediateUpdate({
                                  reminder_offsets: newOffsets,
                                });
                              }}
                            >
                              {preset.label}
                            </Badge>
                          );
                        })}
                      </div>
                    </div>
                  </PropertyRow>
                </div>

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
                    onTaskUpdated();
                  }}
                  onUpdated={() => {
                    fetchTask();
                    onTaskUpdated();
                  }}
                />

                <TaskAttachmentsSection
                  effectiveTaskId={effectiveTaskId}
                  attachments={attachments}
                  setAttachments={setAttachments}
                />

                {/* コメント */}
                <div className="space-y-4">
                  <h2 className="text-sm font-medium">コメント</h2>
                  {comments.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      コメントはまだありません
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {comments.map((c) => (
                        <div
                          key={c.id}
                          className="rounded-lg border p-3 text-sm space-y-1"
                        >
                          <p>{c.content}</p>
                          <p className="text-xs text-muted-foreground">
                            {formatDateTime(c.created_at)}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="flex gap-2">
                    <Textarea
                      value={commentText}
                      onChange={(e) => setCommentText(e.target.value)}
                      placeholder="コメントを入力..."
                      rows={2}
                      className="resize-none flex-1"
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                          handleSendComment();
                        }
                      }}
                    />
                    <Button
                      size="icon"
                      onClick={handleSendComment}
                      disabled={sendingComment || !commentText.trim()}
                      className="shrink-0 self-end"
                    >
                      <Send className="size-4" />
                    </Button>
                  </div>
                </div>
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