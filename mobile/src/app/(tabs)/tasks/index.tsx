/**
 * タスク一覧画面 — 検索・プロジェクト切替・複数選択に対応した高速操作版
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Alert,
  FlatList,
  GestureResponderEvent,
  LayoutChangeEvent,
  NativeSyntheticEvent,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  TextInputKeyPressEventData,
  View,
} from "react-native";
import {
  Button,
  Chip,
  Dialog,
  FAB,
  IconButton,
  Menu,
  Portal,
  Snackbar,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useRouter } from "expo-router";
import { tasksRepo } from "../../../repositories";
import { useProject } from "../../../contexts/ProjectContext";
import { useAuth } from "../../../contexts/AuthContext";
import { taskApi } from "../../../lib/task-api";
import type { Project, Tag, Task } from "../../../types/api";
import { formatTaskDateLabel } from "../../../lib/task-date-label";
import {
  isTaskDateOnlyInput,
  toTaskWallClockIso,
} from "../../../lib/task-datetime";
import {
  COMPLETED_TASK_STATUSES,
  isFutureTask,
} from "../../../lib/task-visibility";
import {
  createTaskCompletionUndoEntry,
  enqueueTaskCompletionUndoBatch,
  isTaskCompletionTransition,
  useTaskCompletionUndoStore,
} from "../../../stores/task-completion-undo";

const STATUS_COLORS: Record<string, string> = {
  open: "#89b4fa",
  in_progress: "#f38ba8",
  closed: "#a6e3a1",
  cancelled: "#a6adc8",
};
const STATUS_LABELS: Record<string, string> = {
  open: "未着手",
  in_progress: "進行中",
  closed: "完了",
  cancelled: "キャンセル",
};
const PRIORITY_COLORS: Record<string, string> = {
  urgent: "#f38ba8",
  high: "#fab387",
  normal: "#89b4fa",
  low: "#a6adc8",
};
const FILTERS = ["all", "open", "in_progress", "closed"] as const;
const FILTER_LABELS: Record<string, string> = {
  all: "すべて",
  open: "未着手",
  in_progress: "進行中",
  closed: "完了済み",
};
const STATUS_OPTIONS = [
  { value: "open", label: "未着手", icon: "circle-outline" },
  { value: "in_progress", label: "進行中", icon: "progress-clock" },
  { value: "closed", label: "完了", icon: "check-circle" },
  { value: "cancelled", label: "取消", icon: "close-circle-outline" },
];

const PRIORITY_OPTIONS = [
  { value: "urgent", label: "Urgent" },
  { value: "high", label: "High" },
  { value: "normal", label: "Normal" },
  { value: "low", label: "Low" },
  { value: "none", label: "None" },
];
const REMINDER_PRESETS = [
  { value: 5, label: "5分前" },
  { value: 15, label: "15分前" },
  { value: 30, label: "30分前" },
  { value: 60, label: "1時間前" },
  { value: 1440, label: "1日前" },
];
const DISALLOWED_PLACEHOLDER_TITLES = new Set([
  "無題のタスク",
  "Untitled task",
]);

type RowLayout = { y: number; height: number };

function reorderIds(
  ids: string[],
  draggedId: string,
  targetId: string,
  insertAfter: boolean,
): string[] {
  if (draggedId === targetId) return ids;
  const next = ids.filter((id) => id !== draggedId);
  const targetIndex = next.indexOf(targetId);
  if (targetIndex === -1) return ids;
  next.splice(targetIndex + (insertAfter ? 1 : 0), 0, draggedId);
  return next;
}

function sameOrder(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((id, index) => id === right[index])
  );
}

type TaskCreateDialogProps = {
  visible: boolean;
  selectedProjectId: string | null;
  projects: Project[];
  onDismiss: () => void;
  onSubmit: (data: Record<string, unknown>) => Promise<void>;
};

type TaskCommandDialogProps = {
  visible: boolean;
  targetTitle: string | null;
  error: string | null;
  busy: boolean;
  onDismiss: () => void;
  onSubmit: (value: string) => void;
  onChange: () => void;
};

function TaskCreateDialog({
  visible,
  selectedProjectId,
  projects,
  onDismiss,
  onSubmit,
}: TaskCreateDialogProps) {
  const [sessionKey, setSessionKey] = useState(0);
  const [projectDraft, setProjectDraft] = useState<string | null>(
    selectedProjectId,
  );
  const [titleDraft, setTitleDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [statusDraft, setStatusDraft] = useState("open");
  const [priorityDraft, setPriorityDraft] = useState("normal");
  const [startAtDraft, setStartAtDraft] = useState("");
  const [endAtDraft, setEndAtDraft] = useState("");
  const [estimatedHoursDraft, setEstimatedHoursDraft] = useState("");
  const [allDayDraft, setAllDayDraft] = useState(false);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminderOffsets, setReminderOffsets] = useState<number[]>([]);
  const [availableTags, setAvailableTags] = useState<Tag[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [newTagDraft, setNewTagDraft] = useState("");
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);
  const [statusMenuVisible, setStatusMenuVisible] = useState(false);
  const [priorityMenuVisible, setPriorityMenuVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [tagBusy, setTagBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const descriptionInputRef = useRef<{ focus: () => void } | null>(null);

  useEffect(() => {
    if (!visible) {
      setProjectMenuVisible(false);
      setStatusMenuVisible(false);
      setPriorityMenuVisible(false);
      setSubmitting(false);
      setTagBusy(false);
      return;
    }
    const initialProjectId = selectedProjectId ?? projects[0]?.id ?? null;
    setSessionKey((key) => key + 1);
    setProjectDraft(initialProjectId);
    setTitleDraft("");
    setDescriptionDraft("");
    setStatusDraft("open");
    setPriorityDraft("normal");
    setStartAtDraft("");
    setEndAtDraft("");
    setEstimatedHoursDraft("");
    setAllDayDraft(false);
    setNotificationsEnabled(true);
    setReminderOffsets([]);
    setSelectedTagIds([]);
    setNewTagDraft("");
    setFormError(null);
  }, [projects, selectedProjectId, visible]);

  useEffect(() => {
    if (!visible || !projectDraft) {
      setAvailableTags([]);
      setSelectedTagIds([]);
      return;
    }
    let cancelled = false;
    taskApi
      .listTags(projectDraft)
      .then((tags) => {
        if (cancelled) return;
        setAvailableTags(tags);
        setSelectedTagIds((prev) =>
          prev.filter((id) => tags.some((tag) => tag.id === id)),
        );
      })
      .catch(() => {
        if (!cancelled) setAvailableTags([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectDraft, visible]);

  const selectedProject = projects.find(
    (project) => project.id === projectDraft,
  );
  const priorityOpt = PRIORITY_OPTIONS.find(
    (option) => option.value === priorityDraft,
  );
  const statusOpt = STATUS_OPTIONS.find(
    (option) => option.value === statusDraft,
  );
  const normalizedTitle = titleDraft.trim();
  const estimatedHoursValue = estimatedHoursDraft.trim()
    ? Number(estimatedHoursDraft.trim())
    : null;
  const estimateInvalid =
    estimatedHoursValue != null && !Number.isFinite(estimatedHoursValue);
  const canSubmit =
    Boolean(normalizedTitle) &&
    Boolean(projectDraft) &&
    !DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle) &&
    !estimateInvalid &&
    !submitting;

  const focusDescriptionInput = useCallback(() => {
    descriptionInputRef.current?.focus();
  }, []);

  const handleTitleKeyPress = useCallback(
    (event: NativeSyntheticEvent<TextInputKeyPressEventData>) => {
      if (event.nativeEvent.key !== "ArrowDown") return;
      focusDescriptionInput();
    },
    [focusDescriptionInput],
  );

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
    if (!name || !projectDraft || tagBusy) return;
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
      const created = await taskApi.createTag(projectDraft, { name });
      setAvailableTags((prev) => [...prev, created]);
      setSelectedTagIds((prev) => [...prev, created.id]);
      setNewTagDraft("");
    } catch (error) {
      setFormError(
        error instanceof Error ? error.message : "タグ作成に失敗しました",
      );
    } finally {
      setTagBusy(false);
    }
  }, [availableTags, newTagDraft, projectDraft, tagBusy, toggleTag]);

  const submit = async () => {
    if (!canSubmit || !projectDraft) return;
    const data: Record<string, unknown> = {
      title: normalizedTitle,
      project_id: projectDraft,
      status: statusDraft,
      priority: priorityDraft,
      start_at: startAtDraft ? toTaskWallClockIso(startAtDraft) : null,
      end_at: endAtDraft ? toTaskWallClockIso(endAtDraft) : null,
      all_day:
        allDayDraft ||
        isTaskDateOnlyInput(startAtDraft) ||
        isTaskDateOnlyInput(endAtDraft),
      estimated_hours: estimatedHoursValue,
      notifications_enabled: notificationsEnabled,
      reminder_offsets: reminderOffsets,
      tag_ids: selectedTagIds,
    };
    if (descriptionDraft.trim()) data.description = descriptionDraft.trim();

    setSubmitting(true);
    setFormError(null);
    try {
      await onSubmit(data);
    } catch (error) {
      setFormError(
        error instanceof Error ? error.message : "タスク作成に失敗しました",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
      <Dialog.Title style={styles.dialogTitle}>タスクを作成</Dialog.Title>
      <Dialog.ScrollArea style={{ maxHeight: 560 }}>
        <ScrollView style={{ padding: 4 }} keyboardShouldPersistTaps="handled">
          <View style={styles.dialogRow}>
            <Text style={styles.dialogLabel}>プロジェクト</Text>
            <Menu
              visible={projectMenuVisible}
              onDismiss={() => setProjectMenuVisible(false)}
              anchor={
                <Chip
                  onPress={() => setProjectMenuVisible(true)}
                  style={styles.selectChip}
                >
                  {selectedProject?.name || "選択"}
                </Chip>
              }
              contentStyle={styles.menuContent}
            >
              {projects.map((project) => (
                <Menu.Item
                  key={project.id}
                  title={project.name}
                  leadingIcon={
                    project.id === projectDraft ? "check" : undefined
                  }
                  onPress={() => {
                    setProjectDraft(project.id);
                    setSelectedTagIds([]);
                    setProjectMenuVisible(false);
                  }}
                />
              ))}
            </Menu>
          </View>

          <TextInput
            key={`title-${sessionKey}`}
            label="タイトル"
            defaultValue=""
            onChangeText={setTitleDraft}
            onKeyPress={handleTitleKeyPress}
            mode="outlined"
            style={styles.dialogInput}
            autoCorrect={false}
          />
          <TextInput
            key={`description-${sessionKey}`}
            ref={(instance: { focus: () => void } | null) => {
              descriptionInputRef.current = instance;
            }}
            label="説明（Markdown可）"
            defaultValue=""
            onChangeText={setDescriptionDraft}
            mode="outlined"
            style={styles.dialogInput}
            autoCorrect={false}
            multiline
            numberOfLines={4}
          />

          <View style={styles.dialogTwoColumn}>
            <View style={styles.dialogColumn}>
              <Text style={styles.dialogLabel}>ステータス</Text>
              <Menu
                visible={statusMenuVisible}
                onDismiss={() => setStatusMenuVisible(false)}
                anchor={
                  <Chip
                    onPress={() => setStatusMenuVisible(true)}
                    style={styles.selectChip}
                  >
                    {statusOpt?.label || statusDraft}
                  </Chip>
                }
                contentStyle={styles.menuContent}
              >
                {STATUS_OPTIONS.map((opt) => (
                  <Menu.Item
                    key={opt.value}
                    title={opt.label}
                    onPress={() => {
                      setStatusDraft(opt.value);
                      setStatusMenuVisible(false);
                    }}
                  />
                ))}
              </Menu>
            </View>
            <View style={styles.dialogColumn}>
              <Text style={styles.dialogLabel}>優先度</Text>
              <Menu
                visible={priorityMenuVisible}
                onDismiss={() => setPriorityMenuVisible(false)}
                anchor={
                  <Chip
                    onPress={() => setPriorityMenuVisible(true)}
                    style={styles.selectChip}
                  >
                    {priorityOpt?.label || priorityDraft}
                  </Chip>
                }
                contentStyle={styles.menuContent}
              >
                {PRIORITY_OPTIONS.map((opt) => (
                  <Menu.Item
                    key={opt.value}
                    title={opt.label}
                    onPress={() => {
                      setPriorityDraft(opt.value);
                      setPriorityMenuVisible(false);
                    }}
                  />
                ))}
              </Menu>
            </View>
          </View>

          <View style={styles.dialogTwoColumn}>
            <TextInput
              key={`start-at-${sessionKey}`}
              label="開始"
              defaultValue=""
              onChangeText={setStartAtDraft}
              mode="outlined"
              style={[styles.dialogInput, styles.dialogColumn]}
              placeholder="yyyy-MM-ddTHH:mm"
              autoCorrect={false}
              autoCapitalize="none"
            />
            <TextInput
              key={`end-at-${sessionKey}`}
              label="期日"
              defaultValue=""
              onChangeText={setEndAtDraft}
              mode="outlined"
              style={[styles.dialogInput, styles.dialogColumn]}
              placeholder="yyyy-MM-ddTHH:mm"
              autoCorrect={false}
              autoCapitalize="none"
            />
          </View>
          <View style={styles.dialogTwoColumn}>
            <TextInput
              key={`estimate-${sessionKey}`}
              label="見積時間"
              defaultValue=""
              onChangeText={setEstimatedHoursDraft}
              mode="outlined"
              style={[styles.dialogInput, styles.dialogColumn]}
              keyboardType="decimal-pad"
              error={estimateInvalid}
            />
            <View style={[styles.dialogColumn, styles.switchRow]}>
              <Text style={styles.dialogLabel}>終日</Text>
              <Switch value={allDayDraft} onValueChange={setAllDayDraft} />
            </View>
          </View>

          <View style={styles.switchRowFull}>
            <Text style={styles.dialogLabel}>通知</Text>
            <Switch
              value={notificationsEnabled}
              onValueChange={setNotificationsEnabled}
            />
          </View>
          <Text style={styles.dialogSectionLabel}>リマインダー</Text>
          <View style={styles.dialogChipRow}>
            {REMINDER_PRESETS.map((preset) => (
              <Chip
                key={preset.value}
                compact
                selected={reminderOffsets.includes(preset.value)}
                disabled={!notificationsEnabled}
                onPress={() => toggleReminder(preset.value)}
                style={styles.filterChip}
                textStyle={styles.filterChipText}
              >
                {preset.label}
              </Chip>
            ))}
          </View>

          <Text style={styles.dialogSectionLabel}>タグ</Text>
          <View style={styles.dialogChipRow}>
            {availableTags.length === 0 ? (
              <Text style={styles.hintText}>
                タグなし / オフラインでは既存タグを取得できません
              </Text>
            ) : (
              availableTags.map((tag) => (
                <Chip
                  key={tag.id}
                  compact
                  selected={selectedTagIds.includes(tag.id)}
                  onPress={() => toggleTag(tag.id)}
                  style={[
                    styles.filterChip,
                    tag.color ? { borderColor: tag.color } : null,
                  ]}
                  textStyle={styles.filterChipText}
                >
                  {tag.name}
                </Chip>
              ))
            )}
          </View>
          <View style={styles.newTagRow}>
            <TextInput
              value={newTagDraft}
              onChangeText={setNewTagDraft}
              mode="outlined"
              dense
              placeholder="新規タグ"
              style={styles.newTagInput}
              autoCorrect={false}
            />
            <Button
              mode="outlined"
              onPress={() => void handleCreateTag()}
              disabled={!projectDraft || !newTagDraft.trim() || tagBusy}
              loading={tagBusy}
            >
              追加
            </Button>
          </View>
          {formError ? (
            <Text style={styles.commandError}>{formError}</Text>
          ) : null}
        </ScrollView>
      </Dialog.ScrollArea>
      <Dialog.Actions>
        <Button onPress={onDismiss} textColor="#a6adc8" disabled={submitting}>
          キャンセル
        </Button>
        <Button
          onPress={() => {
            void submit();
          }}
          textColor="#7c3aed"
          loading={submitting}
          disabled={!canSubmit}
        >
          作成
        </Button>
      </Dialog.Actions>
    </Dialog>
  );
}

function TaskCommandDialog({
  visible,
  targetTitle,
  error,
  busy,
  onDismiss,
  onSubmit,
  onChange,
}: TaskCommandDialogProps) {
  const [sessionKey, setSessionKey] = useState(0);
  const [draft, setDraft] = useState("/status ");

  useEffect(() => {
    if (!visible) return;
    setSessionKey((key) => key + 1);
    setDraft("/status ");
  }, [visible]);

  const handleChangeText = useCallback(
    (value: string) => {
      setDraft(value);
      onChange();
    },
    [onChange],
  );

  return (
    <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
      <Dialog.Title style={styles.dialogTitle}>タスクコマンド</Dialog.Title>
      <Dialog.Content>
        <Text style={styles.commandTarget}>
          対象: {targetTitle || "タスク未選択"}
        </Text>
        <TextInput
          key={`command-${sessionKey}`}
          label="コマンド"
          defaultValue="/status "
          onChangeText={handleChangeText}
          mode="outlined"
          style={styles.dialogInput}
          autoCapitalize="none"
          autoCorrect={false}
          placeholder="/status in"
          onSubmitEditing={() => onSubmit(draft)}
        />
        <Text style={styles.commandHint}>
          利用可能: /status open|in|in_progress|closed|cancelled
        </Text>
        {error ? <Text style={styles.commandError}>{error}</Text> : null}
      </Dialog.Content>
      <Dialog.Actions>
        <Button onPress={onDismiss} textColor="#a6adc8" disabled={busy}>
          キャンセル
        </Button>
        <Button
          onPress={() => onSubmit(draft)}
          textColor="#7c3aed"
          loading={busy}
        >
          実行
        </Button>
      </Dialog.Actions>
    </Dialog>
  );
}

function normalizeTaskCommandStatus(raw: string): string | null {
  const normalized = raw.trim().toLowerCase();
  const map: Record<string, string> = {
    open: "open",
    todo: "open",
    new: "open",
    in: "in_progress",
    in_progress: "in_progress",
    progress: "in_progress",
    ip: "in_progress",
    wip: "in_progress",
    done: "closed",
    complete: "closed",
    completed: "closed",
    close: "closed",
    closed: "closed",
    cancel: "cancelled",
    cancelled: "cancelled",
    canceled: "cancelled",
  };
  return map[normalized] ?? null;
}

function parseTaskSlashCommand(input: string): { status: string } | null {
  const match = input.match(/^\s*\/status\s+(.+?)\s*$/i);
  if (!match) return null;
  const status = normalizeTaskCommandStatus(match[1]);
  return status ? { status } : null;
}

export default function TaskListScreen() {
  const router = useRouter();
  const {
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    selectedSpace,
    projects,
    setSelectedProjectId,
    refreshProjects,
  } = useProject();
  const { isAuthenticated, isAnonymous, user } = useAuth();
  const authScope = isAuthenticated
    ? `auth:${user?.user_id ?? "unknown"}`
    : isAnonymous
      ? "anonymous"
      : "signed_out";
  const [tasks, setTasks] = useState<Task[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [showFuture, setShowFuture] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // 作成フォーム
  const [showCreate, setShowCreate] = useState(false);
  const completionRefreshToken = useTaskCompletionUndoStore(
    (state) => state.refreshToken,
  );
  const [showCommandDialog, setShowCommandDialog] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [commandBusy, setCommandBusy] = useState(false);
  const [statusBusyIds, setStatusBusyIds] = useState<Set<string>>(new Set());
  const [statusMenuTaskId, setStatusMenuTaskId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [draggingTaskId, setDraggingTaskId] = useState<string | null>(null);
  const [dragOverTaskId, setDragOverTaskId] = useState<string | null>(null);
  const rowLayoutsRef = useRef(new Map<string, RowLayout>());
  const listFrameRef = useRef<View | null>(null);
  const listWindowYRef = useRef(0);
  const listScrollYRef = useRef(0);
  const tasksRef = useRef<Task[]>([]);
  const filteredTasksRef = useRef<Task[]>([]);
  const dragOriginalTasksRef = useRef<Task[]>([]);
  const dragOriginalOrderRef = useRef<string[]>([]);
  const dragCurrentOrderRef = useRef<string[]>([]);
  const dragActiveRef = useRef(false);
  const dragSaveBusyRef = useRef(false);

  const loadTasks = useCallback(async () => {
    try {
      const list = selectedProjectId
        ? await tasksRepo.list(selectedProjectId)
        : await tasksRepo.listByScope(
            selectedSpaceId ? { space_id: selectedSpaceId } : {},
          );
      setTasks(list);
      setLoadError(null);
    } catch (error) {
      setTasks([]);
      setLoadError(
        error instanceof Error ? error.message : "タスク取得に失敗しました",
      );
    }
  }, [authScope, selectedProjectId, selectedSpaceId]);

  useFocusEffect(
    useCallback(() => {
      void refreshProjects();
      void loadTasks();
    }, [loadTasks, refreshProjects]),
  );

  useEffect(() => {
    void loadTasks();
  }, [completionRefreshToken, loadTasks]);

  useEffect(() => {
    setSelectedIds((prev) => {
      const next = new Set(
        [...prev].filter((taskId) => tasks.some((task) => task.id === taskId)),
      );
      return next.size === prev.size ? prev : next;
    });
  }, [tasks]);

  const onRefresh = async () => {
    setRefreshing(true);
    await refreshProjects();
    await loadTasks();
    setRefreshing(false);
  };

  const filteredTasks = useMemo(() => {
    let result =
      filter === "all"
        ? tasks.filter((task) => !COMPLETED_TASK_STATUSES.has(task.status))
        : filter === "closed"
          ? tasks.filter((task) => COMPLETED_TASK_STATUSES.has(task.status))
          : tasks.filter((task) => task.status === filter);

    if (!showFuture) {
      result = result.filter((task) => !isFutureTask(task));
    }

    const query = search.trim().toLowerCase();
    if (query) {
      result = result.filter(
        (task) =>
          task.title.toLowerCase().includes(query) ||
          task.description?.toLowerCase().includes(query) ||
          task.project_name?.toLowerCase().includes(query) ||
          task.tags.some((tag) => tag.name.toLowerCase().includes(query)),
      );
    }

    return result;
  }, [filter, search, showFuture, tasks]);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => {
    filteredTasksRef.current = filteredTasks;
  }, [filteredTasks]);

  const measureListFrame = useCallback(() => {
    requestAnimationFrame(() => {
      listFrameRef.current?.measureInWindow((_x, y) => {
        listWindowYRef.current = y;
      });
    });
  }, []);

  const handleTaskRowLayout = useCallback(
    (taskId: string, event: LayoutChangeEvent) => {
      const { y, height } = event.nativeEvent.layout;
      rowLayoutsRef.current.set(taskId, { y, height });
    },
    [],
  );

  const getReorderableTasks = useCallback(
    (source: Task[]) => {
      const spaceProjectIds = selectedSpaceId
        ? new Set(
            projects
              .filter((project) => project.space_id === selectedSpaceId)
              .map((project) => project.id),
          )
        : null;
      return source.filter((task) => {
        if (task.parent_task_id) return false;
        if (selectedProjectId) return task.project_id === selectedProjectId;
        if (spaceProjectIds) return spaceProjectIds.has(task.project_id);
        return true;
      });
    },
    [projects, selectedProjectId, selectedSpaceId],
  );

  const applyOrderToTasks = useCallback(
    (source: Task[], orderedIds: string[]) => {
      const order = new Map(orderedIds.map((taskId, index) => [taskId, index]));
      const reorderableIds = new Set(
        getReorderableTasks(source).map((task) => task.id),
      );
      const reordered = [...source].sort((a, b) => {
        const aOrder = order.get(a.id);
        const bOrder = order.get(b.id);
        if (aOrder != null && bOrder != null) return aOrder - bOrder;
        if (reorderableIds.has(a.id) && reorderableIds.has(b.id)) {
          if (aOrder != null) return -1;
          if (bOrder != null) return 1;
        }
        return 0;
      });
      return reordered.map((task) => {
        const nextOrder = order.get(task.id);
        return nextOrder == null ? task : { ...task, sort_order: nextOrder };
      });
    },
    [getReorderableTasks],
  );

  const findDragTarget = useCallback((pageY: number) => {
    const visibleIds = filteredTasksRef.current
      .filter((task) => !task.parent_task_id)
      .map((task) => task.id);
    const contentY = pageY - listWindowYRef.current + listScrollYRef.current;
    let nearest: {
      taskId: string;
      distance: number;
      insertAfter: boolean;
    } | null = null;

    for (const taskId of visibleIds) {
      const layout = rowLayoutsRef.current.get(taskId);
      if (!layout) continue;
      const mid = layout.y + layout.height / 2;
      const distance = Math.abs(contentY - mid);
      const candidate = {
        taskId,
        distance,
        insertAfter: contentY >= mid,
      };
      if (contentY >= layout.y && contentY <= layout.y + layout.height) {
        return candidate;
      }
      if (!nearest || distance < nearest.distance) nearest = candidate;
    }

    return nearest;
  }, []);

  const updateDragPosition = useCallback(
    (pageY: number) => {
      if (!dragActiveRef.current || !draggingTaskId) return;
      const target = findDragTarget(pageY);
      if (!target || target.taskId === draggingTaskId) return;
      const nextOrder = reorderIds(
        dragCurrentOrderRef.current,
        draggingTaskId,
        target.taskId,
        target.insertAfter,
      );
      if (sameOrder(nextOrder, dragCurrentOrderRef.current)) return;
      dragCurrentOrderRef.current = nextOrder;
      setDragOverTaskId(target.taskId);
      setTasks((current) => applyOrderToTasks(current, nextOrder));
    },
    [applyOrderToTasks, draggingTaskId, findDragTarget],
  );

  const beginTaskDrag = useCallback(
    (task: Task, pageY: number) => {
      if (bulkBusy || dragSaveBusyRef.current) return;
      if (task.parent_task_id) {
        setActionMessage("サブタスクの並び替えはまだ対応していません");
        return;
      }
      const reorderableTasks = getReorderableTasks(tasksRef.current);
      const order = reorderableTasks.map((item) => item.id);
      if (order.length < 2 || !order.includes(task.id)) return;

      dragActiveRef.current = true;
      dragOriginalTasksRef.current = tasksRef.current;
      dragOriginalOrderRef.current = order;
      dragCurrentOrderRef.current = order;
      setDraggingTaskId(task.id);
      setDragOverTaskId(task.id);
      setSelectedIds(new Set());
      measureListFrame();
      updateDragPosition(pageY);
    },
    [
      bulkBusy,
      getReorderableTasks,
      measureListFrame,
      selectedProjectId,
      updateDragPosition,
    ],
  );

  const handleTaskTouchMove = useCallback(
    (event: GestureResponderEvent) => {
      if (!dragActiveRef.current) return;
      updateDragPosition(event.nativeEvent.pageY);
    },
    [updateDragPosition],
  );

  const finishTaskDrag = useCallback(async () => {
    if (!dragActiveRef.current || dragSaveBusyRef.current) return;

    const nextOrder = dragCurrentOrderRef.current;
    const previousOrder = dragOriginalOrderRef.current;
    const previousTasks = dragOriginalTasksRef.current;
    const changed = !sameOrder(previousOrder, nextOrder);

    dragActiveRef.current = false;
    setDraggingTaskId(null);
    setDragOverTaskId(null);
    dragOriginalOrderRef.current = [];
    dragCurrentOrderRef.current = [];

    if (!changed) return;

    dragSaveBusyRef.current = true;
    try {
      await tasksRepo.reorder(selectedProjectId, nextOrder);
      await loadTasks();
      setActionMessage("並び替えを保存しました");
    } catch (error) {
      setTasks(previousTasks);
      setActionMessage(
        error instanceof Error ? error.message : "並び替えの保存に失敗しました",
      );
    } finally {
      dragSaveBusyRef.current = false;
      dragOriginalTasksRef.current = [];
    }
  }, [loadTasks, selectedProjectId]);

  const selectedTasks = useMemo(
    () => tasks.filter((task) => selectedIds.has(task.id)),
    [selectedIds, tasks],
  );
  const selectedCommandTask = useMemo(
    () => (selectedTasks.length === 1 ? selectedTasks[0] : null),
    [selectedTasks],
  );

  const clearSelection = useCallback(() => setSelectedIds(new Set()), []);

  const closeTaskCommandDialog = useCallback(() => {
    setShowCommandDialog(false);
    setCommandError(null);
    setCommandBusy(false);
  }, []);

  const clearTaskCommandError = useCallback(() => {
    if (commandError) setCommandError(null);
  }, [commandError]);

  const toggleSelection = useCallback((taskId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  }, []);

  const handleCreate = useCallback(
    async (data: Record<string, unknown>) => {
      try {
        await tasksRepo.create(data);
        setShowCreate(false);
        await loadTasks();
      } catch (error) {
        setActionMessage(
          error instanceof Error ? error.message : "タスク作成に失敗しました",
        );
        throw error;
      }
    },
    [loadTasks],
  );

  const updateTaskStatus = useCallback(
    async (task: Task, next: string) => {
      if (task.status === next || statusBusyIds.has(task.id)) return;
      const previousTask = task;
      setStatusBusyIds((prev) => new Set(prev).add(task.id));
      setTasks((prev) =>
        prev.map((item) =>
          item.id === task.id
            ? {
                ...item,
                status: next,
                completed_at:
                  next === "closed"
                    ? (item.completed_at ?? new Date().toISOString())
                    : null,
              }
            : item,
        ),
      );
      try {
        await tasksRepo.update(task.id, { status: next });
        await loadTasks();
        if (isTaskCompletionTransition(previousTask.status, next)) {
          enqueueTaskCompletionUndoBatch({
            entries: [createTaskCompletionUndoEntry(previousTask)],
          });
        }
      } catch (error) {
        setTasks((prev) =>
          prev.map((item) =>
            item.id === previousTask.id ? previousTask : item,
          ),
        );
        setActionMessage(
          error instanceof Error
            ? error.message
            : "ステータス更新に失敗しました",
        );
      } finally {
        setStatusBusyIds((prev) => {
          const nextIds = new Set(prev);
          nextIds.delete(task.id);
          return nextIds;
        });
      }
    },
    [loadTasks, statusBusyIds],
  );

  const updateTaskStatusFromMenu = useCallback(
    async (task: Task, next: string) => {
      setStatusMenuTaskId(null);
      await updateTaskStatus(task, next);
    },
    [updateTaskStatus],
  );

  const handleStatusToggle = (task: Task) => {
    const next = task.status === "closed" ? "open" : "closed";
    void updateTaskStatus(task, next);
  };

  const handleBulkStatusToggle = async () => {
    if (!selectedTasks.length) return;
    setBulkBusy(true);
    try {
      const nextStatus = selectedTasks.some((task) => task.status !== "closed")
        ? "closed"
        : "open";
      const completedTasks = selectedTasks.filter((task) =>
        isTaskCompletionTransition(task.status, nextStatus),
      );
      await Promise.all(
        selectedTasks.map((task) =>
          tasksRepo.update(task.id, { status: nextStatus }),
        ),
      );
      clearSelection();
      await loadTasks();
      if (completedTasks.length > 0) {
        enqueueTaskCompletionUndoBatch({
          entries: completedTasks.map((task) =>
            createTaskCompletionUndoEntry(task),
          ),
        });
      }
    } catch (error) {
      setActionMessage(
        error instanceof Error ? error.message : "一括更新に失敗しました",
      );
    } finally {
      setBulkBusy(false);
    }
  };

  const handleBulkDuplicate = async () => {
    if (!selectedTasks.length) return;
    setBulkBusy(true);
    try {
      await Promise.all(
        selectedTasks.map((task) => {
          const payload: Record<string, unknown> = {
            project_id: task.project_id,
            title: task.title,
            description: task.description || "",
            status: task.status,
            priority: task.priority,
            start_at: task.start_at ?? null,
            end_at: task.end_at ?? null,
            all_day: task.all_day,
            notifications_enabled: task.notifications_enabled,
            metadata: task.metadata ?? {},
          };
          if (task.tags.length > 0) {
            payload.tag_ids = task.tags.map((tag) => tag.id);
          }
          return tasksRepo.create(payload);
        }),
      );
      clearSelection();
      await loadTasks();
    } catch (error) {
      setActionMessage(
        error instanceof Error ? error.message : "複製に失敗しました",
      );
    } finally {
      setBulkBusy(false);
    }
  };

  const handleBulkDelete = useCallback(() => {
    if (!selectedTasks.length) return;
    Alert.alert(
      "タスク削除",
      `${selectedTasks.length}件のタスクを削除しますか？`,
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "削除",
          style: "destructive",
          onPress: async () => {
            setBulkBusy(true);
            try {
              await Promise.all(
                selectedTasks.map((task) => tasksRepo.delete(task.id)),
              );
              clearSelection();
              await loadTasks();
            } catch (error) {
              setActionMessage(
                error instanceof Error ? error.message : "削除に失敗しました",
              );
            } finally {
              setBulkBusy(false);
            }
          },
        },
      ],
    );
  }, [clearSelection, loadTasks, selectedTasks]);

  const handleTaskCommand = useCallback(
    async (commandValue: string) => {
      if (!selectedCommandTask) {
        setCommandError("1件だけ選択してください");
        return;
      }
      const parsed = parseTaskSlashCommand(commandValue);
      if (!parsed) {
        setCommandError("`/status ...` を入力してください");
        return;
      }

      setCommandBusy(true);
      setCommandError(null);
      try {
        if (parsed.status !== selectedCommandTask.status) {
          await tasksRepo.update(selectedCommandTask.id, {
            status: parsed.status,
          });
          await loadTasks();
        }
        closeTaskCommandDialog();
      } catch {
        setCommandError("コマンド実行に失敗しました");
      } finally {
        setCommandBusy(false);
      }
    },
    [closeTaskCommandDialog, loadTasks, selectedCommandTask],
  );

  const handleTaskPress = useCallback(
    (task: Task) => {
      if (dragActiveRef.current || dragSaveBusyRef.current) return;
      if (selectedIds.size > 0) {
        toggleSelection(task.id);
        return;
      }
      router.push(`/(tabs)/tasks/${task.id}`);
    },
    [router, selectedIds.size, toggleSelection],
  );

  const renderItem = ({ item }: { item: Task }) => {
    const selected = selectedIds.has(item.id);

    return (
      <Surface
        style={[
          styles.taskCard,
          selected && styles.taskCardSelected,
          draggingTaskId === item.id && styles.taskCardDragging,
          dragOverTaskId === item.id &&
            draggingTaskId !== item.id &&
            styles.taskCardDropTarget,
        ]}
        elevation={0}
        onLayout={(event) => handleTaskRowLayout(item.id, event)}
      >
        <View style={styles.taskRow}>
          <IconButton
            icon={item.status === "closed" ? "check-circle" : "circle-outline"}
            iconColor={STATUS_COLORS[item.status] || "#a6adc8"}
            size={24}
            mode="contained-tonal"
            disabled={statusBusyIds.has(item.id)}
            onPress={() => handleStatusToggle(item)}
            onLongPress={() => toggleSelection(item.id)}
            style={styles.statusButton}
            accessibilityLabel={`${item.title}を完了/未完了に切り替え`}
          />
          <Pressable
            style={({ pressed }) => [
              styles.taskContent,
              pressed && styles.taskContentPressed,
            ]}
            onPress={() => handleTaskPress(item)}
            onLongPress={(event) =>
              beginTaskDrag(item, event.nativeEvent.pageY)
            }
            delayLongPress={280}
          >
            <View style={styles.taskTitleRow}>
              <Text
                style={[
                  styles.taskTitle,
                  item.status === "closed" && styles.taskTitleDone,
                ]}
                numberOfLines={2}
              >
                {item.title}
              </Text>
              {selected ? (
                <Chip
                  compact
                  style={styles.selectedChip}
                  textStyle={styles.selectedChipText}
                >
                  選択中
                </Chip>
              ) : null}
            </View>
            <View style={styles.taskMeta}>
              <View
                style={[
                  styles.priorityDot,
                  {
                    backgroundColor:
                      PRIORITY_COLORS[item.priority] || "#a6adc8",
                  },
                ]}
              />
              <Menu
                visible={statusMenuTaskId === item.id}
                onDismiss={() => setStatusMenuTaskId(null)}
                anchor={
                  <Chip
                    compact
                    icon={
                      STATUS_OPTIONS.find(
                        (option) => option.value === item.status,
                      )?.icon || "circle-outline"
                    }
                    disabled={selected || statusBusyIds.has(item.id)}
                    onPress={() => setStatusMenuTaskId(item.id)}
                    style={styles.currentStatusChip}
                    textStyle={[
                      styles.currentStatusChipText,
                      { color: STATUS_COLORS[item.status] || "#a6adc8" },
                    ]}
                  >
                    {STATUS_LABELS[item.status] || item.status}
                  </Chip>
                }
              >
                {STATUS_OPTIONS.map((option) => (
                  <Menu.Item
                    key={option.value}
                    leadingIcon={option.icon}
                    title={option.label}
                    disabled={
                      item.status === option.value || statusBusyIds.has(item.id)
                    }
                    onPress={() =>
                      void updateTaskStatusFromMenu(item, option.value)
                    }
                  />
                ))}
              </Menu>
              {item.end_at ? (
                <Text style={styles.dueDate}>
                  〆{" "}
                  {formatTaskDateLabel(item.end_at, {
                    allDay: item.all_day,
                    absoluteStyle: "short",
                  })}
                </Text>
              ) : null}
              {item.active_time_entry ? (
                <Chip
                  compact
                  style={styles.timerChip}
                  textStyle={styles.timerChipText}
                >
                  ⏱ 計測中
                </Chip>
              ) : null}
            </View>
            {!selectedProjectId && item.project_name ? (
              <Text style={styles.projectText}>{item.project_name}</Text>
            ) : null}
            {item.tags && item.tags.length > 0 ? (
              <View style={styles.tagRow}>
                {item.tags.map((tag) => (
                  <View
                    key={tag.id}
                    style={[
                      styles.tagDot,
                      { backgroundColor: tag.color || "#45475a" },
                    ]}
                  >
                    <Text style={styles.tagDotText}>{tag.name}</Text>
                  </View>
                ))}
              </View>
            ) : null}
          </Pressable>
        </View>
      </Surface>
    );
  };

  return (
    <View
      style={styles.container}
      onTouchMove={handleTaskTouchMove}
      onTouchEnd={() => {
        void finishTaskDrag();
      }}
      onTouchCancel={() => {
        void finishTaskDrag();
      }}
    >
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerTop}>
          <View style={styles.headerCopy}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              タスク
            </Text>
            <Text style={styles.headerSubtext}>
              {selectedProject?.name || "すべてのプロジェクト"}
              {selectedSpace && !selectedProject
                ? ` / ${selectedSpace.name}`
                : ""}
            </Text>
          </View>
          <Menu
            visible={projectMenuVisible}
            onDismiss={() => setProjectMenuVisible(false)}
            anchor={
              <Chip
                compact
                onPress={() => {
                  setProjectMenuVisible(true);
                  void refreshProjects();
                }}
                style={styles.projectChip}
                textStyle={styles.projectChipText}
              >
                プロジェクト
              </Chip>
            }
            contentStyle={styles.menuContent}
          >
            <Menu.Item
              title="すべてのプロジェクト"
              leadingIcon={!selectedProjectId ? "check" : undefined}
              onPress={() => {
                setProjectMenuVisible(false);
                setSelectedProjectId(null);
              }}
            />
            {projects.map((project) => (
              <Menu.Item
                key={project.id}
                title={project.name}
                leadingIcon={
                  project.id === selectedProjectId ? "check" : undefined
                }
                onPress={() => {
                  setProjectMenuVisible(false);
                  setSelectedProjectId(project.id);
                }}
              />
            ))}
          </Menu>
        </View>

        <TextInput
          value={search}
          onChangeText={setSearch}
          mode="outlined"
          dense
          placeholder="タスク・タグ・プロジェクトを検索"
          style={styles.searchInput}
          left={<TextInput.Icon icon="magnify" />}
        />

        <View style={styles.filterRow}>
          {FILTERS.map((item) => (
            <Chip
              key={item}
              selected={filter === item}
              onPress={() => setFilter(item)}
              compact
              style={[
                styles.filterChip,
                filter === item && styles.filterChipActive,
              ]}
              textStyle={[
                styles.filterChipText,
                filter === item && styles.filterChipTextActive,
              ]}
            >
              {FILTER_LABELS[item]}
            </Chip>
          ))}
          <Chip
            selected={showFuture}
            onPress={() => setShowFuture((value) => !value)}
            compact
            style={[styles.filterChip, showFuture && styles.filterChipActive]}
            textStyle={[
              styles.filterChipText,
              showFuture && styles.filterChipTextActive,
            ]}
          >
            未来も表示
          </Chip>
        </View>

        {selectedIds.size > 0 ? (
          <View style={styles.bulkBar}>
            <Text style={styles.bulkText}>{selectedIds.size}件選択中</Text>
            <View style={styles.bulkActions}>
              {selectedCommandTask ? (
                <Button
                  compact
                  mode="outlined"
                  textColor="#cdd6f4"
                  onPress={() => {
                    setCommandError(null);
                    setShowCommandDialog(true);
                  }}
                  disabled={bulkBusy || commandBusy}
                >
                  コマンド
                </Button>
              ) : null}
              <Button
                compact
                mode="outlined"
                textColor="#a6e3a1"
                onPress={handleBulkStatusToggle}
                disabled={bulkBusy}
              >
                完了/戻す
              </Button>
              <Button
                compact
                mode="outlined"
                textColor="#89b4fa"
                onPress={handleBulkDuplicate}
                disabled={bulkBusy}
              >
                複製
              </Button>
              <Button
                compact
                mode="outlined"
                textColor="#f38ba8"
                onPress={handleBulkDelete}
                disabled={bulkBusy}
              >
                削除
              </Button>
              <Button compact textColor="#a6adc8" onPress={clearSelection}>
                解除
              </Button>
            </View>
          </View>
        ) : (
          <Text style={styles.hintText}>
            タップで開く / 長押しで並び替え / ステータス長押しで複数選択
          </Text>
        )}
      </Surface>

      <View
        ref={listFrameRef}
        style={styles.listFrame}
        onLayout={measureListFrame}
      >
        <FlatList
          data={filteredTasks}
          keyExtractor={(item) => item.id}
          renderItem={renderItem}
          ItemSeparatorComponent={() => <View style={{ height: 4 }} />}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor="#7c3aed"
            />
          }
          onScroll={(event) => {
            listScrollYRef.current = event.nativeEvent.contentOffset.y;
          }}
          scrollEventThrottle={16}
          contentContainerStyle={styles.listContent}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={styles.emptyText}>
                {loadError ?? "タスクがありません"}
              </Text>
            </View>
          }
        />
      </View>

      <FAB
        icon="plus"
        style={styles.fab}
        onPress={() => setShowCreate(true)}
        color="#cdd6f4"
      />

      <Portal>
        <TaskCreateDialog
          visible={showCreate}
          selectedProjectId={selectedProjectId}
          projects={projects}
          onDismiss={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
        <Snackbar
          visible={!!actionMessage}
          onDismiss={() => setActionMessage(null)}
          duration={3500}
          style={styles.snackbar}
        >
          {actionMessage}
        </Snackbar>
        <TaskCommandDialog
          visible={showCommandDialog}
          targetTitle={selectedCommandTask?.title ?? null}
          error={commandError}
          busy={commandBusy}
          onDismiss={closeTaskCommandDialog}
          onSubmit={(value) => {
            void handleTaskCommand(value);
          }}
          onChange={clearTaskCommandError}
        />
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: {
    padding: 16,
    paddingTop: 56,
    backgroundColor: "#1e1e2e",
    gap: 12,
  },
  headerTop: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
  },
  headerCopy: { flex: 1 },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  projectChip: { backgroundColor: "#313244" },
  projectChipText: { color: "#cdd6f4" },
  searchInput: { backgroundColor: "transparent" },
  filterRow: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  filterChip: { backgroundColor: "#313244" },
  filterChipActive: { backgroundColor: "#4c1d95" },
  filterChipText: { color: "#a6adc8", fontSize: 12 },
  filterChipTextActive: { color: "#cdd6f4", fontSize: 12 },
  bulkBar: { gap: 10 },
  bulkText: { color: "#cdd6f4", fontSize: 13, fontWeight: "600" },
  bulkActions: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  hintText: { color: "#9399b2", fontSize: 12 },
  listFrame: { flex: 1 },
  listContent: { padding: 8 },
  taskCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 10,
    marginHorizontal: 4,
    borderWidth: 1,
    borderColor: "transparent",
  },
  taskCardSelected: {
    borderColor: "#7c3aed",
    backgroundColor: "#221b3b",
  },
  taskCardDragging: {
    borderColor: "#89b4fa",
    backgroundColor: "#1b2b45",
    opacity: 0.82,
  },
  taskCardDropTarget: {
    borderColor: "#89b4fa",
  },
  taskRow: { flexDirection: "row", alignItems: "flex-start", padding: 8 },
  statusButton: { margin: 0, marginRight: 4, backgroundColor: "#313244" },
  taskContent: { flex: 1, paddingVertical: 4 },
  taskContentPressed: { opacity: 0.75 },
  taskTitleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 8,
  },
  taskTitle: { color: "#cdd6f4", fontSize: 15, marginBottom: 6, flex: 1 },
  taskTitleDone: { textDecorationLine: "line-through", color: "#a6adc8" },
  selectedChip: { backgroundColor: "#7c3aed", height: 22 },
  selectedChipText: { color: "#f5e9ff", fontSize: 10 },
  taskMeta: { flexDirection: "row", alignItems: "center", gap: 8 },
  priorityDot: { width: 8, height: 8, borderRadius: 4 },
  currentStatusChip: { backgroundColor: "#181825", height: 28 },
  currentStatusChipText: { fontSize: 11, fontWeight: "700" },
  dueDate: { color: "#a6adc8", fontSize: 11 },
  timerChip: { backgroundColor: "#4c1d95", height: 22 },
  timerChipText: { color: "#f9e2af", fontSize: 10 },
  projectText: { color: "#bac2de", fontSize: 11, marginTop: 6 },
  tagRow: { flexDirection: "row", flexWrap: "wrap", gap: 4, marginTop: 6 },
  tagDot: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 4 },
  tagDotText: { color: "#cdd6f4", fontSize: 10 },
  fab: {
    position: "absolute",
    right: 16,
    bottom: 16,
    backgroundColor: "#7c3aed",
  },
  empty: { alignItems: "center", paddingTop: 60 },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogInput: { marginBottom: 12 },
  dialogRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 12,
  },
  dialogTwoColumn: { flexDirection: "row", gap: 10, marginBottom: 10 },
  dialogColumn: { flex: 1 },
  dialogLabel: { color: "#a6adc8", fontSize: 14 },
  dialogSectionLabel: { color: "#a6adc8", fontSize: 13, marginBottom: 8 },
  dialogChipRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    marginBottom: 12,
  },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    minHeight: 54,
  },
  switchRowFull: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 12,
  },
  newTagRow: {
    flexDirection: "row",
    gap: 8,
    alignItems: "center",
    marginBottom: 8,
  },
  newTagInput: { flex: 1, backgroundColor: "transparent" },
  commandTarget: { color: "#cdd6f4", marginBottom: 12 },
  commandHint: { color: "#9399b2", fontSize: 12 },
  commandError: { color: "#f38ba8", fontSize: 12, marginTop: 8 },
  selectChip: { backgroundColor: "#313244" },
  menuContent: { backgroundColor: "#1e1e2e" },
  snackbar: { backgroundColor: "#313244" },
});
