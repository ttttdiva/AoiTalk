import React, { useMemo, useState } from "react";
import {
  Modal,
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
      title: "スペースなし",
      type: "no-space",
      space: null,
      data: ungrouped,
    });
  }

  return sections;
}

type ScopeSwitcherProps = {
  label?: string;
  variant?: "button" | "chip";
  accessibilityLabel?: string;
};

export function ScopeSwitcher({
  label = "範囲",
  variant = "button",
  accessibilityLabel,
}: ScopeSwitcherProps) {
  const {
    spaces,
    projects,
    selectedSpaceId,
    selectedProjectId,
    selectedSpace,
    selectedProject,
    setSelectedSpaceId,
    setSelectedProjectId,
    refreshProjects,
  } = useProject();
  const [visible, setVisible] = useState(false);
  const [search, setSearch] = useState("");

  const currentScopeLabel = selectedProject
    ? selectedProject.name
    : selectedSpace
      ? selectedSpace.name
      : "すべてのプロジェクト";

  const close = () => {
    setVisible(false);
    setSearch("");
  };
  const open = () => {
    setVisible(true);
    void refreshProjects();
  };

  const selectAll = () => {
    setSelectedProjectId(null);
    close();
  };

  const selectSpace = (id: string) => {
    setSelectedSpaceId(id);
    close();
  };

  const selectProject = (id: string) => {
    setSelectedProjectId(id);
    close();
  };

  const sections = useMemo(
    () => buildScopeSections({ spaces, projects, search }),
    [projects, search, spaces],
  );

  const anchor =
    variant === "chip" ? (
      <Chip
        compact
        icon="tune-variant"
        accessibilityLabel={
          accessibilityLabel ?? `範囲: ${currentScopeLabel}`
        }
        onPress={open}
        style={styles.chip}
        textStyle={styles.chipText}
      >
        {label}
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
      >
        {label}
      </Button>
    );

  const renderItem = ({ item }: SectionListRenderItemInfo<ScopeRow>) => {
    if (item.type === "all") {
      const selected = !selectedProjectId && !selectedSpaceId;
      return (
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ selected }}
          accessibilityLabel="すべてのプロジェクト"
          onPress={selectAll}
          style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
        >
          <Text style={styles.rowTitle}>すべてのプロジェクト</Text>
          {selected ? <Text style={styles.check}>✓</Text> : null}
        </Pressable>
      );
    }

    const selected = item.project.id === selectedProjectId;
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ selected }}
        accessibilityLabel={`${item.project.name}を選択`}
        onPress={() => selectProject(item.project.id)}
        style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      >
        <View style={styles.rowText}>
          <Text style={styles.rowTitle} numberOfLines={1}>
            {item.project.name}
          </Text>
          {item.space ? (
            <Text style={styles.rowSecondary} numberOfLines={1}>
              {item.space.name}
            </Text>
          ) : null}
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
    if (section.type === "no-space") {
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
        accessibilityState={{ selected }}
        accessibilityLabel={`スペース: ${section.space.name}を選択`}
        onPress={() => selectSpace(section.space!.id)}
        style={({ pressed }) => [
          styles.sectionHeader,
          pressed && styles.rowPressed,
        ]}
      >
        <Text style={styles.sectionTitle}>スペース: {section.space.name}</Text>
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
        </SafeAreaView>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  chip: { backgroundColor: "#313244", maxWidth: 190 },
  chipText: { color: "#cdd6f4" },
  button: { borderColor: "#45475a" },
  buttonContent: { height: 34 },
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
  sectionTitle: { color: "#bac2de", fontSize: 14, fontWeight: "700" },
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
