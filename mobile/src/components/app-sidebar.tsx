import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Animated,
  FlatList,
  PanResponder,
  Pressable,
  StyleSheet,
  View,
  useWindowDimensions,
} from "react-native";
import { usePathname, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  ActivityIndicator,
  Button,
  Divider,
  Icon,
  IconButton,
  List,
  Menu,
  SegmentedButtons,
  Surface,
  Text,
} from "react-native-paper";
import { format } from "date-fns";
import { useAuth } from "../contexts/AuthContext";
import { useProject } from "../contexts/ProjectContext";
import { conversationsRepo } from "../repositories/conversations";
import { tasksRepo } from "../repositories/tasks";
import { getDefaultCharacterName } from "../lib/preferences";
import { filesApi, type FilesEntry, type FilesSource } from "../lib/files-api";
import { COMPLETED_TASK_STATUSES, isFutureTask } from "../lib/task-visibility";
import type { ConversationSession, Task } from "../types/api";

type SidebarTab = "chat" | "tasks" | "files";

const EDGE_HIT_WIDTH = 28;
const MIN_OPEN_DISTANCE = 72;
const MAX_DRAWER_WIDTH = 340;
const SIDEBAR_BG = "#181825";
const ACTIVE_BG = "#313244";
const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";

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

function NavigationRow({
  icon,
  label,
  active,
  count,
  onPress,
}: {
  icon: string;
  label: string;
  active?: boolean;
  count?: number;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.navRow,
        active && styles.navRowActive,
        pressed && styles.navRowPressed,
      ]}
    >
      <Icon source={icon} size={20} color={active ? "#ffffff" : MUTED} />
      <Text style={[styles.navLabel, active && styles.navLabelActive]}>
        {label}
      </Text>
      {typeof count === "number" && (
        <Text style={styles.navCount}>{count > 99 ? "99+" : count}</Text>
      )}
    </Pressable>
  );
}

function ContextSwitcher() {
  const {
    spaces,
    projects,
    selectedSpaceId,
    selectedProjectId,
    selectedSpace,
    selectedProject,
    setSelectedSpaceId,
    setSelectedProjectId,
  } = useProject();
  const [spaceMenuVisible, setSpaceMenuVisible] = useState(false);
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);

  if (spaces.length === 0 && projects.length === 0) return null;

  return (
    <View style={styles.contextBlock}>
      {spaces.length > 0 && (
        <Menu
          visible={spaceMenuVisible}
          onDismiss={() => setSpaceMenuVisible(false)}
          anchor={
            <Button
              mode="outlined"
              compact
              icon="layers-outline"
              textColor={TEXT}
              style={styles.contextButton}
              contentStyle={styles.contextButtonContent}
              onPress={() => setSpaceMenuVisible(true)}
            >
              {selectedSpace?.name || "スペース"}
            </Button>
          }
        >
          <Menu.Item
            leadingIcon={
              !selectedSpaceId && !selectedProjectId ? "check" : undefined
            }
            onPress={() => {
              setSelectedProjectId(null);
              setSpaceMenuVisible(false);
            }}
            title="すべてのスペース"
          />
          {spaces.map((space) => (
            <Menu.Item
              key={space.id}
              leadingIcon={space.id === selectedSpaceId ? "check" : undefined}
              onPress={() => {
                setSelectedSpaceId(space.id);
                setSpaceMenuVisible(false);
              }}
              title={space.name}
            />
          ))}
        </Menu>
      )}
      {projects.length > 0 && (
        <Menu
          visible={projectMenuVisible}
          onDismiss={() => setProjectMenuVisible(false)}
          anchor={
            <Button
              mode="outlined"
              compact
              icon="folder-outline"
              textColor={TEXT}
              style={styles.contextButton}
              contentStyle={styles.contextButtonContent}
              onPress={() => setProjectMenuVisible(true)}
            >
              {selectedProject?.name || "プロジェクト"}
            </Button>
          }
        >
          <Menu.Item
            leadingIcon={
              !selectedProjectId && !selectedSpaceId ? "check" : undefined
            }
            onPress={() => {
              setSelectedProjectId(null);
              setProjectMenuVisible(false);
            }}
            title="すべてのスペース"
          />
          {projects.map((project) => (
            <Menu.Item
              key={project.id}
              leadingIcon={project.id === selectedProjectId ? "check" : undefined}
              onPress={() => {
                setSelectedProjectId(project.id);
                setProjectMenuVisible(false);
              }}
              title={project.name}
            />
          ))}
        </Menu>
      )}
    </View>
  );
}

function ChatSidebar({ close }: { close: () => void }) {
  const router = useRouter();
  const { selectedProjectId } = useProject();
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [loading, setLoading] = useState(false);

  const loadSessions = useCallback(async () => {
    setLoading(true);
    try {
      setSessions(await conversationsRepo.listSessions());
    } catch {
      // Keep the current list visible if a refresh fails.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const createSession = async () => {
    const characterName = await getDefaultCharacterName();
    const session = await conversationsRepo.createSession(
      characterName,
      selectedProjectId,
    );
    close();
    router.push(`/(tabs)/chat/${session.id}`);
  };

  return (
    <View style={styles.panel}>
      <View style={styles.groupHeader}>
        <Text style={styles.groupLabel}>会話履歴</Text>
        <IconButton
          icon="plus"
          size={18}
          iconColor={TEXT}
          style={styles.smallIconButton}
          onPress={createSession}
        />
      </View>
      {loading && sessions.length === 0 ? (
        <ActivityIndicator color="#7c3aed" style={styles.loading} />
      ) : (
        <FlatList
          data={sessions}
          keyExtractor={(item) => item.id}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          ListEmptyComponent={
            <Text style={styles.emptyText}>会話がありません</Text>
          }
          renderItem={({ item }) => (
            <List.Item
              title={item.title || "無題の会話"}
              description={
                item.last_activity
                  ? `${item.character_name || "default"} ・ ${formatRelativeTime(
                      item.last_activity,
                    )}`
                  : item.character_name || "default"
              }
              titleNumberOfLines={1}
              descriptionNumberOfLines={1}
              titleStyle={styles.listTitle}
              descriptionStyle={styles.listDescription}
              left={(props) => (
                <List.Icon {...props} icon="message-outline" color="#7c3aed" />
              )}
              right={() => (
                <Text style={styles.itemCount}>{item.message_count}</Text>
              )}
              onPress={() => {
                close();
                router.push(`/(tabs)/chat/${item.id}`);
              }}
              style={styles.listItem}
            />
          )}
        />
      )}
    </View>
  );
}

function TaskSidebar({ close }: { close: () => void }) {
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
    <View style={styles.panel}>
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
      {loading && visibleTasks.length === 0 ? (
        <ActivityIndicator color="#7c3aed" style={styles.loading} />
      ) : (
        <FlatList
          data={visibleTasks}
          keyExtractor={(item) => item.id}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          ListEmptyComponent={
            <Text style={styles.emptyText}>未完了タスクはありません</Text>
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
      )}
    </View>
  );
}

function statusDotStyle(status: string) {
  if (status === "in_progress") return { borderColor: "#f38ba8" };
  if (status === "closed") return { borderColor: "#a6e3a1", backgroundColor: "#a6e3a1" };
  return { borderColor: "#89b4fa" };
}

function FilesSidebar({ close }: { close: () => void }) {
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
    <View style={styles.panel}>
      <View style={styles.groupHeader}>
        <Text style={styles.groupLabel}>ファイラー</Text>
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
      {loading ? (
        <ActivityIndicator color="#7c3aed" style={styles.loading} />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(item) => `${item.source}:${item.path}`}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          ListEmptyComponent={
            <Text style={styles.emptyText}>
              {needsProjectSelection
                ? "プロジェクトを選択してください"
                : "ファイルはありません"}
            </Text>
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
      )}
    </View>
  );
}

function AppSidebarContent({ close }: { close: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const [tab, setTab] = useState<SidebarTab>("chat");

  const navigate = (href: string) => {
    close();
    router.push(href);
  };

  return (
    <View style={styles.drawerContent}>
      <View style={styles.header}>
        <View>
          <Text style={styles.appName}>AoiTalk</Text>
          <Text style={styles.appSubtext}>Workspace</Text>
        </View>
        <IconButton icon="close" iconColor={TEXT} size={20} onPress={close} />
      </View>

      <ContextSwitcher />

      <View style={styles.section}>
        <NavigationRow
          icon="message-outline"
          label="チャット"
          active={pathname.startsWith("/chat") || pathname.includes("/chat")}
          onPress={() => navigate("/(tabs)/chat")}
        />
        <NavigationRow
          icon="checkbox-marked-outline"
          label="タスク"
          active={pathname.includes("/tasks")}
          onPress={() => navigate("/(tabs)/tasks")}
        />
        <NavigationRow
          icon="calendar-month-outline"
          label="カレンダー"
          active={pathname.includes("/calendar")}
          onPress={() => navigate("/(tabs)/calendar")}
        />
        <NavigationRow
          icon="folder-outline"
          label="ファイル"
          active={pathname.includes("/filer")}
          onPress={() => navigate("/(tabs)/filer")}
        />
        <NavigationRow
          icon="file-tree-outline"
          label="Docs"
          active={pathname.includes("/docs")}
          onPress={() => navigate("/(tabs)/docs")}
        />
      </View>

      <View style={styles.section}>
        <NavigationRow
          icon="view-dashboard-outline"
          label="プロジェクト"
          active={pathname.includes("/projects") || pathname.includes("/project/")}
          onPress={() => navigate("/projects")}
        />
        <NavigationRow
          icon="chart-timeline-variant"
          label="レポート"
          active={pathname.includes("/reports") || pathname.includes("/report/")}
          onPress={() => navigate("/reports")}
        />
        <NavigationRow
          icon="book-open-page-variant-outline"
          label="シナリオ"
          active={pathname.includes("/scenarios") || pathname.includes("/scenario/")}
          onPress={() => navigate("/scenarios")}
        />
        <NavigationRow
          icon="dice-multiple-outline"
          label="TRPG"
          active={pathname.includes("/trpg")}
          onPress={() => navigate("/trpg")}
        />
        <NavigationRow
          icon="cog-outline"
          label="設定"
          active={pathname.includes("/settings")}
          onPress={() => navigate("/(tabs)/settings")}
        />
      </View>

      <SegmentedButtons
        value={tab}
        onValueChange={(value) => setTab(value as SidebarTab)}
        style={styles.segment}
        density="small"
        buttons={[
          { value: "chat", label: "チャット", icon: "message-outline" },
          { value: "tasks", label: "タスク", icon: "checkbox-marked-outline" },
          { value: "files", label: "ファイル", icon: "folder-outline" },
        ]}
      />

      {tab === "chat" && <ChatSidebar close={close} />}
      {tab === "tasks" && <TaskSidebar close={close} />}
      {tab === "files" && <FilesSidebar close={close} />}
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
  const insets = useSafeAreaInsets();
  const { width } = useWindowDimensions();
  const drawerWidth = Math.min(MAX_DRAWER_WIDTH, Math.round(width * 0.86));
  const [open, setOpen] = useState(initialOpen);
  const translateX = useRef(new Animated.Value(-drawerWidth)).current;

  useEffect(() => {
    Animated.timing(translateX, {
      toValue: open ? 0 : -drawerWidth,
      duration: 180,
      useNativeDriver: false,
    }).start();
  }, [drawerWidth, open, translateX]);

  const close = useCallback(() => setOpen(false), []);
  const openSidebar = useCallback(() => setOpen(true), []);

  const edgePanResponder = useMemo(
    () =>
      PanResponder.create({
        onMoveShouldSetPanResponder: (_, gesture) =>
          !open &&
          gesture.moveX <= EDGE_HIT_WIDTH + Math.max(0, gesture.dx) &&
          gesture.dx > 8 &&
          Math.abs(gesture.dx) > Math.abs(gesture.dy) * 1.4,
        onPanResponderRelease: (_, gesture) => {
          if (gesture.dx > MIN_OPEN_DISTANCE) {
            openSidebar();
          }
        },
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
    <View style={styles.root}>
      {children}
      <View
        testID="app-sidebar-edge-gesture"
        style={styles.edgeGesture}
        {...edgePanResponder.panHandlers}
      />
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
  contextButton: {
    borderColor: "#45475a",
    borderRadius: 8,
  },
  contextButtonContent: {
    justifyContent: "flex-start",
  },
  section: {
    paddingVertical: 6,
    borderTopColor: "#313244",
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  navRow: {
    minHeight: 40,
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingHorizontal: 10,
    borderRadius: 8,
  },
  navRowActive: {
    backgroundColor: ACTIVE_BG,
  },
  navRowPressed: {
    backgroundColor: "#45475a",
  },
  navLabel: {
    flex: 1,
    color: TEXT,
    fontSize: 14,
  },
  navLabelActive: {
    fontWeight: "700",
  },
  navCount: {
    color: MUTED,
    fontSize: 12,
  },
  segment: {
    marginVertical: 8,
  },
  panel: {
    flex: 1,
    minHeight: 0,
    borderTopColor: "#313244",
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  groupHeader: {
    height: 42,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 8,
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
  statusDot: {
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 2,
    marginLeft: 16,
    marginTop: 16,
  },
});
