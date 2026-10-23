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

export function SyncStatusPanel({
  scopes,
  onCopy,
  onOpenConflicts,
}: {
  scopes: SyncPanelScope[];
  onCopy?: (text: string) => void;
  onOpenConflicts?: () => void;
}) {
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

  const confirmSafeStop = () => {
    Alert.alert("同期を安全に停止", "進行中の同期を停止し、保存済みの端末データは保持します。", [
      { text: "キャンセル", style: "cancel" },
      { text: "停止", style: "destructive", onPress: () => { void getSyncV3Engine()?.safeStop(); } },
    ]);
  };

  return (
    <View style={styles.root}>
      <Text variant="titleMedium">{view.label}</Text>
      {view.detail ? <Text variant="bodySmall">{view.detail}</Text> : null}
      {status ? (
        <>
          <Text variant="bodySmall">認証scope: {status.authScope ?? "-"}</Text>
          <Text variant="bodySmall">cursor: {status.cursor ?? "-"}</Text>
          <Text variant="bodySmall">scope revision: {status.scopeSetRevision ?? "-"}</Text>
          <Text variant="bodySmall">最終同期成功: {formatTime(status.lastSuccessAt ?? null)}</Text>
          {status.sweepError ? <Text variant="bodySmall">scope保全停止: {status.sweepError}</Text> : null}
          <LaneRow name="push" lane={status.push} />
          <LaneRow name="changes" lane={status.changes} />
          <LaneRow name="bootstrap" lane={status.bootstrap} />
          <Text variant="bodySmall">queue: queued {status.queue.queued} / backoff {status.queue.backoff} / sent {status.queue.sent}</Text>
          <Text variant="bodySmall">競合: {status.openConflicts}件</Text>
          <Text variant="bodySmall">entity counts: {JSON.stringify(status.entityCounts ?? {})}</Text>
          <Text variant="bodySmall">SQLite write units: {status.writeUnits?.count ?? 0}件 / p95 {status.writeUnits?.p95Ms ?? "-"}ms / max {status.writeUnits?.maxMs ?? "-"}ms</Text>
          {status.recentRequests?.length ? (
            <View style={styles.requests}>
              <Text variant="titleSmall">直近のrequest outcome</Text>
              {status.recentRequests.slice(-8).reverse().map((request) => (
                <View key={request.requestId} style={styles.request}>
                  <Text variant="bodySmall">{request.lane}: {request.outcome} / HTTP {request.status ?? "-"}</Text>
                  <Text variant="bodySmall" selectable>{request.requestId}</Text>
                  {onCopy ? <Button compact onPress={() => onCopy(request.requestId)}>request idをコピー</Button> : null}
                </View>
              ))}
            </View>
          ) : null}
        </>
      ) : null}
      <Button mode="contained" onPress={() => getSyncV3Engine()?.requestSync("manual")}>
        手動で同期
      </Button>
      <Button mode="outlined" onPress={confirmSafeStop}>同期を安全に停止</Button>
      {onOpenConflicts ? <Button onPress={onOpenConflicts}>競合を確認</Button> : null}
      {scopes.map((scope) => (
        <View key={scope.scopeKey} style={styles.scope}>
          <Text variant="bodyMedium">{scope.label}</Text>
          <Button
            mode="outlined"
            disabled={status?.sweepError != null || status?.overall === "disabled" || status?.overall === "migration_pending"}
            onPress={() => confirmRebootstrap(scope)}
          >
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
  requests: { gap: 6, marginTop: 8 },
  request: { gap: 2, padding: 8, borderRadius: 8, backgroundColor: "#242236" },
  scope: { gap: 4 },
});
