import React, { useMemo, useRef, useState } from "react";
import {
  Modal,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  SectionList,
  StyleSheet,
  View,
  type SectionListData,
  type SectionListRenderItemInfo,
} from "react-native";
import {
  Button,
  Chip,
  IconButton,
  Text,
  TextInput,
} from "react-native-paper";
import { KeyboardController } from "react-native-keyboard-controller";
import { SafeAreaView } from "react-native-safe-area-context";
import { useProject } from "../contexts/ProjectContext";
import type { Project, Space } from "../types/api";

/** A row rendered by the full-screen scope selector. */
export type ScopeRow =
  | {
      key: "all";
      type: "all";
    }
  | {
      key: string;
      type: "project";
      project: Project;
      space: Space | null;
    };

export type ScopeSection = {
  key: string;
  title: string;
  type: "all" | "space" | "no-space";
  space: Space | null;
  data: ScopeRow[];
};

export type BuildScopeSectionsOptions = {
  spaces: readonly Space[];
  projects: readonly Project[];
  /** Search by project name or by the name of its space. */
  search?: string;
  /** `query` is accepted as a readable alias for callers outside the UI. */
  query?: string;
};

/**
 * Build the virtualized selector sections without any React state.
 *
 * Projects are de-duplicated by id only. In particular, two projects with the
 * same name but different ids remain separate rows. The first occurrence wins
 * when the upstream list contains the same id more than once.
 */
export function buildScopeSections(
  options: BuildScopeSectionsOptions,
): ScopeSection[];
export function buildScopeSections(
  spaces: readonly Space[],
  projects: readonly Project[],
  search?: string,
): ScopeSection[];
export function buildScopeSections(
  optionsOrSpaces: BuildScopeSectionsOptions | readonly Space[],
  projectsArg?: readonly Project[],
  searchArg = "",
): ScopeSection[] {
  const options: BuildScopeSectionsOptions = Array.isArray(optionsOrSpaces)
    ? {
        spaces: optionsOrSpaces,
        projects: projectsArg ?? [],
        search: searchArg,
      }
    : (optionsOrSpaces as BuildScopeSectionsOptions);
  const search = (options.search ?? options.query ?? "").trim().toLocaleLowerCase();

  const spaceById = new Map<string, Space>();
  for (const space of options.spaces) {
    if (!spaceById.has(space.id)) spaceById.set(space.id, space);
  }

  const projectById = new Map<string, Project>();
  for (const project of options.projects) {
    if (project.deleted_at) continue;
    if (!projectById.has(project.id)) projectById.set(project.id, project);
  }

  const projects = [...projectById.values()];
  const matches = (value: string | null | undefined) =>
    !search || (value ?? "").toLocaleLowerCase().includes(search);
  const matchingSpaceIds = new Set(
    [...spaceById.values()]
      .filter((space) => matches(space.name))
      .map((space) => space.id),
  );

  const projectRows = projects
    .map((project) => {
      const space = project.space_id
        ? (spaceById.get(project.space_id) ?? null)
        : null;
      const matchesProject = matches(project.name);
      const matchesSpace = Boolean(space && matchingSpaceIds.has(space.id));
      return { project, space, matchesProject, matchesSpace };
    })
    .filter(({ matchesProject, matchesSpace }) => matchesProject || matchesSpace);

  const sections: ScopeSection[] = [
    {
      key: "all",
      title: "",
      type: "all",
      space: null,
      data: [{ key: "all", type: "all" }],
    },
  ];

  for (const space of spaceById.values()) {
    const data = projectRows
      .filter(({ space: projectSpace }) => projectSpace?.id === space.id)
      .map(({ project }) => ({
        key: `project-${project.id}`,
        type: "project" as const,
        project,
        space,
      }));
    // Keep an empty Space visible so its header can still select the scope.
    // When searching, hide unrelated empty spaces but retain a space-name hit.
    if (data.length === 0 && search && !matches(space.name)) continue;
    sections.push({
      key: `space-${space.id}`,
      title: space.name,
      type: "space",
      space,
      data,
    });
  }

  const ungrouped = projectRows
    .filter(({ space }) => !space)
    .map(({ project }) => ({
      key: `project-${project.id}`,
      type: "project" as const,
      project,
      space: null,
    }));
  if (ungrouped.length > 0) {
    sections.push({
      key: "no-space",
      title: "未所属・ローカル／所属情報未取得",
      type: "no-space",
      space: null,
      data: ungrouped,
    });
  }

  return sections;
}

type ScopeSwitcherProps = {
  label?: string;
  variant?: "button" | "chip" | "inline";
  accessibilityLabel?: string;
  projects?: Project[];
  projectId?: string | null;
  onSelectProject?: (id: string | null) => void | Promise<void>;
  allowAll?: boolean;
  allLabel?: string;
  disabled?: boolean;
  allowSpaceSelection?: boolean;
  /** Compact chat header: display the project name, not its space path. */
  projectNameOnly?: boolean;
};

export function projectScopeLabel(project: Project, spaces: readonly Space[], projects: readonly Project[] = []): string {
  const space = spaces.find((item) => item.id === project.space_id);
  const parent = space
    ? `スペース: ${space.name}${spaces.some((item) => item.id !== space.id && item.name === space.name) ? ` [${space.id}]` : ""}`
    : project.space_id ? `所属情報未取得 [${project.space_id}]`
      : project.metadata?.local_only ? "ローカル" : "未所属";
  const duplicate = projects.some((item) => item.id !== project.id && item.name === project.name && item.space_id === project.space_id);
  return `${parent} / ${project.name}${duplicate ? ` [${project.id}]` : ""}`;
}

function spaceScopeLabel(space: Space, spaces: readonly Space[]): string {
  return `スペース: ${space.name}${spaces.some((item) => item.id !== space.id && item.name === space.name) ? ` [${space.id}]` : ""} / 全プロジェクト`;
}

export function ScopeSwitcher({
  label,
  variant = "button",
  accessibilityLabel,
  projects: projectsOverride,
  projectId,
  onSelectProject,
  allowAll = true,
  allLabel = "すべてのプロジェクト",
  disabled = false,
  allowSpaceSelection = false,
  projectNameOnly = false,
}: ScopeSwitcherProps) {
  const {
    spaces,
    projects: contextProjects,
    selectedSpaceId,
    selectedProjectId: contextProjectId,
    selectedSpace,
    setSelectedSpaceId,
    setSelectedProjectId,
    refreshProjects,
  } = useProject();
  const projects = projectsOverride ?? contextProjects;
  const selectedProjectId = onSelectProject ? projectId : contextProjectId;
  const selectedProject = projects.find((project) => project.id === selectedProjectId);
  const [visible, setVisible] = useState(false);
  const [search, setSearch] = useState("");
  const [saving, setSaving] = useState(false);
  const selectionInFlight = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const currentScopeLabel = projectNameOnly
    ? (selectedProject?.name || (selectedProjectId ? "プロジェクト" : allLabel))
    : selectedProject
    ? projectScopeLabel(selectedProject, spaces, projects)
    : selectedProjectId ? `取得できないプロジェクト [${selectedProjectId}]`
    : selectedSpace && (!onSelectProject || allowSpaceSelection)
      ? spaceScopeLabel(selectedSpace, spaces)
      : allLabel;

  const close = () => void select(async () => {});
  const open = () => {
    setError(null);
    setVisible(true);
    void refreshProjects();
  };

  const select = async (operation: () => void | Promise<void>) => {
    if (selectionInFlight.current) return;
    selectionInFlight.current = true;
    setSaving(true);
    setError(null);
    try {
      // Keep the native Modal mounted until the IME hide event reaches the
      // root keyboard-aware layout. Removing a focused Modal first can leave
      // the whole app (including its bottom tabs) at the keyboard-open height.
      await KeyboardController.dismiss();
      await operation();
      setVisible(false);
      setSearch("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "範囲の変更に失敗しました。再選択して再試行できます。");
    } finally { selectionInFlight.current = false; setSaving(false); }
  };
  const selectProject = (id: string | null) => void select(async () => {
    await (onSelectProject ?? setSelectedProjectId)(id);
    if (onSelectProject && allowSpaceSelection) await setSelectedProjectId(id);
  });
  const selectSpace = (id: string) => void select(async () => {
    if (onSelectProject) await onSelectProject(null);
    await setSelectedSpaceId(id);
  });

  const sections = useMemo(
    () => buildScopeSections({ spaces, projects, search }).filter((section) => allowAll || section.type !== "all"),
    [projects, search, spaces, allowAll],
  );

  const anchor =
    variant === "inline" ? (
      <Pressable
        testID="scope-switcher-inline"
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel ?? `範囲: ${currentScopeLabel}`}
        accessibilityState={{ disabled }}
        disabled={disabled}
        onPress={open}
        style={({ pressed }) => [styles.inline, pressed && styles.rowPressed]}
      >
        <Text testID="scope-switcher-inline-label" numberOfLines={1} ellipsizeMode="tail" style={[styles.inlineText, projectNameOnly && styles.projectName]}>
          {currentScopeLabel.replace(/^スペース: /, "")}
        </Text>
        <Text style={styles.inlineChevron} importantForAccessibility="no">⌄</Text>
      </Pressable>
    ) : variant === "chip" ? (
      <Chip
        compact
        icon="tune-variant"
        accessibilityLabel={
          accessibilityLabel ?? `範囲: ${currentScopeLabel}`
        }
        onPress={open}
        disabled={disabled}
        style={styles.chip}
        textStyle={styles.chipText}
      >
        {label ? `${label}: ${currentScopeLabel}` : currentScopeLabel}
      </Chip>
    ) : (
      <Button
        compact
        mode="outlined"
        icon="target"
        accessibilityLabel={
          accessibilityLabel ?? `範囲: ${currentScopeLabel}`
        }
        textColor="#cdd6f4"
        style={styles.button}
        contentStyle={styles.buttonContent}
        onPress={open}
        disabled={disabled}
      >
        {label ? `${label}: ${currentScopeLabel}` : currentScopeLabel}
      </Button>
    );

  const renderItem = ({ item }: SectionListRenderItemInfo<ScopeRow>) => {
    if (item.type === "all") {
      const selected = !selectedProjectId && ((Boolean(onSelectProject) && !allowSpaceSelection) || !selectedSpaceId);
      return (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ selected }}
          accessibilityLabel={allLabel}
          disabled={saving}
          onPress={() => selectProject(null)}
          style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
        >
          <Text style={styles.rowTitle}>{allLabel}</Text>
          {selected ? <Text style={styles.check}>✓</Text> : null}
        </Pressable>
      );
    }

    const selected = item.project.id === selectedProjectId;
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ selected }}
        accessibilityLabel={`${projectScopeLabel(item.project, spaces, projects)}を選択`}
        disabled={saving}
        onPress={() => selectProject(item.project.id)}
        style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      >
        <View style={styles.rowText}>
          <Text style={styles.rowTitle}>
            {item.project.name}
          </Text>
          {(
            <Text style={styles.rowSecondary}>
              {projectScopeLabel(item.project, spaces, projects)}
            </Text>
          )}
        </View>
        {selected ? <Text style={styles.check}>✓</Text> : null}
      </Pressable>
    );
  };

  const renderSectionHeader = ({
    section,
  }: {
    section: SectionListData<ScopeRow, ScopeSection>;
  }) => {
    if (section.type === "no-space" || (onSelectProject && !allowSpaceSelection)) {
      if (section.type === "all") return null;
      return (
        <View style={styles.sectionHeader}>
          <Text style={styles.sectionTitle}>{section.title}</Text>
        </View>
      );
    }
    if (section.type !== "space" || !section.space) return null;
    const selected = !selectedProjectId && selectedSpaceId === section.space.id;
    return (
      <Pressable
        accessibilityRole="button"
        disabled={saving}
        accessibilityState={{ selected }}
        accessibilityLabel={`${spaceScopeLabel(section.space, spaces)}を選択`}
        onPress={() => selectSpace(section.space!.id)}
        style={({ pressed }) => [
          styles.sectionHeader,
          pressed && styles.rowPressed,
        ]}
      >
        <Text style={styles.sectionTitle}>{spaceScopeLabel(section.space, spaces)}</Text>
        {selected ? <Text style={styles.check}>✓</Text> : null}
      </Pressable>
    );
  };

  return (
    <>
      {anchor}
      <Modal
        visible={visible}
        animationType="slide"
        presentationStyle="fullScreen"
        onRequestClose={close}
      >
        <SafeAreaView style={styles.modal} edges={["top", "bottom"]}>
          <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : "height"}>
          <View style={styles.modalHeader}>
            <Text variant="titleLarge" style={styles.modalTitle}>
              表示範囲
            </Text>
            <IconButton
              icon="close"
              accessibilityLabel="閉じる"
              onPress={close}
              iconColor="#cdd6f4"
            />
          </View>
          <Text style={styles.currentScope}>現在: {currentScopeLabel}</Text>
          {error ? <Text accessibilityRole="alert" style={styles.currentScope}>{error}</Text> : null}
          <TextInput
            value={search}
            onChangeText={setSearch}
            mode="outlined"
            dense
            placeholder="プロジェクト・スペースを検索"
            accessibilityLabel="プロジェクト・スペースを検索"
            style={styles.searchInput}
            left={<TextInput.Icon icon="magnify" />}
          />
          <SectionList
            style={{ flex: 1 }}
            sections={sections}
            keyExtractor={(item) => item.key}
            renderItem={renderItem}
            renderSectionHeader={renderSectionHeader}
            stickySectionHeadersEnabled={false}
            keyboardShouldPersistTaps="handled"
            contentContainerStyle={styles.listContent}
            ListEmptyComponent={
              <Text style={styles.emptyText}>該当する範囲がありません</Text>
            }
          />
          </KeyboardAvoidingView>
        </SafeAreaView>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  inline: { flex: 1, minWidth: 0, minHeight: 44, flexDirection: "row", alignItems: "center", paddingLeft: 6, paddingRight: 2 },
  inlineText: { flex: 1, minWidth: 0, color: "#cdd6f4", fontSize: 12 },
  projectName: { fontSize: 13, fontWeight: "600" },
  inlineChevron: { color: "#a6adc8", fontSize: 16, marginLeft: 2 },
  chip: { backgroundColor: "#313244", maxWidth: 190 },
  chipText: { color: "#cdd6f4" },
  button: { borderColor: "#45475a" },
  buttonContent: { minHeight: 34 },
  modal: { flex: 1, backgroundColor: "#11111b" },
  modalHeader: {
    minHeight: 56,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 16,
    paddingRight: 4,
  },
  modalTitle: { color: "#cdd6f4", fontWeight: "700" },
  searchInput: { marginHorizontal: 12, marginBottom: 8 },
  currentScope: { color: "#cdd6f4", marginHorizontal: 16, marginBottom: 8 },
  listContent: { paddingHorizontal: 12, paddingBottom: 24 },
  sectionHeader: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 12,
    marginTop: 8,
    borderRadius: 8,
    backgroundColor: "#1e1e2e",
  },
  sectionTitle: { color: "#bac2de", fontSize: 14, fontWeight: "700", flex: 1, paddingVertical: 8 },
  row: {
    minHeight: 52,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#313244",
  },
  rowPressed: { opacity: 0.72 },
  rowText: { flex: 1, minWidth: 0, paddingVertical: 7 },
  rowTitle: { color: "#cdd6f4", fontSize: 15 },
  rowSecondary: { color: "#9399b2", fontSize: 12, marginTop: 2 },
  check: { color: "#a6e3a1", fontSize: 18, marginLeft: 12 },
  emptyText: { color: "#9399b2", textAlign: "center", paddingTop: 32 },
});
