/**
 * Docs ↔ Task の canonical binding surface.
 *
 * Task binding is an online-only operation because the authoritative pointer
 * lives on the Task row (and is not part of the Docs outbox tables).  The
 * server re-checks both Project and Docs ACL before accepting a PATCH.
 */

import React, { useCallback, useMemo, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  List,
  Portal,
  Text,
  TextInput,
} from "react-native-paper";
import { docsApi, type DocsTaskBinding } from "../../lib/docs-api";
import { useNetworkStore } from "../../stores/network";

type DocsTaskBindingProps = {
  nodeId: string;
  projectId?: string | null;
  readOnly?: boolean;
};

function statusLabel(status: string): string {
  switch (status) {
    case "closed":
    case "done":
      return "完了";
    case "in_progress":
      return "進行中";
    case "cancelled":
      return "キャンセル";
    default:
      return "未完了";
  }
}

export function DocsTaskBinding({ nodeId, projectId, readOnly = false }: DocsTaskBindingProps) {
  const online = useNetworkStore((state) => state.online);
  const [visible, setVisible] = useState(false);
  const [query, setQuery] = useState("");
  const [tasks, setTasks] = useState<DocsTaskBinding[]>([]);
  const [boundTask, setBoundTask] = useState<DocsTaskBinding | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const candidates = useMemo(
    () =>
      tasks
        .filter((task) => task.id !== boundTask?.id)
        .filter((task) => !task.knowledge_node_id || task.knowledge_node_id === nodeId)
        .filter((task) => projectId === undefined || task.project_id === projectId)
        .slice(0, 30),
    [boundTask?.id, nodeId, projectId, tasks],
  );

  const loadTasks = useCallback(async (search = query) => {
    if (!online) return;
    setLoading(true);
    setError("");
    try {
      const rows = await docsApi.listTasksForBinding(search);
      const current = rows.find((task) => task.knowledge_node_id === nodeId) ?? null;
      setTasks(rows);
      setBoundTask(current);
    } catch {
      setError("タスク一覧を取得できませんでした");
      setTasks([]);
      setBoundTask(null);
    } finally {
      setLoading(false);
    }
  }, [nodeId, online, query]);

  const openPicker = useCallback(() => {
    if (!online) return;
    setVisible(true);
    setQuery("");
    setError("");
    void loadTasks("");
  }, [loadTasks, online]);

  const bind = useCallback(
    async (task: DocsTaskBinding) => {
      if (!online || readOnly || saving) return;
      setSaving(true);
      setError("");
      try {
        const next = await docsApi.bindTask(task.id, nodeId);
        setBoundTask(next);
        setTasks((current) => current.map((item) => (item.id === next.id ? next : item)));
        setVisible(false);
      } catch {
        setError("タスクとの連携に失敗しました");
      } finally {
        setSaving(false);
      }
    },
    [nodeId, online, readOnly, saving],
  );

  const unbind = useCallback(async () => {
    if (!boundTask || !online || readOnly || saving) return;
    setSaving(true);
    setError("");
    try {
      await docsApi.unbindTask(boundTask.id);
      setBoundTask(null);
      setTasks((current) =>
        current.map((task) =>
          task.id === boundTask.id ? { ...task, knowledge_node_id: null } : task,
        ),
      );
    } catch {
      setError("タスク連携の解除に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [boundTask, online, readOnly, saving]);

  return (
    <View style={styles.container}>
      <Text style={styles.sectionLabel}>タスク連携</Text>
      {!online ? (
        <Text style={styles.muted}>タスク連携はオンライン時のみ利用できます</Text>
      ) : boundTask ? (
        <View style={styles.boundRow}>
          <List.Item
            title={boundTask.title}
            description={statusLabel(boundTask.status)}
            titleStyle={styles.title}
            descriptionStyle={styles.muted}
            left={(props) => <List.Icon {...props} icon="checkbox-marked-outline" color="#a6e3a1" />}
            style={styles.boundItem}
          />
          <Button
            mode="text"
            compact
            textColor="#f38ba8"
            disabled={readOnly || saving}
            loading={saving}
            onPress={() => void unbind()}
          >
            解除
          </Button>
        </View>
      ) : (
        <Button
          mode="outlined"
          icon="link-variant"
          disabled={readOnly}
          onPress={openPicker}
          textColor="#cdd6f4"
        >
          タスクを連携
        </Button>
      )}
      {readOnly ? <Text style={styles.muted}>読み取り専用のため変更できません</Text> : null}
      {error ? <Text style={styles.error}>{error}</Text> : null}

      <Portal>
        <Dialog visible={visible} onDismiss={() => (saving ? undefined : setVisible(false))} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>連携するタスクを選択</Dialog.Title>
          <Dialog.Content>
            <TextInput
              value={query}
              onChangeText={setQuery}
              mode="outlined"
              dense
              placeholder="タスクを検索"
              autoCorrect={false}
              onSubmitEditing={() => void loadTasks()}
              right={<TextInput.Icon icon="magnify" onPress={() => void loadTasks()} />}
            />
            {loading ? <ActivityIndicator color="#7c3aed" style={styles.loading} /> : null}
            {error ? <Text style={styles.error}>{error}</Text> : null}
            {!loading && candidates.length === 0 ? (
              <Text style={styles.muted}>連携可能なタスクがありません</Text>
            ) : (
              <ScrollView style={styles.results} keyboardShouldPersistTaps="handled">
                {candidates.map((task) => (
                  <List.Item
                    key={task.id}
                    title={task.title}
                    description={statusLabel(task.status)}
                    titleStyle={styles.title}
                    descriptionStyle={styles.muted}
                    left={(props) => <List.Icon {...props} icon="checkbox-blank-outline" color="#89b4fa" />}
                    onPress={() => void bind(task)}
                    disabled={saving}
                  />
                ))}
              </ScrollView>
            )}
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setVisible(false)} disabled={saving}>
              閉じる
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: 8 },
  sectionLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700" },
  boundRow: { flexDirection: "row", alignItems: "center" },
  boundItem: { flex: 1, backgroundColor: "#181825", paddingVertical: 0 },
  title: { color: "#cdd6f4", fontSize: 14 },
  muted: { color: "#a6adc8", fontSize: 12 },
  error: { color: "#f38ba8", fontSize: 12 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  loading: { marginVertical: 16 },
  results: { maxHeight: 320, marginTop: 8 },
});
