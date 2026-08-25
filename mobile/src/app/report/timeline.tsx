import React, { useCallback, useMemo, useState, useEffect } from "react";
import { Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { goBackOrReplace } from "../../lib/navigation";
import { format } from "date-fns";
import {
  Button,
  Chip,
  IconButton,
  Surface,
  Switch,
  Text,
} from "react-native-paper";
import {
  calculateTimeEntryDuration,
  tasksRepo,
  timeEntriesRepo,
} from "../../repositories";
import { useAuth } from "../../contexts/AuthContext";
import { useProject } from "../../contexts/ProjectContext";
import { taskApi } from "../../lib/task-api";
import { TaskQuickViewDialog } from "../../components/task-quick-view-dialog";
import { ScopeSwitcher } from "../../components/scope-switcher";
import type { Task, TimeEntry } from "../../types/api";

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

type TimelineItem =
  | {
      kind: "entry";
      id: string;
      sortAt: string;
      entry: TimeEntry;
    }
  | {
      kind: "schedule";
      id: string;
      sortAt: string;
      task: Task;
    };

function parseDateValue(value?: string | null): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function isTaskScheduledInRange(task: Task, from: Date, to: Date): boolean {
  const start = parseDateValue(task.start_at);
  const end = parseDateValue(task.end_at);
  if (!start || !end || end <= start || task.all_day) return false;
  return end >= from && start <= to;
}

function formatScheduleWindow(startAt?: string | null, endAt?: string | null) {
  const start = parseDateValue(startAt);
  const end = parseDateValue(endAt);
  if (!start || !end) return "-";
  return `${format(start, "yyyy-MM-dd HH:mm")} -> ${format(end, "HH:mm")}`;
}

export default function ReportTimelineScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const { selectedProjectId, selectedProject, selectedSpaceId, selectedSpace } =
    useProject();
  const [days, setDays] = useState<7 | 30>(7);
  const [entries, setEntries] = useState<TimeEntry[]>([]);
  const [scheduledTasks, setScheduledTasks] = useState<Task[]>([]);
  const [showScheduleFrames, setShowScheduleFrames] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<TimeEntry | null>(null);
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    let active = true;

    const loadSettings = async () => {
      if (!isAuthenticated) {
        if (active) setPrefsLoaded(true);
        return;
      }
      try {
        const settings = await taskApi.getUserSettings();
        if (!active) return;
        const timelineDays = settings.reports_view?.timeline_days;
        if (timelineDays === 7 || timelineDays === 30) {
          setDays(timelineDays);
        }
        if (typeof settings.reports_view?.show_schedule_frames === "boolean") {
          setShowScheduleFrames(settings.reports_view.show_schedule_frames);
        }
      } catch (err) {
        console.error("タイムライン表示設定の取得に失敗しました", err);
      } finally {
        if (active) setPrefsLoaded(true);
      }
    };

    void loadSettings();
    return () => {
      active = false;
    };
  }, [isAuthenticated]);

  useEffect(() => {
    if (!prefsLoaded || !isAuthenticated) return;

    void taskApi
      .updateUserSettings({
        reports_view: {
          timeline_days: days,
          show_schedule_frames: showScheduleFrames,
        },
      })
      .catch((err) => {
        console.error("タイムライン表示設定の保存に失敗しました", err);
      });
  }, [days, isAuthenticated, prefsLoaded, showScheduleFrames]);

  const load = useCallback(async () => {
    const scope = selectedSpaceId
      ? { space_id: selectedSpaceId }
      : selectedProjectId
        ? { project_id: selectedProjectId }
        : {};
    const to = new Date();
    to.setHours(23, 59, 59, 999);
    const from = new Date();
    from.setDate(from.getDate() - days);
    from.setHours(0, 0, 0, 0);
    const [data, tasks] = await Promise.all([
      timeEntriesRepo.list(
        scope,
        format(from, "yyyy-MM-dd"),
        format(to, "yyyy-MM-dd"),
      ),
      showScheduleFrames
        ? tasksRepo.listByScope(scope)
        : Promise.resolve([] as Task[]),
    ]);
    setEntries(
      [...data].sort((a, b) =>
        (b.started_at || "").localeCompare(a.started_at || ""),
      ),
    );
    setScheduledTasks(
      showScheduleFrames
        ? tasks
            .filter((task) => isTaskScheduledInRange(task, from, to))
            .sort((a, b) =>
              String(b.start_at ?? "").localeCompare(String(a.start_at ?? "")),
            )
        : [],
    );
  }, [days, selectedProjectId, selectedSpaceId, showScheduleFrames]);

  useFocusEffect(
    useCallback(() => {
      if (!prefsLoaded) return;
      void load();
    }, [load, prefsLoaded]),
  );

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60 * 1000);
    return () => clearInterval(timer);
  }, []);

  const totalSeconds = useMemo(
    () =>
      entries.reduce(
        (sum, entry) => sum + (calculateTimeEntryDuration(entry, now) || 0),
        0,
      ),
    [entries, now],
  );

  const timelineItems = useMemo<TimelineItem[]>(() => {
    const items: TimelineItem[] = entries.map((entry) => ({
      kind: "entry",
      id: entry.id,
      sortAt: entry.started_at ?? "",
      entry,
    }));
    if (showScheduleFrames) {
      items.push(
        ...scheduledTasks.map((task) => ({
          kind: "schedule" as const,
          id: `schedule-${task.id}`,
          sortAt: task.start_at ?? "",
          task,
        })),
      );
    }
    return items.sort((a, b) => b.sortAt.localeCompare(a.sortAt));
  }, [entries, scheduledTasks, showScheduleFrames]);

  const openTaskQuickView = useCallback((item: TimelineItem) => {
    const taskId = item.kind === "entry" ? item.entry.task_id : item.task.id;
    if (!taskId) return;
    setSelectedTaskId(taskId);
    setSelectedEntry(item.kind === "entry" ? item.entry : null);
  }, []);

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/reports')}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Timeline
            </Text>
            <Text style={styles.headerSubtext}>
              {selectedSpace?.name || selectedProject?.name || "All projects"}
            </Text>
          </View>
          <ScopeSwitcher accessibilityLabel="タイムラインの範囲を変更" />
        </View>
        <View style={styles.rangeRow}>
          {[7, 30].map((value) => (
            <Chip
              key={value}
              selected={days === value}
              onPress={() => setDays(value as 7 | 30)}
              style={[styles.chip, days === value && styles.chipActive]}
              textStyle={styles.chipText}
            >
              {value}d
            </Chip>
          ))}
        </View>
        <View style={styles.toggleRow}>
          <Text style={styles.toggleLabel}>予定時間の枠を表示</Text>
          <Switch
            value={showScheduleFrames}
            onValueChange={setShowScheduleFrames}
          />
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Summary</Text>
          <Text style={styles.bodyText}>Entries: {entries.length}</Text>
          <Text style={styles.bodyText}>
            Hours: {(totalSeconds / 3600).toFixed(2)}
          </Text>
        </Surface>

        {timelineItems.map((item) => (
          <Pressable key={item.id} onPress={() => openTaskQuickView(item)}>
            <Surface
              style={[
                styles.card,
                item.kind === "schedule" && styles.scheduleCard,
              ]}
              elevation={0}
            >
              <Text
                style={[
                  styles.entryTitle,
                  item.kind === "schedule" && styles.scheduleTitle,
                ]}
              >
                {item.kind === "entry"
                  ? item.entry.task_title || "Untitled task"
                  : item.task.title || "Untitled task"}
              </Text>
              <Text
                style={[
                  styles.kindBadge,
                  item.kind === "schedule"
                    ? styles.scheduleBadge
                    : styles.actualBadge,
                ]}
              >
                {item.kind === "schedule" ? "Scheduled" : "Actual"}
              </Text>
              {(
                item.kind === "entry"
                  ? item.entry.project_name
                  : item.task.project_name
              ) ? (
                <Text style={styles.projectText}>
                  {item.kind === "entry"
                    ? item.entry.project_name
                    : item.task.project_name}
                </Text>
              ) : null}
              <Text style={styles.entryMeta}>
                {item.kind === "entry"
                  ? `${item.entry.started_at ? format(new Date(item.entry.started_at), "yyyy-MM-dd HH:mm") : "-"} -> ${
                      item.entry.ended_at
                        ? format(new Date(item.entry.ended_at), "HH:mm")
                        : "Running"
                    }`
                  : formatScheduleWindow(item.task.start_at, item.task.end_at)}
              </Text>
              {item.kind === "entry" ? (
                <>
                  <Text style={styles.bodyText}>
                    Duration:{" "}
                    {(
                      (calculateTimeEntryDuration(item.entry, now) || 0) / 3600
                    ).toFixed(2)}
                    h
                  </Text>
                  {item.entry.note ? (
                    <Text style={styles.noteText}>{item.entry.note}</Text>
                  ) : null}
                </>
              ) : (
                <Text style={styles.bodyText}>
                  Planned:{" "}
                  {formatDuration(
                    Math.max(
                      0,
                      Math.floor(
                        ((parseDateValue(item.task.end_at)?.getTime() ?? 0) -
                          (parseDateValue(item.task.start_at)?.getTime() ??
                            0)) /
                          1000,
                      ),
                    ),
                  )}
                </Text>
              )}
            </Surface>
          </Pressable>
        ))}

        {timelineItems.length === 0 ? (
          <Surface style={styles.card} elevation={0}>
            <Text style={styles.bodyText}>No timeline entries yet.</Text>
            <Button
              mode="outlined"
              textColor="#89b4fa"
              onPress={() => router.replace("/reports")}
            >
              Back to Reports
            </Button>
          </Surface>
        ) : null}
      </ScrollView>
      <TaskQuickViewDialog
        taskId={selectedTaskId}
        visible={!!selectedTaskId}
        entryFocus={selectedEntry}
        onDismiss={() => {
          setSelectedTaskId(null);
          setSelectedEntry(null);
        }}
        onTaskChanged={() => {
          void load();
        }}
      />
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
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  rangeRow: {
    flexDirection: "row",
    gap: 8,
    paddingHorizontal: 16,
    marginTop: 8,
  },
  toggleRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 16,
    marginTop: 10,
  },
  toggleLabel: { color: "#cdd6f4", fontSize: 13 },
  chip: { backgroundColor: "#313244" },
  chipActive: { backgroundColor: "#4c1d95" },
  chipText: { color: "#cdd6f4" },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: "#1e1e2e", borderRadius: 12, padding: 16 },
  scheduleCard: {
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: "#89b4fa",
    backgroundColor: "#151827",
  },
  cardTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 10,
  },
  bodyText: { color: "#cdd6f4", fontSize: 13, lineHeight: 19 },
  entryTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  scheduleTitle: { color: "#89b4fa" },
  kindBadge: {
    alignSelf: "flex-start",
    marginTop: 8,
    borderRadius: 999,
    paddingHorizontal: 8,
    paddingVertical: 3,
    fontSize: 10,
    fontWeight: "700",
    overflow: "hidden",
  },
  actualBadge: {
    color: "#1e1e2e",
    backgroundColor: "#f9e2af",
  },
  scheduleBadge: {
    color: "#89b4fa",
    backgroundColor: "#11111b",
  },
  projectText: { color: "#bac2de", fontSize: 12, marginTop: 4 },
  entryMeta: { color: "#a6adc8", fontSize: 12, marginTop: 4, marginBottom: 6 },
  noteText: { color: "#89b4fa", fontSize: 12, marginTop: 6 },
});
