import React, { useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Text, TextInput } from "react-native-paper";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { taskApi } from "../../../lib/task-api";

export default function CustomInstructionsSettingsScreen() {
  const { isAuthenticated } = useAuth();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) return;
    void taskApi.getUserSettings().then((settings) => setValue(settings.custom_instructions ?? ""));
  }, [isAuthenticated]);

  const save = async () => {
    if (!isAuthenticated) return;
    setSaving(true);
    setSaved(false);
    try {
      const next = await taskApi.updateUserSettings({ custom_instructions: value.trim() });
      setValue(next.custom_instructions ?? "");
      setSaved(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <ScreenShell scroll style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.description}>すべての会話に追加するユーザー別指示です。</Text>
      <TextInput
        label="カスタム指示"
        value={value}
        onChangeText={(next) => { setValue(next); setSaved(false); }}
        mode="outlined"
        multiline
        numberOfLines={8}
        disabled={!isAuthenticated}
      />
      {!isAuthenticated ? <Text style={styles.note}>サーバーログイン中のみ利用できます。</Text> : null}
      <View style={styles.actions}>
        <Button mode="contained" loading={saving} disabled={saving || !isAuthenticated} onPress={() => void save()}>保存</Button>
      </View>
      {saved ? <Text style={styles.saved}>保存しました。</Text> : null}
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { padding: 16, gap: 12 },
  description: { color: "#a9a7c5", fontSize: 13, lineHeight: 19 },
  note: { color: "#f9e2af", fontSize: 12 },
  actions: { alignItems: "flex-start", marginTop: 4 },
  saved: { color: "#a6e3a1", fontSize: 13 },
});
