import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Platform, ScrollView, StyleSheet, View } from "react-native";
import * as DocumentPicker from "expo-document-picker";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  Divider,
  IconButton,
  Menu,
  Portal,
  Snackbar,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { format } from "date-fns";
import {
  tasksRepo,
  timeEntriesRepo,
  conversationsRepo,
} from "../../../repositories";
import { useAuth } from "../../../contexts/AuthContext";
import { useNetworkStore } from "../../../stores/network";
import { useProject } from "../../../contexts/ProjectContext";
import { chatApi } from "../../../lib/chat-api";
import { taskApi } from "../../../lib/task-api";
import { getDefaultCharacterName } from "../../../lib/preferences";
import {
  buildTaskAgentPrompt,
  buildTaskAgentSessionTitle,
} from "../../../lib/task-agent";
import {
  isTaskDateOnlyInput,
  toTaskWallClockIso,
} from "../../../lib/task-datetime";
import type {
  Tag,
  Task,
  TaskAttachment,
  TaskComment,
  TimeEntry,
} from "../../../types/api";
import {
  createTaskCompletionUndoEntry,
  enqueueTaskCompletionUndoBatch,
  isTaskCompletionTransition,
  useTaskCompletionUndoStore,
} from "../../../stores/task-completion-undo";

const STATUS_OPTIONS = [
  { value: "open", label: "Open", color: "#89b4fa" },
  { value: "in_progress", label: "In Progress", color: "#f38ba8" },
  { value: "closed", label: "Closed", color: "#a6e3a1" },
  { value: "cancelled", label: "Cancelled", color: "#a6adc8" },
];

const STATUS_SHORTCUT_KEYS: Record<string, string> = {
  c: "closed",
  s: "in_progress",
  x: "open",
};

const PRIORITY_OPTIONS = [
  { value: "urgent", label: "Urgent", color: "#f38ba8" },
  { value: "high", label: "High", color: "#fab387" },
  { value: "normal", label: "Normal", color: "#89b4fa" },
  { value: "low", label: "Low", color: "#a6adc8" },
  { value: "none", label: "None", color: "#a6adc8" },
];

const REMINDER_PRESETS = [
  { value: 5, label: "5分前" },
  { value: 15, label: "15分前" },
  { value: 30, label: "30分前" },
  { value: 60, label: "1時間前" },
  { value: 1440, label: "1日前" },
];

const TASK_SCHEDULE_ESTIMATE_MODE_KEY = "schedule_estimate_mode";
const TASK_AUTOSAVE_DEBOUNCE_MS = 900;

const DISALLOWED_PLACEHOLDER_TITLES = new Set([
  "無題のタスク",
  "Untitled task",
]);

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatBytes(value: number | null | undefined): string {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function shouldPrepareTaskForAgent(
  metadata: Record<string, unknown> | null | undefined,
): boolean {
  const status =
    metadata && typeof metadata.agent_triage_status === "string"
      ? metadata.agent_triage_status
      : "pending";
  return status === "pending" || status === "failed";
}

function inferScheduleEstimateMode(
  estimatedHours: number | null | undefined,
  metadata: Record<string, unknown> | null | undefined,
): "auto" | "manual" {
  const rawMode =
    metadata &&
    typeof metadata === "object" &&
    !Array.isArray(metadata) &&
    metadata[TASK_SCHEDULE_ESTIMATE_MODE_KEY];
  return rawMode === "auto" || rawMode === "manual"
    ? rawMode
    : estimatedHours == null
      ? "auto"
      : "manual";
}

function computeEstimatedHoursFromSchedule(
  startAt: string | null | undefined,
  endAt: string | null | undefined,
): number | null {
  if (!startAt || !endAt) return null;
  const start = new Date(startAt);
  const end = new Date(endAt);
  if (
    Number.isNaN(start.getTime()) ||
    Number.isNaN(end.getTime()) ||
    end <= start
  ) {
    return null;
  }
  return Math.round(((end.getTime() - start.getTime()) / 3600000) * 100) / 100;
}

function formatTaskDateInput(
  value: string | null | undefined,
  allDay: boolean | null | undefined,
): string {
  if (!value) return "";
  return format(new Date(value), allDay ? "yyyy-MM-dd" : "yyyy-MM-dd'T'HH:mm");
}

function normalizeTaskMetadataWithEstimateMode(
  metadata: Record<string, unknown> | null | undefined,
  estimateMode: "auto" | "manual",
): Record<string, unknown> {
  const nextMetadata =
    metadata && typeof metadata === "object" && !Array.isArray(metadata)
      ? { ...metadata }
      : {};
  nextMetadata[TASK_SCHEDULE_ESTIMATE_MODE_KEY] = estimateMode;
  return nextMetadata;
}

function serializeTaskDraft(data: Record<string, unknown>): string {
  return JSON.stringify(data);
}

function buildTaskSavedDraft(task: Task): Record<string, unknown> {
  const estimateMode = inferScheduleEstimateMode(
    task.estimated_hours ?? null,
    task.metadata,
  );
  const startAt = formatTaskDateInput(task.start_at, task.all_day);
  const endAt = formatTaskDateInput(task.end_at, task.all_day);
  return {
    title: task.title.trim(),
    description: task.description?.trim() || null,
    status: task.status,
    priority: task.priority,
    start_at: toTaskWallClockIso(startAt),
    end_at: toTaskWallClockIso(endAt),
    all_day:
      Boolean(task.all_day) ||
      isTaskDateOnlyInput(startAt) ||
      isTaskDateOnlyInput(endAt),
    estimated_hours:
      typeof task.estimated_hours === "number" ? task.estimated_hours : null,
    metadata: normalizeTaskMetadataWithEstimateMode(task.metadata, estimateMode),
    notifications_enabled: task.notifications_enabled ?? true,
    reminder_offsets: task.reminder_offsets ?? [],
    tag_ids: (task.tags ?? []).map((tag) => tag.id),
  };
}

export default function TaskDetailScreen() {
  const { taskId } = useLocalSearchParams<{ taskId: string }>();
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const online = useNetworkStore((state) => state.online);
  const { projects, refreshProjects } = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [activeEntry, setActiveEntry] = useState<TimeEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [autosaving, setAutosaving] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [taskMenuVisible, setTaskMenuVisible] = useState(false);
  const [statusMenuVisible, setStatusMenuVisible] = useState(false);
  const [priorityMenuVisible, setPriorityMenuVisible] = useState(false);
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);
  const [timerElapsed, setTimerElapsed] = useState(0);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("open");
  const [priority, setPriority] = useState("normal");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [estimatedHours, setEstimatedHours] = useState("");
  const [estimateMode, setEstimateMode] = useState<"auto" | "manual">("auto");
  const [allDay, setAllDay] = useState(false);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminderOffsets, setReminderOffsets] = useState<number[]>([]);
  const [availableTags, setAvailableTags] = useState<Tag[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [newTagDraft, setNewTagDraft] = useState("");
  const [tagBusy, setTagBusy] = useState(false);
  const [subtaskDraft, setSubtaskDraft] = useState("");
  const [subtaskSaving, setSubtaskSaving] = useState(false);
  const [commentDraft, setCommentDraft] = useState("");
  const [commentSaving, setCommentSaving] = useState(false);
  const [attachments, setAttachments] = useState<TaskAttachment[]>([]);
  const [attachmentSaving, setAttachmentSaving] = useState(false);
  const [titleError, setTitleError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [descriptionSelection, setDescriptionSelection] = useState({
    start: 0,
    end: 0,
  });
  const [launchingAgent, setLaunchingAgent] = useState(false);
  const [triagingAgent, setTriagingAgent] = useState(false);
  const completionRefreshToken = useTaskCompletionUndoStore(
    (state) => state.refreshToken,
  );
  const autosaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveTaskDraftRef = useRef<(() => Promise<Task | null>) | null>(null);
  const shouldFlushAutosaveRef = useRef(false);
  const isMountedRef = useRef(true);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const loadTask = useCallback(async () => {
    if (!taskId) return;
    setLoading(true);
    try {
      const nextTask = await tasksRepo.get(taskId);
      const nextActiveEntry = await timeEntriesRepo.getActive(taskId);
      setTask(nextTask);
      setActiveEntry(nextActiveEntry);
      if (isAuthenticated && online) {
        try {
          setAttachments(await taskApi.listAttachments(taskId));
        } catch {
          setAttachments([]);
        }
      } else {
        setAttachments([]);
      }
      if (nextTask) {
        setTitle(nextTask.title);
        setDescription(nextTask.description || "");
        setDescriptionSelection({
          start: (nextTask.description || "").length,
          end: (nextTask.description || "").length,
        });
        setStatus(nextTask.status);
        setPriority(nextTask.priority);
        setStartAt(formatTaskDateInput(nextTask.start_at, nextTask.all_day));
        setEndAt(formatTaskDateInput(nextTask.end_at, nextTask.all_day));
        setEstimatedHours(nextTask.estimated_hours?.toString() || "");
        setAllDay(Boolean(nextTask.all_day));
        setReminderOffsets(nextTask.reminder_offsets ?? []);
        setSelectedTagIds((nextTask.tags ?? []).map((tag) => tag.id));
        setEstimateMode(
          inferScheduleEstimateMode(
            nextTask.estimated_hours ?? null,
            nextTask.metadata,
          ),
        );
        setNotificationsEnabled(nextTask.notifications_enabled ?? true);
        setTitleError(null);
        setSaveError(null);
        setSubtaskDraft("");
        setCommentDraft("");
      }
    } finally {
      setLoading(false);
    }
  }, [isAuthenticated, online, taskId]);

  useEffect(() => {
    void loadTask();
  }, [completionRefreshToken, loadTask]);

  useFocusEffect(
    useCallback(() => {
      void refreshProjects();
    }, [refreshProjects]),
  );

  useEffect(() => {
    if (!task?.project_id) {
      setAvailableTags([]);
      return;
    }
    let cancelled = false;
    taskApi
      .listTags(task.project_id)
      .then((tags) => {
        if (cancelled) return;
        setAvailableTags(tags);
        setSelectedTagIds((prev) =>
          prev.filter((id) => tags.some((tag) => tag.id === id)),
        );
      })
      .catch(() => {
        if (!cancelled) setAvailableTags(task.tags ?? []);
      });
    return () => {
      cancelled = true;
    };
  }, [task?.project_id, task?.tags]);

  useEffect(() => {
    if (!activeEntry?.started_at) {
      setTimerElapsed(0);
      return;
    }
    const startedAt = new Date(activeEntry.started_at).getTime();
    const tick = () =>
      setTimerElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [activeEntry?.started_at]);

  useEffect(() => {
    if (estimateMode !== "auto") return;
    const nextEstimated = computeEstimatedHoursFromSchedule(
      toTaskWallClockIso(startAt),
      toTaskWallClockIso(endAt),
    );
    setEstimatedHours(nextEstimated != null ? String(nextEstimated) : "");
  }, [endAt, estimateMode, startAt]);

  const draftPayload = useMemo<Record<string, unknown> | null>(() => {
    if (!task) return null;
    return {
      title: title.trim(),
      description: description.trim() || null,
      status,
      priority,
      start_at: toTaskWallClockIso(startAt),
      end_at: toTaskWallClockIso(endAt),
      all_day:
        allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
      estimated_hours: estimatedHours ? parseFloat(estimatedHours) : null,
      metadata: normalizeTaskMetadataWithEstimateMode(
        task.metadata,
        estimateMode,
      ),
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
      tag_ids: selectedTagIds,
    };
  }, [
    allDay,
    description,
    endAt,
    estimatedHours,
    estimateMode,
    notificationsEnabled,
    priority,
    reminderOffsets,
    selectedTagIds,
    startAt,
    status,
    task,
    title,
  ]);

  const draftSignature = useMemo(
    () => (draftPayload ? serializeTaskDraft(draftPayload) : ""),
    [draftPayload],
  );
  const savedSignature = useMemo(
    () => (task ? serializeTaskDraft(buildTaskSavedDraft(task)) : ""),
    [task],
  );
  const hasUnsavedDraft = Boolean(
    task && draftPayload && draftSignature !== savedSignature,
  );
  const hasValidAutosaveTitle =
    Boolean(title.trim()) && !DISALLOWED_PLACEHOLDER_TITLES.has(title.trim());

  const statusOption = useMemo(
    () => STATUS_OPTIONS.find((item) => item.value === status),
    [status],
  );
  const priorityOption = useMemo(
    () => PRIORITY_OPTIONS.find((item) => item.value === priority),
    [priority],
  );

  useEffect(() => {
    if (Platform.OS !== "web" || !statusMenuVisible) return;
    const handleStatusShortcut = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) {
        return;
      }
      const nextStatus = STATUS_SHORTCUT_KEYS[event.key.toLowerCase()];
      if (!nextStatus) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      setStatus(nextStatus);
      setStatusMenuVisible(false);
    };
    document.addEventListener("keydown", handleStatusShortcut, true);
    return () =>
      document.removeEventListener("keydown", handleStatusShortcut, true);
  }, [statusMenuVisible]);

  const applyDescriptionShortcut = useCallback(
    (type: "heading" | "bold" | "list" | "link" | "code") => {
      const start = descriptionSelection.start;
      const end = descriptionSelection.end;
      const selected = description.slice(start, end);
      let nextValue = description;
      let nextSelection = { start, end };

      if (type === "heading") {
        const prefix = selected.startsWith("# ") ? "" : "# ";
        nextValue = `${description.slice(0, start)}${prefix}${selected}${description.slice(end)}`;
        nextSelection = {
          start: start + prefix.length,
          end: end + prefix.length,
        };
      } else if (type === "bold") {
        nextValue = `${description.slice(0, start)}**${selected || "text"}**${description.slice(end)}`;
        const cursorBase = start + 2;
        nextSelection = selected
          ? { start: cursorBase, end: cursorBase + selected.length }
          : { start: cursorBase, end: cursorBase + 4 };
      } else if (type === "list") {
        const body = selected || "item";
        nextValue = `${description.slice(0, start)}- ${body}${description.slice(end)}`;
        nextSelection = { start: start + 2, end: start + 2 + body.length };
      } else if (type === "link") {
        const body = selected || "label";
        nextValue = `${description.slice(0, start)}[${body}](url)${description.slice(end)}`;
        nextSelection = { start: start + 1, end: start + 1 + body.length };
      } else {
        const body = selected || "code";
        nextValue = `${description.slice(0, start)}\`\`\`\n${body}\n\`\`\`${description.slice(end)}`;
        nextSelection = { start: start + 4, end: start + 4 + body.length };
      }

      setDescription(nextValue);
      setDescriptionSelection(nextSelection);
    },
    [description, descriptionSelection],
  );

  const saveTaskDraft = useCallback(async (options?: {
    navigateAfter?: boolean;
    showErrors?: boolean;
    manual?: boolean;
  }) => {
    const normalizedTitle = title.trim();
    const showErrors = options?.showErrors ?? false;
    if (!taskId || !task || !draftPayload) {
      return null;
    }
    if (!normalizedTitle) {
      if (showErrors) setTitleError("Title is required.");
      return null;
    }
    if (DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle)) {
      if (showErrors) setTitleError("Untitled tasks are not allowed.");
      return null;
    }
    if (!options?.navigateAfter && draftSignature === savedSignature) {
      return task;
    }

    if (isMountedRef.current) {
      if (options?.manual) setSaving(true);
      else setAutosaving(true);
      setSaveError(null);
    }
    try {
      const previousTask = task;
      const updated = await tasksRepo.update(taskId, draftPayload);
      if (isMountedRef.current) {
        setTask(updated);
        setTitleError(null);
      }
      shouldFlushAutosaveRef.current = false;
      if (isTaskCompletionTransition(previousTask.status, status)) {
        enqueueTaskCompletionUndoBatch({
          entries: [createTaskCompletionUndoEntry(previousTask)],
        });
      }
      if (options?.navigateAfter) {
        goBackOrReplace(router, "/(tabs)/tasks");
      }
      return updated;
    } catch (error) {
      if (isMountedRef.current) {
        if (showErrors || options?.manual) {
          setSaveError(
            error instanceof Error ? error.message : "保存に失敗しました",
          );
        } else {
          setSaveError("自動保存に失敗しました。接続状態を確認してください。");
        }
      }
      return null;
    } finally {
      if (isMountedRef.current) {
        if (options?.manual) setSaving(false);
        else setAutosaving(false);
      }
    }
  }, [
    draftPayload,
    draftSignature,
    router,
    savedSignature,
    status,
    task,
    taskId,
    title,
  ]);

  useEffect(() => {
    saveTaskDraftRef.current = () => saveTaskDraft({ showErrors: false });
    shouldFlushAutosaveRef.current =
      hasUnsavedDraft && hasValidAutosaveTitle && !loading;
  }, [hasUnsavedDraft, hasValidAutosaveTitle, loading, saveTaskDraft]);

  useEffect(() => {
    if (autosaveTimerRef.current) {
      clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (!hasUnsavedDraft || !hasValidAutosaveTitle || loading) return;

    autosaveTimerRef.current = setTimeout(() => {
      autosaveTimerRef.current = null;
      void saveTaskDraft({ showErrors: false });
    }, TASK_AUTOSAVE_DEBOUNCE_MS);

    return () => {
      if (autosaveTimerRef.current) {
        clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [hasUnsavedDraft, hasValidAutosaveTitle, loading, saveTaskDraft]);

  useFocusEffect(
    useCallback(() => {
      return () => {
        if (autosaveTimerRef.current) {
          clearTimeout(autosaveTimerRef.current);
          autosaveTimerRef.current = null;
        }
        if (shouldFlushAutosaveRef.current) {
          void saveTaskDraftRef.current?.();
        }
      };
    }, []),
  );

  const handleSave = () => {
    void saveTaskDraft({
      navigateAfter: true,
      showErrors: true,
      manual: true,
    });
  };

  const handleDelete = async () => {
    if (!taskId) return;
    setShowDeleteDialog(false);
    await tasksRepo.delete(taskId);
    goBackOrReplace(router, "/(tabs)/tasks");
  };

  const handleTimerToggle = async () => {
    if (!taskId) return;
    if (activeEntry) {
      await timeEntriesRepo.stopTimer(activeEntry.id);
      setActiveEntry(null);
    } else {
      const entry = await timeEntriesRepo.startTimer(taskId);
      setActiveEntry(entry);
    }
    await loadTask();
  };

  const toggleReminder = useCallback((offset: number) => {
    setReminderOffsets((prev) =>
      prev.includes(offset)
        ? prev.filter((item) => item !== offset)
        : [...prev, offset].sort((a, b) => a - b),
    );
  }, []);

  const toggleTag = useCallback((tagId: string) => {
    setSelectedTagIds((prev) =>
      prev.includes(tagId)
        ? prev.filter((item) => item !== tagId)
        : [...prev, tagId],
    );
  }, []);

  const handleCreateTag = useCallback(async () => {
    const name = newTagDraft.trim();
    if (!name || !task?.project_id || tagBusy) return;
    const existing = availableTags.find(
      (tag) => tag.name.toLowerCase() === name.toLowerCase(),
    );
    if (existing) {
      toggleTag(existing.id);
      setNewTagDraft("");
      return;
    }
    setTagBusy(true);
    try {
      const created = await taskApi.createTag(task.project_id, { name });
      setAvailableTags((prev) => [...prev, created]);
      setSelectedTagIds((prev) => [...prev, created.id]);
      setNewTagDraft("");
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "タグ作成に失敗しました",
      );
    } finally {
      setTagBusy(false);
    }
  }, [availableTags, newTagDraft, tagBusy, task?.project_id, toggleTag]);

  const handleMoveProject = useCallback(
    async (projectId: string) => {
      if (!taskId || !task || task.project_id === projectId) return;
      setProjectMenuVisible(false);
      try {
        const updated = await tasksRepo.update(taskId, {
          project_id: projectId,
        });
        setTask(updated);
        setSelectedTagIds([]);
        await loadTask();
      } catch (error) {
        setSaveError(
          error instanceof Error
            ? error.message
            : "プロジェクト変更に失敗しました",
        );
      }
    },
    [loadTask, task, taskId],
  );

  const handleAddSubtask = useCallback(async () => {
    const titleValue = subtaskDraft.trim();
    if (!task || !titleValue || subtaskSaving) return;
    setSubtaskSaving(true);
    try {
      await tasksRepo.create({
        project_id: task.project_id,
        parent_task_id: task.id,
        title: titleValue,
        status: "open",
        priority: "normal",
        notifications_enabled: task.notifications_enabled,
      });
      setSubtaskDraft("");
      await loadTask();
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "サブタスク作成に失敗しました",
      );
    } finally {
      setSubtaskSaving(false);
    }
  }, [loadTask, subtaskDraft, subtaskSaving, task]);

  const handleSendComment = useCallback(async () => {
    const content = commentDraft.trim();
    if (!taskId || !content || commentSaving) return;
    setCommentSaving(true);
    try {
      await taskApi.addComment(taskId, content);
      setCommentDraft("");
      await loadTask();
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "コメント送信に失敗しました",
      );
    } finally {
      setCommentSaving(false);
    }
  }, [commentDraft, commentSaving, loadTask, taskId]);

  const handleUploadAttachment = useCallback(async () => {
    if (!taskId || attachmentSaving) return;
    if (!isAuthenticated || !online) {
      setSaveError("添付はオンラインでログインしている時だけ利用できます。");
      return;
    }
    setAttachmentSaving(true);
    try {
      const result = await DocumentPicker.getDocumentAsync({
        multiple: true,
        copyToCacheDirectory: true,
      });
      if (result.canceled) return;
      const uploaded: TaskAttachment[] = [];
      for (const asset of result.assets) {
        uploaded.push(
          await taskApi.uploadAttachment(taskId, {
            uri: asset.uri,
            name: asset.name || "uploaded-file",
            mimeType: asset.mimeType,
          }),
        );
      }
      setAttachments((prev) => [...uploaded, ...prev]);
    } catch (error) {
      setSaveError(
        error instanceof Error
          ? error.message
          : "添付アップロードに失敗しました",
      );
    } finally {
      setAttachmentSaving(false);
    }
  }, [attachmentSaving, isAuthenticated, online, taskId]);

  const handleDeleteAttachment = useCallback(
    async (attachmentId: string) => {
      if (!taskId) return;
      try {
        await taskApi.deleteAttachment(taskId, attachmentId);
        setAttachments((prev) =>
          prev.filter((item) => item.id !== attachmentId),
        );
      } catch (error) {
        setSaveError(
          error instanceof Error ? error.message : "添付削除に失敗しました",
        );
      }
    },
    [taskId],
  );

  const handleRunWithAgent = useCallback(async () => {
    if (!task) return;

    const normalizedTitle = title.trim();
    if (
      !normalizedTitle ||
      DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle)
    ) {
      setTitleError("Untitled tasks are not allowed.");
      return;
    }

    if (!isAuthenticated || !online) {
      Alert.alert(
        "Agent",
        "Sign in and go online to run a task with the agent.",
      );
      return;
    }

    const taskSnapshot: Task = {
      ...task,
      title: normalizedTitle,
      description: description.trim() || null,
      status,
      priority,
      start_at: toTaskWallClockIso(startAt),
      end_at: toTaskWallClockIso(endAt),
      all_day:
        allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
      estimated_hours: estimatedHours ? parseFloat(estimatedHours) : null,
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
      tags: availableTags.filter((tag) => selectedTagIds.includes(tag.id)),
    };

    setLaunchingAgent(true);
    try {
      let launchTask = taskSnapshot;
      if (taskId && shouldPrepareTaskForAgent(task.metadata)) {
        setTriagingAgent(true);
        try {
          const result = await taskApi.runAgentTriage(taskId);
          const metadata = {
            ...(launchTask.metadata || {}),
            ...result.metadata,
          };
          launchTask = { ...launchTask, metadata };
          setTask((prev) =>
            prev
              ? {
                  ...prev,
                  metadata,
                }
              : prev,
          );
        } finally {
          setTriagingAgent(false);
        }
      }

      const characterName = await getDefaultCharacterName();
      const session = await conversationsRepo.createSession(
        characterName,
        launchTask.project_id || undefined,
      );
      await conversationsRepo.updateTitle(
        session.id,
        buildTaskAgentSessionTitle(normalizedTitle),
      );
      await chatApi.dispatchMessage(session.id, {
        message: buildTaskAgentPrompt(launchTask),
        project_id: launchTask.project_id || undefined,
        agent_mode: "confirm",
        include_project_context: true,
      });
      router.push(`/(tabs)/chat/${session.id}`);
    } catch (error) {
      Alert.alert(
        "Agent",
        error instanceof Error
          ? error.message
          : "Failed to start the task agent.",
      );
    } finally {
      setLaunchingAgent(false);
    }
  }, [
    description,
    endAt,
    estimatedHours,
    isAuthenticated,
    notificationsEnabled,
    online,
    priority,
    router,
    startAt,
    status,
    task,
    taskId,
    title,
  ]);

  const handleRunAgentTriage = useCallback(async () => {
    if (!taskId || triagingAgent) return;
    if (!isAuthenticated || !online) {
      Alert.alert("Agent triage", "Sign in and go online to triage this task.");
      return;
    }
    setTriagingAgent(true);
    try {
      const result = await taskApi.runAgentTriage(taskId);
      setTask((prev) =>
        prev
          ? {
              ...prev,
              metadata: {
                ...(prev.metadata || {}),
                ...result.metadata,
              },
            }
          : prev,
      );
      await loadTask();
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "Agent triage failed",
      );
    } finally {
      setTriagingAgent(false);
    }
  }, [isAuthenticated, loadTask, online, taskId, triagingAgent]);

  const currentProject = projects.find(
    (project) => project.id === task?.project_id,
  );
  const comments: TaskComment[] = task?.comments ?? [];
  const triageStatus =
    typeof task?.metadata?.agent_triage_status === "string"
      ? task.metadata.agent_triage_status
      : "pending";
  const triageSummary =
    typeof task?.metadata?.agent_triage_summary === "string"
      ? task.metadata.agent_triage_summary
      : "";
  const triageHasSummary = triageSummary.trim().length > 0;
  const triageQuestions = Array.isArray(task?.metadata?.agent_triage_questions)
    ? task.metadata.agent_triage_questions.filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const shouldShowTriageCard =
    triageHasSummary ||
    triageQuestions.length > 0 ||
    triageStatus === "needs_user" ||
    triageStatus === "failed";

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color="#7c3aed" />
      </View>
    );
  }

  if (!task) {
    return (
      <View style={styles.center}>
        <Text style={styles.errorText}>Task not found.</Text>
      </View>
    );
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.timerBar} elevation={0}>
        <View style={styles.timerRow}>
          <IconButton
            icon={activeEntry ? "stop-circle" : "play-circle"}
            iconColor={activeEntry ? "#f38ba8" : "#a6e3a1"}
            size={32}
            onPress={handleTimerToggle}
          />
          <View>
            <Text style={styles.timerLabel}>
              {activeEntry ? "Running" : "Tracked"}
            </Text>
            <Text style={styles.timerValue}>
              {activeEntry
                ? formatDuration(timerElapsed)
                : formatDuration(task.total_time_seconds || 0)}
            </Text>
          </View>
          {task.estimated_hours ? (
            <Text style={styles.estimateLabel}>
              Estimate {task.estimated_hours}h
            </Text>
          ) : null}
        </View>
      </Surface>

      <View style={styles.agentActionRow}>
        <Button
          mode="contained"
          onPress={() => void handleRunWithAgent()}
          loading={launchingAgent}
          disabled={
            launchingAgent ||
            !isAuthenticated ||
            !online ||
            !title.trim() ||
            DISALLOWED_PLACEHOLDER_TITLES.has(title.trim())
          }
          style={styles.agentButton}
        >
          Run with agent
        </Button>
        <Menu
          visible={taskMenuVisible}
          onDismiss={() => setTaskMenuVisible(false)}
          anchor={
            <IconButton
              icon="dots-horizontal"
              iconColor="#cdd6f4"
              size={22}
              style={styles.taskMenuButton}
              onPress={() => setTaskMenuVisible(true)}
            />
          }
          contentStyle={styles.menuContent}
        >
          <Menu.Item
            title={triagingAgent ? "Preparing..." : "Prepare for Agent"}
            leadingIcon="robot-outline"
            disabled={triagingAgent || !isAuthenticated || !online}
            onPress={() => {
              setTaskMenuVisible(false);
              void handleRunAgentTriage();
            }}
          />
        </Menu>
      </View>

      {shouldShowTriageCard ? (
        <Surface style={styles.triageCard}>
          <View style={styles.sectionHeaderRow}>
            <View>
              <Text style={styles.triageTitle}>Agent triage</Text>
              <Text style={styles.triageStatus}>{triageStatus}</Text>
            </View>
          </View>
          {triageHasSummary ? (
            <Text style={styles.triageSummary}>{triageSummary}</Text>
          ) : null}
          {triageQuestions.map((question) => (
            <Text key={question} style={styles.triageQuestion}>
              - {question}
            </Text>
          ))}
        </Surface>
      ) : null}

      <TextInput
        label="Title"
        value={title}
        onChangeText={(value) => {
          setTitle(value);
          if (titleError) setTitleError(null);
        }}
        mode="outlined"
        style={styles.input}
        error={!!titleError}
      />
      {titleError ? <Text style={styles.errorText}>{titleError}</Text> : null}

      <View style={styles.row}>
        <Text style={styles.label}>Project</Text>
        <Menu
          visible={projectMenuVisible}
          onDismiss={() => setProjectMenuVisible(false)}
          anchor={
            <Chip
              onPress={() => setProjectMenuVisible(true)}
              style={styles.selectChip}
              textStyle={{ color: currentProject?.color || "#cdd6f4" }}
            >
              {currentProject?.name || task.project_name || "Move"}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          {projects.map((project) => (
            <Menu.Item
              key={project.id}
              title={project.name}
              leadingIcon={project.id === task.project_id ? "check" : undefined}
              onPress={() => void handleMoveProject(project.id)}
            />
          ))}
        </Menu>
      </View>

      <TextInput
        label="Description"
        value={description}
        onChangeText={setDescription}
        mode="outlined"
        style={styles.input}
        multiline
        numberOfLines={4}
        selection={descriptionSelection}
        onSelectionChange={(event) =>
          setDescriptionSelection(event.nativeEvent.selection)
        }
      />
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={styles.shortcutRow}
      >
        <Chip
          compact
          style={styles.shortcutChip}
          onPress={() => applyDescriptionShortcut("heading")}
        >
          #
        </Chip>
        <Chip
          compact
          style={styles.shortcutChip}
          onPress={() => applyDescriptionShortcut("bold")}
        >
          Bold
        </Chip>
        <Chip
          compact
          style={styles.shortcutChip}
          onPress={() => applyDescriptionShortcut("list")}
        >
          List
        </Chip>
        <Chip
          compact
          style={styles.shortcutChip}
          onPress={() => applyDescriptionShortcut("link")}
        >
          Link
        </Chip>
        <Chip
          compact
          style={styles.shortcutChip}
          onPress={() => applyDescriptionShortcut("code")}
        >
          Code
        </Chip>
      </ScrollView>

      <View style={styles.row}>
        <Text style={styles.label}>Status</Text>
        <Menu
          visible={statusMenuVisible}
          onDismiss={() => setStatusMenuVisible(false)}
          anchor={
            <Chip
              onPress={() => setStatusMenuVisible(true)}
              style={[styles.selectChip, { borderColor: statusOption?.color }]}
              textStyle={{ color: statusOption?.color }}
            >
              {statusOption?.label || status}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          {STATUS_OPTIONS.map((item) => (
            <Menu.Item
              key={item.value}
              title={item.label}
              titleStyle={{ color: item.color }}
              onPress={() => {
                setStatus(item.value);
                setStatusMenuVisible(false);
              }}
            />
          ))}
        </Menu>
      </View>
      <View style={styles.statusQuickRow}>
        {STATUS_OPTIONS.map((item) => (
          <Chip
            key={item.value}
            compact
            selected={status === item.value}
            onPress={() => setStatus(item.value)}
            style={[
              styles.statusQuickChip,
              status === item.value && { borderColor: item.color },
            ]}
            textStyle={[
              styles.statusQuickChipText,
              status === item.value && { color: item.color, fontWeight: "700" },
            ]}
          >
            {item.label}
          </Chip>
        ))}
      </View>

      <View style={styles.row}>
        <Text style={styles.label}>Priority</Text>
        <Menu
          visible={priorityMenuVisible}
          onDismiss={() => setPriorityMenuVisible(false)}
          anchor={
            <Chip
              onPress={() => setPriorityMenuVisible(true)}
              style={[
                styles.selectChip,
                { borderColor: priorityOption?.color },
              ]}
              textStyle={{ color: priorityOption?.color }}
            >
              {priorityOption?.label || priority}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          {PRIORITY_OPTIONS.map((item) => (
            <Menu.Item
              key={item.value}
              title={item.label}
              titleStyle={{ color: item.color }}
              onPress={() => {
                setPriority(item.value);
                setPriorityMenuVisible(false);
              }}
            />
          ))}
        </Menu>
      </View>

      <Divider style={styles.divider} />

      <Text style={[styles.label, styles.sectionLabel]}>Schedule</Text>
      <TextInput
        label="Start"
        value={startAt}
        onChangeText={setStartAt}
        mode="outlined"
        style={styles.input}
        placeholder="yyyy-MM-ddTHH:mm"
        right={
          startAt ? (
            <TextInput.Icon icon="close" onPress={() => setStartAt("")} />
          ) : undefined
        }
      />
      <TextInput
        label="Due"
        value={endAt}
        onChangeText={setEndAt}
        mode="outlined"
        style={styles.input}
        placeholder="yyyy-MM-ddTHH:mm"
        right={
          endAt ? (
            <TextInput.Icon icon="close" onPress={() => setEndAt("")} />
          ) : undefined
        }
      />
      <TextInput
        label="Estimate Hours"
        value={estimatedHours}
        onChangeText={(value) => {
          setEstimateMode("manual");
          setEstimatedHours(value);
        }}
        mode="outlined"
        style={styles.input}
        keyboardType="decimal-pad"
        right={
          estimatedHours ? (
            <TextInput.Icon
              icon="close"
              onPress={() => {
                setEstimateMode("manual");
                setEstimatedHours("");
              }}
            />
          ) : undefined
        }
      />
      <View style={styles.row}>
        <Text style={styles.label}>All Day</Text>
        <Switch value={allDay} onValueChange={setAllDay} />
      </View>
      <Text style={[styles.label, styles.sectionLabel]}>Reminders</Text>
      <View style={styles.tagRow}>
        {REMINDER_PRESETS.map((preset) => (
          <Chip
            key={preset.value}
            compact
            selected={reminderOffsets.includes(preset.value)}
            disabled={!notificationsEnabled}
            onPress={() => toggleReminder(preset.value)}
            style={styles.tagChip}
            textStyle={styles.tagText}
          >
            {preset.label}
          </Chip>
        ))}
      </View>

      <View style={styles.row}>
        <Text style={styles.label}>Task Notifications</Text>
        <Switch
          value={notificationsEnabled}
          onValueChange={setNotificationsEnabled}
        />
      </View>
      <Divider style={styles.divider} />
      <Text style={[styles.label, styles.sectionLabel]}>Tags</Text>
      <View style={styles.tagRow}>
        {availableTags.length === 0 ? (
          <Text style={styles.calendarHint}>
            タグなし / オフラインでは取得できません
          </Text>
        ) : (
          availableTags.map((tag) => (
            <Chip
              key={tag.id}
              compact
              selected={selectedTagIds.includes(tag.id)}
              onPress={() => toggleTag(tag.id)}
              style={[
                styles.tagChip,
                selectedTagIds.includes(tag.id)
                  ? { backgroundColor: tag.color || "#45475a" }
                  : {
                      backgroundColor: "#181825",
                      borderColor: tag.color || "#45475a",
                    },
              ]}
              textStyle={styles.tagText}
            >
              {tag.name}
            </Chip>
          ))
        )}
      </View>
      <View style={styles.inlineFormRow}>
        <TextInput
          value={newTagDraft}
          onChangeText={setNewTagDraft}
          mode="outlined"
          dense
          placeholder="新規タグ"
          style={styles.inlineFormInput}
          autoCorrect={false}
        />
        <Button
          mode="outlined"
          onPress={() => void handleCreateTag()}
          disabled={!newTagDraft.trim() || tagBusy}
          loading={tagBusy}
        >
          追加
        </Button>
      </View>

      <Divider style={styles.divider} />

      {task.created_at ? (
        <View style={styles.metaRow}>
          <Text style={styles.metaLabel}>Created</Text>
          <Text style={styles.metaValue}>
            {format(new Date(task.created_at), "yyyy/MM/dd HH:mm")}
          </Text>
        </View>
      ) : null}
      {task.completed_at ? (
        <View style={styles.metaRow}>
          <Text style={styles.metaLabel}>Closed</Text>
          <Text style={styles.metaValue}>
            {format(new Date(task.completed_at), "yyyy/MM/dd HH:mm")}
          </Text>
        </View>
      ) : null}
      {task.total_time_seconds ? (
        <View style={styles.metaRow}>
          <Text style={styles.metaLabel}>Tracked Total</Text>
          <Text style={styles.metaValue}>
            {formatDuration(task.total_time_seconds)}
          </Text>
        </View>
      ) : null}
      {task.assignees?.length ? (
        <View style={styles.metaRow}>
          <Text style={styles.metaLabel}>Assignees</Text>
          <Text style={styles.metaValue}>
            {task.assignees
              .map((item) => item.display_name || item.username || "")
              .join(", ")}
          </Text>
        </View>
      ) : null}

      <Divider style={styles.divider} />
      <Text style={[styles.label, styles.sectionLabel]}>
        Subtasks (
        {
          (task.subtasks ?? []).filter((item) => item.status === "closed")
            .length
        }
        /{(task.subtasks ?? []).length})
      </Text>
      {(task.subtasks ?? []).map((subtask) => (
        <Surface key={subtask.id} style={styles.subtaskCard}>
          <View style={styles.subtaskRow}>
            <IconButton
              icon={
                subtask.status === "closed" ? "check-circle" : "circle-outline"
              }
              iconColor={subtask.status === "closed" ? "#a6e3a1" : "#a6adc8"}
              size={20}
              onPress={async () => {
                const nextStatus =
                  subtask.status === "closed" ? "open" : "closed";
                await tasksRepo.update(subtask.id, { status: nextStatus });
                if (isTaskCompletionTransition(subtask.status, nextStatus)) {
                  enqueueTaskCompletionUndoBatch({
                    entries: [createTaskCompletionUndoEntry(subtask)],
                  });
                }
                await loadTask();
              }}
              style={{ margin: 0 }}
            />
            <Text
              style={[
                styles.subtaskTitle,
                subtask.status === "closed" ? styles.subtaskDone : null,
              ]}
            >
              {subtask.title}
            </Text>
          </View>
        </Surface>
      ))}
      <View style={styles.inlineFormRow}>
        <TextInput
          value={subtaskDraft}
          onChangeText={setSubtaskDraft}
          mode="outlined"
          dense
          placeholder="サブタスクを追加"
          style={styles.inlineFormInput}
        />
        <Button
          mode="outlined"
          onPress={() => void handleAddSubtask()}
          disabled={!subtaskDraft.trim() || subtaskSaving}
          loading={subtaskSaving}
        >
          追加
        </Button>
      </View>

      <Divider style={styles.divider} />
      <View style={styles.sectionHeaderRow}>
        <Text style={[styles.label, styles.sectionLabel]}>Attachments</Text>
        <Button
          mode="outlined"
          compact
          onPress={() => void handleUploadAttachment()}
          disabled={attachmentSaving}
          loading={attachmentSaving}
        >
          添付
        </Button>
      </View>
      {attachments.length === 0 ? (
        <Text style={styles.calendarHint}>添付ファイルはまだありません</Text>
      ) : (
        attachments.map((attachment) => (
          <Surface key={attachment.id} style={styles.attachmentCard}>
            <View style={styles.attachmentInfo}>
              <Text style={styles.attachmentName} numberOfLines={1}>
                {attachment.display_name}
              </Text>
              <Text style={styles.attachmentMeta}>
                {attachment.kind === "image" ? "image" : "file"} /{" "}
                {formatBytes(attachment.size_bytes)}
              </Text>
            </View>
            <IconButton
              icon="delete-outline"
              iconColor="#f38ba8"
              size={20}
              onPress={() => void handleDeleteAttachment(attachment.id)}
            />
          </Surface>
        ))
      )}

      <Divider style={styles.divider} />
      <Text style={[styles.label, styles.sectionLabel]}>Comments</Text>
      {comments.length === 0 ? (
        <Text style={styles.calendarHint}>コメントはまだありません</Text>
      ) : (
        comments.map((comment) => (
          <Surface key={comment.id} style={styles.commentCard}>
            <Text style={styles.commentText}>{comment.content}</Text>
            <Text style={styles.commentMeta}>
              {comment.display_name || comment.username || "user"}
              {comment.created_at
                ? ` / ${format(new Date(comment.created_at), "yyyy/MM/dd HH:mm")}`
                : ""}
            </Text>
          </Surface>
        ))
      )}
      <TextInput
        value={commentDraft}
        onChangeText={setCommentDraft}
        mode="outlined"
        multiline
        numberOfLines={2}
        placeholder="コメントを入力..."
        style={styles.input}
      />
      <Button
        mode="outlined"
        onPress={() => void handleSendComment()}
        disabled={!commentDraft.trim() || commentSaving}
        loading={commentSaving}
        style={styles.commentButton}
      >
        コメント送信
      </Button>

      <Divider style={styles.divider} />

      <Button
        mode="contained"
        onPress={handleSave}
        loading={saving || autosaving}
        style={styles.saveButton}
      >
        Save
      </Button>
      <Button
        mode="outlined"
        onPress={() => setShowDeleteDialog(true)}
        textColor="#f38ba8"
        style={styles.deleteButton}
      >
        Delete
      </Button>

      <Portal>
        <Snackbar
          visible={!!saveError}
          onDismiss={() => setSaveError(null)}
          duration={4000}
          style={styles.snackbar}
        >
          {saveError}
        </Snackbar>
        <Dialog
          visible={showDeleteDialog}
          onDismiss={() => setShowDeleteDialog(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Delete Task</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>Delete "{task.title}"?</Text>
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              onPress={() => setShowDeleteDialog(false)}
              textColor="#a6adc8"
            >
              Cancel
            </Button>
            <Button onPress={handleDelete} textColor="#f38ba8">
              Delete
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { padding: 16, paddingBottom: 40 },
  center: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    backgroundColor: "#11111b",
  },
  errorText: { color: "#f38ba8" },
  timerBar: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 8,
    marginBottom: 16,
  },
  timerRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  timerLabel: { color: "#a6adc8", fontSize: 12 },
  timerValue: {
    color: "#cdd6f4",
    fontSize: 18,
    fontWeight: "bold",
    fontVariant: ["tabular-nums"],
  },
  estimateLabel: { color: "#a6adc8", fontSize: 12, marginLeft: "auto" },
  input: { marginBottom: 12 },
  shortcutRow: { marginTop: -4, marginBottom: 12 },
  shortcutChip: { marginRight: 6, backgroundColor: "#313244" },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 16,
  },
  label: { color: "#a6adc8", fontSize: 14 },
  sectionLabel: { marginBottom: 8 },
  selectChip: { backgroundColor: "transparent", borderWidth: 1 },
  statusQuickRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    marginTop: -4,
    marginBottom: 12,
  },
  statusQuickChip: {
    backgroundColor: "#181825",
    borderWidth: 1,
    borderColor: "#313244",
  },
  statusQuickChipText: { color: "#a6adc8", fontSize: 12 },
  calendarHint: { color: "#9399b2", fontSize: 12, marginBottom: 8 },
  menuContent: { backgroundColor: "#1e1e2e" },
  divider: { backgroundColor: "#313244", marginVertical: 16 },
  tagRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginBottom: 4 },
  tagChip: { height: 28 },
  tagText: { color: "#cdd6f4", fontSize: 12 },
  sectionHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  inlineFormRow: {
    flexDirection: "row",
    gap: 8,
    alignItems: "center",
    marginTop: 8,
    marginBottom: 8,
  },
  inlineFormInput: { flex: 1, backgroundColor: "transparent" },
  attachmentCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 10,
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 8,
    flexDirection: "row",
    alignItems: "center",
  },
  attachmentInfo: { flex: 1, minWidth: 0 },
  attachmentName: { color: "#cdd6f4", fontSize: 14, fontWeight: "600" },
  attachmentMeta: { color: "#9399b2", fontSize: 11, marginTop: 4 },
  commentCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 10,
    padding: 10,
    marginBottom: 8,
  },
  commentText: { color: "#cdd6f4", fontSize: 14 },
  commentMeta: { color: "#9399b2", fontSize: 11, marginTop: 6 },
  commentButton: { borderColor: "#89b4fa", marginBottom: 8 },
  metaRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  metaLabel: { color: "#a6adc8", fontSize: 13 },
  metaValue: {
    color: "#cdd6f4",
    fontSize: 13,
    flexShrink: 1,
    textAlign: "right",
  },
  subtaskCard: { backgroundColor: "#1e1e2e", borderRadius: 8, marginBottom: 4 },
  subtaskRow: { flexDirection: "row", alignItems: "center" },
  subtaskTitle: { color: "#cdd6f4", fontSize: 14, flex: 1 },
  subtaskDone: { textDecorationLine: "line-through", color: "#a6adc8" },
  agentActionRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 16,
  },
  agentButton: { backgroundColor: "#45475a", flex: 1 },
  taskMenuButton: {
    borderWidth: 1,
    borderColor: "#313244",
    borderRadius: 8,
    margin: 0,
  },
  triageCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 10,
    padding: 12,
    marginBottom: 16,
  },
  triageTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  triageStatus: { color: "#89b4fa", fontSize: 12, marginTop: 2 },
  triageSummary: { color: "#a6adc8", fontSize: 13, lineHeight: 18 },
  triageQuestion: { color: "#fab387", fontSize: 12, marginTop: 6 },
  saveButton: { backgroundColor: "#7c3aed", marginBottom: 12 },
  deleteButton: { borderColor: "#f38ba8" },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogText: { color: "#a6adc8" },
  snackbar: { backgroundColor: "#313244" },
});
