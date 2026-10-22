import React, { useEffect, useState } from "react";
import { Alert, StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";
import { getSyncV3Engine } from "../../sync-v3/contract";
import type { LaneState, SyncV3Status } from "../../sync-v3/contract";
import { describeSyncStatus } from "./status-presentation";

export type SyncPanelScope = { scopeKey: string; label: string };

function formatTime(value: number | null): string {
  return value === null ? "-" : new Date(value).toLocaleString("ja-JP");
}

function LaneRow({ name, lane }: { name: string; lane: LaneState }) {
  return (
    <View style={styles.lane} testID={`sync-lane-${name}`}>
      <Text variant="titleSmall">{name}</Text>
      <Text variant="bodySmall">状態: {lane.state}</Text>
      <Text variant="bodySmall">最終成功: {formatTime(lane.lastSuccessAt)}</Text>
      <Text variant="bodySmall">次回試行: {formatTime(lane.nextAttemptAt)}</Text>
      <Text variant="bodySmall">エラーコード: {lane.lastErrorCode ?? "-"}</Text>
    </View>
  );
}

export function SyncStatusPanel({ scopes }: { scopes: SyncPanelScope[] }) {
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

  const confirmRebootstrap = (scope: SyncPanelScope) => {
    Alert.alert(
      "このscopeを再取得",
      `${scope.label} をサーバーから再取得します。端末の未送信データは消えません。`,
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "再取得",
          onPress: () => {
            void getSyncV3Engine()?.requestRebootstrap(scope.scopeKey);
          },
        },
      ],
    );
  };

  return (
    <View style={styles.root}>
      <Text variant="titleMedium">{view.label}</Text>
      {view.detail ? <Text variant="bodySmall">{view.detail}</Text> : null}
      {status ? (
        <>
          <LaneRow name="push" lane={status.push} />
          <LaneRow name="changes" lane={status.changes} />
          <LaneRow name="bootstrap" lane={status.bootstrap} />
        </>
      ) : null}
      <Button mode="contained" onPress={() => getSyncV3Engine()?.requestSync("manual")}>
        手動で同期
      </Button>
      {scopes.map((scope) => (
        <View key={scope.scopeKey} style={styles.scope}>
          <Text variant="bodyMedium">{scope.label}</Text>
          <Button mode="outlined" onPress={() => confirmRebootstrap(scope)}>
            このscopeを再取得
          </Button>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 8 },
  lane: { gap: 2 },
  scope: { gap: 4 },
});
