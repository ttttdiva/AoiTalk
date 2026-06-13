import React, { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import {
  ActivityIndicator,
  Button,
  IconButton,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";

import { goBackOrReplace } from "../../../lib/navigation";
import {
  createRemoteServer,
  deleteRemoteServer,
  listRemoteServers,
  testRemoteServer,
  updateRemoteServer,
  type RemoteServerProfile,
} from "../../../lib/remote-servers";

const COLOR_PRESETS = [
  "#3b82f6",
  "#a6e3a1",
  "#f38ba8",
  "#f9e2af",
  "#cba6f7",
  "#89dceb",
  "#fab387",
  "#94e2d5",
];

const STATUS_META: Record<string, { label: string; color: string }> = {
  ok: { label: "接続OK", color: "#a6e3a1" },
  error: { label: "接続エラー", color: "#f38ba8" },
};

function statusMeta(status?: string | null): { label: string; color: string } {
  if (!status) return { label: "未確認", color: "#a6adc8" };
  return STATUS_META[status] ?? { label: status, color: "#a6adc8" };
}

export default function RemoteServersScreen() {
  const router = useRouter();
  const [profiles, setProfiles] = useState<RemoteServerProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [color, setColor] = useState(COLOR_PRESETS[0]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setProfiles(await listRemoteServers());
    } catch (err) {
      setError(err instanceof Error ? err.message : "読み込みに失敗しました");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const handleCreate = async () => {
    if (!name.trim() || !baseUrl.trim()) {
      setError("名前とURLは必須です");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      await createRemoteServer({
        name: name.trim(),
        base_url: baseUrl.trim(),
        auth_token: authToken.trim() || null,
        display_color: color,
        enabled: true,
      });
      setName("");
      setBaseUrl("");
      setAuthToken("");
      setColor(COLOR_PRESETS[0]);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "追加に失敗しました");
    } finally {
      setCreating(false);
    }
  };

  const handleTest = async (id: string) => {
    setTestingId(id);
    try {
      await testRemoteServer(id);
      await reload();
    } finally {
      setTestingId(null);
    }
  };

  const handleToggle = async (profile: RemoteServerProfile) => {
    setSavingId(profile.id);
    try {
      await updateRemoteServer(profile.id, { enabled: !profile.enabled });
      await reload();
    } finally {
      setSavingId(null);
    }
  };

  const handleDelete = async (id: string) => {
    setSavingId(id);
    try {
      await deleteRemoteServer(id);
      await reload();
    } finally {
      setSavingId(null);
    }
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, "/(tabs)/settings")}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              外部AoiTalkサーバー接続
            </Text>
            <Text style={styles.headerSubtext}>
              他のAoiTalkサーバーのタスクを表示・操作します。
            </Text>
          </View>
        </View>
      </Surface>

      {loading ? (
        <View style={styles.loadingWrap}>
          <ActivityIndicator size="small" color="#7c3aed" />
        </View>
      ) : (
        profiles.map((profile) => {
          const meta = statusMeta(profile.last_status);
          return (
            <Surface key={profile.id} style={styles.card} elevation={0}>
              <View style={styles.cardHeader}>
                <View
                  style={[
                    styles.colorDot,
                    { backgroundColor: profile.display_color || "#3b82f6" },
                  ]}
                />
                <View style={{ flex: 1 }}>
                  <Text style={styles.cardName}>{profile.name}</Text>
                  <Text style={styles.cardUrl} numberOfLines={1}>
                    {profile.base_url}
                  </Text>
                </View>
                <Switch
                  value={profile.enabled}
                  onValueChange={() => void handleToggle(profile)}
                  disabled={savingId === profile.id}
                />
              </View>
              <View style={styles.cardMeta}>
                <Text style={[styles.statusBadge, { color: meta.color }]}>
                  {meta.label}
                </Text>
                {profile.has_token ? null : (
                  <Text style={styles.noTokenBadge}>トークン未設定</Text>
                )}
              </View>
              <View style={styles.cardActions}>
                <Button
                  mode="outlined"
                  compact
                  textColor="#89b4fa"
                  loading={testingId === profile.id}
                  onPress={() => void handleTest(profile.id)}
                >
                  接続テスト
                </Button>
                <Button
                  mode="text"
                  compact
                  textColor="#f38ba8"
                  disabled={savingId === profile.id}
                  onPress={() => void handleDelete(profile.id)}
                >
                  削除
                </Button>
              </View>
            </Surface>
          );
        })
      )}

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>接続先を追加</Text>
        <TextInput
          mode="outlined"
          label="表示名"
          value={name}
          onChangeText={setName}
          style={styles.input}
        />
        <TextInput
          mode="outlined"
          label="ベースURL (例: https://example.com)"
          value={baseUrl}
          onChangeText={setBaseUrl}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          style={styles.input}
        />
        <TextInput
          mode="outlined"
          label="APIトークン"
          value={authToken}
          onChangeText={setAuthToken}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          style={styles.input}
        />
        <Text style={styles.colorLabel}>表示色</Text>
        <View style={styles.colorRow}>
          {COLOR_PRESETS.map((preset) => (
            <IconButton
              key={preset}
              icon={color === preset ? "check" : "circle"}
              iconColor={color === preset ? "#11111b" : preset}
              size={20}
              style={[styles.colorSwatch, { backgroundColor: preset }]}
              onPress={() => setColor(preset)}
            />
          ))}
        </View>
        {error ? <Text style={styles.errorText}>{error}</Text> : null}
        <Button
          mode="contained"
          buttonColor="#7c3aed"
          textColor="#cdd6f4"
          loading={creating}
          onPress={() => void handleCreate()}
          style={styles.addButton}
        >
          追加
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
  headerSubtext: { color: "#a6adc8", marginTop: 2, fontSize: 12 },
  loadingWrap: { paddingVertical: 24, alignItems: "center" },
  card: {
    backgroundColor: "#1e1e2e",
    borderRadius: 12,
    padding: 16,
    margin: 16,
    marginBottom: 0,
  },
  cardHeader: { flexDirection: "row", alignItems: "center", gap: 10 },
  colorDot: { width: 14, height: 14, borderRadius: 999 },
  cardName: { color: "#cdd6f4", fontSize: 15, fontWeight: "700" },
  cardUrl: { color: "#a6adc8", fontSize: 12, marginTop: 2 },
  cardMeta: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginTop: 10,
  },
  statusBadge: { fontSize: 12, fontWeight: "700" },
  noTokenBadge: { color: "#f9e2af", fontSize: 11 },
  cardActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginTop: 10,
  },
  cardTitle: {
    color: "#7c3aed",
    fontSize: 13,
    fontWeight: "700",
    marginBottom: 12,
  },
  input: { marginBottom: 12, backgroundColor: "#181825" },
  colorLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 6 },
  colorRow: { flexDirection: "row", flexWrap: "wrap", gap: 4, marginBottom: 12 },
  colorSwatch: { margin: 0, borderRadius: 999 },
  errorText: { color: "#f38ba8", fontSize: 12, marginBottom: 8 },
  addButton: { alignSelf: "flex-start" },
});
