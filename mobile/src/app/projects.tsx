import React, { useCallback, useMemo, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { goBackOrReplace } from "../lib/navigation";
import {
  Button,
  Dialog,
  FAB,
  Icon,
  IconButton,
  Menu,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useAuth } from "../contexts/AuthContext";
import { projectsRepo } from "../repositories";
import { useProject } from "../contexts/ProjectContext";
import { getProjectCapabilities } from "../lib/project-api";
import { ProjectColorPicker } from "../components/project-color-picker";
import { ScreenHeader } from "../components/screen-header";
import { EmptyState, ErrorState, LoadingState } from "../components/screen-primitives";
import {
  getProjectColor,
  DEFAULT_PROJECT_COLOR,
  normalizeProjectColor,
} from "../lib/project-colors";
import type { Project, Space } from "../types/api";

/** space_id を持たないプロジェクトをまとめる擬似スペースの ID。 */
const UNGROUPED_ID = "__ungrouped__";

export default function ProjectsScreen() {
  const router = useRouter();
  const { isAuthenticated, user } = useAuth();
  const {
    spaces,
    selectedSpaceId,
    selectedProjectId,
    setSelectedSpaceId,
    setSelectedProjectId,
    refreshProjects,
  } = useProject();
  const [projects, setProjects] = useState<Project[]>([]);
  const [dialogVisible, setDialogVisible] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [color, setColor] = useState(DEFAULT_PROJECT_COLOR);
  const [spaceId, setSpaceId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [menuProjectId, setMenuProjectId] = useState<string | null>(null);
  // 初回は全スペース展開。以降はユーザー操作を保持する。
  const [collapsedSpaces, setCollapsedSpaces] = useState<Set<string>>(
    () => new Set(),
  );
  const [expandedClosed, setExpandedClosed] = useState<Set<string>>(
    () => new Set(),
  );
  // Project creation is a global action.  Keep anonymous local-first
  // projects available, but do not expose mutation controls to a read-only
  // account even when the server returns projects it can read.
  const canCreateProject =
    !user || !["viewer", "readonly", "read_only"].includes(user.role.toLowerCase());

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setProjects(await projectsRepo.list());
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "プロジェクトの読み込みに失敗しました。",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
      void refreshProjects();
    }, [load, refreshProjects]),
  );

  // スペースごとに未完了 / 完了へ振り分ける（WebUI のプロジェクト管理と同じ構図）。
  const { activeBySpace, closedBySpace, hasUngrouped } = useMemo(() => {
    const active = new Map<string, Project[]>();
    const closed = new Map<string, Project[]>();
    let ungrouped = false;
    for (const project of projects) {
      const key = project.space_id ?? UNGROUPED_ID;
      if (key === UNGROUPED_ID) ungrouped = true;
      const target = project.is_completed ? closed : active;
      const list = target.get(key) ?? [];
      list.push(project);
      target.set(key, list);
    }
    const byName = (a: Project, b: Project) => a.name.localeCompare(b.name);
    for (const list of active.values()) list.sort(byName);
    for (const list of closed.values()) list.sort(byName);
    return {
      activeBySpace: active,
      closedBySpace: closed,
      hasUngrouped: ungrouped,
    };
  }, [projects]);

  const toggleSpace = (id: string) => {
    setCollapsedSpaces((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleClosed = (id: string) => {
    setExpandedClosed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const openCreate = () => {
    if (!canCreateProject) return;
    setEditing(null);
    setName("");
    setDescription("");
    setColor(DEFAULT_PROJECT_COLOR);
    setSpaceId(selectedSpaceId ?? spaces[0]?.id ?? null);
    setDialogVisible(true);
  };

  const openEdit = (project: Project) => {
    if (!getProjectCapabilities(project, user).canManageSettings) return;
    setEditing(project);
    setName(project.name);
    setDescription(project.description ?? "");
    setColor(getProjectColor(project));
    setSpaceId(project.space_id ?? null);
    setDialogVisible(true);
  };

  const handleSave = async () => {
    if (!name.trim()) return;
    if (
      (editing && !getProjectCapabilities(editing, user).canManageSettings) ||
      (!editing && !canCreateProject)
    ) {
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await projectsRepo.update(editing.id, {
          name: name.trim(),
          description: description.trim() || null,
          space_id: spaceId,
          project_metadata: {
            ...(editing.metadata ?? {}),
            color: normalizeProjectColor(color),
          },
        });
      } else {
        const created = await projectsRepo.create({
          name: name.trim(),
          description: description.trim() || null,
          space_id: spaceId,
          project_metadata: { color: normalizeProjectColor(color) },
        });
        await setSelectedProjectId(created.id);
      }
      setDialogVisible(false);
      await refreshProjects();
      await load();
    } catch (error) {
      Alert.alert(
        "Project",
        error instanceof Error ? error.message : "Project save failed",
      );
    } finally {
      setSaving(false);
    }
  };

  const handleToggleCompleted = async (project: Project) => {
    if (!getProjectCapabilities(project, user).canManageSettings) return;
    try {
      await projectsRepo.update(project.id, {
        is_completed: !project.is_completed,
      });
      await refreshProjects();
      await load();
    } catch (error) {
      Alert.alert(
        "Project",
        error instanceof Error ? error.message : "完了状態の更新に失敗しました",
      );
    }
  };

  const handleDelete = (project: Project) => {
    if (!getProjectCapabilities(project, user).canDelete) return;
    Alert.alert("Delete Project", `${project.name} を削除しますか？`, [
      { text: "Cancel", style: "cancel" },
      {
        text: "Delete",
        style: "destructive",
        onPress: async () => {
          try {
            await projectsRepo.delete(project.id);
            await refreshProjects();
            await load();
          } catch (error) {
            Alert.alert(
              "Project",
              error instanceof Error ? error.message : "Delete failed",
            );
          }
        },
      },
    ]);
  };

  const renderProjectRow = (project: Project) => {
    const active = selectedProjectId === project.id;
    const completed = Boolean(project.is_completed);
    const capabilities = getProjectCapabilities(project, user);
    return (
      <Pressable
        key={project.id}
        style={[styles.row, active && styles.rowActive]}
        onPress={() => setSelectedProjectId(project.id)}
      >
        <View
          style={[styles.colorDot, { backgroundColor: getProjectColor(project) }]}
        />
        <View style={styles.rowBody}>
          <Text
            numberOfLines={1}
            style={[styles.rowTitle, completed && styles.rowTitleCompleted]}
          >
            {project.name}
          </Text>
          <Text numberOfLines={1} style={styles.rowSubtext}>
            {project.description || project.slug || "説明なし"}
          </Text>
        </View>
        {active ? (
          <Icon source="check-circle" size={16} color="#7c3aed" />
        ) : null}
        <Menu
          visible={menuProjectId === project.id}
          onDismiss={() => setMenuProjectId(null)}
          anchor={
            <IconButton
              icon="dots-vertical"
              size={18}
              iconColor="#a6adc8"
              style={styles.rowMenuButton}
              onPress={() => setMenuProjectId(project.id)}
            />
          }
          contentStyle={styles.menuContent}
        >
          {isAuthenticated ? (
            <Menu.Item
              title="詳細"
              leadingIcon="information-outline"
              onPress={() => {
                setMenuProjectId(null);
                router.push(`/project/${project.id}`);
              }}
            />
          ) : null}
          {capabilities.canManageSettings ? (
            <Menu.Item
              title="編集"
              leadingIcon="pencil"
              onPress={() => {
                setMenuProjectId(null);
                openEdit(project);
              }}
            />
          ) : null}
          {capabilities.canManageSettings ? (
            <Menu.Item
              title={completed ? "未完了に戻す" : "完了にする"}
              leadingIcon={completed ? "restore" : "check-circle-outline"}
              onPress={() => {
                setMenuProjectId(null);
                void handleToggleCompleted(project);
              }}
            />
          ) : null}
          {capabilities.canDelete ? (
            <Menu.Item
              title="削除"
              leadingIcon="trash-can-outline"
              onPress={() => {
                setMenuProjectId(null);
                handleDelete(project);
              }}
            />
          ) : null}
        </Menu>
      </Pressable>
    );
  };

  const renderGroup = (
    id: string,
    groupName: string,
    selectable: boolean,
  ) => {
    const activeProjects = activeBySpace.get(id) ?? [];
    const closedProjects = closedBySpace.get(id) ?? [];
    const expanded = !collapsedSpaces.has(id);
    const closedExpanded = expandedClosed.has(id);
    const spaceSelected = selectable && selectedSpaceId === id;

    return (
      <Surface
        key={id}
        style={[styles.group, spaceSelected && styles.groupActive]}
        elevation={0}
      >
        <Pressable style={styles.groupHeader} onPress={() => toggleSpace(id)}>
          <Icon
            source={expanded ? "chevron-down" : "chevron-right"}
            size={18}
            color="#a6adc8"
          />
          <Icon source="layers-outline" size={15} color="#a6adc8" />
          <Text numberOfLines={1} style={styles.groupTitle}>
            {groupName}
          </Text>
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{activeProjects.length}</Text>
          </View>
          {selectable ? (
            <IconButton
              icon="target"
              size={16}
              iconColor={spaceSelected ? "#7c3aed" : "#6c7086"}
              style={styles.groupScopeButton}
              accessibilityLabel={`${groupName} をスコープに設定`}
              onPress={() => setSelectedSpaceId(id)}
            />
          ) : null}
        </Pressable>

        {expanded ? (
          <View style={styles.groupBody}>
            {activeProjects.length === 0 && closedProjects.length === 0 ? (
              <Text style={styles.groupEmpty}>プロジェクトなし</Text>
            ) : (
              activeProjects.map(renderProjectRow)
            )}

            {closedProjects.length > 0 ? (
              <View style={styles.closedSection}>
                <Pressable
                  style={styles.closedHeader}
                  onPress={() => toggleClosed(id)}
                >
                  <Icon
                    source={closedExpanded ? "chevron-down" : "chevron-right"}
                    size={16}
                    color="#a6adc8"
                  />
                  <Icon source="check-circle" size={14} color="#a6e3a1" />
                  <Text style={styles.closedTitle}>完了</Text>
                  <View style={styles.badge}>
                    <Text style={styles.badgeText}>
                      {closedProjects.length}
                    </Text>
                  </View>
                </Pressable>
                {closedExpanded ? (
                  <View style={styles.closedBody}>
                    {closedProjects.map(renderProjectRow)}
                  </View>
                ) : null}
              </View>
            ) : null}
          </View>
        ) : null}
      </Surface>
    );
  };

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Projects"
        subtitle="選択中のプロジェクトと管理設定"
        onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
      />

      <ScrollView contentContainerStyle={styles.content}>
        {loading && projects.length === 0 ? (
          <LoadingState label="プロジェクトを読み込み中…" />
        ) : loadError && projects.length === 0 ? (
          <ErrorState
            message={loadError}
            action={
              <Button mode="outlined" onPress={() => void load()}>
                再読み込み
              </Button>
            }
          />
        ) : (
          <>
            {spaces.map((space: Space) => renderGroup(space.id, space.name, true))}
            {hasUngrouped ? renderGroup(UNGROUPED_ID, "未分類", false) : null}
            {spaces.length === 0 && projects.length === 0 ? (
              <EmptyState message="No projects available." />
            ) : null}
          </>
        )}
      </ScrollView>

      {canCreateProject ? (
        <FAB
          icon="plus"
          style={styles.fab}
          onPress={openCreate}
          color="#cdd6f4"
        />
      ) : null}

      <Portal>
        <Dialog
          visible={dialogVisible}
          onDismiss={() => setDialogVisible(false)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {editing ? "Edit Project" : "Create Project"}
          </Dialog.Title>
          <Dialog.Content>
            <TextInput
              label="Name"
              value={name}
              onChangeText={setName}
              mode="outlined"
              style={styles.input}
            />
            <TextInput
              label="Description"
              value={description}
              onChangeText={setDescription}
              mode="outlined"
              multiline
              style={styles.input}
            />
            <Text style={styles.fieldLabel}>スペース</Text>
            <View style={styles.spacePicker}>
              {spaces.map((space: Space) => {
                const picked = spaceId === space.id;
                return (
                  <Button
                    key={space.id}
                    compact
                    mode={picked ? "contained" : "outlined"}
                    buttonColor={picked ? "#7c3aed" : undefined}
                    textColor={picked ? "#cdd6f4" : "#a6adc8"}
                    onPress={() => setSpaceId(space.id)}
                  >
                    {space.name}
                  </Button>
                );
              })}
              <Button
                compact
                mode={spaceId === null ? "contained" : "outlined"}
                buttonColor={spaceId === null ? "#7c3aed" : undefined}
                textColor={spaceId === null ? "#cdd6f4" : "#a6adc8"}
                onPress={() => setSpaceId(null)}
              >
                未分類
              </Button>
            </View>
            <ProjectColorPicker value={color} onChange={setColor} />
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setDialogVisible(false)}>
              Cancel
            </Button>
            <Button
              textColor="#7c3aed"
              onPress={handleSave}
              disabled={saving || !name.trim()}
            >
              {saving ? "Saving..." : "Save"}
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { padding: 12, gap: 8, paddingBottom: 96 },

  group: {
    backgroundColor: "#181825",
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#313244",
    overflow: "hidden",
  },
  groupActive: { borderColor: "#7c3aed" },
  groupHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingLeft: 8,
    paddingRight: 4,
    paddingVertical: 8,
    backgroundColor: "#1e1e2e",
  },
  groupTitle: {
    color: "#cdd6f4",
    fontSize: 14,
    fontWeight: "600",
    flexShrink: 1,
  },
  groupScopeButton: { margin: 0, marginLeft: "auto" },
  groupBody: { paddingHorizontal: 6, paddingVertical: 6, gap: 4 },
  groupEmpty: {
    color: "#6c7086",
    fontSize: 12,
    paddingHorizontal: 6,
    paddingVertical: 4,
  },

  badge: {
    minWidth: 20,
    paddingHorizontal: 6,
    paddingVertical: 1,
    borderRadius: 8,
    backgroundColor: "#313244",
    alignItems: "center",
  },
  badgeText: { color: "#a6adc8", fontSize: 11 },

  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingLeft: 10,
    paddingRight: 2,
    paddingVertical: 6,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "transparent",
    backgroundColor: "#1e1e2e",
  },
  rowActive: { borderColor: "#7c3aed", backgroundColor: "#252539" },
  colorDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    borderWidth: 1,
    borderColor: "#cdd6f4",
  },
  rowBody: { flex: 1, minWidth: 0 },
  rowTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "600" },
  rowTitleCompleted: {
    color: "#6c7086",
    textDecorationLine: "line-through",
  },
  rowSubtext: { color: "#6c7086", fontSize: 11, marginTop: 1 },
  rowMenuButton: { margin: 0 },
  menuContent: { backgroundColor: "#1e1e2e" },

  closedSection: { marginTop: 2 },
  closedHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 8,
    paddingVertical: 6,
    borderRadius: 8,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: "#45475a",
  },
  closedTitle: { color: "#a6adc8", fontSize: 12, fontWeight: "600", flex: 1 },
  closedBody: { paddingLeft: 10, paddingTop: 4, gap: 4 },

  empty: { paddingTop: 80, alignItems: "center" },
  emptyText: { color: "#a6adc8" },
  fab: {
    position: "absolute",
    right: 16,
    bottom: 16,
    backgroundColor: "#7c3aed",
  },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  input: { marginBottom: 12 },
  fieldLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 6 },
  spacePicker: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    marginBottom: 12,
  },
});
