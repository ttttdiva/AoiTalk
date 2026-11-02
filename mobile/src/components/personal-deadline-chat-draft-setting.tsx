import React, { useCallback, useEffect, useRef, useState } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Surface, Switch, Text } from "react-native-paper";
import { settingsApi } from "../lib/settings-api";

const KEY = "personal.deadline_chat_draft.enabled";

function readEnabled(value: unknown): boolean {
  if (!value || typeof value !== "object") throw new Error("設定の形式を確認できません");
  const settings = (value as Record<string, unknown>).settings;
  if (!settings || typeof settings !== "object") throw new Error("設定の形式を確認できません");
  const personal = (settings as Record<string, unknown>).personal;
  if (personal === undefined) return false;
  if (!personal || typeof personal !== "object") throw new Error("設定の形式を確認できません");
  const draft = (personal as Record<string, unknown>).deadline_chat_draft;
  if (draft === undefined) return false;
  if (!draft || typeof draft !== "object") throw new Error("設定の形式を確認できません");
  const enabled = (draft as Record<string, unknown>).enabled;
  if (enabled === undefined) return false;
  if (typeof enabled !== "boolean") throw new Error("設定値はJSON booleanではありません");
  return enabled;
}

export function PersonalDeadlineChatDraftSetting({ isAdmin }: { isAdmin: boolean }) {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const epoch = useRef(0);

  const reload = useCallback(async (current: number) => {
    setLoaded(false);
    try {
      const value = readEnabled(await settingsApi.get());
      if (epoch.current !== current) return;
      setEnabled(value);
      setLoaded(true);
      setMessage("");
    } catch (error) {
      if (epoch.current !== current) return;
      setMessage(error instanceof Error ? error.message : "設定を取得できませんでした");
    }
  }, []);

  useEffect(() => {
    const current = ++epoch.current;
    void reload(current);
    return () => { epoch.current += 1; };
  }, [reload]);

  async function toggle(next: boolean) {
    if (!isAdmin || !loaded || busy) return;
    const current = ++epoch.current;
    setBusy(true);
    setMessage("");
    try {
      await settingsApi.update(KEY, next, true);
      const value = readEnabled(await settingsApi.get());
      if (epoch.current !== current) return;
      setEnabled(value);
      setMessage(value === next ? "設定を保存しました。" : "保存値を再取得しました。表示値を確認してください。");
    } catch (error) {
      if (epoch.current !== current) return;
      setMessage(error instanceof Error ? `${error.message}。保存状態を再取得します。` : "保存結果を確認できませんでした。再取得してください。");
      await reload(current);
    } finally {
      if (epoch.current === current) setBusy(false);
    }
  }

  return (
    <Surface style={styles.surface} testID="personal-deadline-chat-draft-setting">
      <Text variant="titleMedium">個人Agentの期限候補</Text>
      <Text variant="bodySmall" style={styles.description}>
        ローカル限定のprivate Chatで、本人が書いた日付を確認用の候補として保存します。台帳への反映には本人の受理が必要です。既定はOFFです。
      </Text>
      <View style={styles.row}>
        <Text variant="bodyMedium">有効にする</Text>
        <Switch accessibilityLabel="個人Agentの期限候補を有効にする" value={enabled}
          disabled={!isAdmin || !loaded || busy} onValueChange={(value) => void toggle(value)} />
      </View>
      {!isAdmin ? <Text variant="bodySmall">変更は管理者のみ可能です。</Text> : null}
      {!loaded && !message ? <Text accessibilityRole="progressbar">設定を取得中…</Text> : null}
      {message ? <Text accessibilityRole="alert">{message}</Text> : null}
      {!loaded && message ? <Button mode="outlined" onPress={() => void reload(epoch.current)}>再取得</Button> : null}
    </Surface>
  );
}

const styles = StyleSheet.create({
  surface: { marginHorizontal: 12, marginTop: 16, padding: 16, borderRadius: 12, gap: 8 },
  description: { opacity: 0.76 },
  row: { minHeight: 48, flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
});
