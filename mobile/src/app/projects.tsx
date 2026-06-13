import React, { useCallback, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { goBackOrReplace } from "../lib/navigation";
import {
  Button,
  Dialog,
  FAB,
  IconButton,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useAuth } from "../contexts/AuthContext";
import { projectsRepo } from "../repositories";
import { useProject } from "../contexts/ProjectContext";
import { ProjectColorPicker } from "../components/project-color-picker";
import {
  getProjectColor,
  DEFAULT_PROJECT_COLOR,
  normalizeProjectColor,
} from "../lib/project-colors";
import type { Project } from "../types/api";

export default function ProjectsScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const { selectedProjectId, setSelectedProjectId, refreshProjects } =
    useProject();
  const [projects, setProjects] = useState<Project[]>([]);
  const [dialogVisible, setDialogVisible] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [color, setColor] = useState(DEFAULT_PROJECT_COLOR);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    const next = await projectsRepo.list();
    setProjects(next);
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const openCreate = () => {
    setEditing(null);
    setName("");
    setDescription("");
    setColor(DEFAULT_PROJECT_COLOR);
    setDialogVisible(true);
  };

  const openEdit = (project: Project) => {
    setEditing(project);
    setName(project.name);
    setDescription(project.description ?? "");
    setColor(getProjectColor(project));
    setDialogVisible(true);
  };

  const handleSave = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      if (editing) {
        await projectsRepo.update(editing.id, {
          name: name.trim(),
          description: description.trim() || null,
          project_metadata: {
            ...(editing.metadata ?? {}),
            color: normalizeProjectColor(color),
          },
        });
      } else {
        const created = await projectsRepo.create({
          name: name.trim(),
          description: description.trim() || null,
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

  const handleDelete = (project: Project) => {
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

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <Text variant="titleLarge" style={styles.headerTitle}>
            Projects
          </Text>
        </View>
        <Text style={styles.headerSubtext}>選択中のプロジェクトと管理設定</Text>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {projects.map((project) => {
          const active = selectedProjectId === project.id;
          return (
            <Surface
              key={project.id}
              style={[styles.card, active && styles.cardActive]}
              elevation={0}
            >
              <View style={styles.cardTop}>
                <View style={styles.cardBody}>
                  <View style={styles.titleRow}>
                    <View
                      style={[
                        styles.colorDot,
                        { backgroundColor: getProjectColor(project) },
                      ]}
                    />
                    <Text style={styles.cardTitle}>{project.name}</Text>
                  </View>
                  <Text style={styles.cardDesc}>
                    {project.description || project.slug || "No description"}
                  </Text>
                </View>
                <Button
                  compact
                  mode={active ? "contained" : "outlined"}
                  buttonColor={active ? "#7c3aed" : undefined}
                  textColor={active ? "#cdd6f4" : "#a6adc8"}
                  onPress={() => setSelectedProjectId(project.id)}
                >
                  {active ? "Selected" : "Use"}
                </Button>
              </View>
              <View style={styles.cardActions}>
                {isAuthenticated ? (
                  <Button
                    compact
                    textColor="#89b4fa"
                    onPress={() => router.push(`/project/${project.id}`)}
                  >
                    Details
                  </Button>
                ) : null}
                <Button
                  compact
                  textColor="#89b4fa"
                  onPress={() => openEdit(project)}
                >
                  Edit
                </Button>
                <Button
                  compact
                  textColor="#f38ba8"
                  onPress={() => handleDelete(project)}
                >
                  Delete
                </Button>
              </View>
            </Surface>
          );
        })}

        {projects.length === 0 ? (
          <View style={styles.empty}>
            <Text style={styles.emptyText}>No projects available.</Text>
          </View>
        ) : null}
      </ScrollView>

      <FAB
        icon="plus"
        style={styles.fab}
        onPress={openCreate}
        color="#cdd6f4"
      />

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
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginLeft: 16, marginTop: 4 },
  content: { padding: 16, gap: 12, paddingBottom: 96 },
  card: { padding: 16, backgroundColor: "#1e1e2e", borderRadius: 12 },
  cardActive: { borderWidth: 1, borderColor: "#7c3aed" },
  cardTop: { flexDirection: "row", alignItems: "flex-start", gap: 12 },
  cardBody: { flex: 1 },
  titleRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  colorDot: {
    width: 12,
    height: 12,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: "#cdd6f4",
  },
  cardTitle: { color: "#cdd6f4", fontSize: 16, fontWeight: "600" },
  cardDesc: { color: "#a6adc8", fontSize: 13, marginTop: 4 },
  cardActions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    marginTop: 8,
    gap: 8,
  },
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
});
