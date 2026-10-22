import React from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import type { DocsListLayoutSnapshot } from "./list-layout-trace";

/** Explicit read-only QA view. No console/file/network/clipboard output. */
export function DocsListLayoutInspector({ snapshot, onDismiss }: {
  snapshot: DocsListLayoutSnapshot | null;
  onDismiss: () => void;
}) {
  return <Modal testID="docs-layout-inspector" transparent visible={snapshot !== null} onRequestClose={onDismiss}>
    <View style={styles.backdrop}>
      <View style={styles.panel}>
        <View style={styles.heading}>
          <Text style={styles.title}>一覧のレイアウト計測</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="一覧の計測を閉じる"
            onPress={onDismiss} style={styles.close}><Text style={styles.text}>閉じる</Text></Pressable>
        </View>
        <Text style={styles.note}>高さ・位置・可視件数と処理順だけを表示します。</Text>
        <ScrollView>
          <Text selectable testID="docs-layout-trace" style={styles.trace}>
            {snapshot ? JSON.stringify(snapshot, null, 2) : ""}
          </Text>
        </ScrollView>
      </View>
    </View>
  </Modal>;
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, justifyContent: "center", padding: 20, backgroundColor: "#0008" },
  panel: { maxHeight: "85%", padding: 16, borderRadius: 12, backgroundColor: "#1e1e2e" },
  heading: { flexDirection: "row", alignItems: "center", gap: 8 },
  title: { flex: 1, fontSize: 18, fontWeight: "600", color: "#cdd6f4" },
  close: { minWidth: 44, minHeight: 44, alignItems: "center", justifyContent: "center" },
  text: { color: "#89b4fa" },
  note: { color: "#a6adc8", marginVertical: 12 },
  trace: { color: "#cdd6f4", fontSize: 12, lineHeight: 18 },
});
