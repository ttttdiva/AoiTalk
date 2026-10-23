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
        <Text variant="bodyMedium">エラーコード: {legacy.errorCode ?? "-"}</Text>
        <Text variant="bodyMedium">データは変更されていません。</Text>
      </View>
    );
  }

  if (legacy.state === "capturing") {
    return <Text variant="bodyMedium">旧版データを退避しています…</Text>;
  }
  if (legacy.state === "migrating") {
    return <Text variant="bodyMedium">旧版データを移行しています…</Text>;
  }
  if (legacy.state === "migrated") {
    return (
      <View style={styles.root}>
        <Text variant="bodyMedium">旧版データの移行が完了しました。</Text>
        <Text variant="bodySmall">所有者不明: {legacy.ownerUnknownCount ?? 0}件（別ユーザーへ割り当てず保持）</Text>
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
        <Text variant="bodySmall" selectable>
          退避ファイル: {legacy.captureFile}
        </Text>
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

const styles = StyleSheet.create({
  root: { gap: 6 },
});
