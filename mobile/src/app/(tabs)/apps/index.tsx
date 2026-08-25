import React, { useCallback, useState } from "react";
import {
  FlatList,
  RefreshControl,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  FAB,
  Icon,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useRouter } from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { useProject } from "../../../contexts/ProjectContext";
import { useAuth } from "../../../contexts/AuthContext";
import { useNetworkStore } from "../../../stores/network";
import { appsRepo } from "../../../repositories/apps";
import type { AppSummary } from "../../../lib/apps-api";

const SURFACE = "#1e1e2e";
const BACKGROUND = "#11111b";
const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";

function permissionLabel(permission: string | null | undefined): string {
  return permission ? `権限: ${permission}` : "権限情報なし";
}

function AppListItem({ app, onPress }: { app: AppSummary; onPress: () => void }) {
  return (
    <Surface style={styles.card} elevation={1}>
      <Button
        mode="text"
        contentStyle={styles.cardButtonContent}
        onPress={onPress}
        accessibilityRole="button"
        accessibilityLabel={`${app.name}を開く`}
      >
        <View style={styles.iconCircle}>
          <Icon source="application-brackets-outline" size={24} color="#89b4fa" />
        </View>
        <View style={styles.cardCopy}>
          <Text style={styles.cardTitle} numberOfLines={1}>
            {app.name}
          </Text>
          <Text style={styles.cardMeta} numberOfLines={1}>
            {app.slug} · {permissionLabel(app.permission)}
          </Text>
          {app.description ? (
            <Text style={styles.cardDescription} numberOfLines={2}>
              {app.description}
            </Text>
          ) : null}
          {app.related_project_ids && app.related_project_ids.length > 0 ? (
            <Text style={styles.cardRelated} numberOfLines={1}>
              Project {app.related_project_ids.length}件
            </Text>
          ) : null}
        </View>
      </Button>
    </Surface>
  );
}

export default function AppsListScreen() {
  const router = useRouter();
  const { selectedProjectId } = useProject();
  const { isAuthenticated } = useAuth();
  const online = useNetworkStore((state) => state.online);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createVisible, setCreateVisible] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createSlug, setCreateSlug] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async (force = false) => {
    setError(null);
    try {
      const next = await appsRepo.list({ projectId: selectedProjectId, force });
      setApps(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Appsを読み込めませんでした");
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const handleRefresh = async () => {
    setRefreshing(true);
    await load(true);
    setRefreshing(false);
  };

  const handleCreate = async () => {
    const name = createName.trim();
    if (!name) return;
    setCreating(true);
    setError(null);
    try {
      const created = await appsRepo.create({
        name,
        slug: createSlug.trim() || undefined,
        description: createDescription.trim() || undefined,
        origin_project_id: selectedProjectId,
      });
      setApps((previous) => [created, ...previous.filter((item) => item.id !== created.id)]);
      setCreateVisible(false);
      setCreateName("");
      setCreateSlug("");
      setCreateDescription("");
      router.push({
        pathname: "/(tabs)/apps/[appId]",
        params: { appId: created.id, projectId: selectedProjectId ?? "" },
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Appを作成できませんでした");
    } finally {
      setCreating(false);
    }
  };

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Apps"
        subtitle={selectedProjectId ? "選択中 Project の App" : "App の概要・実行状況・関連"}
      />
      {!online ? (
        <Text style={styles.offlineBanner}>オフライン: 最終同期時点の Apps を表示しています（変更不可）</Text>
      ) : null}
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {loading && apps.length === 0 ? (
        <View style={styles.centered}>
          <ActivityIndicator size="large" color="#89b4fa" />
        </View>
      ) : (
        <FlatList
          data={apps}
          keyExtractor={(item) => item.id}
          contentContainerStyle={apps.length === 0 ? styles.emptyContent : styles.listContent}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={handleRefresh}
              tintColor="#89b4fa"
            />
          }
          renderItem={({ item }) => (
            <AppListItem
              app={item}
              onPress={() =>
                router.push({
                  pathname: "/(tabs)/apps/[appId]",
                  params: { appId: item.id, projectId: selectedProjectId ?? "" },
                })
              }
            />
          )}
          ListEmptyComponent={
            <View style={styles.empty}>
              <Icon source="application-brackets-outline" size={42} color="#585b70" />
              <Text style={styles.emptyTitle}>Appsはまだありません</Text>
              <Text style={styles.emptyText}>
                App を作成すると、Project・Task・Chatとの関係と実行状況をここで確認できます。
              </Text>
            </View>
          }
        />
      )}
      {isAuthenticated && online ? (
        <FAB
          icon="plus"
          label="Appを作成"
          color="#11111b"
          style={styles.fab}
          onPress={() => setCreateVisible(true)}
          accessibilityLabel="Appを作成"
        />
      ) : null}
      <Portal>
        <Dialog visible={createVisible} onDismiss={() => !creating && setCreateVisible(false)}>
          <Dialog.Title>Appを作成</Dialog.Title>
          <Dialog.Content>
            <TextInput
              label="名前"
              value={createName}
              onChangeText={setCreateName}
              mode="outlined"
              autoFocus
              style={styles.dialogInput}
            />
            <TextInput
              label="Slug（任意）"
              value={createSlug}
              onChangeText={setCreateSlug}
              mode="outlined"
              autoCapitalize="none"
              style={styles.dialogInput}
            />
            <TextInput
              label="説明（任意）"
              value={createDescription}
              onChangeText={setCreateDescription}
              mode="outlined"
              multiline
              style={styles.dialogInput}
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setCreateVisible(false)} disabled={creating}>キャンセル</Button>
            <Button onPress={() => void handleCreate()} loading={creating} disabled={!createName.trim() || creating}>
              作成
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: BACKGROUND },
  centered: { flex: 1, justifyContent: "center", alignItems: "center" },
  listContent: { padding: 12, paddingBottom: 100 },
  emptyContent: { flexGrow: 1, justifyContent: "center", padding: 24 },
  card: { backgroundColor: SURFACE, borderRadius: 14, marginBottom: 10 },
  cardButtonContent: { justifyContent: "flex-start", paddingHorizontal: 12, paddingVertical: 12 },
  iconCircle: { width: 42, height: 42, borderRadius: 21, backgroundColor: "#313244", justifyContent: "center", alignItems: "center", marginRight: 10 },
  cardCopy: { flex: 1, alignItems: "flex-start" },
  cardTitle: { color: TEXT, fontWeight: "700", fontSize: 16 },
  cardMeta: { color: MUTED, fontSize: 12, marginTop: 3 },
  cardDescription: { color: "#bac2de", fontSize: 13, marginTop: 5 },
  cardRelated: { color: "#89b4fa", fontSize: 12, marginTop: 5 },
  empty: { alignItems: "center", gap: 8 },
  emptyTitle: { color: TEXT, fontSize: 18, fontWeight: "700", marginTop: 8 },
  emptyText: { color: MUTED, textAlign: "center", lineHeight: 20 },
  error: { color: "#f38ba8", paddingHorizontal: 16, paddingTop: 8 },
  offlineBanner: { color: "#f9e2af", backgroundColor: "#3b3341", paddingHorizontal: 16, paddingVertical: 8, fontSize: 12 },
  fab: { position: "absolute", right: 16, bottom: 20, backgroundColor: "#89b4fa" },
  dialogInput: { marginBottom: 10 },
});
