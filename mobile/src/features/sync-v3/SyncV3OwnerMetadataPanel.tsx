import React from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";
import type { OwnerSyncDiagnostics, ScopedDiagnosticCount } from "../../sync-v3/owner-diagnostics";

function countLabel(value: ScopedDiagnosticCount): string {
  return value.status === "available" && value.count != null ? `${value.count}件` : "UNKNOWN";
}

export function SyncV3OwnerMetadataPanel({ metadata }: { metadata: OwnerSyncDiagnostics | null }) {
  if (!metadata) {
    return <Text testID="sync-v3-owner-metadata-loading" style={styles.unknown}>本人scope診断: 読取中 / UNKNOWN</Text>;
  }
  return (
    <View testID="sync-v3-owner-metadata" style={styles.root}>
      <Text style={styles.title}>本人scopeの診断件数</Text>
      <Text style={styles.body}>server/user確認: {metadata.scopeStatus === "verified" ? "確認済み" : "UNKNOWN"}</Text>
      <Text style={styles.body}>保存済みChat下書き: {countLabel(metadata.chatComposerDrafts)}</Text>
      <Text style={styles.body}>resolution下書き: {countLabel(metadata.resolutionDrafts)}</Text>
      <Text style={styles.body}>v2 fallback hold: {countLabel(metadata.v2FallbackHolds)}</Text>
      <Text style={styles.body}>
        Docs dirty（knowledge_nodes / knowledge_supertags / knowledge_node_supertags / knowledge_field_values）: {countLabel(metadata.docsDirtyByTable.knowledge_nodes)} / {countLabel(metadata.docsDirtyByTable.knowledge_supertags)} / {countLabel(metadata.docsDirtyByTable.knowledge_node_supertags)} / {countLabel(metadata.docsDirtyByTable.knowledge_field_values)}
      </Text>
      {metadata.docsDirtyReason ? <Text style={styles.unknown}>Docs dirty: scopeへserver fingerprintが保存されていないためUNKNOWN</Text> : null}
      {metadata.v2FallbackHoldReason ? <Text style={styles.unknown}>v2 fallback hold: scopeへserver fingerprintが保存されていないため未確認（0件とは判定しません）。結果不明のまま保留された操作がある場合、その操作は自動再送されません</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 4, padding: 12, borderRadius: 10, backgroundColor: "#1b1a2b" },
  title: { color: "#f0efff", fontSize: 14, fontWeight: "600" },
  body: { color: "#cdd6f4", fontSize: 12 },
  unknown: { color: "#8f8aa8", fontSize: 12 },
});
