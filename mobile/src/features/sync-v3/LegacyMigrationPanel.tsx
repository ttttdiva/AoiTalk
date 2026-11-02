import React, { useEffect, useState } from "react";
import { Alert, StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";
import { getSyncV3Engine } from "../../sync-v3/contract";
import { getSyncV3LegacyExport } from "../../sync-v3/legacy-migration";
import type { SyncV3Status } from "../../sync-v3/contract";

export function LegacyMigrationPanel({ onCopy }: { onCopy?: (text: string) => void }) {
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

  const legacy = status?.legacy;
  if (!legacy || legacy.state === "not_needed") return null;

  if (legacy.state === "failed") {
    return (
      <View style={styles.root}>
        <Text variant="titleSmall">旧版データの移行を完了できませんでした</Text>
        <Text variant="bodyMedium">詳細エラーは非表示です。データ状態を確認してください。</Text>
        <Text variant="bodyMedium">データは変更されていません。</Text>
        <CaptureMetadata legacy={legacy} />
      </View>
    );
  }

  if (legacy.state === "capturing") {
    return <View style={styles.root}>
      <Text variant="bodyMedium">旧版データを退避しています…</Text>
      <CaptureMetadata legacy={legacy} />
    </View>;
  }
  if (legacy.state === "migrating") {
    return <View style={styles.root}>
      <Text variant="bodyMedium">旧版データを移行しています…</Text>
      <CaptureMetadata legacy={legacy} />
    </View>;
  }
  if (legacy.state === "migrated") {
    return (
      <View style={styles.root}>
        <Text variant="bodyMedium">旧版データの移行が完了しました。</Text>
        <Text variant="bodySmall">所有者不明: {legacy.ownerUnknownCount ?? 0}件（別ユーザーへ割り当てず保持）</Text>
        <CaptureMetadata legacy={legacy} />
        {onCopy ? (
          <Button onPress={() => {
            void getSyncV3LegacyExport().then((value) => { if (value) onCopy(value); });
          }}>
            退避データをコピー
          </Button>
        ) : null}
      </View>
    );
  }

  const sha = legacy.captureSha256;
  const confirmMigration = () => {
    if (!sha) return;
    Alert.alert(
      "移行を実行",
      `旧版の未送信データ ${legacy.legacyCount ?? 0}件を新しい同期へ移行します。`,
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "移行を実行",
          onPress: () => {
            void getSyncV3Engine()?.confirmLegacyMigration(sha);
          },
        },
      ],
    );
  };

  return (
    <View style={styles.root}>
      <Text variant="titleSmall">旧版の未送信データを移行します</Text>
      <Text variant="bodyMedium">件数: {legacy.legacyCount ?? 0}件</Text>
      <Text variant="bodyMedium">
        所有者不明: {legacy.ownerUnknownCount ?? 0}件（保持され、削除されません）
      </Text>
      <CaptureMetadata legacy={legacy} />
      {sha ? (
        <>
          <Text variant="bodyMedium">確認用ハッシュ: {sha.slice(0, 16)}</Text>
          <Text variant="bodySmall" selectable>
            {sha}
          </Text>
          {onCopy ? <Button onPress={() => onCopy(sha)}>ハッシュ全文をコピー</Button> : null}
        </>
      ) : null}
      {legacy.captureFile ? (
        <Text variant="bodySmall">退避ファイルがあります（pathは非表示）</Text>
      ) : null}
      {onCopy ? (
        <Button onPress={() => {
          void getSyncV3LegacyExport().then((value) => { if (value) onCopy(value); });
        }}>
          退避データをコピー
        </Button>
      ) : null}
      <Button mode="contained" disabled={!sha} onPress={confirmMigration}>
        移行を実行
      </Button>
    </View>
  );
}

function count(value: number | null | undefined): string {
  return value == null ? "UNKNOWN" : `${value}件`;
}

function CaptureMetadata({ legacy }: { legacy: NonNullable<SyncV3Status["legacy"]> }) {
  const metadata = legacy.metadata;
  if (!metadata) {
    return <Text variant="bodySmall">capture metadata: UNKNOWN</Text>;
  }
  return (
    <View testID="legacy-capture-metadata" style={styles.metadata}>
      <Text variant="bodySmall">capture metadata: {metadata.available ? "available" : "UNKNOWN"}</Text>
      <Text variant="bodySmall">
        capture generation: {metadata.generation ?? "UNKNOWN"} / updated: {metadata.updatedAt == null ? "UNKNOWN" : new Date(metadata.updatedAt).toLocaleString("ja-JP")}
      </Text>
      <Text variant="bodySmall">
        total op/dirty: {count(metadata.opCount)} / {count(metadata.dirtyCount)}
      </Text>
      <Text variant="bodySmall">
        active op/dirty: {count(metadata.active?.opCount)} / {count(metadata.active?.dirtyCount)}
      </Text>
      <Text variant="bodySmall">
        archived snapshots/op/dirty: {metadata.archived?.snapshotCount ?? "UNKNOWN"} / {count(metadata.archived?.opCount)} / {count(metadata.archived?.dirtyCount)}
      </Text>
      <Text variant="bodySmall">
        comparison_available: {metadata.comparisonAvailable ? "true" : "false"}
        {metadata.comparisonAvailable && metadata.comparison
          ? ` (capture/op/dirty Δ ${metadata.comparison.captureCount}/${metadata.comparison.opCount}/${metadata.comparison.dirtyCount}; added row variants ${metadata.comparison.addedOutboxVariants}/${metadata.comparison.addedDirtyVariants})`
          : " (delta UNKNOWN)"}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 6 },
  metadata: { gap: 2 },
});
