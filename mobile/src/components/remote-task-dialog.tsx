import React, { useEffect, useState } from "react";
import { Linking, StyleSheet, View } from "react-native";
import {
  Button,
  Dialog,
  Divider,
  Menu,
  Portal,
  Text,
  TextInput,
} from "react-native-paper";

import { addRemoteTaskComment, patchRemoteTask } from "../lib/remote-tasks";

export type RemoteTaskDialogTarget = {
  profileId: string;
  profileName: string;
  profileColor?: string | null;
  baseUrl: string;
  taskId: string;
  title: string;
  status: string;
  startAt?: string | null;
  endAt?: string | null;
};

const STATUS_OPTIONS = ["open", "in_progress", "review", "on_hold", "closed"];

const STATUS_LABELS: Record<string, string> = {
  open: "未着手",
  in_progress: "進行中",
  review: "レビュー待ち",
  on_hold: "保留",
  closed: "完了",
  // 選択肢からは外したが、既存データの表示用に残す。
  cancelled: "取消",
};

/** "2026-06-12T10:00:00" 形式を分まで（16文字）に整形する。 */
function toLocalInput(value?: string | null): string {
  if (!value) return "";
  const trimmed = value.replace("Z", "");
  return trimmed.length >= 16 ? trimmed.slice(0, 16) : trimmed;
}

export function RemoteTaskDialog({
  target,
  onDismiss,
  onUpdated,
}: {
  target: RemoteTaskDialogTarget | null;
  onDismiss: () => void;
  onUpdated?: () => void;
}) {
  const [status, setStatus] = useState("open");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [comment, setComment] = useState("");
  const [savingField, setSavingField] = useState<string | null>(null);
  const [statusMenuVisible, setStatusMenuVisible] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    setStatus(target.status || "open");
    setStartAt(toLocalInput(target.startAt));
    setEndAt(toLocalInput(target.endAt));
    setComment("");
    setMessage(null);
  }, [target]);

  if (!target) return null;

  const externalUrl = `${target.baseUrl.replace(/\/$/, "")}/tasks`;

  const handleStatusSave = async (next: string) => {
    setStatus(next);
    setStatusMenuVisible(false);
    setSavingField("status");
    setMessage(null);
    try {
      await patchRemoteTask(target.profileId, target.taskId, { status: next });
      setMessage("ステータスを更新しました");
      onUpdated?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "更新に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  const handleDatesSave = async () => {
    setSavingField("dates");
    setMessage(null);
    try {
      await patchRemoteTask(target.profileId, target.taskId, {
        start_at: startAt || null,
        end_at: endAt || null,
      });
      setMessage("日付を更新しました");
      onUpdated?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "更新に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  const handleCommentSave = async () => {
    if (!comment.trim()) return;
    setSavingField("comment");
    setMessage(null);
    try {
      await addRemoteTaskComment(
        target.profileId,
        target.taskId,
        comment.trim(),
      );
      setComment("");
      setMessage("コメントを追加しました");
      onUpdated?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "追加に失敗しました");
    } finally {
      setSavingField(null);
    }
  };

  return (
    <Portal>
      <Dialog visible={!!target} onDismiss={onDismiss} style={styles.dialog}>
        <Dialog.Title style={styles.dialogTitle} numberOfLines={2}>
          {target.title}
        </Dialog.Title>
        <Dialog.Content>
          <View style={styles.serverRow}>
            <View
              style={[
                styles.colorDot,
                { backgroundColor: target.profileColor || "#3b82f6" },
              ]}
            />
            <Text style={styles.serverName}>{target.profileName}</Text>
            <Text style={styles.serverHint}>外部サーバーのタスク</Text>
          </View>

          <Text style={styles.label}>ステータス</Text>
          <Menu
            visible={statusMenuVisible}
            onDismiss={() => setStatusMenuVisible(false)}
            anchor={
              <Button
                mode="outlined"
                textColor="#cdd6f4"
                style={styles.statusButton}
                loading={savingField === "status"}
                onPress={() => setStatusMenuVisible(true)}
              >
                {STATUS_LABELS[status] ?? status}
              </Button>
            }
          >
            {STATUS_OPTIONS.map((opt) => (
              <Menu.Item
                key={opt}
                leadingIcon={opt === status ? "check" : undefined}
                onPress={() => void handleStatusSave(opt)}
                title={STATUS_LABELS[opt] ?? opt}
              />
            ))}
          </Menu>

          <Divider style={styles.divider} />

          <Text style={styles.label}>日付（YYYY-MM-DDTHH:mm）</Text>
          <TextInput
            mode="outlined"
            label="開始"
            value={startAt}
            onChangeText={setStartAt}
            placeholder="2026-06-12T10:00"
            autoCapitalize="none"
            autoCorrect={false}
            style={styles.input}
          />
          <TextInput
            mode="outlined"
            label="終了"
            value={endAt}
            onChangeText={setEndAt}
            placeholder="2026-06-12T11:00"
            autoCapitalize="none"
            autoCorrect={false}
            style={styles.input}
          />
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            onPress={() => void handleDatesSave()}
            loading={savingField === "dates"}
            style={styles.saveButton}
          >
            日付を保存
          </Button>

          <Divider style={styles.divider} />

          <Text style={styles.label}>コメント追加</Text>
          <TextInput
            mode="outlined"
            value={comment}
            onChangeText={setComment}
            placeholder="コメントを入力..."
            multiline
            numberOfLines={2}
            style={styles.input}
          />
          <Button
            mode="contained-tonal"
            onPress={() => void handleCommentSave()}
            loading={savingField === "comment"}
            disabled={!comment.trim()}
            style={styles.saveButton}
          >
            コメントを追加
          </Button>

          {message ? <Text style={styles.message}>{message}</Text> : null}
        </Dialog.Content>
        <Dialog.Actions>
          <Button
            onPress={() => void Linking.openURL(externalUrl)}
            textColor="#89b4fa"
            icon="open-in-new"
          >
            外部画面で開く
          </Button>
          <Button onPress={onDismiss} textColor="#a6adc8">
            閉じる
          </Button>
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

const styles = StyleSheet.create({
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4", fontSize: 18 },
  serverRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 12,
  },
  colorDot: { width: 12, height: 12, borderRadius: 999 },
  serverName: { color: "#cdd6f4", fontSize: 13, fontWeight: "700" },
  serverHint: { color: "#a6adc8", fontSize: 11 },
  label: {
    color: "#7c3aed",
    fontSize: 12,
    fontWeight: "700",
    marginBottom: 6,
  },
  statusButton: { borderColor: "#45475a", alignSelf: "flex-start" },
  input: { marginBottom: 10, backgroundColor: "#181825" },
  saveButton: { alignSelf: "flex-start" },
  divider: { backgroundColor: "#313244", marginVertical: 14 },
  message: { color: "#a6e3a1", fontSize: 12, marginTop: 12 },
});
