import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { format } from "date-fns";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  Divider,
  Portal,
  Text,
} from "react-native-paper";
import { tasksRepo, timeEntriesRepo } from "../repositories";
import { useProject } from "../contexts/ProjectContext";
import { taskApi } from "../lib/task-api";
import type { Space, Task, TimeEntry } from "../types/api";

function formatDuration(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const remainder = safeSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "-";
  return format(parsed, "yyyy/MM/dd HH:mm");
}

function formatTimeRange(
  startedAt?: string | null,
  endedAt?: string | null,
): string {
  if (!startedAt) return "-";
  const endText = endedAt ? formatDateTime(endedAt).slice(-5) : "Running";
  return `${formatDateTime(startedAt)} -> ${endText}`;
}

type TaskQuickViewDialogProps = {
  taskId: string | null;
  visible: boolean;
  entryFocus?: TimeEntry | null;
  onDismiss: () => void;
  onTaskChanged?: () => void;
};

export function TaskQuickViewDialog({
  taskId,
  visible,
  entryFocus,
  onDismiss,
  onTaskChanged,
}: TaskQuickViewDialogProps) {
  const router = useRouter();
  const { projects } = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [activeEntry, setActiveEntry] = useState<TimeEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [timerSaving, setTimerSaving] = useState(false);
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectPickerVisible, setProjectPickerVisible] = useState(false);
  const [timerElapsed, setTimerElapsed] = useState(0);

  const loadTask = useCallback(async () => {
    if (!taskId) {
      setTask(null);
      setActiveEntry(null);
      return;
    }
    setLoading(true);
    try {
      const [nextTask, nextActiveEntry] = await Promise.all([
        tasksRepo.get(taskId),
        timeEntriesRepo.getActive(taskId),
      ]);
      setTask(nextTask);
      setActiveEntry(nextActiveEntry);
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    if (!visible) return;
    void loadTask();
  }, [loadTask, visible]);

  useEffect(() => {
    if (!visible) {
      setProjectPickerVisible(false);
      return;
    }
    let active = true;
    void taskApi
      .listSpaces()
      .then((list) => {
        if (active) setSpaces(list);
      })
      .catch(() => {
        if (active) setSpaces([]);
      });
    return () => {
      active = false;
    };
  }, [visible]);

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

  const trackedTotal = useMemo(() => {
    if (activeEntry) return timerElapsed;
    return Math.max(
      task?.total_time_seconds ?? 0,
      entryFocus?.duration_seconds ?? 0,
    );
  }, [
    activeEntry,
    entryFocus?.duration_seconds,
    task?.total_time_seconds,
    timerElapsed,
  ]);

  const currentProject = useMemo(
    () =>
      task?.project_id
        ? (projects.find((project) => project.id === task.project_id) ?? null)
        : null,
    [projects, task?.project_id],
  );

  const currentSpaceName = useMemo(() => {
    if (!currentProject?.space_id) return null;
    return (
      spaces.find((space) => space.id === currentProject.space_id)?.name ?? null
    );
  }, [currentProject?.space_id, spaces]);

  const projectGroups = useMemo(() => {
    const groups = spaces
      .map((space) => ({
        key: space.id,
        label: space.name,
        projects: projects.filter((project) => project.space_id === space.id),
      }))
      .filter((group) => group.projects.length > 0);
    const ungrouped = projects.filter((project) => !project.space_id);
    if (ungrouped.length > 0) {
      groups.push({
        key: "no-space",
        label: "No Space",
        projects: ungrouped,
      });
    }
    if (groups.length === 0 && projects.length > 0) {
      groups.push({
        key: "all-projects",
        label: "Projects",
        projects,
      });
    }
    return groups;
  }, [projects, spaces]);

  const handleTimerToggle = useCallback(async () => {
    if (!taskId) return;
    setTimerSaving(true);
    try {
      if (activeEntry) {
        await timeEntriesRepo.stopTimer(activeEntry.id);
        setActiveEntry(null);
      } else {
        const nextEntry = await timeEntriesRepo.startTimer(taskId);
        setActiveEntry(nextEntry);
      }
      await loadTask();
      onTaskChanged?.();
    } finally {
      setTimerSaving(false);
    }
  }, [activeEntry, loadTask, onTaskChanged, taskId]);

  const handleOpenDetail = useCallback(() => {
    if (!taskId) return;
    onDismiss();
    router.push(`/(tabs)/tasks/${taskId}`);
  }, [onDismiss, router, taskId]);

  const handleMoveTask = useCallback(
    async (projectId: string) => {
      if (!taskId || !task || !projectId || projectId === task.project_id) {
        setProjectPickerVisible(false);
        return;
      }
      setProjectSaving(true);
      try {
        const updatedTask = await tasksRepo.update(taskId, {
          project_id: projectId,
        });
        setTask(updatedTask);
        setProjectPickerVisible(false);
        await loadTask();
        onTaskChanged?.();
      } finally {
        setProjectSaving(false);
      }
    },
    [loadTask, onTaskChanged, task, taskId],
  );

  return (
    <Portal>
      <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
        <Dialog.Title style={styles.dialogTitle}>Task</Dialog.Title>
        <Dialog.Content>
          {loading ? (
            <View style={styles.loadingWrap}>
              <ActivityIndicator size="small" color="#7c3aed" />
            </View>
          ) : task ? (
            <View style={styles.content}>
              <View style={styles.scopeRow}>
                {currentSpaceName ? (
                  <Chip
                    compact
                    icon="folder-multiple-outline"
                    onPress={() => setProjectPickerVisible(true)}
                    style={styles.scopeChip}
                    textStyle={styles.scopeChipText}
                    disabled={projectSaving || projects.length <= 1}
                  >
                    {currentSpaceName}
                  </Chip>
                ) : null}
                {task.project_name ? (
                  <Chip
                    compact
                    icon="folder-outline"
                    onPress={() => setProjectPickerVisible(true)}
                    style={styles.scopeChip}
                    textStyle={styles.scopeChipText}
                    disabled={projectSaving || projects.length <= 1}
                  >
                    {task.project_name}
                  </Chip>
                ) : null}
              </View>
              <Text style={styles.titleText}>
                {task.title || "Untitled task"}
              </Text>
              <Text style={styles.metaText}>
                {activeEntry ? "Running" : "Tracked"}{" "}
                {formatDuration(trackedTotal)}
              </Text>
              {entryFocus ? (
                <>
                  <Divider style={styles.divider} />
                  <Text style={styles.sectionLabel}>Selected Entry</Text>
                  <Text style={styles.entryMetaText}>
                    {formatTimeRange(
                      entryFocus.started_at,
                      entryFocus.ended_at,
                    )}
                  </Text>
                  <Text style={styles.entryMetaText}>
                    Duration {formatDuration(entryFocus.duration_seconds || 0)}
                  </Text>
                  {entryFocus.note ? (
                    <Text style={styles.noteText}>{entryFocus.note}</Text>
                  ) : null}
                </>
              ) : null}

              {activeEntry ? (
                <>
                  <Divider style={styles.divider} />
                  <Text style={styles.sectionLabel}>Active Timer</Text>
                  <Text style={styles.entryMetaText}>
                    {formatTimeRange(
                      activeEntry.started_at,
                      activeEntry.ended_at,
                    )}
                  </Text>
                </>
              ) : null}
            </View>
          ) : (
            <Text style={styles.metaText}>Task not found.</Text>
          )}
        </Dialog.Content>
        <Dialog.Actions>
          <Button onPress={onDismiss} textColor="#a6adc8">
            Close
          </Button>
          <Button
            onPress={() => void handleTimerToggle()}
            loading={timerSaving}
            disabled={loading || projectSaving || !taskId}
            textColor={activeEntry ? "#f38ba8" : "#a6e3a1"}
          >
            {activeEntry ? "Stop Timer" : "Start Timer"}
          </Button>
          <Button
            onPress={handleOpenDetail}
            disabled={!taskId}
            textColor="#89b4fa"
          >
            Open Detail
          </Button>
        </Dialog.Actions>
      </Dialog>

      <Dialog
        visible={projectPickerVisible}
        onDismiss={() => setProjectPickerVisible(false)}
        style={styles.dialog}
      >
        <Dialog.Title style={styles.dialogTitle}>Move Task</Dialog.Title>
        <Dialog.Content>
          <ScrollView style={styles.projectPickerScroll}>
            {projectGroups.map((group) => (
              <View key={group.key} style={styles.projectGroup}>
                <Text style={styles.sectionLabel}>{group.label}</Text>
                {group.projects.map((project) => {
                  const selected = project.id === task?.project_id;
                  return (
                    <Button
                      key={project.id}
                      mode={selected ? "contained-tonal" : "text"}
                      onPress={() => void handleMoveTask(project.id)}
                      disabled={projectSaving}
                      loading={projectSaving && selected}
                      contentStyle={styles.projectButtonContent}
                      style={styles.projectButton}
                      textColor={selected ? "#cdd6f4" : "#89b4fa"}
                    >
                      {project.name}
                    </Button>
                  );
                })}
              </View>
            ))}
          </ScrollView>
        </Dialog.Content>
        <Dialog.Actions>
          <Button
            onPress={() => setProjectPickerVisible(false)}
            textColor="#a6adc8"
          >
            Close
          </Button>
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

const styles = StyleSheet.create({
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  loadingWrap: { paddingVertical: 20, alignItems: "center" },
  content: { gap: 6 },
  scopeRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  scopeChip: { backgroundColor: "#313244" },
  scopeChipText: { color: "#cdd6f4" },
  projectText: { color: "#89b4fa", fontSize: 12 },
  titleText: { color: "#cdd6f4", fontSize: 18, fontWeight: "700" },
  metaText: { color: "#a6adc8", fontSize: 13 },
  sectionLabel: {
    color: "#7c3aed",
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 2,
  },
  entryMetaText: { color: "#bac2de", fontSize: 13 },
  noteText: { color: "#cdd6f4", fontSize: 13 },
  divider: { backgroundColor: "#313244", marginVertical: 10 },
  projectPickerScroll: { maxHeight: 320 },
  projectGroup: { marginBottom: 12 },
  projectButton: { alignItems: "flex-start" },
  projectButtonContent: { justifyContent: "flex-start" },
});
