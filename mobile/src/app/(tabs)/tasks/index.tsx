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
  PanResponder,
  Pressable,
  RefreshControl,
  StyleSheet,
  type GestureResponderEvent,
  type LayoutChangeEvent,
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
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useRouter } from "expo-router";
import { tasksRepo } from "../../../repositories";
import { runSync } from "../../../sync/engine";
import { useProjectStore } from "../../../stores/project";
import { useProject } from "../../../contexts/ProjectContext";
import { useAuth } from "../../../contexts/AuthContext";
import type { Task } from "../../../types/api";
import { formatTaskDateLabel } from "../../../lib/task-date-label";
import {
  COMPLETED_TASK_STATUSES,
  isFutureTask,
} from "../../../lib/task-visibility";
import {
  getTaskStatusOption,
  areTaskOrdersEqual,
  reorderVisibleTaskIds,
  resolveTaskPressAction,
  sortTasksCanonical,
  TASK_STATUS_OPTIONS,
} from "../../../features/tasks/task-list-state";
import {
  createTaskCompletionUndoEntry,
  enqueueTaskCompletionUndoBatch,
  isTaskCompletionTransition,
  useTaskCompletionUndoStore,
} from "../../../stores/task-completion-undo";
import { ScreenHeader } from "../../../components/screen-header";
import { ScopeSwitcher } from "../../../components/scope-switcher";

const FILTERS = ["all", "open", "in_progress", "closed"] as const;
const FILTER_LABELS: Record<string, string> = {
  all: "すべて",
  open: "未着手",
  in_progress: "進行中",
  closed: "完了済み",
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
          利用可能: /status open|in|in_progress|on_hold|review|closed
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
    on_hold: "on_hold",
    hold: "on_hold",
    review: "review",
    done: "closed",
    complete: "closed",
    completed: "closed",
    close: "closed",
    closed: "closed",
  };
  return map[normalized] ?? null;
}

function parseTaskSlashCommand(input: string): { status: string } | null {
  const match = input.match(/^\s*\/status\s+(.+?)\s*$/i);
  if (!match) return null;
  const status = normalizeTaskCommandStatus(match[1]);
  return status ? { status } : null;
}

const TASK_LONG_PRESS_DELAY = 350;
const TASK_TOUCH_SLOP = 8;

type TaskCardProps = {
  item: Task;
  selected: boolean;
  selectedProjectId: string | null;
  statusOption: ReturnType<typeof getTaskStatusOption>;
  statusBusy: boolean;
  statusMenuVisible: boolean;
  dragging: boolean;
  onLayout: (taskId: string, event: LayoutChangeEvent) => void;
  onPress: (task: Task) => void;
  onLongPress: (taskId: string) => void;
  onDragStart: (
    taskId: string,
    pageY: number,
    locationY: number,
  ) => void;
  onDragMove: (taskId: string, pageY: number) => void;
  onDragEnd: (taskId: string, pageY: number) => void;
  onStatusMenuDismiss: () => void;
  onStatusMenuOpen: () => void;
  onStatusUpdate: (task: Task, status: string) => void;
};

function readTouchPosition(event: unknown): {
  pageY: number;
  locationY: number;
} {
  const nativeEvent = (
    event as {
      nativeEvent?: { pageY?: unknown; locationY?: unknown };
    }
  )?.nativeEvent;
  const pageY =
    typeof nativeEvent?.pageY === "number" ? nativeEvent.pageY : 0;
  const locationY =
    typeof nativeEvent?.locationY === "number" ? nativeEvent.locationY : 0;
  return { pageY, locationY };
}

/**
 * Task row touch handling.
 *
 * PressableのlongPressだけに頼ると、指を離した後にもう一度drag操作を
 * 始める必要がある。row自身でタイマーとPanResponderを併用し、長押しが
 *成立した同じtouch sequenceをそのままD&Dへ引き継ぐ。
 */
function TaskCard({
  item,
  selected,
  selectedProjectId,
  statusOption,
  statusBusy,
  statusMenuVisible,
  dragging,
  onLayout,
  onPress,
  onLongPress,
  onDragStart,
  onDragMove,
  onDragEnd,
  onStatusMenuDismiss,
  onStatusMenuOpen,
  onStatusUpdate,
}: TaskCardProps) {
  const touchRef = useRef({
    active: false,
    cancelled: false,
    longPressed: false,
    dragArmed: false,
    dragActive: false,
    suppressNextPress: false,
    startPageY: 0,
    timer: null as ReturnType<typeof setTimeout> | null,
  });
  const callbacksRef = useRef({
    onLongPress,
    onDragStart,
    onDragMove,
    onDragEnd,
  });
  callbacksRef.current = { onLongPress, onDragStart, onDragMove, onDragEnd };

  const clearLongPressTimer = useCallback(() => {
    const timer = touchRef.current.timer;
    if (timer !== null) {
      clearTimeout(timer);
      touchRef.current.timer = null;
    }
  }, []);

  const startDrag = useCallback(
    (pageY: number, locationY: number) => {
      const touch = touchRef.current;
      if (!touch.active || touch.cancelled || !touch.longPressed) return;
      if (touch.dragActive) return;
      touch.dragActive = true;
      touch.suppressNextPress = true;
      callbacksRef.current.onDragStart(item.id, pageY, locationY);
    },
    [item.id],
  );

  const finishDrag = useCallback(
    (pageY: number) => {
      const touch = touchRef.current;
      if (!touch.dragActive) return;
      touch.dragActive = false;
      touch.dragArmed = false;
      touch.suppressNextPress = true;
      callbacksRef.current.onDragEnd(item.id, pageY);
    },
    [item.id],
  );

  const beginLongPress = useCallback(() => {
    const touch = touchRef.current;
    if (touch.longPressed || touch.cancelled) return;
    // テスト環境や一部のnative responderではonPressInが省略されて
    // onLongPressだけ届くことがある。その場合も同じtouchとしてdragを
    // 継続できるようactiveを補う。
    if (!touch.active) {
      touch.active = true;
      touch.startPageY = 0;
    }
    touch.longPressed = true;
    touch.dragArmed = true;
    clearLongPressTimer();
    callbacksRef.current.onLongPress(item.id);
  }, [clearLongPressTimer, item.id]);

  const beginTouch = useCallback(
    (event: unknown) => {
      const position = readTouchPosition(event);
      const touch = touchRef.current;
      clearLongPressTimer();
      touch.active = true;
      touch.cancelled = false;
      touch.longPressed = false;
      touch.dragArmed = false;
      touch.dragActive = false;
      touch.suppressNextPress = false;
      touch.startPageY = position.pageY;
      touch.timer = setTimeout(beginLongPress, TASK_LONG_PRESS_DELAY);
    },
    [beginLongPress, clearLongPressTimer],
  );

  const moveTouch = useCallback(
    (event: unknown) => {
      const touch = touchRef.current;
      if (!touch.active) return;
      const position = readTouchPosition(event);
      if (!touch.longPressed) {
        if (
          Math.abs(position.pageY - touch.startPageY) > TASK_TOUCH_SLOP
        ) {
          touch.cancelled = true;
          clearLongPressTimer();
        }
        return;
      }
      startDrag(position.pageY, position.locationY);
      if (touch.dragActive) {
        callbacksRef.current.onDragMove(item.id, position.pageY);
      }
    },
    [clearLongPressTimer, item.id, startDrag],
  );

  const endTouch = useCallback(
    (event: unknown) => {
      const position = readTouchPosition(event);
      clearLongPressTimer();
      finishDrag(position.pageY);
      touchRef.current.active = false;
      touchRef.current.dragArmed = false;
    },
    [clearLongPressTimer, finishDrag],
  );

  const handlePress = useCallback(() => {
    if (touchRef.current.suppressNextPress) {
      touchRef.current.suppressNextPress = false;
      return;
    }
    onPress(item);
  }, [item, onPress]);

  const panResponder = useRef(
    PanResponder.create({
      onMoveShouldSetPanResponderCapture: () => touchRef.current.dragArmed,
      onMoveShouldSetPanResponder: () => touchRef.current.dragArmed,
      onPanResponderGrant: (event: GestureResponderEvent) => {
        const position = readTouchPosition(event);
        startDrag(position.pageY, position.locationY);
      },
      onPanResponderMove: (_event, gestureState) => {
        if (!touchRef.current.dragActive) return;
        callbacksRef.current.onDragMove(item.id, gestureState.moveY);
      },
      onPanResponderRelease: (_event, gestureState) => {
        finishDrag(gestureState.moveY);
        touchRef.current.active = false;
        touchRef.current.dragArmed = false;
        clearLongPressTimer();
      },
      onPanResponderTerminate: (_event, gestureState) => {
        finishDrag(gestureState.moveY);
        touchRef.current.active = false;
        touchRef.current.dragArmed = false;
        clearLongPressTimer();
      },
      onPanResponderTerminationRequest: () => false,
    }),
  ).current;

  return (
    <View
      onLayout={(event) => onLayout(item.id, event)}
      style={[styles.taskCard, selected && styles.taskCardSelected, dragging && styles.taskCardDragging]}
    >
      <Pressable
        {...panResponder.panHandlers}
        style={({ pressed }) => [
          styles.taskCardPressable,
          pressed && styles.taskCardPressed,
        ]}
        onPressIn={beginTouch}
        onPress={handlePress}
        onPressOut={endTouch}
        onTouchMove={moveTouch}
        onTouchEnd={endTouch}
        onLongPress={beginLongPress}
        delayLongPress={TASK_LONG_PRESS_DELAY}
        hitSlop={4}
        accessibilityRole="button"
        accessibilityState={{ selected }}
        accessibilityLabel={`${item.title}を開く`}
        accessibilityHint="長押しで選択し、そのままドラッグして並べ替えます"
      >
        <View style={styles.taskRow}>
          <View style={styles.statusButtonSpacer} />
          <View style={styles.taskContent} pointerEvents="none">
            <View style={styles.compactMainRow}>
              <Text
                style={[
                  styles.taskTitle,
                  item.status === "closed" && styles.taskTitleDone,
                ]}
                numberOfLines={1}
              >
                {item.title}
              </Text>
              {item.tags?.slice(0, 2).map((tag) => (
                <View
                  key={tag.id}
                  style={[
                    styles.compactTag,
                    { backgroundColor: tag.color || "#45475a" },
                  ]}
                >
                  <Text style={styles.compactTagText} numberOfLines={1}>
                    {tag.name}
                  </Text>
                </View>
              ))}
              {(item.tags?.length ?? 0) > 2 ? (
                <Text style={styles.moreTagsText}>+{item.tags.length - 2}</Text>
              ) : null}
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
            {item.start_at ||
            (!selectedProjectId && item.project_name) ||
            item.active_time_entry ? (
              <View style={styles.compactMetaRow}>
                {item.start_at ? (
                  <Text style={styles.dueDate} numberOfLines={1}>
                    {formatTaskDateLabel(item.start_at, {
                      allDay: item.all_day,
                      absoluteStyle: "short",
                    })}
                  </Text>
                ) : null}
                {!selectedProjectId && item.project_name ? (
                  <Text style={styles.projectText} numberOfLines={1}>
                    {item.project_name}
                  </Text>
                ) : null}
                {item.active_time_entry ? (
                  <Text style={styles.compactTimerText}>● 計測中</Text>
                ) : null}
              </View>
            ) : null}
          </View>
        </View>
      </Pressable>
      <View style={styles.statusAction}>
        <Menu
          visible={statusMenuVisible}
          onDismiss={onStatusMenuDismiss}
          anchor={
            <IconButton
              icon={statusOption?.icon || "circle-outline"}
              iconColor={statusOption?.color || "#a6adc8"}
              size={24}
              mode="contained-tonal"
              disabled={statusBusy}
              onPress={() => onStatusMenuOpen()}
              onLongPress={() => onLongPress(item.id)}
              style={styles.statusButton}
              accessibilityLabel={`${item.title}のステータスを選択`}
              accessibilityRole="button"
              hitSlop={4}
            />
          }
          contentStyle={styles.menuContent}
        >
          {TASK_STATUS_OPTIONS.map((option) => (
            <Menu.Item
              key={option.value}
              title={option.label}
              leadingIcon={item.status === option.value ? "check" : option.icon}
              disabled={item.status === option.value || statusBusy}
              onPress={() => onStatusUpdate(item, option.value)}
            />
          ))}
        </Menu>
      </View>
    </View>
  );
}

export default function TaskListScreen() {
  const router = useRouter();
  const {
    selectedProjectId,
    selectedProject,
    selectedSpaceId,
    selectedSpace,
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
  const [statusFilterMenuVisible, setStatusFilterMenuVisible] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
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
  const longPressedTaskIdRef = useRef<string | null>(null);
  const loadRequestRef = useRef(0);
  const tasksRef = useRef<Task[]>([]);
  const selectedIdsRef = useRef<Set<string>>(new Set());
  const filteredTasksRef = useRef<Task[]>([]);
  const taskLayoutsRef = useRef<Map<string, { y: number; height: number }>>(
    new Map(),
  );
  const dragRef = useRef<{
    taskId: string;
    selectedTaskIds: Set<string>;
    canonicalTaskIds: string[];
    visibleTaskIds: string[];
    targetVisibleIndex: number;
    sourcePageY: number;
    sourceLocationY: number;
    sourceLayoutY: number | null;
  } | null>(null);

  const loadTasks = useCallback(
    async (options?: { force?: boolean }) => {
    const requestId = ++loadRequestRef.current;
    try {
      const list = selectedProjectId
        ? await tasksRepo.list(selectedProjectId, options)
        : await tasksRepo.listByScope(
            selectedSpaceId ? { space_id: selectedSpaceId } : {},
            options,
          );
      if (requestId === loadRequestRef.current) {
        const canonical = sortTasksCanonical(list);
        tasksRef.current = canonical;
        setTasks(canonical);
        setLoadError(null);
      }
    } catch (error) {
      if (requestId === loadRequestRef.current) {
        setLoadError(
          error instanceof Error ? error.message : "タスク取得に失敗しました",
        );
      }
    }
  }, [authScope, selectedProjectId, selectedSpaceId]);

  useFocusEffect(
    useCallback(() => {
      let active = true;
      // フォーカス時はデルタ同期で鮮度を担保し、フル取得は throttle 側に委ねる。
      // 同期はSQLiteへ1行ずつ書き込むため、同時に読むと書き込み途中の中間状態を
      // 拾って毎回違う並びになる。同期完了後にもう一度読み直す。
      void (async () => {
        void loadTasks();
        try {
          await runSync();
        } catch {
          // 同期失敗はローカル表示を妨げない。
        }
        if (active) void loadTasks();
      })();
      void refreshProjects();
      return () => {
        active = false;
      };
    }, [loadTasks, refreshProjects]),
  );

  useEffect(() => {
    void loadTasks();
  }, [completionRefreshToken, loadTasks]);

  useEffect(() => {
    selectedIdsRef.current = selectedIds;
  }, [selectedIds]);

  useEffect(() => {
    setSelectedIds((prev) => {
      const next = new Set(
        [...prev].filter((taskId) => tasks.some((task) => task.id === taskId)),
      );
      selectedIdsRef.current = next;
      return next.size === prev.size ? prev : next;
    });
  }, [tasks]);

  const onRefresh = async () => {
    setRefreshing(true);
    // pull-to-refresh は throttle を無視してフル取得を強制する。
    await useProjectStore.getState().refreshProjects({ force: true });
    await loadTasks({ force: true });
    setRefreshing(false);
  };

  const filteredTasks = useMemo(() => {
    const topLevelTasks = tasks.filter((task) => !task.parent_task_id);
    let result =
      filter === "all"
        ? topLevelTasks.filter(
            (task) => !COMPLETED_TASK_STATUSES.has(task.status),
          )
        : filter === "closed"
          ? topLevelTasks.filter((task) =>
              COMPLETED_TASK_STATUSES.has(task.status),
            )
          : topLevelTasks.filter((task) => task.status === filter);

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

    return sortTasksCanonical(result);
  }, [filter, search, showFuture, tasks]);

  useEffect(() => {
    tasksRef.current = tasks;
    filteredTasksRef.current = filteredTasks;
  }, [filteredTasks, tasks]);

  const scopeLabel = selectedProject
    ? `プロジェクト: ${selectedProject.name}`
    : selectedSpace
      ? `スペース: ${selectedSpace.name}`
      : "すべてのプロジェクト";
  const scopeSwitcherLabel =
    selectedProject?.name ?? selectedSpace?.name ?? "全体";

  const selectedTasks = useMemo(
    () => tasks.filter((task) => selectedIds.has(task.id)),
    [selectedIds, tasks],
  );
  const selectedCommandTask = useMemo(
    () => (selectedTasks.length === 1 ? selectedTasks[0] : null),
    [selectedTasks],
  );

  const clearSelection = useCallback(() => {
    selectedIdsRef.current = new Set();
    setSelectedIds(new Set());
  }, []);

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
      selectedIdsRef.current = next;
      return next;
    });
  }, []);

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
      const action = resolveTaskPressAction({
        wasLongPress: longPressedTaskIdRef.current === task.id,
        selectionMode: selectedIds.size > 0,
      });
      longPressedTaskIdRef.current = null;
      if (action === "toggle-selection") {
        toggleSelection(task.id);
      } else if (action === "navigate") {
        router.push(`/(tabs)/tasks/${task.id}`);
      }
    },
    [router, selectedIds.size, toggleSelection],
  );

  const handleTaskLongPress = useCallback(
    (taskId: string) => {
      longPressedTaskIdRef.current = taskId;
      // 選択中のtaskを長押しした場合はselection blockを維持し、そのまま
      // block dragへ入る。未選択taskだけselectionへ追加する。
      if (!selectedIdsRef.current.has(taskId)) {
        toggleSelection(taskId);
      }
    },
    [toggleSelection],
  );

  const handleTaskLayout = useCallback(
    (taskId: string, event: LayoutChangeEvent) => {
      const { y, height } = event.nativeEvent.layout;
      taskLayoutsRef.current.set(taskId, { y, height });
    },
    [],
  );

  const beginTaskDrag = useCallback(
    (taskId: string, pageY: number, locationY: number) => {
      if (dragRef.current) return;
      const canonicalTaskIds = tasksRef.current
        .filter((task) => !task.parent_task_id)
        .map((task) => task.id);
      const visibleTaskIds = filteredTasksRef.current.map((task) => task.id);
      if (
        !canonicalTaskIds.includes(taskId) ||
        !visibleTaskIds.includes(taskId)
      ) {
        longPressedTaskIdRef.current = null;
        return;
      }
      const selectedTaskIds = new Set(
        visibleTaskIds.filter((id) => selectedIdsRef.current.has(id)),
      );
      selectedTaskIds.add(taskId);
      const sourceLayoutY = taskLayoutsRef.current.get(taskId)?.y ?? null;
      dragRef.current = {
        taskId,
        selectedTaskIds,
        canonicalTaskIds,
        visibleTaskIds,
        targetVisibleIndex: visibleTaskIds.indexOf(taskId),
        sourcePageY: pageY,
        sourceLocationY: locationY,
        sourceLayoutY,
      };
      setDraggingTaskId(taskId);
    },
    [],
  );

  const updateTaskDrag = useCallback((taskId: string, pageY: number) => {
    const drag = dragRef.current;
    if (!drag || drag.taskId !== taskId) return;

    const sourceIndex = drag.visibleTaskIds.indexOf(taskId);
    const sourceTop =
      drag.sourcePageY -
      drag.sourceLocationY -
      (drag.sourceLayoutY ?? sourceIndex * 56);
    const fallbackHeight =
      taskLayoutsRef.current.get(taskId)?.height ??
      [...taskLayoutsRef.current.values()][0]?.height ??
      56;
    let targetVisibleIndex = drag.visibleTaskIds.length;
    for (const [index, visibleTaskId] of drag.visibleTaskIds.entries()) {
      const layout = taskLayoutsRef.current.get(visibleTaskId);
      const top = layout ? sourceTop + layout.y : sourceTop + index * fallbackHeight;
      const height = layout?.height ?? fallbackHeight;
      if (pageY < top + height / 2) {
        targetVisibleIndex = index;
        break;
      }
    }
    drag.targetVisibleIndex = targetVisibleIndex;
  }, []);

  const applyTopLevelOrder = useCallback(
    (baseTasks: readonly Task[], orderedTopLevelIds: readonly string[]) => {
      const byId = new Map(baseTasks.map((task) => [task.id, task]));
      let topLevelIndex = 0;
      return baseTasks.map((task) => {
        if (task.parent_task_id) return task;
        const nextIndex = topLevelIndex++;
        const nextId = orderedTopLevelIds[nextIndex];
        const nextTask = nextId ? byId.get(nextId) : undefined;
        return nextTask
          ? { ...nextTask, sort_order: nextIndex }
          : task;
      });
    },
    [],
  );

  const finishTaskDrag = useCallback(
    async (taskId: string, _pageY: number) => {
      const drag = dragRef.current;
      if (!drag || drag.taskId !== taskId) return;
      dragRef.current = null;
      setDraggingTaskId(null);
      longPressedTaskIdRef.current = null;

      const nextCanonicalTaskIds = reorderVisibleTaskIds({
        canonicalTaskIds: drag.canonicalTaskIds,
        visibleTaskIds: drag.visibleTaskIds,
        selectedTaskIds: drag.selectedTaskIds,
        targetVisibleIndex: drag.targetVisibleIndex,
      });
      if (areTaskOrdersEqual(nextCanonicalTaskIds, drag.canonicalTaskIds)) {
        return;
      }

      const previousTasks = tasksRef.current;
      const optimisticTasks = applyTopLevelOrder(
        previousTasks,
        nextCanonicalTaskIds,
      );
      tasksRef.current = optimisticTasks;
      setTasks(optimisticTasks);
      try {
        await tasksRepo.reorder(
          selectedProjectId || null,
          nextCanonicalTaskIds,
        );
      } catch (error) {
        // local-first repositoryが成功した場合は警告を出さず、失敗時だけ
        // optimistic orderを元へ戻す。offline専用の文言は表示しない。
        if (tasksRef.current === optimisticTasks) {
          tasksRef.current = previousTasks;
          setTasks(previousTasks);
        }
        setActionMessage(
          error instanceof Error
            ? error.message
            : "並び替えの保存に失敗しました",
        );
      }
    },
    [applyTopLevelOrder, selectedProjectId],
  );

  const renderItem = ({ item }: { item: Task }) => {
    return (
      <TaskCard
        item={item}
        selected={selectedIds.has(item.id)}
        selectedProjectId={selectedProjectId}
        statusOption={getTaskStatusOption(item.status)}
        statusBusy={statusBusyIds.has(item.id)}
        statusMenuVisible={statusMenuTaskId === item.id}
        dragging={draggingTaskId === item.id}
        onLayout={handleTaskLayout}
        onPress={handleTaskPress}
        onLongPress={handleTaskLongPress}
        onDragStart={beginTaskDrag}
        onDragMove={updateTaskDrag}
        onDragEnd={finishTaskDrag}
        onStatusMenuDismiss={() => setStatusMenuTaskId(null)}
        onStatusMenuOpen={() => {
          if (longPressedTaskIdRef.current === item.id) {
            longPressedTaskIdRef.current = null;
            return;
          }
          longPressedTaskIdRef.current = null;
          setStatusMenuTaskId(item.id);
        }}
        onStatusUpdate={(task, status) => {
          void updateTaskStatusFromMenu(task, status);
        }}
      />
    );
  };

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="タスク"
        subtitle={scopeLabel}
        right={
          <ScopeSwitcher
            label={scopeSwitcherLabel}
            variant="chip"
            accessibilityLabel={`表示範囲: ${scopeSwitcherLabel}`}
          />
        }
      />
      <Surface style={styles.header} elevation={1}>
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
          <Menu
            visible={statusFilterMenuVisible}
            onDismiss={() => setStatusFilterMenuVisible(false)}
            anchor={
              <Chip
                compact
                icon="filter-variant"
                onPress={() => setStatusFilterMenuVisible(true)}
                style={styles.filterChip}
                textStyle={styles.filterChipText}
              >
                状態: {FILTER_LABELS[filter]}
              </Chip>
            }
            contentStyle={styles.menuContent}
          >
            {FILTERS.map((item) => (
              <Menu.Item
                key={item}
                title={FILTER_LABELS[item]}
                leadingIcon={filter === item ? "check" : undefined}
                onPress={() => {
                  setFilter(item);
                  setStatusFilterMenuVisible(false);
                }}
              />
            ))}
          </Menu>

          <Chip
            compact
            selected={showFuture}
            icon={showFuture ? "calendar-clock" : "calendar-outline"}
            onPress={() => setShowFuture((value) => !value)}
            style={styles.filterChip}
            textStyle={styles.filterChipText}
          >
            未来を表示
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
            タップで開く / 長押しで複数選択
          </Text>
        )}
      </Surface>

      <View style={styles.listFrame}>
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
        onPress={() =>
          router.push({
            pathname: "/(tabs)/tasks/create",
            params: {
              ...(selectedProjectId ? { projectId: selectedProjectId } : {}),
              ...(selectedSpaceId ? { spaceId: selectedSpaceId } : {}),
            },
          })
        }
        color="#cdd6f4"
      />

      <Portal>
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
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: "#1e1e2e",
    gap: 7,
  },
  displayConditionChip: { backgroundColor: "#313244", maxWidth: 190 },
  filterRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  filterChip: { backgroundColor: "#313244", maxWidth: 190 },
  filterChipText: { color: "#cdd6f4" },
  projectChip: { backgroundColor: "#313244" },
  scopeChip: { backgroundColor: "#181825" },
  scopeActions: { flexDirection: "row", alignItems: "center", gap: 6 },
  projectChipText: { color: "#cdd6f4" },
  searchInput: { backgroundColor: "transparent" },
  bulkBar: { gap: 10 },
  bulkText: { color: "#cdd6f4", fontSize: 13, fontWeight: "600" },
  bulkActions: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  hintText: { color: "#9399b2", fontSize: 11 },
  listFrame: { flex: 1 },
  listContent: { padding: 6, paddingBottom: 84 },
  taskCard: {
    backgroundColor: "#1e1e2e",
    borderRadius: 8,
    marginHorizontal: 2,
    borderWidth: 1,
    borderColor: "transparent",
    position: "relative",
  },
  taskCardSelected: {
    borderColor: "#7c3aed",
    backgroundColor: "#221b3b",
  },
  taskCardDragging: {
    borderColor: "#cba6f7",
    opacity: 0.82,
  },
  taskCardPressable: { borderRadius: 8 },
  taskCardPressed: { opacity: 0.75 },
  taskRow: { flexDirection: "row", alignItems: "center", padding: 4 },
  statusAction: { position: "absolute", left: 4, top: 4, zIndex: 1 },
  statusButtonSpacer: { width: 38, height: 36, flexShrink: 0 },
  statusButton: {
    margin: 0,
    marginRight: 2,
    backgroundColor: "transparent",
    width: 36,
    height: 36,
  },
  taskContent: { flex: 1, paddingVertical: 2, paddingRight: 5 },
  taskTitleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 8,
  },
  taskTitle: { color: "#cdd6f4", fontSize: 14, flex: 1, minWidth: 48 },
  taskTitleDone: { textDecorationLine: "line-through", color: "#a6adc8" },
  selectedChip: { backgroundColor: "#7c3aed", height: 22 },
  selectedChipText: { color: "#f5e9ff", fontSize: 10 },
  taskMeta: { flexDirection: "row", alignItems: "center", gap: 8 },
  currentStatusChip: { backgroundColor: "#181825", height: 28 },
  currentStatusChipText: { fontSize: 11, fontWeight: "700" },
  dueDate: { color: "#a6adc8", fontSize: 11 },
  timerChip: { backgroundColor: "#4c1d95", height: 22 },
  timerChipText: { color: "#f9e2af", fontSize: 10 },
  projectText: { color: "#bac2de", fontSize: 11, marginTop: 6 },
  tagRow: { flexDirection: "row", flexWrap: "wrap", gap: 4, marginTop: 6 },
  tagDot: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 4 },
  tagDotText: { color: "#cdd6f4", fontSize: 10 },
  compactMainRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    minHeight: 26,
  },
  compactTag: {
    maxWidth: 96,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 6,
  },
  compactTagText: { color: "#f5f5f7", fontSize: 12, fontWeight: "600" },
  moreTagsText: { color: "#bac2de", fontSize: 11, fontWeight: "600" },
  compactMetaRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    minHeight: 16,
  },
  compactTimerText: { color: "#f9e2af", fontSize: 10 },
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
  commandTarget: { color: "#cdd6f4", marginBottom: 12 },
  commandHint: { color: "#9399b2", fontSize: 12 },
  commandError: { color: "#f38ba8", fontSize: 12, marginTop: 8 },
  menuContent: { backgroundColor: "#1e1e2e" },
  snackbar: { backgroundColor: "#313244" },
});
