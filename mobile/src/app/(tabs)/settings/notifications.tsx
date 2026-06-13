import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import {
  Button,
  IconButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import {
  googleCalendarApi,
  type GoogleCalendarSettings,
} from "../../../lib/google-calendar-api";
import { taskApi } from "../../../lib/task-api";
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import {
  getLocalNotificationPermissionLabel,
  initializeLocalNotifications,
  rescheduleLocalTaskNotificationsFromCache,
  sendLocalNotificationTest,
  setLocalTaskNotificationDefaultOffset,
} from "../../../lib/local-notifications";

export default function SettingsNotificationsScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    google_calendar?: string;
    message?: string;
  }>();
  const { isAuthenticated } = useAuth();
  const { selectedProjectId, selectedProject } = useProject();
  const [webhookUrl, setWebhookUrl] = useState("");
  const [reminders, setReminders] = useState("15");
  const [notifyOverdue, setNotifyOverdue] = useState(true);
  const [taskNotificationMinutesBefore, setTaskNotificationMinutesBefore] =
    useState("5");
  const [googleCalendarSettings, setGoogleCalendarSettings] =
    useState<GoogleCalendarSettings | null>(null);
  const [googleCalendarBusy, setGoogleCalendarBusy] = useState(false);
  const [deviceNotificationStatus, setDeviceNotificationStatus] =
    useState("確認中");
  const [deviceNotificationBusy, setDeviceNotificationBusy] = useState(false);
  const handledGoogleResultRef = useRef<string | null>(null);

  const loadNotifications = useCallback(async () => {
    if (!isAuthenticated) return;
    const [userPrefs, projectSettings] = await Promise.all([
      taskApi.getUserNotificationPreferences().catch(() => null),
      selectedProjectId
        ? taskApi.getNotificationSettings(selectedProjectId)
        : Promise.resolve(null),
    ]);
    if (userPrefs) {
      const minutes = Number(userPrefs.task_notification_minutes_before ?? 5);
      setTaskNotificationMinutesBefore(
        String(Number.isFinite(minutes) && minutes >= 0 ? minutes : 5),
      );
      await setLocalTaskNotificationDefaultOffset(minutes, {
        reschedule: false,
      });
    }
    if (projectSettings) {
      setWebhookUrl(projectSettings.discord_webhook_url || "");
      setReminders((projectSettings.default_reminder_offsets || []).join(", "));
      setNotifyOverdue(projectSettings.notify_overdue);
    }
  }, [isAuthenticated, selectedProjectId]);

  const loadGoogleCalendarSettings = useCallback(async () => {
    if (!isAuthenticated) {
      setGoogleCalendarSettings(null);
      return;
    }
    try {
      setGoogleCalendarSettings(await googleCalendarApi.getSettings());
    } catch {
      setGoogleCalendarSettings(null);
    }
  }, [isAuthenticated]);

  const refreshDeviceNotificationStatus = useCallback(async () => {
    setDeviceNotificationStatus(await getLocalNotificationPermissionLabel());
  }, []);

  useFocusEffect(
    useCallback(() => {
      void loadNotifications();
      void loadGoogleCalendarSettings();
      void refreshDeviceNotificationStatus();
    }, [
      loadGoogleCalendarSettings,
      loadNotifications,
      refreshDeviceNotificationStatus,
    ]),
  );

  useEffect(() => {
    const result =
      typeof params.google_calendar === "string"
        ? params.google_calendar
        : null;
    const message = typeof params.message === "string" ? params.message : null;
    const key = result ? `${result}:${message || ""}` : null;
    if (!key || handledGoogleResultRef.current === key) return;
    handledGoogleResultRef.current = key;
    if (result === "connected") {
      Alert.alert("Google Calendar", "Connected successfully.");
      void loadGoogleCalendarSettings();
      return;
    }
    if (result === "error") {
      Alert.alert("Google Calendar", message || "Connection failed.");
    }
  }, [loadGoogleCalendarSettings, params.google_calendar, params.message]);

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => router.back()}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Task notifications / Calendar
            </Text>
            <Text style={styles.headerSubtext}>
              {selectedProject?.name || "No project selected"}
            </Text>
          </View>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>端末通知</Text>
        <Text style={styles.statusText}>状態: {deviceNotificationStatus}</Text>
        <Text style={styles.helperText}>
          スマホ側は端末のローカル通知としてタスク開始前に鳴らします。
        </Text>
        <View style={styles.buttonRow}>
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            loading={deviceNotificationBusy}
            disabled={deviceNotificationBusy}
            onPress={async () => {
              setDeviceNotificationBusy(true);
              try {
                const enabled = await initializeLocalNotifications();
                if (enabled) {
                  await rescheduleLocalTaskNotificationsFromCache();
                }
                await refreshDeviceNotificationStatus();
                Alert.alert(
                  "端末通知",
                  enabled
                    ? "通知を許可し、タスク通知を再スケジュールしました。"
                    : "端末側で通知が許可されていません。",
                );
              } finally {
                setDeviceNotificationBusy(false);
              }
            }}
          >
            許可 / 再スケジュール
          </Button>
          <Button
            mode="outlined"
            textColor="#89b4fa"
            loading={deviceNotificationBusy}
            disabled={deviceNotificationBusy}
            onPress={async () => {
              setDeviceNotificationBusy(true);
              try {
                const sent = await sendLocalNotificationTest();
                await refreshDeviceNotificationStatus();
                Alert.alert(
                  "テスト通知",
                  sent
                    ? "1秒後にテスト通知を表示します。"
                    : "端末側で通知が許可されていません。",
                );
              } finally {
                setDeviceNotificationBusy(false);
              }
            }}
          >
            テスト通知
          </Button>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Task reminder</Text>
        {!isAuthenticated ? (
          <Text style={styles.helperText}>
            通知設定の同期はサーバーログイン中のみ利用できます。
          </Text>
        ) : null}
        <Text style={styles.helperText}>
          タスク開始の何分前に通知するかを決めます。
        </Text>
        <TextInput
          mode="outlined"
          label="開始何分前"
          value={taskNotificationMinutesBefore}
          onChangeText={setTaskNotificationMinutesBefore}
          style={styles.input}
          keyboardType="number-pad"
          disabled={!isAuthenticated}
        />
        <Button
          mode="contained"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          disabled={!isAuthenticated}
          onPress={async () => {
            const minutes = Number(taskNotificationMinutesBefore.trim());
            const safeMinutes =
              Number.isFinite(minutes) && minutes >= 0
                ? Math.floor(minutes)
                : 5;
            await taskApi.updateUserNotificationPreferences({
              task_notification_minutes_before: safeMinutes,
            });
            await setLocalTaskNotificationDefaultOffset(safeMinutes);
            setTaskNotificationMinutesBefore(String(safeMinutes));
            Alert.alert(
              "Task reminder",
              "通知タイミングを保存し、端末通知を再スケジュールしました。",
            );
          }}
        >
          保存
        </Button>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Google Calendar</Text>
        {!isAuthenticated ? (
          <Text style={styles.helperText}>
            Google 連携はサーバーログイン中のみ利用できます。
          </Text>
        ) : (
          <>
            <Text style={styles.statusText}>
              {googleCalendarSettings?.connected
                ? `Connected: ${googleCalendarSettings.email || "Google account"}`
                : "Not connected"}
            </Text>
            {!googleCalendarSettings?.configured ? (
              <Text style={styles.helperText}>
                サーバー側の Google OAuth 設定が未完了です。
              </Text>
            ) : (
              <Text style={styles.helperText}>
                タスクの日時をGoogle Calendarへ送ります。
              </Text>
            )}
            <View style={styles.buttonRow}>
              <Button
                mode="contained"
                buttonColor="#7c3aed"
                textColor="#cdd6f4"
                loading={googleCalendarBusy}
                disabled={
                  googleCalendarBusy ||
                  googleCalendarSettings?.configured === false
                }
                onPress={async () => {
                  setGoogleCalendarBusy(true);
                  try {
                    await googleCalendarApi.connect();
                  } finally {
                    setGoogleCalendarBusy(false);
                  }
                }}
              >
                {googleCalendarSettings?.connected
                  ? "Reconnect"
                  : "Connect Google"}
              </Button>
              <Button
                mode="outlined"
                textColor="#89b4fa"
                loading={googleCalendarBusy}
                disabled={
                  googleCalendarBusy || !googleCalendarSettings?.connected
                }
                onPress={async () => {
                  setGoogleCalendarBusy(true);
                  try {
                    setGoogleCalendarSettings(
                      await googleCalendarApi.disconnect(),
                    );
                  } finally {
                    setGoogleCalendarBusy(false);
                  }
                }}
              >
                Disconnect
              </Button>
            </View>
            <Text style={styles.helperText}>タスクからCalendarを開く時の動作</Text>
            <View style={styles.buttonRow}>
              <Button
                mode={
                  googleCalendarSettings?.default_action === "open_template"
                    ? "contained"
                    : "outlined"
                }
                buttonColor="#45475a"
                textColor="#cdd6f4"
                disabled={!isAuthenticated || googleCalendarBusy}
                onPress={async () => {
                  const next = await googleCalendarApi.updateSettings({
                    default_action: "open_template",
                  });
                  setGoogleCalendarSettings(next);
                }}
              >
                作成画面を開く
              </Button>
              <Button
                mode={
                  googleCalendarSettings?.default_action === "create_event"
                    ? "contained"
                    : "outlined"
                }
                buttonColor="#45475a"
                textColor="#cdd6f4"
                disabled={!isAuthenticated || googleCalendarBusy}
                onPress={async () => {
                  const next = await googleCalendarApi.updateSettings({
                    default_action: "create_event",
                  });
                  setGoogleCalendarSettings(next);
                }}
              >
                直接作成
              </Button>
            </View>
          </>
        )}
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Project task defaults</Text>
        <Text style={styles.helperText}>
          選択中プロジェクトのタスク通知既定値とDiscord通知先です。
        </Text>
        {!isAuthenticated ? (
          <Text style={styles.helperText}>
            サーバーログイン中のみ利用できます。
          </Text>
        ) : null}
        {!selectedProjectId ? (
          <Text style={styles.helperText}>
            プロジェクトを選ぶと保存できます。
          </Text>
        ) : null}
        <TextInput
          mode="outlined"
          label="Discord Webhook"
          value={webhookUrl}
          onChangeText={setWebhookUrl}
          style={styles.input}
          disabled={!isAuthenticated}
        />
        <TextInput
          mode="outlined"
          label="リマインダー分数（カンマ区切り）"
          value={reminders}
          onChangeText={setReminders}
          style={styles.input}
          disabled={!isAuthenticated}
        />
        <View style={styles.switchRow}>
          <Text style={styles.switchLabel}>期限切れタスクを通知</Text>
          <Switch
            value={notifyOverdue}
            onValueChange={setNotifyOverdue}
            disabled={!isAuthenticated}
          />
        </View>
        <Button
          mode="contained"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          disabled={!selectedProjectId || !isAuthenticated}
          onPress={async () => {
            if (!selectedProjectId) return;
            await taskApi.updateNotificationSettings(selectedProjectId, {
              discord_webhook_url: webhookUrl.trim() || null,
              default_reminder_offsets: reminders
                .split(",")
                .map((value) => Number(value.trim()))
                .filter((value) => Number.isFinite(value)),
              notify_overdue: notifyOverdue,
            });
          }}
        >
          保存
        </Button>
      </Surface>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  card: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 16,
    margin: 16,
    marginBottom: 0,
  },
  cardTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 10,
  },
  input: { marginBottom: 12 },
  buttonRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  switchRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 16,
  },
  switchLabel: { color: "#cdd6f4", fontSize: 14 },
  statusText: { color: "#cdd6f4", fontSize: 14, marginBottom: 10 },
  helperText: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
    marginBottom: 12,
  },
});
