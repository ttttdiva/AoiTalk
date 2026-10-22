import React from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";
import { activityTime, attentionLabel, duration, type OperationsCard } from "../../lib/operations-api";

export function OperationsWorkCard({ item, onOpen }: { item: OperationsCard; onOpen?: () => void }) {
  return <Pressable accessibilityRole={onOpen ? "button" : undefined} accessibilityLabel={`${item.title}${onOpen ? " 実行の詳細" : " 要対応"}`} onPress={onOpen} style={styles.card}>
    <Text style={styles.context}>{item.project} · {item.agent}</Text>
    <Text variant="titleMedium" style={styles.title}>{item.title}</Text>
    <Text style={styles.state}>{item.state}</Text>
    {item.attention.length ? <Text style={styles.attention}>{item.attention.map(attentionLabel).join(" / ")}</Text> : null}
    {item.approvalDetails?.map((detail, index) => <Text key={index} style={styles.text}>{detail}</Text>)}
    {item.sourceKind === "work" ? <View style={styles.metadata}>
      <Text style={styles.text}>Phase: {item.summary.phase ?? "未記録"}</Text>
      <Text style={styles.text}>Tool: {item.summary.tool ?? "未記録"}</Text>
      <Text style={styles.text}>経過: {duration(item.summary.elapsed_seconds)}</Text>
      <Text style={styles.text}>最終活動: {activityTime(item.summary.last_activity_at)}</Text>
      <Text style={styles.text}>Attempt {item.summary.attempt_count ?? 0}/{item.summary.max_attempts ?? "?"}</Text>
      {item.summary.blocker_code || item.summary.safe_error_code ? <Text style={styles.attention}>停止理由: {item.summary.blocker_code || item.summary.safe_error_code}</Text> : null}
      {item.summary.observation_status && item.summary.observation_status !== "available" ? <Text style={styles.attention}>一部の情報は未確認です</Text> : null}
    </View> : <Text style={styles.text}>外部操作の変更や承認は正本のAction画面で確認してください。</Text>}
    <Text style={styles.link}>{onOpen ? "実行の詳細 →" : "正本のOperationsで確認してください"}</Text>
  </Pressable>;
}
const styles = StyleSheet.create({ card: { backgroundColor: "#1e1e2e", borderWidth: 1, borderColor: "#45475a", borderRadius: 12, padding: 16, marginBottom: 12 }, context: { color: "#a6adc8", fontSize: 12 }, title: { color: "#cdd6f4", marginTop: 8 }, state: { color: "#89b4fa", marginTop: 6 }, attention: { color: "#f9e2af", fontSize: 12, marginTop: 6 }, metadata: { gap: 4, marginTop: 10 }, text: { color: "#bac2de", fontSize: 12 }, link: { color: "#89b4fa", marginTop: 14 } });


