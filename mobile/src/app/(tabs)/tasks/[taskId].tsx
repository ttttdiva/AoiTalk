import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Alert, Platform, ScrollView, StyleSheet, View } from "react-native";
import * as DocumentPicker from "expo-document-picker";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  Divider,
  Icon,
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
import {
  readAttachmentsSnapshot,
  writeAttachmentsSnapshot,
} from "../../../repositories/tasks";
import { useAuth } from "../../../contexts/AuthContext";
import { useNetworkStore } from "../../../stores/network";
import { useProject } from "../../../contexts/ProjectContext";
import { chatApi } from "../../../lib/chat-api";
import { taskApi } from "../../../lib/task-api";
import { projectApi } from "../../../lib/project-api";
import { createCurrentCharacterSession } from "../../../features/characters/current-character";
import {
  buildTaskAgentDispatchPayload,
  buildTaskAgentSessionTitle,
} from "../../../lib/task-agent";
import {
  isTaskDateOnlyInput,
  toTaskWallClockIso,
} from "../../../lib/task-datetime";
import { TaskDateField } from "../../../components/task-date-field";
import {
  getTaskStatusOption,
  TASK_STATUS_OPTIONS,
  TASK_STATUS_SHORTCUT_KEYS,
} from "../../../features/tasks/task-list-state";
import type {
  Tag,
  Task,
  TaskAttachment,
  TaskComment,
  TaskAssigneeCandidate,
  TaskRecurrence,
  TaskReference,
  TimeEntry,
} from "../../../types/api";
import {
  createTaskCompletionUndoEntry,
  enqueueTaskCompletionUndoBatch,
  isTaskCompletionTransition,
  useTaskCompletionUndoStore,
} from "../../../stores/task-completion-undo";
import { createAsyncSerialQueue } from "../../../lib/async-serial-queue";
import { appsRepo } from "../../../repositories/apps";
import type { AppSummary, TaskAppLink } from "../../../lib/apps-api";

const REMINDER_PRESETS = [
  { value: 5, label: "5分前" },
  { value: 15, label: "15分前" },
  { value: 30, label: "30分前" },
  { value: 60, label: "1時間前" },
  { value: 1440, label: "1日前" },
];

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

function formatTaskDateInput(
  value: string | null | undefined,
  allDay: boolean | null | undefined,
): string {
  if (!value) return "";
  return format(new Date(value), allDay ? "yyyy-MM-dd" : "yyyy-MM-dd'T'HH:mm");
}

function serializeTaskDraft(data: Record<string, unknown>): string {
  return JSON.stringify(data);
}

function buildTaskSavedDraft(task: Task): Record<string, unknown> {
  const startAt = formatTaskDateInput(task.start_at, task.all_day);
  const endAt = formatTaskDateInput(task.end_at, task.all_day);
  return {
    title: task.title.trim(),
    description: task.description?.trim() || null,
    status: task.status,
    start_at: toTaskWallClockIso(startAt),
    end_at: toTaskWallClockIso(endAt),
    all_day:
      Boolean(task.all_day) ||
      isTaskDateOnlyInput(startAt) ||
      isTaskDateOnlyInput(endAt),
    notifications_enabled: task.notifications_enabled ?? true,
    reminder_offsets: task.reminder_offsets ?? [],
    tag_ids: (task.tags ?? []).map((tag) => tag.id),
    assignee_ids: (task.assignees ?? []).map((assignee) => assignee.user_id),
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
  const [autosaving, setAutosaving] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [taskMenuVisible, setTaskMenuVisible] = useState(false);
  const [statusMenuVisible, setStatusMenuVisible] = useState(false);
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);
  const [tagMenuVisible, setTagMenuVisible] = useState(false);
  const [assigneeMenuVisible, setAssigneeMenuVisible] = useState(false);
  const [timerElapsed, setTimerElapsed] = useState(0);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("open");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [allDay, setAllDay] = useState(false);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminderOffsets, setReminderOffsets] = useState<number[]>([]);
  const [availableTags, setAvailableTags] = useState<Tag[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [availableMembers, setAvailableMembers] = useState<
    TaskAssigneeCandidate[]
  >([]);
  const [selectedAssigneeIds, setSelectedAssigneeIds] = useState<string[]>([]);
  const [newTagDraft, setNewTagDraft] = useState("");
  const [tagBusy, setTagBusy] = useState(false);
  const [subtaskDraft, setSubtaskDraft] = useState("");
  const [subtaskSaving, setSubtaskSaving] = useState(false);
  const [commentDraft, setCommentDraft] = useState("");
  const [commentSaving, setCommentSaving] = useState(false);
  const [attachments, setAttachments] = useState<TaskAttachment[]>([]);
  const [attachmentSaving, setAttachmentSaving] = useState(false);
  const [attachmentsStale, setAttachmentsStale] = useState(false);
  const [attachmentsCachedAt, setAttachmentsCachedAt] = useState<string | null>(
    null,
  );
  const [appLinks, setAppLinks] = useState<TaskAppLink[]>([]);
  const [availableApps, setAvailableApps] = useState<AppSummary[]>([]);
  const [appLinkDialogVisible, setAppLinkDialogVisible] = useState(false);
  const [selectedAppLinkId, setSelectedAppLinkId] = useState<string | null>(
    null,
  );
  const [appLinkBusy, setAppLinkBusy] = useState(false);
  const [titleError, setTitleError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [activeInfoTab, setActiveInfoTab] = useState<
    "comments" | "attachments"
  >("comments");
  const [showHistoryDialog, setShowHistoryDialog] = useState(false);
  const [launchingAgent, setLaunchingAgent] = useState(false);
  const [triagingAgent, setTriagingAgent] = useState(false);
  const [recurrenceRule, setRecurrenceRule] = useState<TaskRecurrence | null>(
    null,
  );
  const [references, setReferences] = useState<TaskReference[]>([]);
  const [recurrenceDialogVisible, setRecurrenceDialogVisible] = useState(false);
  const [referenceDialogVisible, setReferenceDialogVisible] = useState(false);
  const [recurrenceFrequency, setRecurrenceFrequency] = useState<
    "DAILY" | "WEEKLY" | "MONTHLY"
  >("DAILY");
  const [recurrenceInterval, setRecurrenceInterval] = useState("1");
  const [referenceUrlDraft, setReferenceUrlDraft] = useState("");
  const [referenceNameDraft, setReferenceNameDraft] = useState("");
  const [advancedSaving, setAdvancedSaving] = useState(false);
  const completionRefreshToken = useTaskCompletionUndoStore(
    (state) => state.refreshToken,
  );
  const autosaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveTaskDraftRef = useRef<(() => Promise<Task | null>) | null>(null);
  const saveTaskQueueRef = useRef(createAsyncSerialQueue());
  const saveTaskGenerationRef = useRef(0);
  const shouldFlushAutosaveRef = useRef(false);
  const isMountedRef = useRef(true);
  const userEditedDuringRefreshRef = useRef(false);
  const loadTaskRequestRef = useRef(0);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
      loadTaskRequestRef.current += 1;
      saveTaskGenerationRef.current += 1;
    };
  }, []);

  const applyLoadedTask = useCallback((nextTask: Task | null) => {
    setTask(nextTask);
    if (!nextTask) return;
    setTitle(nextTask.title);
    setDescription(nextTask.description || "");
    setStatus(nextTask.status);
    setStartAt(formatTaskDateInput(nextTask.start_at, nextTask.all_day));
    setEndAt(formatTaskDateInput(nextTask.end_at, nextTask.all_day));
    setAllDay(Boolean(nextTask.all_day));
    setReminderOffsets(nextTask.reminder_offsets ?? []);
    setSelectedTagIds((nextTask.tags ?? []).map((tag) => tag.id));
    setSelectedAssigneeIds(
      (nextTask.assignees ?? []).map((assignee) => assignee.user_id),
    );
    setNotificationsEnabled(nextTask.notifications_enabled ?? true);
    setTitleError(null);
    setSaveError(null);
    setSubtaskDraft("");
    setCommentDraft("");
  }, []);

  const loadTask = useCallback(async () => {
    if (!taskId) return;
    const requestId = ++loadTaskRequestRef.current;
    const isCurrentRequest = () =>
      isMountedRef.current && requestId === loadTaskRequestRef.current;
    userEditedDuringRefreshRef.current = false;
    setLoading(true);
    try {
      const localTask = await tasksRepo.getLocal(taskId);
      if (!isCurrentRequest()) return;
      applyLoadedTask(localTask);
      if (localTask) setLoading(false);

      const remoteTaskPromise = tasksRepo.get(taskId).then((remoteTask) => {
        if (!isCurrentRequest()) return;
        if (userEditedDuringRefreshRef.current) {
          setTask(remoteTask);
          return;
        }
        applyLoadedTask(remoteTask);
      });
      const timerPromise = timeEntriesRepo
        .getActive(taskId)
        .then((entry) => {
          if (isCurrentRequest()) setActiveEntry(entry);
        })
        .catch(() => {
          if (isCurrentRequest()) setActiveEntry(null);
        });
      const applyCachedAttachments = () => {
        if (!isCurrentRequest()) return;
        const snapshot = readAttachmentsSnapshot(taskId);
        if (snapshot) {
          setAttachments(snapshot.attachments);
          setAttachmentsStale(true);
          setAttachmentsCachedAt(snapshot.cachedAt || null);
        } else {
          setAttachments([]);
          setAttachmentsStale(false);
          setAttachmentsCachedAt(null);
        }
      };
      const attachmentPromise =
        isAuthenticated && online
          ? taskApi
              .listAttachments(taskId)
              .then((nextAttachments) => {
                // 成功時は最新一覧を表示し、スナップショットを更新する。
                writeAttachmentsSnapshot(taskId, nextAttachments);
                if (isCurrentRequest()) {
                  setAttachments(nextAttachments);
                  setAttachmentsStale(false);
                  setAttachmentsCachedAt(null);
                }
              })
              // 失敗時はスナップショットがあれば最終同期時点の内容を表示する。
              .catch(applyCachedAttachments)
          : // オフライン・未ログイン時はスナップショットを表示（閲覧のみ）。
            Promise.resolve().then(applyCachedAttachments);
      const recurrencePromise =
        isAuthenticated && online
          ? taskApi
              .getTaskRecurrence(taskId)
              .then((rule) => {
                if (isCurrentRequest()) setRecurrenceRule(rule);
              })
              .catch(() => {
                if (isCurrentRequest()) setRecurrenceRule(null);
              })
          : Promise.resolve();
      const referencesPromise =
        isAuthenticated && online
          ? taskApi
              .listTaskReferences(taskId)
              .then((items) => {
                if (isCurrentRequest()) setReferences(items);
              })
              .catch(() => {
                if (isCurrentRequest()) setReferences([]);
              })
          : Promise.resolve();
      await Promise.allSettled([
        remoteTaskPromise,
        timerPromise,
        attachmentPromise,
        recurrencePromise,
        referencesPromise,
      ]);
    } finally {
      if (isCurrentRequest()) setLoading(false);
    }
  }, [applyLoadedTask, isAuthenticated, online, taskId]);

  useEffect(() => {
    void loadTask();
  }, [completionRefreshToken, loadTask]);

  useFocusEffect(
    useCallback(() => {
      void refreshProjects();
    }, [refreshProjects]),
  );

  useEffect(() => {
    if (!taskId) {
      setAppLinks([]);
      setAvailableApps([]);
      return;
    }
    let cancelled = false;
    void appsRepo
      .listTaskApps(taskId)
      .then((links) => {
        if (!cancelled) setAppLinks(links);
      })
      .catch(() => {
        if (!cancelled) setAppLinks([]);
      });
    if (task?.project_id) {
      void appsRepo
        .list({ projectId: task.project_id })
        .then((apps) => {
          if (!cancelled) setAvailableApps(apps);
        })
        .catch(() => {
          if (!cancelled) setAvailableApps([]);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [online, task?.project_id, taskId]);

  // task.tags はロード毎に新しい配列になり、ローカル→リモート差し替えで
  // listTags が1表示で多重発火していた。タグidの安定キーへ依存を切り替える。
  const taskTagSignature = useMemo(
    () => (task?.tags ?? []).map((tag) => tag.id).join(","),
    [task?.tags],
  );
  useEffect(() => {
    if (!task?.project_id) {
      setAvailableTags([]);
      return;
    }
    let cancelled = false;
    const fallbackTags = task.tags ?? [];
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
        if (!cancelled) setAvailableTags(fallbackTags);
      });
    return () => {
      cancelled = true;
    };
  }, [task?.project_id, taskTagSignature]);

  useEffect(() => {
    if (!task?.project_id) {
      setAvailableMembers([]);
      return;
    }
    let cancelled = false;
    projectApi
      .listAssigneeCandidates(task.project_id)
      .then((members) => {
        if (!cancelled) setAvailableMembers(members);
      })
      .catch(() => {
        if (!cancelled) setAvailableMembers([]);
      });
    return () => {
      cancelled = true;
    };
  }, [task?.project_id]);

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

  const draftPayload = useMemo<Record<string, unknown> | null>(() => {
    if (!task) return null;
    return {
      title: title.trim(),
      description: description.trim() || null,
      status,
      start_at: toTaskWallClockIso(startAt),
      end_at: toTaskWallClockIso(endAt),
      all_day:
        allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
      tag_ids: selectedTagIds,
      assignee_ids: selectedAssigneeIds,
    };
  }, [
    allDay,
    description,
    endAt,
    notificationsEnabled,
    reminderOffsets,
    selectedAssigneeIds,
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
  const draftSignatureRef = useRef(draftSignature);
  draftSignatureRef.current = draftSignature;
  const savedSignature = useMemo(
    () => (task ? serializeTaskDraft(buildTaskSavedDraft(task)) : ""),
    [task],
  );
  const hasUnsavedDraft = Boolean(
    task && draftPayload && draftSignature !== savedSignature,
  );
  useEffect(() => {
    if (hasUnsavedDraft) userEditedDuringRefreshRef.current = true;
  }, [hasUnsavedDraft]);
  const hasValidAutosaveTitle =
    Boolean(title.trim()) && !DISALLOWED_PLACEHOLDER_TITLES.has(title.trim());

  const statusOption = useMemo(
    () => getTaskStatusOption(status),
    [status],
  );
  useEffect(() => {
    if (Platform.OS !== "web" || !statusMenuVisible) return;
    const handleStatusShortcut = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) {
        return;
      }
      const nextStatus = TASK_STATUS_SHORTCUT_KEYS[event.key.toLowerCase()];
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

  const saveTaskDraft = useCallback(
    async (options?: { navigateAfter?: boolean; showErrors?: boolean }) => {
      const normalizedTitle = title.trim();
      const showErrors = options?.showErrors ?? false;
      if (!taskId || !task || !draftPayload) {
        return null;
      }
      if (!normalizedTitle) {
        if (showErrors) setTitleError("タイトルを入力してください。");
        return null;
      }
      if (DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle)) {
        if (showErrors) setTitleError("仮タイトルは使用できません。");
        return null;
      }
      if (!options?.navigateAfter && draftSignature === savedSignature) {
        return task;
      }

      const saveGeneration = saveTaskGenerationRef.current + 1;
      saveTaskGenerationRef.current = saveGeneration;
      if (isMountedRef.current) {
        setAutosaving(true);
        setSaveError(null);
      }
      try {
        const previousTask = task;
        const updated = await saveTaskQueueRef.current.enqueue(() =>
          tasksRepo.update(taskId, draftPayload),
        );
        const isLatestSave =
          isMountedRef.current &&
          saveTaskGenerationRef.current === saveGeneration;
        const selectedAssignees = availableMembers
          .filter((member) => selectedAssigneeIds.includes(member.user_id))
          .map((member, index) => ({
            id: `draft-${member.user_id}`,
            task_id: taskId,
            user_id: member.user_id,
            is_primary: index === 0,
            display_name: member.display_name,
            username: member.username,
          }));
        const displayedTask = {
          ...updated,
          assignees:
            selectedAssigneeIds.length > 0 && updated.assignees.length === 0
              ? selectedAssignees
              : updated.assignees,
        };
        if (isLatestSave) {
          setTask(displayedTask);
          setTitleError(null);
          if (draftSignatureRef.current === draftSignature) {
            shouldFlushAutosaveRef.current = false;
          }
        }
        if (
          isLatestSave &&
          isTaskCompletionTransition(previousTask.status, status)
        ) {
          enqueueTaskCompletionUndoBatch({
            entries: [createTaskCompletionUndoEntry(previousTask)],
          });
        }
        if (isLatestSave && options?.navigateAfter) {
          goBackOrReplace(router, "/(tabs)/tasks");
        }
        return displayedTask;
      } catch (error) {
        if (
          isMountedRef.current &&
          saveTaskGenerationRef.current === saveGeneration
        ) {
          if (showErrors) {
            setSaveError(
              error instanceof Error ? error.message : "保存に失敗しました",
            );
          } else {
            setSaveError(
              "自動保存に失敗しました。接続状態を確認してください。",
            );
          }
        }
        return null;
      } finally {
        if (
          isMountedRef.current &&
          saveTaskGenerationRef.current === saveGeneration
        ) {
          setAutosaving(false);
        }
      }
    },
    [
      draftPayload,
      draftSignature,
      availableMembers,
      router,
      savedSignature,
      selectedAssigneeIds,
      status,
      task,
      taskId,
      title,
    ],
  );

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

  const saveRecurrence = useCallback(async () => {
    if (!taskId || advancedSaving) return;
    setAdvancedSaving(true);
    try {
      const interval = Math.max(1, Number.parseInt(recurrenceInterval, 10) || 1);
      const next = await taskApi.upsertTaskRecurrence(taskId, {
        rrule: `FREQ=${recurrenceFrequency};INTERVAL=${interval}`,
        timezone: "Asia/Tokyo",
      });
      setRecurrenceRule(next);
      setRecurrenceDialogVisible(false);
      setTask((current) =>
        current ? { ...current, has_recurrence: true, recurrence_rule: next } : current,
      );
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "繰り返し設定に失敗しました");
    } finally {
      setAdvancedSaving(false);
    }
  }, [advancedSaving, recurrenceFrequency, recurrenceInterval, taskId]);

  const clearRecurrence = useCallback(async () => {
    if (!taskId || advancedSaving) return;
    setAdvancedSaving(true);
    try {
      await taskApi.deleteTaskRecurrence(taskId);
      setRecurrenceRule(null);
      setTask((current) =>
        current ? { ...current, has_recurrence: false, recurrence_rule: null } : current,
      );
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "繰り返し解除に失敗しました");
    } finally {
      setAdvancedSaving(false);
    }
  }, [advancedSaving, taskId]);

  const addTaskReference = useCallback(async () => {
    if (!taskId || advancedSaving || !referenceUrlDraft.trim()) return;
    setAdvancedSaving(true);
    try {
      const created = await taskApi.createTaskReference(taskId, {
        reference_type: "url",
        relation_type: "related",
        target_url: referenceUrlDraft.trim(),
        display_name: referenceNameDraft.trim() || referenceUrlDraft.trim(),
      });
      setReferences((current) => [...current, created]);
      setReferenceUrlDraft("");
      setReferenceNameDraft("");
      setReferenceDialogVisible(false);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "参照先の追加に失敗しました");
    } finally {
      setAdvancedSaving(false);
    }
  }, [advancedSaving, referenceNameDraft, referenceUrlDraft, taskId]);

  const removeTaskReference = useCallback(
    (reference: TaskReference) => {
      if (!taskId || advancedSaving) return;
      Alert.alert("参照先を削除しますか？", reference.display_name || "この参照先", [
        { text: "キャンセル", style: "cancel" },
        {
          text: "削除",
          style: "destructive",
          onPress: () => {
            void taskApi
              .deleteTaskReference(taskId, reference.id, { confirmSource: true })
              .then(() => setReferences((current) => current.filter((item) => item.id !== reference.id)))
              .catch((error) =>
                setSaveError(error instanceof Error ? error.message : "参照先の削除に失敗しました"),
              );
          },
        },
      ]);
    },
    [advancedSaving, taskId],
  );

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
        setSelectedAssigneeIds([]);
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
      setAttachments((prev) => {
        const next = [...uploaded, ...prev];
        writeAttachmentsSnapshot(taskId, next);
        return next;
      });
      setAttachmentsStale(false);
      setAttachmentsCachedAt(null);
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

  const openAppLinkDialog = useCallback(() => {
    if (!online) {
      setSaveError("オフラインでは Task と App の関係を変更できません");
      return;
    }
    setSelectedAppLinkId(null);
    setAppLinkDialogVisible(true);
  }, [online]);

  const handleLinkApp = useCallback(async () => {
    if (!taskId || !selectedAppLinkId || !online) return;
    const selected = availableApps.find((app) => app.id === selectedAppLinkId);
    setAppLinkBusy(true);
    try {
      const link = await appsRepo.linkTaskApp(
        taskId,
        { app_id: selectedAppLinkId, relation_type: "related" },
        selected?.permission,
      );
      setAppLinks((previous) => [
        ...previous.filter(
          (item) =>
            !(
              item.app_id === link.app_id &&
              item.target_id === link.target_id &&
              item.relation_type === link.relation_type
            ),
        ),
        link,
      ]);
      setAppLinkDialogVisible(false);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "App連携に失敗しました");
    } finally {
      setAppLinkBusy(false);
    }
  }, [availableApps, online, selectedAppLinkId, taskId]);

  const handleUnlinkApp = useCallback(
    async (link: TaskAppLink) => {
      if (!taskId || !online) return;
      try {
        await appsRepo.unlinkTaskApp(taskId, link.app_id, {
          targetId: link.target_id,
          relationType: link.relation_type,
          permission: link.app?.permission,
        });
        setAppLinks((previous) => previous.filter((item) => item.id !== link.id));
      } catch (error) {
        setSaveError(error instanceof Error ? error.message : "App連携の解除に失敗しました");
      }
    },
    [online, taskId],
  );

  const handleRunWithAgent = useCallback(async () => {
    if (!task) return;

    const normalizedTitle = title.trim();
    if (
      !normalizedTitle ||
      DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle)
    ) {
      setTitleError("仮タイトルは使用できません。");
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
      start_at: toTaskWallClockIso(startAt),
      end_at: toTaskWallClockIso(endAt),
      all_day:
        allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
      tags: availableTags.filter((tag) => selectedTagIds.includes(tag.id)),
      assignees: selectedAssigneeIds.map((userId, index) => {
        const member = availableMembers.find(
          (candidate) => candidate.user_id === userId,
        );
        const existing = task.assignees.find(
          (candidate) => candidate.user_id === userId,
        );
        return {
          id: existing?.id ?? `draft-${userId}`,
          task_id: task.id,
          user_id: userId,
          is_primary: index === 0,
          display_name: member?.display_name ?? existing?.display_name,
          username: member?.username ?? existing?.username,
        };
      }),
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

      const session = await createCurrentCharacterSession(
        launchTask.project_id || undefined,
      );
      await conversationsRepo.updateTitle(
        session.id,
        buildTaskAgentSessionTitle(normalizedTitle),
      );
      await chatApi.dispatchMessage(
        session.id,
        buildTaskAgentDispatchPayload(launchTask),
      );
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
    allDay,
    availableMembers,
    availableTags,
    description,
    endAt,
    isAuthenticated,
    notificationsEnabled,
    online,
    reminderOffsets,
    router,
    selectedAssigneeIds,
    selectedTagIds,
    startAt,
    status,
    task,
    taskId,
    title,
  ]);

  const handleRunAgentTriage = useCallback(async () => {
    if (!taskId || triagingAgent) return;
    if (!isAuthenticated || !online) {
      Alert.alert(
        "エージェント事前確認",
        "ログインしてオンラインにすると実行できます。",
      );
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
        error instanceof Error ? error.message : "事前確認に失敗しました",
      );
    } finally {
      setTriagingAgent(false);
    }
  }, [isAuthenticated, loadTask, online, taskId, triagingAgent]);

  const currentProject = projects.find(
    (project) => project.id === task?.project_id,
  );
  const comments: TaskComment[] = task?.comments ?? [];
  const selectedTags = [
    ...(task?.tags ?? []),
    ...availableTags.filter((tag) => selectedTagIds.includes(tag.id)),
  ].filter(
    (tag, index, all) =>
      selectedTagIds.includes(tag.id) &&
      all.findIndex((candidate) => candidate.id === tag.id) === index,
  );
  const selectedAssignees = availableMembers.filter((member) =>
    selectedAssigneeIds.includes(member.user_id),
  );
  const assigneeLabel =
    selectedAssignees.length || task?.assignees?.length
      ? (selectedAssignees.length ? selectedAssignees : task?.assignees ?? [])
        .map((item) => item.display_name || item.username || item.user_id)
        .filter(Boolean)
        .join(", ")
      : "未設定";
  const completedSubtasks = (task?.subtasks ?? []).filter(
    (item) => item.status === "closed",
  ).length;
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
        <Text style={styles.errorText}>タスクが見つかりません。</Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      keyboardShouldPersistTaps="handled"
    >
      <View style={styles.redesignTitleRow}>
        <TextInput
          value={title}
          onChangeText={(value) => {
            setTitle(value);
            if (titleError) setTitleError(null);
          }}
          mode="flat"
          placeholder="タスク名"
          style={styles.redesignTitleInput}
          contentStyle={styles.redesignTitleInputContent}
          underlineColor="transparent"
          activeUnderlineColor="#89b4fa"
          error={!!titleError}
        />
        <Menu
          visible={taskMenuVisible}
          onDismiss={() => setTaskMenuVisible(false)}
          anchor={
            <IconButton
              icon="dots-horizontal"
              iconColor="#cdd6f4"
              size={22}
              onPress={() => setTaskMenuVisible(true)}
            />
          }
          contentStyle={styles.menuContent}
        >
          <Menu.Item
            title="履歴"
            leadingIcon="history"
            onPress={() => {
              setTaskMenuVisible(false);
              setShowHistoryDialog(true);
            }}
          />
          <Menu.Item
            title={triagingAgent ? "準備中..." : "エージェント用に準備"}
            leadingIcon="robot-outline"
            disabled={triagingAgent || !isAuthenticated || !online}
            onPress={() => {
              setTaskMenuVisible(false);
              void handleRunAgentTriage();
            }}
          />
          <Divider />
          <Menu.Item
            title="タスクを削除"
            leadingIcon="delete-outline"
            titleStyle={{ color: "#f38ba8" }}
            onPress={() => {
              setTaskMenuVisible(false);
              setShowDeleteDialog(true);
            }}
          />
        </Menu>
      </View>
      {titleError ? <Text style={styles.errorText}>{titleError}</Text> : null}

      <View style={styles.redesignAttributeRow}>
        <Menu
          visible={projectMenuVisible}
          onDismiss={() => setProjectMenuVisible(false)}
          anchor={
            <Chip
              icon="folder-outline"
              compact
              onPress={() => setProjectMenuVisible(true)}
              style={styles.redesignAttributeChip}
              textStyle={{ color: currentProject?.color || "#cdd6f4" }}
            >
              {currentProject?.name || task.project_name || "Project"}
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
        <IconButton
          icon="check-circle"
          iconColor={status === "closed" ? "#f5f5f7" : "#a6adc8"}
          containerColor={status === "closed" ? "#40a85a" : "#181825"}
          size={19}
          mode="contained"
          onPress={() => setStatus(status === "closed" ? "open" : "closed")}
          style={styles.redesignCompleteButton}
          accessibilityLabel={
            status === "closed" ? "未着手に戻す" : "完了にする"
          }
        />
        <Menu
          visible={statusMenuVisible}
          onDismiss={() => setStatusMenuVisible(false)}
          anchor={
            <Chip
              compact
              onPress={() => setStatusMenuVisible(true)}
              style={[
                styles.redesignAttributeChip,
                { borderColor: statusOption?.color },
              ]}
              textStyle={{ color: statusOption?.color }}
            >
              {statusOption?.label || status}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          {TASK_STATUS_OPTIONS.map((item) => (
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
        <Menu
          visible={assigneeMenuVisible}
          onDismiss={() => setAssigneeMenuVisible(false)}
          anchor={
            <Chip
              compact
              icon="account-outline"
              onPress={() => setAssigneeMenuVisible(true)}
              style={styles.redesignAttributeChip}
              textStyle={styles.redesignAssigneeText}
              accessibilityLabel={`担当者: ${assigneeLabel}`}
            >
              {assigneeLabel}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          <Menu.Item
            title="未設定"
            leadingIcon={
              selectedAssigneeIds.length === 0 ? "check-circle" : "circle-outline"
            }
            onPress={() => {
              setSelectedAssigneeIds([]);
              setAssigneeMenuVisible(false);
            }}
          />
          {availableMembers.map((member) => {
            const selected = selectedAssigneeIds.includes(member.user_id);
            return (
              <Menu.Item
                key={member.user_id}
                title={
                  member.display_name || member.username || member.user_id
                }
                leadingIcon={selected ? "check-circle" : "circle-outline"}
                onPress={() =>
                  setSelectedAssigneeIds((previous) =>
                    selected
                      ? previous.filter((id) => id !== member.user_id)
                      : [...previous, member.user_id],
                  )
                }
              />
            );
          })}
          {availableMembers.length === 0 ? (
            <Menu.Item title="担当者候補を取得できません" disabled />
          ) : null}
        </Menu>
      </View>

      <View style={styles.redesignDateRow}>
        <TaskDateField
          label="Start Date"
          value={startAt}
          onChange={setStartAt}
          allDay={allDay}
          style={styles.redesignDateInput}
        />
        <TaskDateField
          label="Due Date"
          value={endAt}
          onChange={setEndAt}
          allDay={allDay}
          style={styles.redesignDateInput}
        />
      </View>

      <View style={styles.redesignImportantTags}>
        {selectedTags.map((tag) => (
          <Chip
            key={tag.id}
            compact
            style={[
              styles.redesignImportantTagChip,
              { backgroundColor: tag.color || "#45475a" },
            ]}
            textStyle={styles.redesignImportantTagText}
            onPress={() => setTagMenuVisible(true)}
          >
            {tag.name}
          </Chip>
        ))}
        <Menu
          visible={tagMenuVisible}
          onDismiss={() => setTagMenuVisible(false)}
          anchor={
            <Chip
              compact
              icon="plus"
              style={styles.redesignAddTagChip}
              textStyle={styles.redesignAddTagText}
              onPress={() => setTagMenuVisible(true)}
            >
              {selectedTags.length ? "タグを編集" : "タグを追加"}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          {availableTags.length === 0 ? (
            <Menu.Item title="利用できるタグがありません" disabled />
          ) : (
            availableTags.map((tag) => (
              <Menu.Item
                key={tag.id}
                title={tag.name}
                leadingIcon={
                  selectedTagIds.includes(tag.id)
                    ? "check-circle"
                    : "circle-outline"
                }
                onPress={() => toggleTag(tag.id)}
              />
            ))
          )}
          <Divider />
          <View style={styles.compactTagCreateRow}>
            <TextInput
              value={newTagDraft}
              onChangeText={setNewTagDraft}
              mode="outlined"
              dense
              placeholder="新規タグ"
              style={styles.compactTagCreateInput}
            />
            <IconButton
              icon="plus"
              iconColor="#89b4fa"
              size={20}
              onPress={() => void handleCreateTag()}
              disabled={!newTagDraft.trim() || tagBusy}
              loading={tagBusy}
              accessibilityLabel="タグを追加"
            />
          </View>
        </Menu>
      </View>

      <Surface style={styles.redesignSectionCard} elevation={0}>
        <View style={styles.sectionHeaderRow}>
          <Text style={styles.redesignSectionTitle}>繰り返し・参照</Text>
          <View style={styles.advancedActions}>
            <Button
              mode="text"
              compact
              icon="calendar-sync"
              onPress={() => setRecurrenceDialogVisible(true)}
              disabled={!online}
            >
              {recurrenceRule ? "変更" : "設定"}
            </Button>
            <Button
              mode="text"
              compact
              icon="link-plus"
              onPress={() => setReferenceDialogVisible(true)}
              disabled={!online}
            >
              参照
            </Button>
          </View>
        </View>
        <Text style={styles.advancedMeta}>
          {recurrenceRule
            ? `${recurrenceRule.rrule}${recurrenceRule.timezone ? ` (${recurrenceRule.timezone})` : ""}`
            : "繰り返しなし"}
        </Text>
        {recurrenceRule ? (
          <Button
            mode="text"
            compact
            textColor="#f38ba8"
            icon="calendar-remove"
            onPress={() => void clearRecurrence()}
            disabled={!online || advancedSaving}
          >
            繰り返しを解除
          </Button>
        ) : null}
        {references.length === 0 ? (
          <Text style={styles.redesignEmptyText}>関連付けられた参照先はありません</Text>
        ) : (
          references.map((reference) => (
            <View key={reference.id} style={styles.referenceRow}>
              <Icon source="link-variant" size={18} color="#89b4fa" />
              <View style={styles.referenceCopy}>
                <Text style={styles.referenceTitle} numberOfLines={1}>
                  {reference.display_name || reference.target_url || reference.id}
                </Text>
                <Text style={styles.referenceMeta}>{reference.reference_type}</Text>
              </View>
              <IconButton
                icon="link-off"
                iconColor="#f38ba8"
                size={18}
                onPress={() => removeTaskReference(reference)}
                disabled={!online || advancedSaving}
                accessibilityLabel="タスク参照を削除"
              />
            </View>
          ))
        )}
      </Surface>

      <Surface style={styles.redesignSectionCard} elevation={0}>
        <Text style={styles.redesignSectionTitle}>説明</Text>
        <TextInput
          value={description}
          onChangeText={setDescription}
          mode="flat"
          style={styles.redesignDescriptionInput}
          contentStyle={styles.redesignDescriptionContent}
          multiline
          numberOfLines={5}
          placeholder="説明を追加"
          underlineColor="transparent"
          activeUnderlineColor="#89b4fa"
        />
      </Surface>

      <Surface style={styles.redesignSectionCard} elevation={0}>
        <View style={styles.sectionHeaderRow}>
          <Text style={styles.redesignSectionTitle}>サブタスク</Text>
          <Text style={styles.redesignSectionCount}>
            {completedSubtasks}/{(task.subtasks ?? []).length}
          </Text>
        </View>
        {(task.subtasks ?? []).map((subtask) => (
          <View key={subtask.id} style={styles.redesignSubtaskRow}>
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
              style={styles.redesignSubtaskButton}
            />
            <Text
              style={[
                styles.subtaskTitle,
                subtask.status === "closed" ? styles.subtaskDone : null,
              ]}
              numberOfLines={2}
            >
              {subtask.title}
            </Text>
          </View>
        ))}
        <View style={styles.inlineFormRow}>
          <TextInput
            value={subtaskDraft}
            onChangeText={setSubtaskDraft}
            mode="flat"
            dense
            placeholder="サブタスクを追加"
            style={styles.inlineFormInput}
          />
          <IconButton
            icon="plus"
            iconColor="#89b4fa"
            onPress={() => void handleAddSubtask()}
            disabled={!subtaskDraft.trim() || subtaskSaving}
          />
        </View>
      </Surface>

      <Surface style={styles.redesignSectionCard} elevation={0}>
        <View style={styles.sectionHeaderRow}>
          <Text style={styles.redesignSectionTitle}>関連 Apps</Text>
          <Button
            mode="text"
            compact
            icon="link-plus"
            onPress={openAppLinkDialog}
            disabled={!online || availableApps.length === 0}
          >
            追加
          </Button>
        </View>
        {appLinks.length === 0 ? (
          <Text style={styles.redesignEmptyText}>
            関連付けられた App はありません
          </Text>
        ) : (
          appLinks.map((link) => (
            <View key={link.id} style={styles.appLinkRow}>
              <Icon source="application-brackets-outline" size={20} color="#89b4fa" />
              <View style={styles.appLinkCopy}>
                <Text style={styles.appLinkTitle} numberOfLines={1}>
                  {link.app?.name || link.app_id}
                </Text>
                <Text style={styles.appLinkMeta}>{link.relation_type}</Text>
              </View>
              <IconButton
                icon="link-off"
                iconColor="#f38ba8"
                size={18}
                onPress={() => void handleUnlinkApp(link)}
                disabled={!online}
                accessibilityLabel="App連携を解除"
              />
            </View>
          ))
        )}
        {!online ? <Text style={styles.attachmentStaleNote}>オフラインでは連携を変更できません</Text> : null}
      </Surface>

      <Portal>
        <Dialog
          visible={appLinkDialogVisible}
          onDismiss={() => !appLinkBusy && setAppLinkDialogVisible(false)}
        >
          <Dialog.Title>TaskにAppを追加</Dialog.Title>
          <Dialog.Content>
            {availableApps.length === 0 ? (
              <Text style={styles.redesignEmptyText}>このProjectで利用できる App がありません</Text>
            ) : (
              availableApps.map((app) => (
                <Chip
                  key={app.id}
                  selected={selectedAppLinkId === app.id}
                  onPress={() => setSelectedAppLinkId(app.id)}
                  style={styles.appLinkChoice}
                >
                  {app.name} ({app.permission || "viewer"})
                </Chip>
              ))
            )}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setAppLinkDialogVisible(false)} disabled={appLinkBusy}>キャンセル</Button>
            <Button onPress={() => void handleLinkApp()} loading={appLinkBusy} disabled={!selectedAppLinkId || appLinkBusy}>追加</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>

      <View style={styles.redesignTabRow}>
        <Chip
          compact
          selected={activeInfoTab === "comments"}
          onPress={() => setActiveInfoTab("comments")}
          style={styles.redesignTabChip}
        >
          コメント {comments.length}
        </Chip>
        <Chip
          compact
          selected={activeInfoTab === "attachments"}
          onPress={() => setActiveInfoTab("attachments")}
          style={styles.redesignTabChip}
        >
          添付 {attachments.length}
        </Chip>
      </View>

      <Surface style={styles.redesignSectionCard} elevation={0}>
        {activeInfoTab === "comments" ? (
          <View>
            {comments.length === 0 ? (
              <Text style={styles.redesignEmptyText}>
                コメントはまだありません
              </Text>
            ) : (
              comments.map((comment) => (
                <View key={comment.id} style={styles.commentCard}>
                  <Text style={styles.commentText}>{comment.content}</Text>
                  <Text style={styles.commentMeta}>
                    {comment.display_name || comment.username || "user"}
                    {comment.created_at
                      ? ` / ${format(new Date(comment.created_at), "yyyy/MM/dd HH:mm")}`
                      : ""}
                  </Text>
                </View>
              ))
            )}
            <View style={styles.redesignCommentComposer}>
              <TextInput
                value={commentDraft}
                onChangeText={setCommentDraft}
                mode="flat"
                multiline
                placeholder="コメントを入力"
                style={styles.redesignCommentInput}
              />
              <IconButton
                icon="send"
                iconColor="#89b4fa"
                onPress={() => void handleSendComment()}
                disabled={!commentDraft.trim() || commentSaving}
              />
            </View>
          </View>
        ) : null}

        {activeInfoTab === "attachments" ? (
          <View>
            <View style={styles.sectionHeaderRow}>
              <Text style={styles.redesignSectionTitle}>添付ファイル</Text>
              <Button
                mode="text"
                compact
                icon="paperclip"
                onPress={() => void handleUploadAttachment()}
                disabled={attachmentSaving}
                loading={attachmentSaving}
              >
                追加
              </Button>
            </View>
            {attachmentsStale ? (
              <Text style={styles.attachmentStaleNote}>
                オフライン表示: 最終同期時点の内容です
                {attachmentsCachedAt
                  ? `（${format(new Date(attachmentsCachedAt), "MM/dd HH:mm")}時点）`
                  : ""}
              </Text>
            ) : null}
            {attachments.length === 0 ? (
              <Text style={styles.redesignEmptyText}>
                添付ファイルはまだありません
              </Text>
            ) : (
              attachments.map((attachment) => (
                <View key={attachment.id} style={styles.attachmentCard}>
                  <View style={styles.attachmentInfo}>
                    <Text style={styles.attachmentName} numberOfLines={1}>
                      {attachment.display_name}
                    </Text>
                    <Text style={styles.attachmentMeta}>
                      {attachment.kind === "image" ? "画像" : "ファイル"} /{" "}
                      {formatBytes(attachment.size_bytes)}
                    </Text>
                  </View>
                  <IconButton
                    icon="delete-outline"
                    iconColor="#f38ba8"
                    size={20}
                    onPress={() => void handleDeleteAttachment(attachment.id)}
                  />
                </View>
              ))
            )}
          </View>
        ) : null}
      </Surface>

      <Surface style={styles.redesignDetailsCard} elevation={0}>
        <View style={styles.redesignDetailsContent}>
            <Text style={styles.redesignSectionTitle}>通知</Text>
            <View style={styles.redesignSettingRow}>
              <Text style={styles.redesignSettingLabel}>終日</Text>
              <Switch value={allDay} onValueChange={setAllDay} />
            </View>
            <Divider style={styles.redesignDetailsDivider} />
            <View style={styles.redesignSettingRow}>
              <Text style={styles.redesignSettingLabel}>通知</Text>
              <Switch
                value={notificationsEnabled}
                onValueChange={setNotificationsEnabled}
              />
            </View>
            <Text style={styles.redesignSettingHeading}>リマインダー</Text>
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
          </View>
      </Surface>

      {shouldShowTriageCard ? (
        <Surface style={styles.triageCard} elevation={0}>
          <Text style={styles.triageTitle}>エージェント事前確認</Text>
          <Text style={styles.triageStatus}>{triageStatus}</Text>
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

      <Surface style={styles.redesignBottomActions} elevation={0}>
        <View style={styles.redesignSaveAndTimerRow}>
          <Text style={styles.autosaveStatus}>
            {autosaving
              ? "保存中…"
              : hasUnsavedDraft
                ? "変更は自動保存されます"
                : "保存済み"}
          </Text>
          <Button
            mode="text"
            compact
            icon={activeEntry ? "stop-circle" : "play-circle"}
            textColor={activeEntry ? "#f38ba8" : "#a6e3a1"}
            onPress={() => void handleTimerToggle()}
          >
            {activeEntry
              ? formatDuration(timerElapsed)
              : formatDuration(task.total_time_seconds || 0)}
          </Button>
        </View>
        <Button
          mode="contained"
          icon="robot-outline"
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
          エージェントで実行
        </Button>
      </Surface>

      <Portal>
        <Dialog
          visible={showHistoryDialog}
          onDismiss={() => setShowHistoryDialog(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>履歴</Dialog.Title>
          <Dialog.Content>
            {task.completed_at ? (
              <View style={styles.redesignHistoryRow}>
                <Text style={styles.redesignHistoryLabel}>完了</Text>
                <Text style={styles.redesignHistoryValue}>
                  {format(new Date(task.completed_at), "yyyy/MM/dd HH:mm")}
                </Text>
              </View>
            ) : null}
            {task.created_at ? (
              <View style={styles.redesignHistoryRow}>
                <Text style={styles.redesignHistoryLabel}>作成</Text>
                <Text style={styles.redesignHistoryValue}>
                  {format(new Date(task.created_at), "yyyy/MM/dd HH:mm")}
                </Text>
              </View>
            ) : null}
            {!task.created_at && !task.completed_at ? (
              <Text style={styles.redesignEmptyText}>履歴はまだありません</Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setShowHistoryDialog(false)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={recurrenceDialogVisible}
          onDismiss={() => !advancedSaving && setRecurrenceDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>繰り返しを設定</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>このタスクの予定を自動生成します。</Text>
            <View style={styles.dialogChipRow}>
              {(["DAILY", "WEEKLY", "MONTHLY"] as const).map((value) => (
                <Chip
                  key={value}
                  selected={recurrenceFrequency === value}
                  onPress={() => setRecurrenceFrequency(value)}
                >
                  {value === "DAILY" ? "毎日" : value === "WEEKLY" ? "毎週" : "毎月"}
                </Chip>
              ))}
            </View>
            <TextInput
              label="間隔（回）"
              value={recurrenceInterval}
              onChangeText={(value) => setRecurrenceInterval(value.replace(/[^0-9]/g, ""))}
              keyboardType="number-pad"
              mode="outlined"
              style={styles.dialogInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setRecurrenceDialogVisible(false)} disabled={advancedSaving}>
              キャンセル
            </Button>
            <Button onPress={() => void saveRecurrence()} loading={advancedSaving}>
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Dialog
          visible={referenceDialogVisible}
          onDismiss={() => !advancedSaving && setReferenceDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>参照先を追加</Dialog.Title>
          <Dialog.Content>
            <TextInput
              label="URL"
              value={referenceUrlDraft}
              onChangeText={setReferenceUrlDraft}
              mode="outlined"
              autoCapitalize="none"
              keyboardType="url"
              style={styles.dialogInput}
            />
            <TextInput
              label="表示名（任意）"
              value={referenceNameDraft}
              onChangeText={setReferenceNameDraft}
              mode="outlined"
              style={styles.dialogInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setReferenceDialogVisible(false)} disabled={advancedSaving}>
              キャンセル
            </Button>
            <Button
              onPress={() => void addTaskReference()}
              loading={advancedSaving}
              disabled={!referenceUrlDraft.trim() || advancedSaving}
            >
              追加
            </Button>
          </Dialog.Actions>
        </Dialog>
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
          <Dialog.Title style={styles.dialogTitle}>タスクを削除</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>
              「{task.title}」を削除しますか？
            </Text>
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              onPress={() => setShowDeleteDialog(false)}
              textColor="#a6adc8"
            >
              キャンセル
            </Button>
            <Button onPress={handleDelete} textColor="#f38ba8">
              削除
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
  groupHeading: {
    color: "#cdd6f4",
    fontSize: 18,
    fontWeight: "700",
    marginBottom: 10,
    marginTop: 4,
  },
  primaryCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 12,
    marginBottom: 20,
  },
  primaryTitleRow: { flexDirection: "row", alignItems: "center", gap: 4 },
  primaryTitleInput: { flex: 1, marginBottom: 12 },
  autosaveStatus: {
    color: "#9399b2",
    fontSize: 12,
    textAlign: "right",
  },
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
  advancedActions: { flexDirection: "row", alignItems: "center" },
  advancedMeta: { color: "#a6adc8", fontSize: 12, marginBottom: 6 },
  referenceRow: {
    flexDirection: "row",
    alignItems: "center",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
    paddingVertical: 5,
  },
  referenceCopy: { flex: 1, marginLeft: 8 },
  referenceTitle: { color: "#cdd6f4", fontSize: 13 },
  referenceMeta: { color: "#9399b2", fontSize: 11, marginTop: 2 },
  dialogChipRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginVertical: 10 },
  dialogInput: { marginBottom: 10, backgroundColor: "#181825" },
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
  attachmentStaleNote: { color: "#9399b2", fontSize: 11, marginBottom: 8 },
  appLinkRow: {
    flexDirection: "row",
    alignItems: "center",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
    paddingVertical: 6,
  },
  appLinkCopy: { flex: 1, marginLeft: 8 },
  appLinkTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "600" },
  appLinkMeta: { color: "#9399b2", fontSize: 11, marginTop: 2 },
  appLinkChoice: { marginBottom: 8 },
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
  redesignTitleRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 6,
  },
  redesignCompleteButton: {
    width: 32,
    height: 32,
    margin: 0,
    borderWidth: 1,
    borderColor: "#313244",
  },
  redesignTitleInput: {
    flex: 1,
    backgroundColor: "transparent",
    minHeight: 54,
  },
  redesignTitleInputContent: {
    color: "#f5f5f7",
    fontSize: 23,
    fontWeight: "700",
    paddingHorizontal: 4,
  },
  redesignAttributeRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 7,
    marginBottom: 9,
  },
  redesignDateRow: {
    flexDirection: "row",
    gap: 8,
    marginBottom: 9,
  },
  redesignDateInput: {
    flex: 1,
    minWidth: 0,
    backgroundColor: "#181825",
  },
  redesignAttributeChip: {
    backgroundColor: "#181825",
    borderWidth: 1,
    borderColor: "#313244",
  },
  redesignAssigneeText: { maxWidth: 132 },
  redesignImportantTags: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    marginBottom: 16,
  },
  redesignImportantTagChip: { height: 29 },
  redesignImportantTagText: { color: "#f5f5f7", fontSize: 12 },
  redesignAddTagChip: {
    height: 29,
    backgroundColor: "transparent",
    borderWidth: 1,
    borderColor: "#585b70",
  },
  redesignAddTagText: { color: "#a6adc8", fontSize: 12 },
  compactTagCreateRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 8,
    paddingVertical: 4,
    minWidth: 240,
  },
  compactTagCreateInput: {
    flex: 1,
    height: 40,
    backgroundColor: "#181825",
  },
  redesignSectionCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 14,
    padding: 13,
    marginBottom: 12,
  },
  redesignSectionTitle: {
    color: "#cdd6f4",
    fontSize: 15,
    fontWeight: "700",
  },
  redesignSectionCount: { color: "#9399b2", fontSize: 13 },
  redesignDescriptionInput: {
    backgroundColor: "#181825",
    borderRadius: 10,
    marginTop: 9,
    minHeight: 126,
  },
  redesignDescriptionContent: {
    color: "#cdd6f4",
    fontSize: 15,
    lineHeight: 21,
    paddingHorizontal: 10,
    paddingTop: 8,
  },
  redesignSubtaskRow: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 36,
  },
  redesignSubtaskButton: { margin: 0 },
  redesignTabRow: {
    flexDirection: "row",
    gap: 7,
    marginBottom: 9,
  },
  redesignTabChip: { flex: 1, backgroundColor: "#181825" },
  redesignHistoryRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 7,
  },
  redesignHistoryLabel: { color: "#a6adc8", fontSize: 13 },
  redesignHistoryValue: { color: "#cdd6f4", fontSize: 13 },
  redesignEmptyText: {
    color: "#9399b2",
    fontSize: 13,
    paddingVertical: 8,
  },
  redesignCommentComposer: {
    flexDirection: "row",
    alignItems: "flex-end",
    marginTop: 8,
  },
  redesignCommentInput: {
    flex: 1,
    backgroundColor: "#181825",
    borderRadius: 10,
  },
  redesignDetailsCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 14,
    marginBottom: 12,
    overflow: "hidden",
  },
  redesignDetailsButtonContent: { justifyContent: "flex-start" },
  redesignDetailsContent: { padding: 13, paddingTop: 4 },
  redesignSettingRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    minHeight: 44,
  },
  redesignSettingLabel: { color: "#a6adc8", fontSize: 14 },
  redesignSettingValue: { color: "#cdd6f4", fontSize: 14 },
  redesignSettingHeading: {
    color: "#cdd6f4",
    fontSize: 14,
    fontWeight: "600",
    marginBottom: 9,
  },
  redesignDetailsDivider: {
    backgroundColor: "#313244",
    marginVertical: 12,
  },
  redesignBottomActions: {
    backgroundColor: "#1e1e2e",
    borderRadius: 14,
    padding: 12,
    marginBottom: 8,
  },
  redesignSaveAndTimerRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
});
