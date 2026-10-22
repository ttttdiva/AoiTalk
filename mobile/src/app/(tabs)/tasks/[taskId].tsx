import { ScopeSwitcher } from "../../../components/scope-switcher";
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Alert,
  Linking,
  Platform,
  ScrollView,
  StyleSheet,
  TextInput as NativeTextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import {
  TaskDetailAction,
  TaskDetailSection,
} from "../../../components/task-detail-layout";
import {
  taskDateWithAllDay,
  taskRecurrenceLabel,
  taskReminderLabel,
} from "../../../features/tasks/task-detail-state";
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
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import {
  useFocusEffect,
  useLocalSearchParams,
  useNavigation,
  useRouter,
} from "expo-router";
import { usePreventRemove } from "@react-navigation/native";
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
import { registerOutboxResolutionEditor } from "../../../lib/outbox-resolution-editor-guard";
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
const TASK_EDITOR_IDLE_POLL_MS = 100;
const TASK_EDITOR_IDLE_MAX_POLLS = 300;
const TASK_LIST_ROUTE = "/(tabs)/tasks" as const;

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

function scheduleTaskIdleTimer(
  callback: () => void,
  delayMs: number,
): ReturnType<typeof setTimeout> {
  const timer = setTimeout(callback, delayMs);
  (timer as unknown as { unref?: () => void }).unref?.();
  return timer;
}

async function waitForTaskEditorIdle(
  pendingSets: ReadonlyArray<ReadonlySet<Promise<unknown>>>,
  isDirty: () => boolean,
): Promise<boolean> {
  for (let poll = 0; poll <= TASK_EDITOR_IDLE_MAX_POLLS; poll += 1) {
    const flights = pendingSets.flatMap((pending) => [...pending]);
    if (flights.length === 0) return !isDirty();
    await Promise.race([
      Promise.allSettled(flights),
      new Promise<void>((resolve) => scheduleTaskIdleTimer(resolve, TASK_EDITOR_IDLE_POLL_MS)),
    ]);
    // Yield at the same low frequency even when a settled flight has not yet
    // run its tracker cleanup callback.
    await new Promise<void>((resolve) => scheduleTaskIdleTimer(resolve, TASK_EDITOR_IDLE_POLL_MS));
  }
  return false;
}

function sameStringArray(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function buildTaskSavedDraft(
  task: Task,
  options?: { includeAssignees?: boolean; includeTags?: boolean },
): Record<string, unknown> {
  const startAt = formatTaskDateInput(task.start_at, task.all_day);
  const endAt = formatTaskDateInput(task.end_at, task.all_day);
  const draft: Record<string, unknown> = {
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
  if (options?.includeAssignees === false) {
    delete draft.assignee_ids;
  }
  if (options?.includeTags === false) {
    delete draft.tag_ids;
  }
  return draft;
}

export default function TaskDetailScreen() {
  const { taskId } = useLocalSearchParams<{ taskId: string }>();
  const router = useRouter();
  const navigation = useNavigation();
  const { isAuthenticated, isAnonymous, user } = useAuth();
  const outboxResolutionAuthScope = isAnonymous
    ? null
    : user?.user_id?.trim()
      ? `auth:${user.user_id.trim()}`
      : "metadata-only";
  const online = useNetworkStore((state) => state.connected ?? state.online);
  const { projects, refreshProjects } = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [activeEntry, setActiveEntry] = useState<TimeEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [autosaving, setAutosaving] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [taskMenuVisible, setTaskMenuVisible] = useState(false);
  const [statusMenuVisible, setStatusMenuVisible] = useState(false);
  const [tagMenuVisible, setTagMenuVisible] = useState(false);
  const [assigneeMenuVisible, setAssigneeMenuVisible] = useState(false);
  const [timerElapsed, setTimerElapsed] = useState(0);
  const [timerBusy, setTimerBusy] = useState(false);
  const timerBusyRef = useRef(false);
  const [notificationDialogVisible, setNotificationDialogVisible] =
    useState(false);
  const [subtaskComposerVisible, setSubtaskComposerVisible] = useState(false);

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
  const [tagSelectionTouched, setTagSelectionTouched] = useState(false);
  const [availableMembers, setAvailableMembers] = useState<
    TaskAssigneeCandidate[]
  >([]);
  const [selectedAssigneeIds, setSelectedAssigneeIds] = useState<string[]>([]);
  const [assigneeSelectionTouched, setAssigneeSelectionTouched] =
    useState(false);
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
    "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY"
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
  const backNavigationPendingRef = useRef(false);
  const navigationSaveRef = useRef(false);
  const replayNavigationRef = useRef(false);
  const deletingRef = useRef(false);
  const pendingTaskDraftSavesRef = useRef(new Set<Promise<unknown>>());
  const pendingTaskTargetWritesRef = useRef(new Set<Promise<unknown>>());
  const taskDraftDirtyRef = useRef(false);

  const trackTaskTargetWrite = useCallback(<T,>(operation: () => Promise<T>): Promise<T> => {
    const flight = operation();
    pendingTaskTargetWritesRef.current.add(flight);
    void flight
      .finally(() => pendingTaskTargetWritesRef.current.delete(flight))
      .catch(() => undefined);
    return flight;
  }, []);

  // This detail route can be opened from Tasks, Calendar, or another route.
  // `router.back()` follows whichever history entry happens to precede it,
  // which can leave the user on the wrong tab.  The task detail's stable
  // destination is the Tasks list, so keep that decision local to this
  // screen instead of changing the shared navigation helper.
  const navigateToTaskList = useCallback(() => {
    // POP_TO removes the detail route when the Tasks list is already in the
    // nested stack, and Expo Router falls back to replace when it is not.
    router.dismissTo(TASK_LIST_ROUTE);
  }, [router]);

  const cancelPendingDraftFlush = useCallback(() => {
    if (autosaveTimerRef.current) {
      clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    shouldFlushAutosaveRef.current = false;
    saveTaskGenerationRef.current += 1;
  }, []);

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
    setTagSelectionTouched(false);
    setSelectedAssigneeIds(
      (nextTask.assignees ?? []).map((assignee) => assignee.user_id),
    );
    setAssigneeSelectionTouched(false);
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
      setRecurrenceRule(localTask?.recurrence_rule ?? null);
      if (localTask) setLoading(false);

      const remoteTaskPromise = trackTaskTargetWrite(() => tasksRepo.get(taskId)).then((remoteTask) => {
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
          ? trackTaskTargetWrite(() => taskApi
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
              .catch(applyCachedAttachments))
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
                // Keep the locally cached rule when a recurrence refresh fails.
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
  }, [applyLoadedTask, isAuthenticated, online, taskId, trackTaskTargetWrite]);

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
    const payload: Record<string, unknown> = {
      title: title.trim(),
      description: description.trim() || null,
      status,
      start_at: toTaskWallClockIso(startAt),
      end_at: toTaskWallClockIso(endAt),
      all_day:
        allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
    };
    if (tagSelectionTouched) {
      // PATCH omission is a no-op; [] is an explicit clear.  A compact
      // response must never make an unrelated autosave clear cached tags.
      payload.tag_ids = selectedTagIds;
    }
    if (assigneeSelectionTouched) {
      // UpdateTaskPayload treats an omitted assignee_ids as no-op and [] as
      // an explicit clear.  Keep the field out of unrelated autosaves so an
      // incomplete/legacy response cannot erase server-side assignments.
      payload.assignee_ids = selectedAssigneeIds;
    }
    return payload;
  }, [
    allDay,
    assigneeSelectionTouched,
    description,
    endAt,
    notificationsEnabled,
    reminderOffsets,
    selectedAssigneeIds,
    selectedTagIds,
    tagSelectionTouched,
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
    () =>
      task
        ? serializeTaskDraft(
            buildTaskSavedDraft(task, {
              includeAssignees: assigneeSelectionTouched,
              includeTags: tagSelectionTouched,
            }),
          )
        : "",
    [assigneeSelectionTouched, tagSelectionTouched, task],
  );
  const hasUnsavedDraft = Boolean(
    task && draftPayload && draftSignature !== savedSignature,
  );
  taskDraftDirtyRef.current = hasUnsavedDraft;
  useEffect(() => {
    if (hasUnsavedDraft) userEditedDuringRefreshRef.current = true;
  }, [hasUnsavedDraft]);
  const hasValidAutosaveTitle =
    Boolean(title.trim()) && !DISALLOWED_PLACEHOLDER_TITLES.has(title.trim());

  const statusOption = useMemo(() => getTaskStatusOption(status), [status]);
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

  const performSaveTaskDraft = useCallback(
    async (options?: { showErrors?: boolean }) => {
      const normalizedTitle = title.trim();
      const showErrors = options?.showErrors ?? false;
      if (deletingRef.current) return null;
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
      if (draftSignature === savedSignature) {
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
        const updated = await saveTaskQueueRef.current.enqueue(() => {
          if (deletingRef.current) {
            return Promise.reject(new Error("タスク削除中は保存を停止しました"));
          }
          return trackTaskTargetWrite(() => tasksRepo.update(taskId, draftPayload));
        });
        const isLatestSave =
          isMountedRef.current &&
          saveTaskGenerationRef.current === saveGeneration;
        const selectedAssignees = selectedAssigneeIds.map((userId, index) => {
          const member = availableMembers.find(
            (candidate) => candidate.user_id === userId,
          );
          const existing = previousTask.assignees?.find(
            (candidate) => candidate.user_id === userId,
          );
          return {
            id: existing?.id ?? `draft-${userId}`,
            task_id: taskId,
            user_id: userId,
            is_primary: index === 0,
            display_name: member?.display_name ?? existing?.display_name,
            username: member?.username ?? existing?.username,
          };
        });
        const updatedAssignees = Array.isArray(updated.assignees)
          ? updated.assignees
          : [];
        const updatedAssigneesAreComplete =
          updated.assignees_complete === true ||
          (updated.assignees_complete === undefined &&
            updatedAssignees.length > 0);
        const updatedTags = Array.isArray(updated.tags) ? updated.tags : [];
        const updatedTagsAreComplete =
          updated.tags_complete === true ||
          (updated.tags_complete === undefined && updatedTags.length > 0);
        const selectedTagObjects = selectedTagIds.map((tagId) => {
          const existing = previousTask.tags?.find((tag) => tag.id === tagId);
          const available = availableTags.find((tag) => tag.id === tagId);
          return (
            available ??
            existing ?? {
              id: tagId,
              project_id: previousTask.project_id,
              name: tagId,
            }
          );
        });
        const selectedAssigneeIdsMatchResponse = sameStringArray(
          selectedAssigneeIds,
          updatedAssignees.map((assignee) => assignee.user_id),
        );
        const selectedTagIdsMatchResponse = sameStringArray(
          selectedTagIds,
          updatedTags.map((tag) => tag.id),
        );
        const useLocalAssigneeSelection =
          assigneeSelectionTouched &&
          (!updatedAssigneesAreComplete || !selectedAssigneeIdsMatchResponse);
        const useLocalTagSelection =
          tagSelectionTouched &&
          (!updatedTagsAreComplete || !selectedTagIdsMatchResponse);
        const displayedTask = {
          ...updated,
          comments: updated.comments ?? previousTask.comments,
          subtasks: updated.subtasks ?? previousTask.subtasks,
          attachments: updated.attachments ?? previousTask.attachments,
          tags:
            useLocalTagSelection
              ? selectedTagObjects
              : !updatedTagsAreComplete && previousTask.tags?.length
                ? previousTask.tags
                : updatedTags,
          tags_complete:
            updated.tags_complete ?? previousTask.tags_complete,
          assignees:
            useLocalAssigneeSelection
              ? selectedAssignees
              : !updatedAssigneesAreComplete && previousTask.assignees?.length
                ? previousTask.assignees
                : updated.assignees_complete !== true &&
                    selectedAssigneeIds.length > 0 &&
                    updatedAssignees.length === 0
                  ? selectedAssignees
                  : updatedAssignees,
          assignees_complete:
            updated.assignees_complete ?? previousTask.assignees_complete,
        };
        if (isLatestSave) {
          setTask(displayedTask);
          setTitleError(null);
          if (draftSignatureRef.current === draftSignature) {
            shouldFlushAutosaveRef.current = false;
            setAssigneeSelectionTouched(false);
            setTagSelectionTouched(false);
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
      savedSignature,
      assigneeSelectionTouched,
      tagSelectionTouched,
      selectedAssigneeIds,
      status,
      task,
      taskId,
      title,
      trackTaskTargetWrite,
    ],
  );

  // Track only save promises that already exist. The unmount guard waits for
  // these flights to settle without starting a new save during cleanup.
  const saveTaskDraft = useCallback(
    (options?: { showErrors?: boolean }) => {
      const draftSignatureAtStart = draftSignatureRef.current;
      const flight = performSaveTaskDraft(options);
      pendingTaskDraftSavesRef.current.add(flight);
      void flight.then(
        (result) => {
          if (result !== null && draftSignatureRef.current === draftSignatureAtStart) {
            taskDraftDirtyRef.current = false;
          }
        },
        () => undefined,
      );
      void flight
        .finally(() => pendingTaskDraftSavesRef.current.delete(flight))
        .catch(() => undefined);
      return flight;
    },
    [performSaveTaskDraft],
  );

  useEffect(() => {
    saveTaskDraftRef.current = () => saveTaskDraft({ showErrors: false });
    shouldFlushAutosaveRef.current =
      hasUnsavedDraft && hasValidAutosaveTitle && !loading && !deletingRef.current;
  }, [hasUnsavedDraft, hasValidAutosaveTitle, loading, saveTaskDraft]);

  useEffect(() => {
    if (autosaveTimerRef.current) {
      clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (!hasUnsavedDraft || !hasValidAutosaveTitle || loading || deletingRef.current) return;

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

  const retryAutosaveAfterDeleteFailure = useCallback(() => {
    if (autosaveTimerRef.current) {
      clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    if (!hasUnsavedDraft || !hasValidAutosaveTitle || loading) return;
    shouldFlushAutosaveRef.current = true;
    autosaveTimerRef.current = setTimeout(() => {
      autosaveTimerRef.current = null;
      if (!deletingRef.current) void saveTaskDraftRef.current?.();
    }, TASK_AUTOSAVE_DEBOUNCE_MS);
  }, [hasUnsavedDraft, hasValidAutosaveTitle, loading]);

  useFocusEffect(
    useCallback(() => {
      return () => {
        if (autosaveTimerRef.current) {
          clearTimeout(autosaveTimerRef.current);
          autosaveTimerRef.current = null;
        }
        if (shouldFlushAutosaveRef.current && !deletingRef.current) {
          void saveTaskDraftRef.current?.();
        }
      };
    }, []),
  );

  useEffect(() => {
    const entityId = typeof taskId === "string" ? taskId.trim() : "";
    if (!outboxResolutionAuthScope || !entityId) return;
    const cleanupState = { started: false };
    const idleProbe = () => {
      if (!cleanupState.started) return Promise.resolve(false);
      return waitForTaskEditorIdle(
        [pendingTaskDraftSavesRef.current, pendingTaskTargetWritesRef.current],
        () => taskDraftDirtyRef.current,
      );
    };
    const release = registerOutboxResolutionEditor(
      outboxResolutionAuthScope,
      "tasks",
      entityId,
      { idleProbe },
    );
    return () => {
      cleanupState.started = true;
      queueMicrotask(() => {
        void idleProbe().then((idle) => {
          if (idle) release();
        }).catch(() => {
          // Keep the guard registered when the idle probe cannot complete.
        });
      });
    };
  }, [outboxResolutionAuthScope, taskId]);

  const flushAndNavigate = useCallback(async () => {
    const signatureAtAttempt = draftSignatureRef.current;
    const saved = await saveTaskDraft({ showErrors: true });
    // A newer keystroke may arrive while the API save is in flight. Keep that
    // newer draft on screen and let its normal autosave run before leaving.
    if (!saved || draftSignatureRef.current !== signatureAtAttempt) return false;
    replayNavigationRef.current = true;
    navigateToTaskList();
    return true;
  }, [navigateToTaskList, saveTaskDraft]);

  // Native-stack back gestures bypass the toolbar handler. Keep the route
  // alive until the current draft is durably handed to the repository, then
  // replay a list-targeted POP_TO action.
  usePreventRemove(Boolean(task), ({ data }) => {
    if (replayNavigationRef.current) {
      replayNavigationRef.current = false;
      navigation.dispatch(data.action);
      return;
    }
    if (!hasUnsavedDraft) {
      replayNavigationRef.current = true;
      navigateToTaskList();
      return;
    }
    if (navigationSaveRef.current) return;
    navigationSaveRef.current = true;
    void flushAndNavigate().finally(() => {
      navigationSaveRef.current = false;
    });
  });

  const handleBack = useCallback(() => {
    if (backNavigationPendingRef.current) return;
    // While the task is still loading (or was not found), there is no draft to
    // flush and the list route remains the only useful destination.
    if (loading || !task) {
      navigateToTaskList();
      return;
    }
    if (!hasUnsavedDraft) {
      replayNavigationRef.current = true;
      navigateToTaskList();
      return;
    }

    // A navigation tap must not race the focus cleanup's fire-and-forget flush.
    // Save first, then replay the stable list dismissal; invalid drafts stay
    // on this screen and retain their contents.
    backNavigationPendingRef.current = true;
    void flushAndNavigate().finally(() => {
      backNavigationPendingRef.current = false;
    });
  }, [flushAndNavigate, hasUnsavedDraft, loading, navigateToTaskList, task]);

  const handleDelete = async () => {
    if (!taskId || deletingRef.current) return;
    deletingRef.current = true;
    cancelPendingDraftFlush();
    setShowDeleteDialog(false);
    try {
      // Serialize deletion behind any already-running draft mutation. This
      // prevents an older API save from arriving after the row was deleted.
      await trackTaskTargetWrite(() =>
        saveTaskQueueRef.current.enqueue(() => tasksRepo.delete(taskId)),
      );
      // The target is being deleted; the current draft no longer needs a
      // resolution guard after the delete settles successfully.
      taskDraftDirtyRef.current = false;
      // The task is gone, so replay the requested list dismissal without
      // asking the navigation guard to save the deleted row again.
      cancelPendingDraftFlush();
      replayNavigationRef.current = true;
      navigateToTaskList();
    } catch (error) {
      deletingRef.current = false;
      retryAutosaveAfterDeleteFailure();
      setSaveError(
        error instanceof Error ? error.message : "タスクを削除できませんでした",
      );
    }
  };

  const handleTimerToggle = async () => {
    if (!taskId || timerBusyRef.current) return;
    timerBusyRef.current = true;
    setTimerBusy(true);
    try {
      if (activeEntry) {
        const stopped = await timeEntriesRepo.stopTimer(activeEntry.id);
        setTask((current) =>
          current
            ? {
                ...current,
                total_time_seconds:
                  (current.total_time_seconds || 0) +
                  (stopped.duration_seconds ?? timerElapsed),
              }
            : current,
        );
        setActiveEntry(null);
      } else {
        setActiveEntry(await timeEntriesRepo.startTimer(taskId));
      }
      // Starting/stopping a timer must not reload and overwrite an in-flight draft.
    } catch (error) {
      setSaveError(
        error instanceof Error
          ? error.message
          : "タイマーを変更できませんでした",
      );
    } finally {
      timerBusyRef.current = false;
      setTimerBusy(false);
    }
  };

  const changeAllDay = useCallback((value: boolean) => {
    setAllDay(value);
    setStartAt((current) => taskDateWithAllDay(current, value));
    setEndAt((current) => taskDateWithAllDay(current, value));
  }, []);

  const openRecurrence = useCallback(() => {
    const parts = Object.fromEntries(
      (recurrenceRule?.rrule ?? "")
        .replace(/^RRULE:/i, "")
        .split(";")
        .map((part) => part.split("=")),
    );
    const frequency = parts.FREQ;
    setRecurrenceFrequency(
      frequency === "WEEKLY" ||
        frequency === "MONTHLY" ||
        frequency === "YEARLY"
        ? frequency
        : "DAILY",
    );
    setRecurrenceInterval(parts.INTERVAL || "1");
    setRecurrenceDialogVisible(true);
  }, [recurrenceRule]);

  const toggleReminder = useCallback((offset: number) => {
    setReminderOffsets((prev) =>
      prev.includes(offset)
        ? prev.filter((item) => item !== offset)
        : [...prev, offset].sort((a, b) => a - b),
    );
  }, []);

  const toggleTag = useCallback((tagId: string) => {
    setTagSelectionTouched(true);
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
      setTagSelectionTouched(true);
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
      if (!(await saveTaskDraft({ showErrors: true }))) return;
      const interval = Math.max(
        1,
        Number.parseInt(recurrenceInterval, 10) || 1,
      );
      const next = await trackTaskTargetWrite(() => taskApi.upsertTaskRecurrence(taskId, {
        // Preserve custom weekday/end constraints when editing interval/frequency.
        rrule: [
          `FREQ=${recurrenceFrequency}`,
          `INTERVAL=${interval}`,
          ...(
            recurrenceRule?.rrule.replace(/^RRULE:/i, "").split(";") ?? []
          ).filter((part) => part && !/^(FREQ|INTERVAL)=/.test(part)),
        ].join(";"),
        timezone:
          recurrenceRule?.timezone ||
          Intl.DateTimeFormat().resolvedOptions().timeZone ||
          "Asia/Tokyo",
      }));
      setRecurrenceRule(next);
      setRecurrenceDialogVisible(false);
      setTask((current) =>
        current
          ? { ...current, has_recurrence: true, recurrence_rule: next }
          : current,
      );
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "繰り返し設定に失敗しました",
      );
    } finally {
      setAdvancedSaving(false);
    }
  }, [
    advancedSaving,
    recurrenceFrequency,
    recurrenceInterval,
    recurrenceRule,
    saveTaskDraft,
    taskId,
    trackTaskTargetWrite,
  ]);

  const clearRecurrence = useCallback(async () => {
    if (!taskId || advancedSaving) return;
    setAdvancedSaving(true);
    try {
      await trackTaskTargetWrite(() => taskApi.deleteTaskRecurrence(taskId));
      setRecurrenceRule(null);
      setRecurrenceDialogVisible(false);
      setTask((current) =>
        current
          ? { ...current, has_recurrence: false, recurrence_rule: null }
          : current,
      );
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "繰り返し解除に失敗しました",
      );
    } finally {
      setAdvancedSaving(false);
    }
  }, [advancedSaving, taskId, trackTaskTargetWrite]);

  const addTaskReference = useCallback(async () => {
    if (!taskId || advancedSaving || !referenceUrlDraft.trim()) return;
    setAdvancedSaving(true);
    try {
      const created = await trackTaskTargetWrite(() => taskApi.createTaskReference(taskId, {
        reference_type: "url",
        relation_type: "related",
        target_url: referenceUrlDraft.trim(),
        display_name: referenceNameDraft.trim() || referenceUrlDraft.trim(),
      }));
      setReferences((current) => [...current, created]);
      setReferenceUrlDraft("");
      setReferenceNameDraft("");
      setReferenceDialogVisible(false);
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "参照先の追加に失敗しました",
      );
    } finally {
      setAdvancedSaving(false);
    }
  }, [advancedSaving, referenceNameDraft, referenceUrlDraft, taskId, trackTaskTargetWrite]);

  const removeTaskReference = useCallback(
    (reference: TaskReference) => {
      if (!taskId || advancedSaving) return;
      Alert.alert(
        "参照先を削除しますか？",
        reference.display_name || "この参照先",
        [
          { text: "キャンセル", style: "cancel" },
          {
            text: "削除",
            style: "destructive",
            onPress: () => {
              void trackTaskTargetWrite(() => taskApi
                .deleteTaskReference(taskId, reference.id, {
                  confirmSource: true,
                }))
                .then(() =>
                  setReferences((current) =>
                    current.filter((item) => item.id !== reference.id),
                  ),
                )
                .catch((error) =>
                  setSaveError(
                    error instanceof Error
                      ? error.message
                      : "参照先の削除に失敗しました",
                  ),
                );
            },
          },
        ],
      );
    },
    [advancedSaving, taskId, trackTaskTargetWrite],
  );

  const handleMoveProject = useCallback(
    async (projectId: string) => {
      if (!taskId || !task || task.project_id === projectId) return;
      try {
        if (!(await saveTaskDraft({ showErrors: true })))
          throw new Error("変更を保存してからプロジェクトを移動してください。");
        const updated = await trackTaskTargetWrite(() => tasksRepo.update(taskId, {
          project_id: projectId,
        }));
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
        throw error;
      }
    },
    [loadTask, saveTaskDraft, task, taskId, trackTaskTargetWrite],
  );

  const handleAddSubtask = useCallback(async () => {
    const titleValue = subtaskDraft.trim();
    if (!task || !titleValue || subtaskSaving) return;
    setSubtaskSaving(true);
    try {
      if (!(await saveTaskDraft({ showErrors: true }))) return;
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
  }, [loadTask, saveTaskDraft, subtaskDraft, subtaskSaving, task]);

  const handleSendComment = useCallback(async () => {
    const content = commentDraft.trim();
    if (!taskId || !content || commentSaving) return;
    setCommentSaving(true);
    try {
      if (!(await saveTaskDraft({ showErrors: true }))) return;
      await trackTaskTargetWrite(() => taskApi.addComment(taskId, content));
      setCommentDraft("");
      await loadTask();
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : "コメント送信に失敗しました",
      );
    } finally {
      setCommentSaving(false);
    }
  }, [commentDraft, commentSaving, loadTask, saveTaskDraft, taskId, trackTaskTargetWrite]);

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
          await trackTaskTargetWrite(() => taskApi.uploadAttachment(taskId, {
            uri: asset.uri,
            name: asset.name || "uploaded-file",
            mimeType: asset.mimeType,
          })),
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
  }, [attachmentSaving, isAuthenticated, online, taskId, trackTaskTargetWrite]);

  const handleDeleteAttachment = useCallback(
    async (attachmentId: string) => {
      if (!taskId) return;
      try {
        await trackTaskTargetWrite(() => taskApi.deleteAttachment(taskId, attachmentId));
        setAttachments((prev) =>
          prev.filter((item) => item.id !== attachmentId),
        );
      } catch (error) {
        setSaveError(
          error instanceof Error ? error.message : "添付削除に失敗しました",
        );
      }
    },
    [taskId, trackTaskTargetWrite],
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
      const link = await trackTaskTargetWrite(() => appsRepo.linkTaskApp(
        taskId,
        { app_id: selectedAppLinkId, relation_type: "related" },
        selected?.permission,
      ));
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
      setSaveError(
        error instanceof Error ? error.message : "App連携に失敗しました",
      );
    } finally {
      setAppLinkBusy(false);
    }
  }, [availableApps, online, selectedAppLinkId, taskId, trackTaskTargetWrite]);

  const handleUnlinkApp = useCallback(
    async (link: TaskAppLink) => {
      if (!taskId || !online) return;
      try {
        await trackTaskTargetWrite(() => appsRepo.unlinkTaskApp(taskId, link.app_id, {
          targetId: link.target_id,
          relationType: link.relation_type,
          permission: link.app?.permission,
        }));
        setAppLinks((previous) =>
          previous.filter((item) => item.id !== link.id),
        );
      } catch (error) {
        setSaveError(
          error instanceof Error
            ? error.message
            : "App連携の解除に失敗しました",
        );
      }
    },
    [online, taskId, trackTaskTargetWrite],
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
        "AIチャットに引き継ぐ",
        "ログインしてオンラインにすると利用できます。",
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
      if (!(await saveTaskDraft({ showErrors: true }))) return;
      let launchTask = taskSnapshot;
      if (taskId && shouldPrepareTaskForAgent(task.metadata)) {
        setTriagingAgent(true);
        try {
          const result = await trackTaskTargetWrite(() => taskApi.runAgentTriage(taskId));
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
        { userId: user?.user_id },
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
        "AIチャットに引き継ぐ",
        error instanceof Error
          ? error.message
          : "AIチャットを開始できませんでした。",
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
    saveTaskDraft,
    trackTaskTargetWrite,
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
      const result = await trackTaskTargetWrite(() => taskApi.runAgentTriage(taskId));
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
  }, [isAuthenticated, loadTask, online, taskId, triagingAgent, trackTaskTargetWrite]);

  const comments: TaskComment[] = task?.comments ?? [];
  const selectedTags = [
    ...(task?.tags ?? []),
    ...availableTags.filter((tag) => selectedTagIds.includes(tag.id)),
  ].filter(
    (tag, index, all) =>
      selectedTagIds.includes(tag.id) &&
      all.findIndex((candidate) => candidate.id === tag.id) === index,
  );
  const assigneeLabel =
    selectedAssigneeIds
      .map((id) => {
        const member =
          availableMembers.find((item) => item.user_id === id) ??
          task?.assignees?.find((item) => item.user_id === id);
        return member?.display_name || member?.username || id;
      })
      .join(", ") || "未設定";
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

  if (loading || !task) {
    return (
      <SafeAreaView edges={["top", "left", "right"]} style={styles.container}>
        <View style={styles.toolbar}>
          <TaskDetailAction
            icon="arrow-left"
            accessibilityLabel="タスク一覧に戻る"
            onPress={handleBack}
          />
        </View>
        <View style={styles.center}>
          {loading ? (
            <ActivityIndicator size="large" color="#7c3aed" />
          ) : (
            <Text style={styles.errorText}>タスクが見つかりません。</Text>
          )}
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView edges={["top", "left", "right"]} style={styles.container}>
      <View style={styles.toolbar} testID="task-detail-toolbar">
        <TaskDetailAction
          icon="arrow-left"
          accessibilityLabel="タスク一覧に戻る"
          onPress={handleBack}
        />
        <Text
          style={styles.autosaveStatus}
          accessibilityLiveRegion="polite"
        >
          {saveError
            ? "保存エラー"
            : autosaving
              ? "保存中…"
              : hasUnsavedDraft
                ? "未保存"
                : "保存済み"}
        </Text>
        <TaskDetailAction
          icon={activeEntry ? "stop-circle-outline" : "play-circle-outline"}
          label={formatDuration(
            activeEntry ? timerElapsed : task.total_time_seconds || 0,
          )}
          style={styles.timerAction}
          color={activeEntry ? "#a6e3a1" : "#a6adc8"}
          accessibilityLabel={`${activeEntry ? "計測を停止" : "計測を開始"}: ${formatDuration(activeEntry ? timerElapsed : task.total_time_seconds || 0)}`}
          disabled={timerBusy}
          onPress={() => void handleTimerToggle()}
        />
        <Menu
          visible={taskMenuVisible}
          onDismiss={() => setTaskMenuVisible(false)}
          anchor={
            <IconButton
              icon="dots-horizontal"
              iconColor="#cdd6f4"
              size={20}
              style={styles.toolbarIcon}
              accessibilityLabel="タスクのメニュー"
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
          <Menu.Item
            title={
              launchingAgent ? "チャットを準備中…" : "AIチャットに引き継ぐ"
            }
            leadingIcon="chat-processing-outline"
            disabled={
              launchingAgent ||
              triagingAgent ||
              !isAuthenticated ||
              !online ||
              !hasValidAutosaveTitle
            }
            onPress={() => {
              setTaskMenuVisible(false);
              void handleRunWithAgent();
            }}
          />
          <Menu.Item
            title="参照リンクを追加"
            leadingIcon="link-plus"
            disabled={!isAuthenticated || !online}
            onPress={() => {
              setTaskMenuVisible(false);
              setReferenceDialogVisible(true);
            }}
          />
          <Menu.Item
            title="アプリを関連付ける"
            leadingIcon="application-brackets-outline"
            disabled={!isAuthenticated || !online}
            onPress={() => {
              setTaskMenuVisible(false);
              openAppLinkDialog();
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
      <ScrollView
        contentContainerStyle={styles.content}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="on-drag"
      >
        <NativeTextInput
          value={title}
          onChangeText={(value) => {
            setTitle(value);
            if (titleError) setTitleError(null);
          }}
          accessibilityLabel="タスク名"
          placeholder="タスク名"
          placeholderTextColor="#9399b2"
          multiline
          style={styles.titleInput}
        />
        {titleError ? <Text style={styles.errorText}>{titleError}</Text> : null}
        <View style={styles.attributes} testID="task-detail-attributes">
          <View style={styles.projectControl}>
            <ScopeSwitcher
              variant="inline"
              projectNameOnly
              accessibilityLabel="プロジェクトを変更"
              projects={projects}
              projectId={task.project_id}
              allowAll={false}
              onSelectProject={async (id) => {
                if (id) await handleMoveProject(id);
              }}
            />
          </View>
          <Menu
            visible={statusMenuVisible}
            onDismiss={() => setStatusMenuVisible(false)}
            anchor={
              <TaskDetailAction
                icon={
                  status === "closed"
                    ? "check-circle-outline"
                    : "circle-slice-4"
                }
                label={statusOption?.label || status}
                color={statusOption?.color}
                accessibilityLabel={`進捗: ${statusOption?.label || status}`}
                onPress={() => setStatusMenuVisible(true)}
              />
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
          <View style={styles.assigneeControl}>
            <Menu
              visible={assigneeMenuVisible}
              onDismiss={() => setAssigneeMenuVisible(false)}
              anchor={
                <TaskDetailAction
                  icon="account-outline"
                  label={assigneeLabel}
                  accessibilityLabel={`担当者: ${assigneeLabel}`}
                  onPress={() => setAssigneeMenuVisible(true)}
                />
              }
              contentStyle={styles.menuContent}
            >
              <Menu.Item
                title="未設定"
                leadingIcon={
                  selectedAssigneeIds.length === 0
                    ? "check-circle"
                    : "circle-outline"
                }
                onPress={() => {
                  setAssigneeSelectionTouched(true);
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
                    onPress={() => {
                      setAssigneeSelectionTouched(true);
                      setSelectedAssigneeIds((previous) =>
                        selected
                          ? previous.filter((id) => id !== member.user_id)
                          : [...previous, member.user_id],
                      );
                    }}
                  />
                );
              })}
              {availableMembers.length === 0 ? (
                <Menu.Item title="担当者候補を取得できません" disabled />
              ) : null}
            </Menu>
          </View>
        </View>
        <View style={styles.dateRow}>
          <TaskDateField
            compact
            label="開始"
            value={startAt}
            onChange={setStartAt}
            allDay={allDay}
            onAllDayChange={changeAllDay}
            onOpenRecurrence={openRecurrence}
            recurrenceLabel={taskRecurrenceLabel(recurrenceRule?.rrule)}
            recurrenceDisabled={!isAuthenticated || !online}
            style={styles.dateInput}
          />
          <TaskDateField
            compact
            label="期限"
            value={endAt}
            onChange={setEndAt}
            allDay={allDay}
            onAllDayChange={changeAllDay}
            onOpenRecurrence={openRecurrence}
            recurrenceLabel={taskRecurrenceLabel(recurrenceRule?.rrule)}
            recurrenceDisabled={!isAuthenticated || !online}
            style={styles.dateInput}
          />
        </View>
        {recurrenceRule ? (
          <TaskDetailAction
            icon="repeat"
            label={taskRecurrenceLabel(recurrenceRule.rrule)}
            accessibilityLabel="繰り返しを変更"
            style={styles.recurrenceSummary}
            disabled={!isAuthenticated || !online}
            onPress={openRecurrence}
          />
        ) : null}
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
        <View style={styles.descriptionSection}>
          <NativeTextInput
            value={description}
            onChangeText={setDescription}
            accessibilityLabel="説明"
            multiline
            placeholder="説明を追加…"
            placeholderTextColor="#9399b2"
            style={styles.descriptionInput}
          />
        </View>
        <View style={styles.subtaskSection}>
          <View style={styles.sectionHeaderRow}>
            <Icon source="format-list-checks" size={18} color="#a6adc8" />
            <Text style={styles.sectionHeading}>サブタスク</Text>
            {(task.subtasks ?? []).length > 0 ? (
              <Text style={styles.redesignSectionCount}>
                {completedSubtasks}/{(task.subtasks ?? []).length}
              </Text>
            ) : null}
            <TaskDetailAction
              icon={subtaskComposerVisible ? "minus" : "plus"}
              accessibilityLabel={
                subtaskComposerVisible
                  ? "サブタスク入力を閉じる"
                  : "サブタスクを追加"
              }
              onPress={() => setSubtaskComposerVisible((value) => !value)}
            />
          </View>
          {(task.subtasks ?? []).map((subtask) => (
            <View key={subtask.id} style={styles.redesignSubtaskRow}>
              <TaskDetailAction
                icon={
                  subtask.status === "closed"
                    ? "check-circle-outline"
                    : "circle-outline"
                }
                color={subtask.status === "closed" ? "#a6e3a1" : "#a6adc8"}
                accessibilityLabel={`サブタスク${subtask.status === "closed" ? "を再開" : "を完了"}: ${subtask.title}`}
                onPress={() => {
                  void (async () => {
                    try {
                      if (!(await saveTaskDraft({ showErrors: true }))) return;
                      const nextStatus =
                        subtask.status === "closed" ? "open" : "closed";
                      await tasksRepo.update(subtask.id, {
                        status: nextStatus,
                      });
                      if (
                        isTaskCompletionTransition(subtask.status, nextStatus)
                      )
                        enqueueTaskCompletionUndoBatch({
                          entries: [createTaskCompletionUndoEntry(subtask)],
                        });
                      await loadTask();
                    } catch (error) {
                      setSaveError(
                        error instanceof Error
                          ? error.message
                          : "サブタスクを更新できませんでした",
                      );
                    }
                  })();
                }}
              />
              <Text
                style={[
                  styles.subtaskTitle,
                  subtask.status === "closed" && styles.subtaskDone,
                ]}
                numberOfLines={2}
              >
                {subtask.title}
              </Text>
            </View>
          ))}
          {subtaskComposerVisible ? (
            <View style={styles.inlineFormRow}>
              <TextInput
                value={subtaskDraft}
                onChangeText={setSubtaskDraft}
                mode="outlined"
                dense
                accessibilityLabel="サブタスク名"
                placeholder="サブタスク名"
                style={styles.inlineFormInput}
                onSubmitEditing={() => void handleAddSubtask()}
              />
              <TaskDetailAction
                icon="plus"
                accessibilityLabel="サブタスクを保存"
                disabled={!subtaskDraft.trim() || subtaskSaving}
                onPress={() => void handleAddSubtask()}
              />
            </View>
          ) : null}
        </View>
        <View style={styles.notificationRow}>
          <TaskDetailAction
            icon={notificationsEnabled ? "bell-outline" : "bell-off-outline"}
            label={`通知  ${taskReminderLabel(notificationsEnabled, reminderOffsets, Boolean(startAt))}`}
            accessibilityLabel={`通知設定: ${taskReminderLabel(notificationsEnabled, reminderOffsets, Boolean(startAt))}`}
            style={styles.notificationAction}
            onPress={() => setNotificationDialogVisible(true)}
          />
          <Icon source="chevron-right" size={18} color="#9399b2" />
        </View>
        <TaskDetailSection
          title="コメント"
          icon="comment-outline"
          count={comments.length}
        >
          {comments.map((comment) => (
            <View key={comment.id} style={styles.commentCard}>
              <Text style={styles.commentText}>{comment.content}</Text>
              <Text style={styles.commentMeta}>
                {comment.display_name || comment.username || "user"}
                {comment.created_at
                  ? ` / ${format(new Date(comment.created_at), "yyyy/MM/dd HH:mm")}`
                  : ""}
              </Text>
            </View>
          ))}
          <View style={styles.redesignCommentComposer}>
            <TextInput
              value={commentDraft}
              onChangeText={setCommentDraft}
              mode="outlined"
              dense
              multiline
              accessibilityLabel="コメント"
              placeholder="コメントを入力"
              style={styles.redesignCommentInput}
            />
            <TaskDetailAction
              icon="send-outline"
              accessibilityLabel="コメントを送信"
              onPress={() => void handleSendComment()}
              disabled={
                !commentDraft.trim() ||
                commentSaving ||
                !isAuthenticated ||
                !online
              }
            />
          </View>
          {!online ? (
            <Text style={styles.attachmentStaleNote}>
              コメントの送信には接続が必要です。
            </Text>
          ) : null}
        </TaskDetailSection>
        <TaskDetailSection
          title="添付ファイル"
          icon="paperclip"
          count={attachments.length}
        >
          <Button
            mode="text"
            compact
            icon="paperclip"
            disabled={attachmentSaving || !isAuthenticated || !online}
            loading={attachmentSaving}
            onPress={() => void handleUploadAttachment()}
          >
            ファイルを追加
          </Button>
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
        </TaskDetailSection>
        {references.length > 0 ? (
          <TaskDetailSection
            title="参照リンク"
            icon="link-variant"
            count={references.length}
          >
            {references.map((reference) => (
              <View key={reference.id} style={styles.referenceRow}>
                <View style={styles.referenceCopy}>
                  <TaskDetailAction
                    icon="link-variant"
                    label={
                      reference.display_name ||
                      reference.target_url ||
                      reference.id
                    }
                    accessibilityLabel={`参照を開く: ${reference.display_name || reference.target_url || reference.id}`}
                    disabled={
                      !reference.target_url ||
                      !/^https?:\/\//i.test(reference.target_url)
                    }
                    onPress={() => {
                      if (reference.target_url)
                        void Linking.openURL(reference.target_url).catch(() =>
                          setSaveError("参照リンクを開けませんでした"),
                        );
                    }}
                  />
                </View>
                <IconButton
                  icon="link-off"
                  size={18}
                  iconColor="#f38ba8"
                  disabled={!online || advancedSaving}
                  accessibilityLabel="タスク参照を削除"
                  onPress={() => removeTaskReference(reference)}
                />
              </View>
            ))}
            <Button
              compact
              onPress={() => setReferenceDialogVisible(true)}
              disabled={!online}
            >
              参照リンクを追加
            </Button>
          </TaskDetailSection>
        ) : null}
        {appLinks.length > 0 ? (
          <TaskDetailSection
            title="関連アプリ"
            icon="application-brackets-outline"
            count={appLinks.length}
          >
            {appLinks.map((link) => (
              <View key={link.id} style={styles.appLinkRow}>
                <Text style={styles.appLinkCopy}>
                  {link.app?.name || link.app_id}
                </Text>
                <IconButton
                  icon="link-off"
                  size={18}
                  iconColor="#f38ba8"
                  accessibilityLabel="App連携を解除"
                  disabled={!online}
                  onPress={() => void handleUnlinkApp(link)}
                />
              </View>
            ))}
            <Button compact onPress={openAppLinkDialog} disabled={!online}>
              アプリを関連付ける
            </Button>
          </TaskDetailSection>
        ) : null}
        {shouldShowTriageCard ? (
          <TaskDetailSection title="AI事前確認" icon="robot-outline">
            <Text style={styles.triageStatus}>{triageStatus}</Text>
            {triageHasSummary ? (
              <Text style={styles.triageSummary}>{triageSummary}</Text>
            ) : null}
            {triageQuestions.map((question) => (
              <Text key={question} style={styles.triageQuestion}>
                {question}
              </Text>
            ))}
          </TaskDetailSection>
        ) : null}
      </ScrollView>
      <Portal>
        <Dialog
          visible={appLinkDialogVisible}
          onDismiss={() => !appLinkBusy && setAppLinkDialogVisible(false)}
        >
          <Dialog.Title>アプリを関連付ける</Dialog.Title>
          <Dialog.Content>
            {availableApps.length === 0 ? (
              <Text style={styles.redesignEmptyText}>
                このプロジェクトで利用できるアプリはありません
              </Text>
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
            <Button
              onPress={() => setAppLinkDialogVisible(false)}
              disabled={appLinkBusy}
            >
              キャンセル
            </Button>
            <Button
              onPress={() => void handleLinkApp()}
              loading={appLinkBusy}
              disabled={!selectedAppLinkId || appLinkBusy}
            >
              追加
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
      <Portal>
        <Dialog
          visible={notificationDialogVisible}
          onDismiss={() => setNotificationDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>通知</Dialog.Title>
          <Dialog.Content>
            <View style={styles.notificationToggle}>
              <Text style={styles.dialogText}>タスクの通知</Text>
              <Switch
                accessibilityLabel="タスクの通知"
                value={notificationsEnabled}
                onValueChange={setNotificationsEnabled}
              />
            </View>
            <Text style={styles.dialogText}>事前に知らせるタイミング</Text>
            <View style={styles.dialogChipRow}>
              {REMINDER_PRESETS.map((preset) => (
                <Chip
                  key={preset.value}
                  selected={reminderOffsets.includes(preset.value)}
                  disabled={!notificationsEnabled}
                  onPress={() => toggleReminder(preset.value)}
                >
                  {preset.label}
                </Chip>
              ))}
            </View>
            {reminderOffsets.some(
              (value) =>
                !REMINDER_PRESETS.some((preset) => preset.value === value),
            ) ? (
              <View style={styles.dialogChipRow}>
                {reminderOffsets
                  .filter(
                    (value) =>
                      !REMINDER_PRESETS.some(
                        (preset) => preset.value === value,
                      ),
                  )
                  .map((value) => (
                    <Chip
                      key={value}
                      selected
                      disabled={!notificationsEnabled}
                      onPress={() => toggleReminder(value)}
                    >
                      {taskReminderLabel(true, [value], true)}
                    </Chip>
                  ))}
              </View>
            ) : null}
            <Button
              compact
              disabled={!notificationsEnabled || reminderOffsets.length === 0}
              onPress={() => setReminderOffsets([])}
            >
              標準設定に戻す
            </Button>
            <Text style={styles.attachmentStaleNote}>
              開始日時を基準に通知します。未選択の場合はアカウント・プロジェクトの標準設定を使います。
            </Text>
            {!startAt ? (
              <Text style={styles.attachmentStaleNote}>
                事前通知には開始日時の設定が必要です。
              </Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setNotificationDialogVisible(false)}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>

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
            <Text style={styles.dialogText}>
              日付に合わせて繰り返す予定を設定します。
            </Text>
            <View style={styles.dialogChipRow}>
              {(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] as const).map(
                (value) => (
                  <Chip
                    key={value}
                    selected={recurrenceFrequency === value}
                    onPress={() => setRecurrenceFrequency(value)}
                  >
                    {value === "DAILY"
                      ? "毎日"
                      : value === "WEEKLY"
                        ? "毎週"
                        : value === "MONTHLY"
                          ? "毎月"
                          : "毎年"}
                  </Chip>
                ),
              )}
            </View>
            <TextInput
              label="間隔（回）"
              value={recurrenceInterval}
              onChangeText={(value) =>
                setRecurrenceInterval(value.replace(/[^0-9]/g, ""))
              }
              keyboardType="number-pad"
              mode="outlined"
              style={styles.dialogInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            {recurrenceRule ? (
              <Button
                textColor="#f38ba8"
                disabled={advancedSaving || !online}
                onPress={() => void clearRecurrence()}
              >
                解除
              </Button>
            ) : null}
            <Button
              onPress={() => setRecurrenceDialogVisible(false)}
              disabled={advancedSaving}
            >
              キャンセル
            </Button>
            <Button
              onPress={() => void saveRecurrence()}
              loading={advancedSaving}
              disabled={advancedSaving || !isAuthenticated || !online}
            >
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
            <Button
              onPress={() => setReferenceDialogVisible(false)}
              disabled={advancedSaving}
            >
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
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingHorizontal: 16, paddingTop: 8, paddingBottom: 24 },
  autosaveStatus: {
    color: "#9399b2",
    fontSize: 12,
    flex: 1,
    minWidth: 44,
    maxWidth: "100%",
    flexShrink: 1,
    flexWrap: "wrap",
    textAlign: "center",
  },
  center: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
    backgroundColor: "#11111b",
  },
  errorText: { color: "#f38ba8" },
  menuContent: { backgroundColor: "#1e1e2e" },
  sectionHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 0,
    gap: 8,
  },
  referenceRow: {
    flexDirection: "row",
    alignItems: "center",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
    paddingVertical: 5,
  },
  referenceCopy: { flex: 1, marginLeft: 8 },
  dialogChipRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    marginVertical: 10,
  },
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
  appLinkCopy: { flex: 1, marginLeft: 8, color: "#cdd6f4" },
  appLinkChoice: { marginBottom: 8 },
  commentCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 10,
    padding: 10,
    marginBottom: 8,
  },
  commentText: { color: "#cdd6f4", fontSize: 14 },
  commentMeta: { color: "#9399b2", fontSize: 11, marginTop: 6 },
  subtaskTitle: { color: "#cdd6f4", fontSize: 14, flex: 1 },
  subtaskDone: { textDecorationLine: "line-through", color: "#a6adc8" },
  triageStatus: { color: "#89b4fa", fontSize: 12, marginTop: 2 },
  triageSummary: { color: "#a6adc8", fontSize: 13, lineHeight: 18 },
  triageQuestion: { color: "#fab387", fontSize: 12, marginTop: 6 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogText: { color: "#a6adc8" },
  snackbar: { backgroundColor: "#313244" },
  redesignImportantTags: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    marginBottom: 12,
    marginTop: 4,
  },
  redesignImportantTagChip: { minHeight: 32 },
  redesignImportantTagText: { color: "#f5f5f7", fontSize: 12 },
  redesignAddTagChip: {
    minHeight: 32,
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
  redesignSectionCount: { color: "#9399b2", fontSize: 13 },
  redesignSubtaskRow: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 36,
  },
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
  toolbar: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "stretch",
    minHeight: 48,
    paddingHorizontal: 6,
    paddingVertical: 4,
    gap: 4,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#242435",
  },
  toolbarIcon: { margin: 0, width: 44, height: 44 },
  timerAction: { maxWidth: "100%", flexShrink: 1 },
  titleInput: {
    color: "#f5f5f7",
    fontSize: 23,
    lineHeight: 30,
    fontWeight: "700",
    paddingHorizontal: 0,
    paddingVertical: 6,
    marginBottom: 4,
  },
  attributes: { flexDirection: "row", alignItems: "center", gap: 2 },
  projectControl: { flex: 1.4, minWidth: 0 },
  assigneeControl: { flex: 1, minWidth: 0 },
  dateRow: { flexDirection: "row", gap: 8, marginVertical: 4 },
  dateInput: { flex: 1, minWidth: 0 },
  recurrenceSummary: { alignSelf: "flex-start" },
  descriptionSection: {
    paddingVertical: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  descriptionInput: {
    color: "#cdd6f4",
    fontSize: 15,
    lineHeight: 23,
    minHeight: 44,
    padding: 0,
    textAlignVertical: "top",
  },
  subtaskSection: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  sectionHeading: {
    color: "#cdd6f4",
    fontSize: 14,
    fontWeight: "600",
    flex: 1,
  },
  notificationRow: {
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  notificationAction: {
    flex: 1,
    justifyContent: "flex-start",
    paddingHorizontal: 0,
  },
  notificationToggle: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 12,
  },
});
