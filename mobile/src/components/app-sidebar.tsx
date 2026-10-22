import AsyncStorage from "@react-native-async-storage/async-storage";
import {
  filterChatSessions,
  filterChatSearchResults,
  groupChatSessionsByProject,
  isChatSessionUnread,
  isChatSessionWorking,
  sortChatSessions,
  type ChatHistoryFilter,
  type ChatHistoryView,
} from "../lib/chat-session-view";
import { ScopeSwitcher } from "./scope-switcher";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Animated,
  BackHandler,
  FlatList,
  PanResponder,
  Platform,
  Pressable,
  StyleSheet,
  TextInput as NativeTextInput,
  View,
  useWindowDimensions,
} from "react-native";
import { usePathname, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  ActivityIndicator,
  Divider,
  Icon,
  IconButton,
  List,
  SegmentedButtons,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { format } from "date-fns";
import { useAuth } from "../contexts/AuthContext";
import { useProject } from "../contexts/ProjectContext";
import {
  conversationsRepo,
  subscribeToConversationChanges,
} from "../repositories/conversations";
import { tasksRepo } from "../repositories/tasks";
import { scheduleSyncAfterInteractions } from "../lib/background-sync";
import { createCurrentCharacterSession } from "../features/characters/current-character";
import {
  animateNextListChange,
  areConversationListsEqual,
} from "../features/ui/list-transitions";
import { useReducedMotion } from "../features/ui/use-reduced-motion";
import { filesApi, type FilesEntry, type FilesSource } from "../lib/files-api";
import { COMPLETED_TASK_STATUSES, isFutureTask } from "../lib/task-visibility";
import type { ConversationSession, Task } from "../types/api";
import { AppSidebarContext } from "./app-sidebar-context";
import { SidebarSessionActions } from "./sidebar-session-actions";
import { ConversationHistoryFilter } from "./conversation-history-filter";
import { chatRepo } from "../repositories/chat";
import type { ConversationSearchResult } from "../lib/chat-api";

const CHAT_HISTORY_VIEW_KEY = "aoitalk-chat-history-view";
const CHAT_HISTORY_FILTER_KEY = "aoitalk-chat-history-status";
type ChatHistoryRow = { kind: "group"; key: string; label: string; count: number } | { kind: "session"; session: ConversationSession };

type SidebarTab = "chat" | "tasks" | "files";

function isChatHistoryFilter(value: string | null): value is ChatHistoryFilter {
  return value === "all" || value === "ready" || value === "needs_attention" || value === "working";
}

const EDGE_HIT_WIDTH = 28;
const MIN_OPEN_DISTANCE = 72;
const MAX_DRAWER_WIDTH = 340;
const SIDEBAR_BG = "#181825";
const ACTIVE_BG = "#313244";
const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";

function hasFocusedNativeTextInput(): boolean {
  return Boolean(NativeTextInput.State?.currentlyFocusedInput?.());
}

function isAndroidSidebarSwipeRoute(pathname: string): boolean {
  return [
    /^\/(?:\(tabs\)\/)?chat(?:\/[^/]+)?$/,
    /^\/(?:\(tabs\)\/)?tasks$/,
    /^\/(?:\(tabs\)\/)?filer$/,
    /^\/(?:\(tabs\)\/)?apps$/,
  ].some((pattern) => pattern.test(pathname));
}

function formatRelativeTime(dateStr?: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffMin < 1) return "たった今";
  if (diffMin < 60) return `${diffMin}分前`;
  if (diffHour < 24) return `${diffHour}時間前`;
  if (diffDay < 7) return `${diffDay}日前`;
  return format(date, "MM/dd");
}

function NavigationShortcut({
  icon,
  label,
  active,
  onPress,
}: {
  icon: string;
  label: string;
  active?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ selected: !!active }}
      style={({ pressed }) => [
        styles.navShortcut,
        active && styles.navRowActive,
        pressed && styles.navRowPressed,
      ]}
    >
      <Icon source={icon} size={22} color={active ? "#ffffff" : MUTED} />
    </Pressable>
  );
}

function ContextSwitcher() {
  return <View style={styles.contextBlock}><ScopeSwitcher /></View>;
}

function ChatSidebar({
  close,
  replaceCurrentChat,
  header,
}: {
  close: () => void;
  replaceCurrentChat: boolean;
  header: React.ReactElement;
}) {
  const router = useRouter();
  const { selectedProjectId, projects } = useProject();
  const pathname = usePathname();
  const activeSessionId = pathname.match(/^\/(?:\(tabs\)\/)?chat\/([^/]+)$/)?.[1] ?? null;
  const { user, isAuthenticated } = useAuth();
  const reduceMotion = useReducedMotion();
  const reduceMotionRef = useRef(reduceMotion);
  reduceMotionRef.current = reduceMotion;
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [historyView, setHistoryView] = useState<ChatHistoryView>("timeline");
  const [historyFilter, setHistoryFilter] = useState<ChatHistoryFilter>("all");
  const viewChangedRef = useRef(false);
  const filterChangedRef = useRef(false);
  const [actionTarget, setActionTarget] = useState<ConversationSession | null>(null);
  const longPressedSessionIdRef = useRef<string | null>(null);
  const actionTargetRef = useRef<ConversationSession | null>(null);
  actionTargetRef.current = actionTarget;
  const activeSessionIdRef = useRef(activeSessionId);
  activeSessionIdRef.current = activeSessionId;
  const mountedRef = useRef(true);
  const projectNameById = useMemo(() => new Map(projects.map((project) => [project.id, project.name])), [projects]);
  const filteredSessions = useMemo(
    () => filterChatSessions(sessions, historyFilter),
    [historyFilter, sessions],
  );
  const rows = useMemo<ChatHistoryRow[]>(() => historyView === "project"
    ? groupChatSessionsByProject(filteredSessions, projectNameById).flatMap((group) => [
        { kind: "group" as const, key: group.key, label: group.label, count: group.sessions.length },
        ...group.sessions.map((session) => ({ kind: "session" as const, session })),
      ])
    : sortChatSessions(filteredSessions).map((session) => ({ kind: "session" as const, session })), [filteredSessions, historyView, projectNameById]);

  useEffect(() => {
    mountedRef.current = true;
    void AsyncStorage.getItem(CHAT_HISTORY_VIEW_KEY).then((saved) => {
      if (mountedRef.current && !viewChangedRef.current && (saved === "timeline" || saved === "project")) setHistoryView(saved);
    }).catch(() => undefined);
    void AsyncStorage.getItem(CHAT_HISTORY_FILTER_KEY).then((saved) => {
      if (mountedRef.current && !filterChangedRef.current && isChatHistoryFilter(saved)) setHistoryFilter(saved);
    }).catch(() => undefined);
    return () => { mountedRef.current = false; };
  }, []);

  const changeHistoryView = (value: string) => {
    if (value !== "timeline" && value !== "project") return;
    viewChangedRef.current = true;
    setHistoryView(value);
    void AsyncStorage.setItem(CHAT_HISTORY_VIEW_KEY, value).catch(() => {
      if (mountedRef.current) Alert.alert("表示方法", "表示方法を保存できませんでした。今回の表示は維持されます。");
    });
  };
  const changeHistoryFilter = (value: ChatHistoryFilter) => {
    filterChangedRef.current = true;
    setHistoryFilter(value);
    void AsyncStorage.setItem(CHAT_HISTORY_FILTER_KEY, value).catch(() => {
      if (mountedRef.current) Alert.alert("履歴フィルター", "選択を保存できませんでした。今回の表示は維持されます。");
    });
  };
  const loadRequestRef = useRef(0);
  const syncTaskRef = useRef<(() => void) | null>(null);

  const loadSessions = useCallback(async () => {
    const requestId = ++loadRequestRef.current;

    const applyList = (list: ConversationSession[]) => {
      if (requestId !== loadRequestRef.current) return;
      setSessions((previous) => {
        if (areConversationListsEqual(previous, list)) return previous;
        animateNextListChange(reduceMotionRef.current);
        return list;
      });
    };

    try {
      applyList(await conversationsRepo.listSessionsLocal());
    } catch {
      // A sync can still repair a failed/stale local read.
    } finally {
      if (requestId === loadRequestRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    void loadSessions().then(() => {
      if (!mounted) return;
      syncTaskRef.current = scheduleSyncAfterInteractions(loadSessions);
    });
    return () => {
      mounted = false;
      loadRequestRef.current += 1;
      syncTaskRef.current?.();
      syncTaskRef.current = null;
    };
  }, [loadSessions]);

  useEffect(
    () => subscribeToConversationChanges(() => void loadSessions()),
    [loadSessions],
  );

  const openSessionActions = (session: ConversationSession) => {
    actionTargetRef.current = session;
    setActionTarget(session);
  };
  const refreshAfterAction = () => {
    void loadSessions();
    syncTaskRef.current?.();
    syncTaskRef.current = scheduleSyncAfterInteractions(() => mountedRef.current ? loadSessions() : undefined);
  };
  const openSession = (sessionId: string) => {
    void conversationsRepo.markSessionRead?.(sessionId);
    close();
    const href = `/(tabs)/chat/${sessionId}` as const;
    if (replaceCurrentChat) router.replace(href);
    else router.push(href);
  };

  const createSession = async () => {
    try {
      const session = await createCurrentCharacterSession(selectedProjectId, {
        localFirst: true,
        userId: user?.user_id,
      });
      close();
      router.push(`/(tabs)/chat/${session.id}`);
    } catch (error) {
      Alert.alert(
        "Chat",
        error instanceof Error ? error.message : "チャット開始に失敗しました。",
      );
    }
  };

  return (
    <View style={styles.sidebarContainer}>
      <FlatList
        style={styles.sidebarList}
        data={rows}
        keyExtractor={(item) => item.kind === "group" ? `group:${item.key}` : `session:${item.session.id}`}
        ItemSeparatorComponent={() => <Divider style={styles.divider} />}
        ListHeaderComponent={
          <View>
            {header}
            <View style={styles.groupHeader}>
              <Text style={styles.groupLabel}>会話履歴</Text>
              <View style={styles.historyHeaderActions}>
                <ConversationHistoryFilter
                  value={historyFilter}
                  onChange={changeHistoryFilter}
                />
                <IconButton
                  icon="plus"
                  size={18}
                  iconColor={TEXT}
                  style={styles.smallIconButton}
                  accessibilityLabel="新規会話"
                  onPress={createSession}
                />
              </View>
            </View>
            <SegmentedButtons
              value={historyView}
              onValueChange={changeHistoryView}
              density="small"
              style={styles.historySegment}
              buttons={[
                { value: "timeline", label: "時系列", accessibilityLabel: "会話を時系列で表示" },
                { value: "project", label: "プロジェクト別", accessibilityLabel: "会話をプロジェクト別で表示" },
              ]}
            />
          </View>
        }
        ListEmptyComponent={
          loading && sessions.length === 0 ? (
            <ActivityIndicator color="#7c3aed" style={styles.loading} />
          ) : (
            <Text style={styles.emptyText}>
              {historyFilter === "all" ? "会話がありません" : "この状態の会話はありません"}
            </Text>
          )
        }
        renderItem={({ item: row }) => {
          if (row.kind === "group") return <Text accessibilityRole="header" style={styles.projectGroupLabel}>{row.label} ({row.count})</Text>;
          const item = row.session;
          const projectName = item.project_id ? projectNameById.get(item.project_id) ?? "不明なプロジェクト" : null;
          return (
          <List.Item
            testID={`sidebar-session-${item.id}`}
            accessibilityRole="button"
            accessibilityHint="タップで会話を開きます。長押しで会話の操作を表示します"
            accessibilityState={{ selected: item.id === activeSessionId }}
            title={item.title || "無題の会話"}
            description={
              (projectName ? `プロジェクト: ${projectName} ・ ` : "") + (item.last_activity
                ? `${item.character_name || "キャラクター未設定"} ・ ${formatRelativeTime(
                    item.last_activity,
                  )}`
                : item.character_name || "キャラクター未設定")
            }
            titleNumberOfLines={1}
            descriptionNumberOfLines={1}
            titleStyle={styles.listTitle}
            descriptionStyle={styles.listDescription}
            left={(props) => (
              isChatSessionWorking(item) ? (
                <View style={styles.sessionIconSlot}>
                  <ActivityIndicator
                    accessibilityLabel="エージェントが作業中"
                    size={18}
                    color="#89b4fa"
                  />
                </View>
              ) : (
                <List.Icon
                  {...props}
                  icon={
                    item.history_status === "ready"
                      ? "check-circle-outline"
                      : item.history_status === "needs_attention"
                        ? "alert-circle-outline"
                        : item.is_pinned
                          ? "pin"
                          : "message-outline"
                  }
                  color={
                    item.history_status === "needs_attention"
                      ? "#f9a8d4"
                      : item.history_status === "ready"
                        ? "#89dceb"
                        : item.is_pinned
                          ? "#c4b5fd"
                          : "#7c3aed"
                  }
                />
              )
            )}
            right={() => (
              <View style={styles.sessionMeta}>
                {isChatSessionUnread(item, activeSessionId) ? (
                  <View accessibilityLabel="未読" style={styles.unreadDot} />
                ) : null}
                <Text style={styles.itemCount}>{item.message_count}</Text>
                <IconButton
                  icon="dots-vertical"
                  accessibilityLabel={`${item.title || "無題の会話"}の操作`}
                  size={18}
                  iconColor={MUTED}
                  style={styles.smallIconButton}
                  onPress={(event) => { event.stopPropagation(); openSessionActions(item); }}
                />
              </View>
            )}
            onPressIn={() => { longPressedSessionIdRef.current = null; }}
            onLongPress={() => {
              longPressedSessionIdRef.current = item.id;
              openSessionActions(item);
            }}
            delayLongPress={350}
            onPress={() => {
              // Some native touchables emit onPress after a long press. Never
              // navigate underneath the operation dialog, including fast taps.
              if (longPressedSessionIdRef.current === item.id || actionTargetRef.current) return;
              openSession(item.id);
            }}
            style={[styles.listItem, item.id === activeSessionId && styles.navRowActive]}
          />
          );
        }}
      />
      {actionTarget ? <SidebarSessionActions
        key={actionTarget.id}
        session={actionTarget}
        authenticated={isAuthenticated}
        onDismiss={() => { actionTargetRef.current = null; setActionTarget(null); }}
        onChanged={refreshAfterAction}
        onForked={(session) => openSession(session.id)}
        onDeleted={(sessionId) => {
          if (activeSessionIdRef.current === sessionId) {
            close();
            router.replace("/(tabs)/chat");
          }
        }}
        onPromoted={(sourceId, targetId) => {
          if (activeSessionIdRef.current === sourceId && sourceId !== targetId) {
            close();
            router.replace(`/(tabs)/chat/${targetId}`);
          }
        }}
      /> : null}
    </View>
  );
}

function TaskSidebar({
  close,
  header,
}: {
  close: () => void;
  header: React.ReactElement;
}) {
  const router = useRouter();
  const { projects, selectedProjectId, selectedSpaceId } = useProject();
  const { isAuthenticated, isAnonymous, user } = useAuth();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const authScope = isAuthenticated
    ? `auth:${user?.user_id ?? "unknown"}`
    : isAnonymous
      ? "anonymous"
      : "signed_out";

  const projectMap = useMemo(
    () => new Map(projects.map((project) => [project.id, project.name])),
    [projects],
  );
  const selectedSpaceProjectIds = useMemo(
    () =>
      selectedSpaceId
        ? new Set(
            projects
              .filter((project) => project.space_id === selectedSpaceId)
              .map((project) => project.id),
          )
        : null,
    [projects, selectedSpaceId],
  );

  const loadTasks = useCallback(async () => {
    setLoading(true);
    try {
      setTasks(await tasksRepo.list());
    } catch {
      setTasks([]);
    } finally {
      setLoading(false);
    }
  }, [authScope]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const visibleTasks = useMemo(
    () =>
      tasks
        .filter((task) => !task.parent_task_id)
        .filter((task) => {
          if (selectedProjectId) return task.project_id === selectedProjectId;
          if (selectedSpaceProjectIds) {
            return selectedSpaceProjectIds.has(task.project_id);
          }
          return true;
        })
        .filter((task) => !COMPLETED_TASK_STATUSES.has(task.status))
        .filter((task) => !isFutureTask(task))
        .slice(0, 40),
    [selectedProjectId, selectedSpaceProjectIds, tasks],
  );

  return (
    <View style={styles.sidebarContainer}>
      <FlatList
        style={styles.sidebarList}
        data={visibleTasks}
        keyExtractor={(item) => item.id}
        ItemSeparatorComponent={() => <Divider style={styles.divider} />}
        ListHeaderComponent={
          <View>
            {header}
            <View style={styles.groupHeader}>
              <Text style={styles.groupLabel}>タスク ({visibleTasks.length})</Text>
              <IconButton
                icon="refresh"
                size={18}
                iconColor={TEXT}
                style={styles.smallIconButton}
                onPress={loadTasks}
              />
            </View>
          </View>
        }
        ListEmptyComponent={
          loading && visibleTasks.length === 0 ? (
            <ActivityIndicator color="#7c3aed" style={styles.loading} />
          ) : (
            <Text style={styles.emptyText}>未完了タスクはありません</Text>
          )
        }
        renderItem={({ item }) => (
          <List.Item
            title={item.title}
            description={projectMap.get(item.project_id) || item.project_name || "不明"}
            titleNumberOfLines={2}
            descriptionNumberOfLines={1}
            titleStyle={styles.listTitle}
            descriptionStyle={styles.listDescription}
            left={() => <View style={[styles.statusDot, statusDotStyle(item.status)]} />}
            onPress={() => {
              close();
              router.push(`/(tabs)/tasks/${item.id}`);
            }}
            style={styles.listItem}
          />
        )}
      />
    </View>
  );
}

function statusDotStyle(status: string) {
  if (status === "in_progress") return { borderColor: "#f38ba8" };
  if (status === "closed") return { borderColor: "#a6e3a1", backgroundColor: "#a6e3a1" };
  return { borderColor: "#89b4fa" };
}

function FilesSidebar({
  close,
  header,
}: {
  close: () => void;
  header: React.ReactElement;
}) {
  const router = useRouter();
  const { isAuthenticated, user } = useAuth();
  const { selectedProjectId, selectedProject } = useProject();
  const isAdmin = user?.role === "admin";
  const [source, setSource] = useState<FilesSource>("local");
  const [items, setItems] = useState<FilesEntry[]>([]);
  const [loading, setLoading] = useState(false);

  // サーバー・ワークスペースのルート。管理者はプロジェクト未選択で管理者ルート、
  // 一般ユーザーは未選択なら null（一覧取得しない）。
  const serverWorkspaceRoot =
    isAdmin && !selectedProjectId
      ? ""
      : selectedProjectId
        ? `_projects/project_${selectedProjectId}`
        : null;

  const projectLabel = selectedProject?.name
    ? selectedProject.name
    : isAdmin
      ? "管理者ルート"
      : "プロジェクト未選択";

  const needsProjectSelection =
    source === "server" && serverWorkspaceRoot === null;

  const loadFiles = useCallback(async () => {
    if (source === "server" && serverWorkspaceRoot === null) {
      setItems([]);
      return;
    }
    setLoading(true);
    try {
      const result =
        source === "server"
          ? await filesApi.list(
              "server",
              serverWorkspaceRoot || undefined,
              "workspace",
            )
          : await filesApi.list("local");
      setItems(result.items.slice(0, 20));
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [serverWorkspaceRoot, source]);

  useEffect(() => {
    if (!isAuthenticated && source === "server") {
      setSource("local");
      return;
    }
    void loadFiles();
  }, [isAuthenticated, loadFiles, source]);

  return (
    <View style={styles.sidebarContainer}>
      <FlatList
        style={styles.sidebarList}
        data={loading ? [] : items}
        keyExtractor={(item) => `${item.source}:${item.path}`}
        ItemSeparatorComponent={() => <Divider style={styles.divider} />}
        ListHeaderComponent={
          <View>
            {header}
            <View style={styles.groupHeader}>
              <Text style={styles.groupLabel}>ファイル</Text>
              <IconButton
                icon="open-in-new"
                size={18}
                iconColor={TEXT}
                style={styles.smallIconButton}
                onPress={() => {
                  close();
                  router.push("/(tabs)/filer");
                }}
              />
            </View>
            <SegmentedButtons
              value={source}
              onValueChange={(value) => setSource(value as FilesSource)}
              style={styles.segment}
              density="small"
              buttons={[
                { value: "local", label: "ローカル", icon: "cellphone" },
                {
                  value: "server",
                  label: "サーバー",
                  icon: "server",
                  disabled: !isAuthenticated,
                },
              ]}
            />
            {source === "server" ? (
              <Text style={styles.filesProjectLabel} numberOfLines={1}>
                プロジェクト: {projectLabel}
              </Text>
            ) : null}
          </View>
        }
        ListEmptyComponent={
          loading ? (
            <ActivityIndicator color="#7c3aed" style={styles.loading} />
          ) : (
            <Text style={styles.emptyText}>
              {needsProjectSelection
                ? "プロジェクトを選択してください"
                : "ファイルはありません"}
            </Text>
          )
        }
        renderItem={({ item }) => (
          <List.Item
            title={item.name}
            description={item.type === "directory" ? "フォルダ" : item.extension || "ファイル"}
            titleNumberOfLines={1}
            descriptionNumberOfLines={1}
            titleStyle={styles.listTitle}
            descriptionStyle={styles.listDescription}
            left={(props) => (
              <List.Icon
                {...props}
                icon={item.type === "directory" ? "folder-outline" : "file-outline"}
                color={item.type === "directory" ? "#f9e2af" : "#89b4fa"}
              />
            )}
            onPress={() => {
              close();
              router.push("/(tabs)/filer");
            }}
            style={styles.listItem}
          />
        )}
      />
    </View>
  );
}

/** Cross-project search stays reachable from all three sidebar tabs. */
function ConversationSearchSidebar({ close, header, replaceCurrentChat }: {
  close: () => void;
  header: React.ReactElement;
  replaceCurrentChat: boolean;
}) {
  const router = useRouter();
  const { projects } = useProject();
  const projectNameById = useMemo(() => new Map(projects.map((project) => [project.id, project.name])), [projects]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ConversationSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [historyFilter, setHistoryFilter] = useState<ChatHistoryFilter>("all");
  const filterChangedRef = useRef(false);
  const filteredResults = useMemo(
    () => filterChatSearchResults(results, historyFilter),
    [historyFilter, results],
  );
  useEffect(() => subscribeToConversationChanges(() => setRevision((value) => value + 1)), []);
  useEffect(() => {
    let mounted = true;
    void AsyncStorage.getItem(CHAT_HISTORY_FILTER_KEY).then((saved) => {
      if (mounted && !filterChangedRef.current && isChatHistoryFilter(saved)) setHistoryFilter(saved);
    }).catch(() => undefined);
    return () => { mounted = false; };
  }, []);
  const changeHistoryFilter = (value: ChatHistoryFilter) => {
    filterChangedRef.current = true;
    setHistoryFilter(value);
    void AsyncStorage.setItem(CHAT_HISTORY_FILTER_KEY, value).catch(() => undefined);
  };
  useEffect(() => {
    let cancelled = false;
    let onlineSettled = false;
    const normalized = query.trim();
    setResults([]);
    setError(null);
    setSearching(Boolean(normalized));
    if (!normalized) return;
    const timer = setTimeout(() => {
      // Local results are visible while the online search scans older history.
      void chatRepo.searchLocal(normalized, undefined, 50).then((local) => {
        if (!cancelled && !onlineSettled) setResults(local);
      }).catch(() => undefined);
      void chatRepo.search(normalized, undefined, 50).then((found) => {
        onlineSettled = true;
        if (!cancelled) { setResults(found); setSearching(false); }
      }).catch((failure) => {
        onlineSettled = true;
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : "検索に失敗しました。");
          setSearching(false);
        }
      });
    }, 180);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [query, revision]);

  return <FlatList
    style={styles.sidebarList}
    data={filteredResults}
    keyExtractor={(item) => item.id}
    keyboardShouldPersistTaps="handled"
    ItemSeparatorComponent={() => <Divider style={styles.divider} />}
    ListHeaderComponent={<View>
      {header}
      <View style={styles.searchHistoryHeader}>
        <Text style={styles.groupLabel}>すべてのプロジェクトの会話を検索</Text>
        <ConversationHistoryFilter
          value={historyFilter}
          onChange={changeHistoryFilter}
        />
      </View>
      <TextInput
        accessibilityLabel="会話のタイトル・本文を検索"
        placeholder="タイトル・本文を検索"
        value={query}
        onChangeText={setQuery}
        autoFocus
        dense
        mode="outlined"
        style={styles.searchInput}
        right={<TextInput.Icon icon="close" accessibilityLabel="検索語をクリア" onPress={() => setQuery("")} />}
      />
      <Text style={styles.searchHint}>過去の会話・端末内の会話を含む最大50件</Text>
      {searching ? <ActivityIndicator accessibilityLabel="会話を検索中" size="small" color="#89b4fa" /> : null}
      {error ? <Text accessibilityRole="alert" style={styles.searchError}>{error}</Text> : null}
    </View>}
    ListEmptyComponent={<Text style={styles.emptyText}>{query.trim() ? searching ? "検索中…" : historyFilter === "all" ? "検索結果はありません" : "この状態の検索結果はありません" : "検索語を入力してください"}</Text>}
    renderItem={({ item }) => <List.Item
      testID={`sidebar-search-result-${item.id}`}
      title={item.title || "無題の会話"}
      description={`${item.project_id ? `プロジェクト: ${projectNameById.get(item.project_id) ?? "不明なプロジェクト"} ・ ` : ""}${item.snippet}`}
      titleNumberOfLines={1}
      descriptionNumberOfLines={3}
      titleStyle={styles.listTitle}
      descriptionStyle={styles.listDescription}
      left={(props) => <List.Icon {...props} icon="text-search" color="#89b4fa" />}
      onPress={() => {
        void conversationsRepo.markSessionRead?.(item.session_id);
        close();
        const href = `/(tabs)/chat/${item.session_id}` as const;
        if (replaceCurrentChat) router.replace(href);
        else router.push(href);
      }}
      style={styles.listItem}
    />}
  />;
}

function AppSidebarContent({ close }: { close: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const [tab, setTab] = useState<SidebarTab>("chat");
  const [searchVisible, setSearchVisible] = useState(false);
  const isChatDetail = /^\/(?:\(tabs\)\/)?chat\/[^/]+$/.test(pathname);

  const navigate = (href: string) => {
    close();
    router.push(href);
  };

  // Keep navigation and tab controls in the active list's scroll content so
  // they cannot consume the entire viewport before the first list row.
  const header = (
    <View>
      <View style={styles.header}>
        <View>
          <Text style={styles.appName}>AoiTalk</Text>
          <Text style={styles.appSubtext}>Workspace</Text>
        </View>
        <IconButton icon="close" iconColor={TEXT} size={20} onPress={close} />
      </View>

      <ContextSwitcher />

      <View style={styles.navigationGrid}>
        <NavigationShortcut
          icon="view-dashboard-outline"
          label="プロジェクト"
          active={pathname.includes("/projects") || pathname.includes("/project/")}
          onPress={() => navigate("/projects")}
        />
        <NavigationShortcut
          icon="robot-outline"
          label="Operations"
          active={pathname.includes("/operations")}
          onPress={() => navigate("/operations")}
        />
        <NavigationShortcut
          icon="chart-timeline-variant"
          label="レポート"
          active={pathname.includes("/reports") || pathname.includes("/report/")}
          onPress={() => navigate("/reports")}
        />
        <NavigationShortcut
          icon="application-brackets-outline"
          label="Apps"
          active={pathname.includes("/apps")}
          onPress={() => navigate("/(tabs)/apps")}
        />
        <NavigationShortcut
          icon="magnify"
          label="会話を検索"
          active={searchVisible}
          onPress={() => setSearchVisible((visible) => !visible)}
        />
      </View>

      <SegmentedButtons
        value={tab}
        onValueChange={(value) => { setTab(value as SidebarTab); setSearchVisible(false); }}
        style={styles.segment}
        density="small"
        buttons={[
          { value: "chat", label: "チャット", icon: "message-outline" },
          { value: "tasks", label: "タスク", icon: "checkbox-marked-outline" },
          { value: "files", label: "ファイル", icon: "folder-outline" },
        ]}
      />
    </View>
  );

  return (
    <View style={styles.drawerContent}>
      {searchVisible ? <ConversationSearchSidebar close={close} header={header} replaceCurrentChat={isChatDetail} /> : null}
      {!searchVisible && tab === "chat" && (
        <ChatSidebar
          close={close}
          replaceCurrentChat={isChatDetail}
          header={header}
        />
      )}
      {!searchVisible && tab === "tasks" && <TaskSidebar close={close} header={header} />}
      {!searchVisible && tab === "files" && <FilesSidebar close={close} header={header} />}
    </View>
  );
}

export function AppSidebar({
  children,
  initialOpen = false,
}: {
  children: React.ReactNode;
  initialOpen?: boolean;
}) {
  const { canUseApp } = useAuth();
  const pathname = usePathname();
  const insets = useSafeAreaInsets();
  const { width } = useWindowDimensions();
  const drawerWidth = Math.min(MAX_DRAWER_WIDTH, Math.round(width * 0.86));
  const [open, setOpen] = useState(initialOpen);
  const reduceMotion = useReducedMotion();
  const translateX = useRef(new Animated.Value(-drawerWidth)).current;
  // iOS keeps the narrow edge overlay. Android routes the responder through
  // the content parent so a touch in the first 28dp still reaches buttons and
  // list rows; the responder only claims a clearly horizontal single-finger
  // move on the allowlisted list routes. A focused native TextInput and
  // editors/detail forms therefore keep their own horizontal and text gestures.
  const edgeGestureEnabled = Platform.OS !== "android";
  const androidSwipeEnabled = Platform.OS === "android" && isAndroidSidebarSwipeRoute(pathname);

  useEffect(() => {
    Animated.timing(translateX, {
      toValue: open ? 0 : -drawerWidth,
      duration: reduceMotion ? 0 : 180,
      useNativeDriver: true,
    }).start();
  }, [drawerWidth, open, reduceMotion, translateX]);

  const close = useCallback(() => setOpen(false), []);
  const openSidebar = useCallback(() => setOpen(true), []);
  useEffect(() => {
    if (Platform.OS !== "android" || !open || !canUseApp) return;

    // BackHandler dispatches JS listeners newest-first. Registering only while
    // open lets the drawer close before handlers belonging to the screen below.
    const subscription = BackHandler.addEventListener(
      "hardwareBackPress",
      () => {
        close();
        return true;
      },
    );
    return () => subscription.remove();
  }, [canUseApp, close, open]);

  const sidebarController = useMemo(
    () => ({ openSidebar }),
    [openSidebar],
  );

  const edgePanResponder = useMemo(
    () =>
      PanResponder.create({
        onMoveShouldSetPanResponder: (_, gesture) => {
          if (
            open ||
            gesture.dx <= 8 ||
            gesture.numberActiveTouches > 1 ||
            (Platform.OS === "android" && hasFocusedNativeTextInput()) ||
            Math.abs(gesture.dx) <= Math.abs(gesture.dy) * 1.4
          ) {
            return false;
          }
          // Prefer the initial touch coordinate when native Android supplies
          // it.  The moveX fallback keeps the responder testable with the
          // small gesture objects used by unit tests and older RN versions.
          // Android listens on the content parent, so a swipe can begin on a
          // row rather than only inside the iOS-style edge hit area.
          if (Platform.OS === "android") return true;
          const initialX = Number.isFinite(gesture.x0)
            ? gesture.x0
            : gesture.moveX - gesture.dx;
          return Number.isFinite(initialX)
            ? initialX <= EDGE_HIT_WIDTH
            : gesture.moveX <= EDGE_HIT_WIDTH + gesture.dx;
        },
        onPanResponderRelease: (_, gesture) => {
          if (gesture.dx > MIN_OPEN_DISTANCE) {
            openSidebar();
          }
        },
        onShouldBlockNativeResponder: () => false,
      }),
    [open, openSidebar],
  );

  const drawerPanResponder = useMemo(
    () =>
      PanResponder.create({
        onMoveShouldSetPanResponder: (_, gesture) =>
          open &&
          gesture.dx < -8 &&
          Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4,
        onPanResponderRelease: (_, gesture) => {
          if (gesture.dx < -MIN_OPEN_DISTANCE) {
            close();
          }
        },
      }),
    [close, open],
  );

  if (!canUseApp) {
    return <>{children}</>;
  }

  return (
    <AppSidebarContext.Provider value={sidebarController}>
      <View
        testID="app-sidebar-root"
        style={styles.root}
        {...(androidSwipeEnabled ? edgePanResponder.panHandlers : {})}
      >
        {children}
        {edgeGestureEnabled ? (
          <View
            testID="app-sidebar-edge-gesture"
            style={styles.edgeGesture}
            {...edgePanResponder.panHandlers}
          />
        ) : null}
        {open && <Pressable style={styles.scrim} onPress={close} />}
        <Animated.View
          testID="app-sidebar-drawer"
          pointerEvents={open ? "auto" : "none"}
          style={[
            styles.drawer,
            {
              width: drawerWidth,
              paddingTop: insets.top,
              paddingBottom: Math.max(insets.bottom, 12),
              transform: [{ translateX }],
            },
          ]}
          {...drawerPanResponder.panHandlers}
        >
          <Surface style={styles.drawerSurface} elevation={4}>
            {open ? <AppSidebarContent close={close} /> : null}
          </Surface>
        </Animated.View>
      </View>
    </AppSidebarContext.Provider>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: "#11111b",
  },
  edgeGesture: {
    position: "absolute",
    top: 0,
    bottom: 0,
    left: 0,
    width: EDGE_HIT_WIDTH,
    zIndex: 20,
  },
  scrim: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(0, 0, 0, 0.45)",
    zIndex: 30,
  },
  drawer: {
    position: "absolute",
    top: 0,
    bottom: 0,
    left: 0,
    zIndex: 40,
  },
  drawerSurface: {
    flex: 1,
    backgroundColor: SIDEBAR_BG,
    borderRightColor: "#313244",
    borderRightWidth: StyleSheet.hairlineWidth,
  },
  drawerContent: {
    flex: 1,
    paddingHorizontal: 10,
  },
  header: {
    minHeight: 58,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 6,
  },
  appName: {
    color: TEXT,
    fontSize: 20,
    fontWeight: "700",
  },
  appSubtext: {
    color: MUTED,
    fontSize: 12,
    marginTop: 2,
  },
  contextBlock: {
    gap: 8,
    paddingBottom: 8,
  },
  navigationGrid: {
    flexDirection: "row",
    flexWrap: "nowrap",
    justifyContent: "space-between",
    paddingVertical: 4,
    borderTopColor: "#313244",
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  navShortcut: {
    flex: 1,
    minWidth: 44,
    minHeight: 48,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 8,
  },
  navRowActive: {
    backgroundColor: ACTIVE_BG,
  },
  navRowPressed: {
    backgroundColor: "#45475a",
  },
  searchInput: { marginVertical: 8, backgroundColor: "#1e1e2e" },
  searchHint: { color: MUTED, fontSize: 11, paddingBottom: 8 },
  searchError: { color: "#f38ba8", paddingVertical: 8 },
  searchHistoryHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 8,
  },
  segment: {
    marginVertical: 8,
  },
  historySegment: { marginHorizontal: 4, marginBottom: 8 },
  projectGroupLabel: { color: MUTED, fontSize: 12, fontWeight: "700", paddingHorizontal: 8, paddingVertical: 10 },
  sidebarContainer: {
    flex: 1,
    minHeight: 0,
  },
  sidebarList: {
    flex: 1,
  },
  groupHeader: {
    borderTopColor: "#313244",
    borderTopWidth: StyleSheet.hairlineWidth,
    height: 42,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 8,
  },
  historyHeaderActions: {
    flexDirection: "row",
    alignItems: "center",
  },
  groupLabel: {
    color: MUTED,
    fontSize: 12,
    fontWeight: "700",
  },
  filesProjectLabel: {
    color: MUTED,
    fontSize: 11,
    paddingHorizontal: 8,
    paddingBottom: 6,
  },
  smallIconButton: {
    margin: 0,
  },
  loading: {
    marginTop: 24,
  },
  divider: {
    backgroundColor: "#313244",
  },
  emptyText: {
    color: MUTED,
    fontSize: 13,
    paddingVertical: 24,
    textAlign: "center",
  },
  listItem: {
    minHeight: 56,
    paddingLeft: 0,
    paddingRight: 0,
  },
  listTitle: {
    color: TEXT,
    fontSize: 14,
  },
  listDescription: {
    color: MUTED,
    fontSize: 12,
  },
  itemCount: {
    alignSelf: "center",
    color: MUTED,
    fontSize: 11,
    paddingRight: 8,
  },
  sessionIconSlot: {
    width: 40,
    justifyContent: "center",
    alignItems: "center",
  },
  sessionMeta: {
    minWidth: 28,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "flex-end",
    gap: 8,
  },
  unreadDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: "#89dceb",
  },
  statusDot: {
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 2,
    marginLeft: 16,
    marginTop: 16,
  },
});
