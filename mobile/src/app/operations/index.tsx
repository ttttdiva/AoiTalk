import React, { useCallback, useMemo } from "react";
import { RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Button, Text } from "react-native-paper";
import { useAuth } from "../../contexts/AuthContext";
import { useProject } from "../../contexts/ProjectContext";
import { ScopeSwitcher } from "../../components/scope-switcher";
import { ScreenHeader } from "../../components/screen-header";
import { OperationsWorkCard } from "../../features/operations/operations-work-card";
import { useOperations } from "../../features/operations/use-operations";
import { activityTime, getOperationsOverview, operationsCards } from "../../lib/operations-api";

export default function OperationsScreen() {
  const router = useRouter(); const insets = useSafeAreaInsets();
  const { user } = useAuth(); const { selectedProjectId, selectedSpaceId } = useProject();
  const loader = useCallback((server: string, signal: AbortSignal) => getOperationsOverview(server, { projectId: selectedProjectId, spaceId: selectedSpaceId }, signal), [selectedProjectId, selectedSpaceId]);
  const { data, error, loading, checkedAt, refresh } = useOperations(`${user?.user_id}:${selectedProjectId}:${selectedSpaceId}`, loader);
  const cards = useMemo(() => operationsCards(data), [data]);
  const attention = cards.filter(item => item.attention.length);
  const running = cards.filter(item => ["running", "claimed"].includes(item.state) && !item.attention.length);
  const waiting = cards.filter(item => !item.attention.length && !["running", "claimed", "succeeded", "cancelled"].includes(item.state));
  const completed = cards.filter(item => item.sourceKind === "work" && !item.attention.length && ["succeeded", "cancelled"].includes(item.state));
  const open = (id: string) => router.push({ pathname: "/operations/work/[workId]", params: { workId: id } });
  return <View style={styles.screen}>
    <ScreenHeader
      title="Operations"
      right={
        <View style={styles.headerActions}>
          <ScopeSwitcher variant="inline" projectNameOnly accessibilityLabel="Operationsの範囲を変更" />
        </View>
      }
    />
    <ScrollView contentContainerStyle={[styles.content, { paddingBottom: insets.bottom + 24 }]} refreshControl={<RefreshControl refreshing={loading} onRefresh={() => void refresh()} tintColor="#89b4fa" />}>
      <Text style={styles.muted}>最終確認 {activityTime(checkedAt)} · サーバーの読み取り専用表示</Text>
      {error ? <View accessibilityRole="alert"><Text style={styles.warning}>{error}</Text><Button onPress={() => void refresh()}>再読み込み</Button></View> : null}
      {!error && data ? <>
        <Text variant="titleMedium" style={styles.section}>要対応</Text>
        {attention.length ? attention.map(item => <OperationsWorkCard key={item.id} item={item} onOpen={item.workId ? () => open(item.workId!) : undefined} />) : <Text style={styles.muted}>取得した仕事に要対応の記録はありません</Text>}
        <Text variant="titleMedium" style={styles.section}>実行中</Text>
        {running.length ? running.map(item => <OperationsWorkCard key={item.id} item={item} onOpen={item.workId ? () => open(item.workId!) : undefined} />) : <Text style={styles.muted}>取得した仕事に実行中の記録はありません</Text>}
        {waiting.length ? <><Text variant="titleMedium" style={styles.section}>待機中</Text>{waiting.map(item => <OperationsWorkCard key={item.id} item={item} onOpen={item.workId ? () => open(item.workId!) : undefined} />)}</> : null}
        {completed.length ? <><Text variant="titleMedium" style={styles.section}>完了・キャンセル（最近）</Text>{completed.map(item => <OperationsWorkCard key={item.id} item={item} onOpen={item.workId ? () => open(item.workId!) : undefined} />)}</> : null}
        <Text style={styles.muted}>選択範囲の最大100件です。全体の件数・工程進捗率ではありません。</Text>
      </> : null}
      {loading && !data ? <Text style={styles.muted}>最新の状況を確認中…</Text> : null}
    </ScrollView>
  </View>;
}
const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#11111b" },
  headerActions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", minWidth: 0, maxWidth: "100%" },
  content: { padding: 16 },
  section: { color: "#cdd6f4", marginTop: 24, marginBottom: 12 },
  muted: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  warning: { color: "#f9e2af", marginTop: 16 },
});


