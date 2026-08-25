import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  Button,
  Chip,
  Menu,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useProject } from "../../../contexts/ProjectContext";
import { tasksRepo } from "../../../repositories";
import { taskApi } from "../../../lib/task-api";
import {
  isTaskDateOnlyInput,
  toTaskWallClockIso,
} from "../../../lib/task-datetime";
import { TaskDateField } from "../../../components/task-date-field";
import type { Tag } from "../../../types/api";

const STATUSES = [
  { value: "open", label: "未着手" },
  { value: "in_progress", label: "進行中" },
  { value: "closed", label: "完了" },
];
const REMINDERS = [
  { value: 5, label: "5分前" },
  { value: 15, label: "15分前" },
  { value: 30, label: "30分前" },
  { value: 60, label: "1時間前" },
  { value: 1440, label: "1日前" },
];
const DISALLOWED_PLACEHOLDER_TITLES = new Set([
  "無題のタスク",
  "Untitled task",
]);

export default function TaskCreateScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    projectId?: string;
    spaceId?: string;
    startDate?: string;
  }>();
  const { projects } = useProject();
  const scopedProjects = useMemo(
    () =>
      params.spaceId
        ? projects.filter((project) => project.space_id === params.spaceId)
        : projects,
    [params.spaceId, projects],
  );
  const [projectId, setProjectId] = useState<string | null>(
    params.projectId ?? null,
  );
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState("open");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [allDay, setAllDay] = useState(false);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminders, setReminders] = useState<number[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [newTagDraft, setNewTagDraft] = useState("");
  const [tagCreating, setTagCreating] = useState(false);
  const [recurrenceFrequency, setRecurrenceFrequency] = useState<
    "none" | "DAILY" | "WEEKLY" | "MONTHLY"
  >("none");
  const [recurrenceInterval, setRecurrenceInterval] = useState("1");
  const [projectMenu, setProjectMenu] = useState(false);
  const [statusMenu, setStatusMenu] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId && scopedProjects.length > 0) {
      setProjectId(scopedProjects[0].id);
    }
  }, [projectId, scopedProjects]);

  useEffect(() => {
    if (!params.startDate || startAt) return;
    // Calendar passes a date-only value; keep the mobile date field's wall
    // clock semantics and let the user adjust the time before saving.
    if (/^\d{4}-\d{2}-\d{2}$/.test(params.startDate)) {
      setStartAt(`${params.startDate}T09:00`);
      setEndAt(`${params.startDate}T10:00`);
    }
  }, [params.startDate, startAt]);

  useEffect(() => {
    if (!projectId) {
      setTags([]);
      return;
    }
    let cancelled = false;
    taskApi
      .listTags(projectId)
      .then((items) => {
        if (!cancelled) setTags(items);
      })
      .catch(() => {
        if (!cancelled) setTags([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const toggleReminder = useCallback((value: number) => {
    setReminders((current) =>
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value].sort((a, b) => a - b),
    );
  }, []);

  const toggleTag = useCallback((id: string) => {
    setSelectedTagIds((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  }, []);

  const createTag = useCallback(async () => {
    const name = newTagDraft.trim();
    if (!projectId || !name || tagCreating) return;
    setTagCreating(true);
    setError(null);
    try {
      const created = await taskApi.createTag(projectId, { name });
      setTags((current) => [...current, created]);
      setSelectedTagIds((current) => [...current, created.id]);
      setNewTagDraft("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "タグ作成に失敗しました");
    } finally {
      setTagCreating(false);
    }
  }, [newTagDraft, projectId, tagCreating]);

  const submit = async () => {
    const normalizedTitle = title.trim();
    if (!normalizedTitle || !projectId || submitting) return;
    if (DISALLOWED_PLACEHOLDER_TITLES.has(normalizedTitle)) {
      setError("仮タイトルは使用できません");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const interval = Math.max(1, Number.parseInt(recurrenceInterval, 10) || 1);
      const recurrenceRrule =
        recurrenceFrequency === "none"
          ? null
          : `FREQ=${recurrenceFrequency};INTERVAL=${interval}`;
      const created = await tasksRepo.create({
        project_id: projectId,
        title: normalizedTitle,
        description: description.trim() || null,
        status,
        start_at: startAt ? toTaskWallClockIso(startAt) : null,
        end_at: endAt ? toTaskWallClockIso(endAt) : null,
        all_day:
          allDay || isTaskDateOnlyInput(startAt) || isTaskDateOnlyInput(endAt),
        notifications_enabled: notificationsEnabled,
        reminder_offsets: reminders,
        tag_ids: selectedTagIds,
        recurrence_rrule: recurrenceRrule,
        recurrence_timezone: "Asia/Tokyo",
      });
      const syncStatus = created.metadata?.mobile_sync_status;
      if (syncStatus === "pending") {
        const syncError = created.metadata?.mobile_sync_error;
        Alert.alert(
          "端末に保存しました",
          typeof syncError === "string"
            ? `サーバー保存に失敗したため、オンライン時に再同期します。\n${syncError}`
            : "現在はオフラインです。オンライン時にサーバーへ同期します。",
          [{ text: "OK", onPress: () => router.back() }],
        );
      } else if (syncStatus === "local_only") {
        Alert.alert(
          "端末に保存しました",
          "ログインしていないため、このタスクは端末内だけに保存されています。",
          [{ text: "OK", onPress: () => router.back() }],
        );
      } else {
        router.back();
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "作成に失敗しました");
    } finally {
      setSubmitting(false);
    }
  };

  const selectedProject = projects.find((project) => project.id === projectId);
  const statusLabel = STATUSES.find((item) => item.value === status)?.label;

  return (
    <View style={styles.container}>
      <ScrollView
        contentContainerStyle={styles.content}
        keyboardShouldPersistTaps="handled"
      >
        <Text style={styles.heading}>新しいタスク</Text>
        <Text style={styles.lead}>必要な情報を落ち着いて入力できる専用画面です。</Text>

        <TextInput
          label="タイトル"
          value={title}
          onChangeText={setTitle}
          mode="outlined"
          autoFocus
          style={styles.input}
        />

        <View style={styles.dateRow}>
          <TaskDateField
            label="Start Date"
            value={startAt}
            onChange={setStartAt}
            allDay={allDay}
            style={styles.dateInput}
          />
          <TaskDateField
            label="Due Date"
            value={endAt}
            onChange={setEndAt}
            allDay={allDay}
            style={styles.dateInput}
          />
          <View style={styles.allDayBox}>
            <Text style={styles.allDayLabel}>終日</Text>
            <Switch value={allDay} onValueChange={setAllDay} />
          </View>
        </View>

        <View style={styles.attributeRow}>
          <Menu
            visible={projectMenu}
            onDismiss={() => setProjectMenu(false)}
            anchor={
              <Chip
                icon="folder-outline"
                compact
                onPress={() => setProjectMenu(true)}
                style={styles.chip}
              >
                {selectedProject?.name ?? "Project"}
              </Chip>
            }
          >
            {scopedProjects.map((project) => (
              <Menu.Item
                key={project.id}
                title={project.name}
                leadingIcon={project.id === projectId ? "check" : undefined}
                onPress={() => {
                  setProjectId(project.id);
                  setSelectedTagIds([]);
                  setProjectMenu(false);
                }}
              />
            ))}
          </Menu>
          <Menu
            visible={statusMenu}
            onDismiss={() => setStatusMenu(false)}
            anchor={
              <Chip
                compact
                onPress={() => setStatusMenu(true)}
                style={styles.chip}
              >
                状態: {statusLabel}
              </Chip>
            }
          >
            {STATUSES.map((item) => (
              <Menu.Item key={item.value} title={item.label} onPress={() => { setStatus(item.value); setStatusMenu(false); }} />
            ))}
          </Menu>
        </View>

        <TextInput
          label="説明（Markdown可）"
          value={description}
          onChangeText={setDescription}
          mode="outlined"
          multiline
          numberOfLines={5}
          style={styles.input}
        />

        <Text style={styles.section}>通知</Text>
        <View style={styles.row}><Text style={styles.label}>通知を有効にする</Text><Switch value={notificationsEnabled} onValueChange={setNotificationsEnabled} /></View>
        <View style={styles.wrap}>
          {REMINDERS.map((item) => <Chip key={item.value} selected={reminders.includes(item.value)} disabled={!notificationsEnabled} onPress={() => toggleReminder(item.value)}>{item.label}</Chip>)}
        </View>

        <Text style={styles.section}>タグ</Text>
        <View style={styles.wrap}>
          {tags.length === 0 ? <Text style={styles.hint}>タグなし / オフラインでは取得できません</Text> : tags.map((tag) => <Chip key={tag.id} selected={selectedTagIds.includes(tag.id)} onPress={() => toggleTag(tag.id)}>{tag.name}</Chip>)}
        </View>
        <View style={styles.tagCreateRow}>
          <TextInput
            label="新しいタグ"
            value={newTagDraft}
            onChangeText={setNewTagDraft}
            mode="outlined"
            dense
            style={styles.tagCreateInput}
            onSubmitEditing={() => void createTag()}
          />
          <Button
            mode="outlined"
            onPress={() => void createTag()}
            loading={tagCreating}
            disabled={!projectId || !newTagDraft.trim() || tagCreating}
          >
            追加
          </Button>
        </View>

        <Text style={styles.section}>繰り返し</Text>
        <View style={styles.wrap}>
          {([
            ["none", "なし"],
            ["DAILY", "毎日"],
            ["WEEKLY", "毎週"],
            ["MONTHLY", "毎月"],
          ] as const).map(([value, label]) => (
            <Chip
              key={value}
              selected={recurrenceFrequency === value}
              onPress={() => setRecurrenceFrequency(value)}
            >
              {label}
            </Chip>
          ))}
        </View>
        {recurrenceFrequency !== "none" ? (
          <TextInput
            label="間隔（回）"
            value={recurrenceInterval}
            onChangeText={(value) => setRecurrenceInterval(value.replace(/[^0-9]/g, ""))}
            keyboardType="number-pad"
            mode="outlined"
            dense
            style={styles.recurrenceInterval}
          />
        ) : null}

        {error ? <Text style={styles.error}>{error}</Text> : null}
        <View style={styles.actions}>
          <Button mode="outlined" onPress={() => router.back()} disabled={submitting}>キャンセル</Button>
          <Button mode="contained" onPress={() => void submit()} loading={submitting} disabled={!title.trim() || !projectId || submitting}>作成</Button>
        </View>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { padding: 20, paddingBottom: 48 },
  heading: { color: "#cdd6f4", fontSize: 26, fontWeight: "700" },
  lead: { color: "#9399b2", marginTop: 4, marginBottom: 20 },
  section: { color: "#cdd6f4", fontSize: 16, fontWeight: "700", marginTop: 18, marginBottom: 10 },
  input: { marginBottom: 12, backgroundColor: "#1e1e2e" },
  row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", minHeight: 48 },
  dateRow: { flexDirection: "row", alignItems: "stretch", gap: 8, marginBottom: 12 },
  dateInput: { flex: 1, minWidth: 0 },
  allDayBox: {
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 4,
    gap: 4,
  },
  allDayLabel: { color: "#9399b2", fontSize: 11 },
  attributeRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginBottom: 12 },
  label: { color: "#bac2de" },
  chip: { backgroundColor: "#313244" },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  hint: { color: "#9399b2", fontSize: 12 },
  tagCreateRow: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 10 },
  tagCreateInput: { flex: 1, backgroundColor: "#1e1e2e" },
  recurrenceInterval: { marginTop: 10, maxWidth: 180, backgroundColor: "#1e1e2e" },
  error: { color: "#f38ba8", marginTop: 16 },
  actions: { flexDirection: "row", justifyContent: "flex-end", gap: 10, marginTop: 28 },
});
