import React, { useCallback, useState, useEffect, useMemo } from "react";
import { Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { goBackOrReplace } from "../lib/navigation";
import { Button, Chip, IconButton, Surface, Text } from "react-native-paper";
import { format, subDays } from "date-fns";
import {
  buildTimeReportFromEntries,
  calculateTimeEntryDuration,
  timeEntriesRepo,
} from "../repositories";
import { useAuth } from "../contexts/AuthContext";
import { useProject } from "../contexts/ProjectContext";
import { taskApi } from "../lib/task-api";
import { TaskQuickViewDialog } from "../components/task-quick-view-dialog";
import type { TimeEntry, TimeReport, TimeReportBucket } from "../types/api";

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function BucketList({
  title,
  buckets,
  onBucketPress,
}: {
  title: string;
  buckets: TimeReportBucket[];
  onBucketPress?: (bucket: TimeReportBucket) => void;
}) {
  if (!buckets.length) return null;
  return (
    <Surface style={styles.section} elevation={0}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {buckets.slice(0, 10).map((bucket) => (
        <Pressable
          key={bucket.key}
          style={({ pressed }) => [
            styles.bucketRow,
            onBucketPress && pressed && styles.rowPressed,
          ]}
          disabled={!onBucketPress}
          onPress={() => onBucketPress?.(bucket)}
        >
          <View style={styles.bucketLabelWrap}>
            <Text style={styles.bucketLabel} numberOfLines={1}>
              {bucket.label}
            </Text>
            {bucket.project_name && bucket.project_name !== bucket.label ? (
              <Text style={styles.bucketSubLabel} numberOfLines={1}>
                {bucket.project_name}
              </Text>
            ) : null}
          </View>
          <Text style={styles.bucketValue}>
            {formatDuration(bucket.seconds)}
          </Text>
        </Pressable>
      ))}
    </Surface>
  );
}

export default function ReportsScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const { selectedProjectId, selectedProject, selectedSpaceId, selectedSpace } =
    useProject();
  const [range, setRange] = useState<"7d" | "30d">("7d");
  const [report, setReport] = useState<TimeReport | null>(null);
  const [entries, setEntries] = useState<TimeEntry[]>([]);
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
        const nextRange = settings.reports_view?.range;
        if (nextRange === "7d" || nextRange === "30d") {
          setRange(nextRange);
        }
      } catch (err) {
        console.error("レポート表示設定の取得に失敗しました", err);
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
          range,
        },
      })
      .catch((err) => {
        console.error("レポート表示設定の保存に失敗しました", err);
      });
  }, [isAuthenticated, prefsLoaded, range]);

  const load = useCallback(async () => {
    const scope = selectedSpaceId
      ? { space_id: selectedSpaceId }
      : selectedProjectId
        ? { project_id: selectedProjectId }
        : {};
    const days = range === "7d" ? 7 : 30;
    const dateFrom = format(subDays(new Date(), days), "yyyy-MM-dd");
    const dateTo = format(new Date(), "yyyy-MM-dd");
    const [nextReport, nextEntries] = await Promise.all([
      timeEntriesRepo.getReport(scope, dateFrom, dateTo),
      timeEntriesRepo.list(scope, dateFrom, dateTo),
    ]);
    setReport(nextReport);
    setEntries(nextEntries);
  }, [range, selectedProjectId, selectedSpaceId]);

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

  const liveReport = useMemo(
    () => (report ? buildTimeReportFromEntries(entries, now) : null),
    [entries, now, report],
  );

  const openTaskQuickView = useCallback(
    (taskId: string | null, entry?: TimeEntry | null) => {
      if (!taskId) return;
      setSelectedTaskId(taskId);
      setSelectedEntry(entry ?? null);
    },
    [],
  );

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Reports
            </Text>
            <Text style={styles.headerSubtext}>
              {selectedSpace?.name ||
                selectedProject?.name ||
                "All projects"}
            </Text>
          </View>
        </View>
        <View style={styles.rangeRow}>
          {(["7d", "30d"] as const).map((value) => (
            <Chip
              key={value}
              selected={range === value}
              onPress={() => setRange(value)}
              style={[
                styles.rangeChip,
                range === value && styles.rangeChipActive,
              ]}
              textStyle={styles.rangeChipText}
            >
              {value}
            </Chip>
          ))}
          <Button
            compact
            mode="text"
            textColor="#89b4fa"
            onPress={() => router.push("/report/timeline")}
          >
            Timeline
          </Button>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {liveReport ? (
          <>
            <View style={styles.summaryRow}>
              <Surface style={styles.summaryCard} elevation={0}>
                <Text style={styles.summaryValue}>
                  {formatDuration(liveReport.summary.total_seconds)}
                </Text>
                <Text style={styles.summaryLabel}>Total</Text>
              </Surface>
              <Surface style={styles.summaryCard} elevation={0}>
                <Text style={styles.summaryValue}>
                  {liveReport.summary.entry_count}
                </Text>
                <Text style={styles.summaryLabel}>Entries</Text>
              </Surface>
              <Surface style={styles.summaryCard} elevation={0}>
                <Text style={styles.summaryValue}>
                  {liveReport.summary.active_entries}
                </Text>
                <Text style={styles.summaryLabel}>Active</Text>
              </Surface>
            </View>

            <BucketList
              title="By Task"
              buckets={liveReport.by_task}
              onBucketPress={(bucket) => {
                openTaskQuickView(bucket.key);
              }}
            />
            <BucketList title="By Day" buckets={liveReport.by_day} />
            <BucketList title="By User" buckets={liveReport.by_user} />
            <BucketList title="By Project" buckets={liveReport.by_project} />

            <Surface style={styles.section} elevation={0}>
              <Text style={styles.sectionTitle}>Recent Entries</Text>
              {entries.length > 0 ? (
                entries.slice(0, 12).map((entry) => (
                  <Pressable
                    key={entry.id}
                    style={({ pressed }) => [
                      styles.entryRow,
                      pressed && styles.rowPressed,
                    ]}
                    onPress={() => {
                      openTaskQuickView(entry.task_id, entry);
                    }}
                  >
                    <View style={{ flex: 1 }}>
                      <Text style={styles.entryTitle}>
                        {entry.task_title || "Untitled task"}
                      </Text>
                      {entry.project_name ? (
                        <Text style={styles.entryProject}>
                          {entry.project_name}
                        </Text>
                      ) : null}
                      <Text style={styles.entryMeta}>
                        {entry.started_at
                          ? format(new Date(entry.started_at), "MM/dd HH:mm")
                          : "-"}
                      </Text>
                    </View>
                    <Text style={styles.entryDuration}>
                      {formatDuration(
                        calculateTimeEntryDuration(entry, now) || 0,
                      )}
                    </Text>
                  </Pressable>
                ))
              ) : (
                <Text style={styles.emptyText}>No time entries yet.</Text>
              )}
            </Surface>
          </>
        ) : (
          <Surface style={styles.section} elevation={0}>
            <Text style={styles.emptyText}>No report data available.</Text>
            <Button
              mode="outlined"
              textColor="#89b4fa"
              onPress={() => router.push("/(tabs)/settings")}
            >
              Open Settings
            </Button>
          </Surface>
        )}
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
  rangeChip: { backgroundColor: "#313244" },
  rangeChipActive: { backgroundColor: "#4c1d95" },
  rangeChipText: { color: "#cdd6f4" },
  content: { padding: 16, gap: 12, paddingBottom: 40 },
  summaryRow: { flexDirection: "row", gap: 8 },
  summaryCard: {
    flex: 1,
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 14,
    alignItems: "center",
  },
  summaryValue: { color: "#cdd6f4", fontSize: 18, fontWeight: "700" },
  summaryLabel: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
  section: { backgroundColor: "#1e1e2e", borderRadius: 12, padding: 16 },
  sectionTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 10,
  },
  bucketRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    gap: 12,
    paddingVertical: 5,
  },
  bucketLabelWrap: { flex: 1 },
  bucketLabel: { color: "#cdd6f4", fontSize: 13 },
  bucketSubLabel: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
  bucketValue: { color: "#a6adc8", fontSize: 13 },
  rowPressed: { opacity: 0.75 },
  entryRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 7,
    borderBottomWidth: 1,
    borderBottomColor: "#313244",
  },
  entryTitle: { color: "#cdd6f4", fontSize: 13 },
  entryProject: { color: "#bac2de", fontSize: 11, marginTop: 2 },
  entryMeta: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
  entryDuration: { color: "#a6adc8", fontSize: 13 },
  emptyText: { color: "#a6adc8", marginBottom: 12 },
});
