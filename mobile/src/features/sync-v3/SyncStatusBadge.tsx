import React, { useEffect, useState } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";
import { getSyncV3Engine } from "../../sync-v3/contract";
import type { SyncV3Status } from "../../sync-v3/contract";
import { describeConflictBadge, describeSyncStatus } from "./status-presentation";
import type { SyncTone } from "./status-presentation";

export const SYNC_TONE_COLORS: Record<SyncTone, string> = {
  ok: "#2e7d32",
  busy: "#1565c0",
  info: "#607d8b",
  warn: "#ef6c00",
  error: "#c62828",
};

export function SyncStatusBadge({ onPress }: { onPress?: () => void }) {
  const [status, setStatus] = useState<SyncV3Status | null>(
    () => getSyncV3Engine()?.getStatus() ?? null,
  );

  useEffect(() => {
    const engine = getSyncV3Engine();
    if (!engine) {
      setStatus(null);
      return undefined;
    }
    setStatus(engine.getStatus());
    return engine.subscribe(setStatus);
  }, []);

  const view = describeSyncStatus(status);
  const conflictLabel = describeConflictBadge(status?.openConflicts ?? 0);

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={view.label}
      onPress={onPress}
      disabled={!onPress}
      style={styles.row}
    >
      <View style={[styles.dot, { backgroundColor: SYNC_TONE_COLORS[view.tone] }]} />
      <Text variant="labelMedium">{view.label}</Text>
      {conflictLabel ? (
        <Text variant="labelMedium" style={styles.conflict}>
          {conflictLabel}
        </Text>
      ) : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center", gap: 6 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  conflict: { color: SYNC_TONE_COLORS.warn, fontWeight: "600" },
});
