import React from "react";
import { Alert, StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { IconButton, List, Switch, Text } from "react-native-paper";
import { goBackOrReplace } from "../../../lib/navigation";
import { ScreenHeader } from "../../../components/screen-header";
import { ScreenShell } from "../../../components/screen-primitives";
import { useChatWavePreference } from "../../../features/conversation/use-chat-wave-preference";

export default function AppearanceSettingsScreen() {
  const router = useRouter();
  const { enabled, loaded, saving, error, update } = useChatWavePreference();
  const openWaveHelp = () => Alert.alert(
    "チャット背景アニメーション",
    "チャットの背面に細かな文字の波を表示します。チャット画面を短くタップすると波紋が広がります。動きを減らす設定が有効な場合は停止します。",
  );
  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={
        <ScreenHeader
          title="表示・外観"
          onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
        />
      }
    >
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>チャット</Text>
        <List.Item
          title="チャット背景アニメーション"
          titleNumberOfLines={2}
          titleStyle={styles.title}
          left={(props) => <List.Icon {...props} icon="wave" color="#a78bfa" />}
          right={() => (
            <View style={styles.actions}>
              <IconButton
                accessibilityLabel="チャット背景アニメーションの説明"
                icon="help-circle-outline"
                iconColor="#a6adc8"
                size={20}
                onPress={openWaveHelp}
              />
              <Switch
                accessibilityLabel="チャット背景アニメーション"
                value={loaded && enabled}
                disabled={!loaded || saving}
                onValueChange={(value) => void update(value)}
              />
            </View>
          )}
          style={styles.row}
        />
        {!loaded ? <Text style={styles.status}>表示設定を読み込み中…</Text> : null}
        {saving ? <Text style={styles.status}>表示設定を保存中…</Text> : null}
        {error ? <Text accessibilityLiveRegion="polite" style={styles.error}>{error}</Text> : null}
      </View>
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { paddingBottom: 32 },
  section: { marginTop: 16, marginHorizontal: 12, borderRadius: 12, backgroundColor: "#1b1a2b", overflow: "hidden" },
  sectionTitle: { color: "#8f8aa8", fontSize: 12, fontWeight: "600", marginHorizontal: 16, marginTop: 14, marginBottom: 4 },
  row: { minHeight: 68, paddingHorizontal: 4 },
  title: { color: "#f0efff", fontSize: 14, fontWeight: "500" },
  actions: { flexDirection: "row", alignItems: "center" },
  status: { color: "#a6adc8", fontSize: 12, paddingHorizontal: 16, paddingBottom: 10 },
  error: { color: "#f38ba8", fontSize: 12, paddingHorizontal: 16, paddingBottom: 12 },
});
