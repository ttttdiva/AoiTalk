import React, { useCallback, useEffect, useMemo, useState } from "react";
import { StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import * as Clipboard from "expo-clipboard";
import { Text } from "react-native-paper";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { LegacyMigrationPanel } from "../../../features/sync-v3/LegacyMigrationPanel";
import { SyncStatusBadge } from "../../../features/sync-v3/SyncStatusBadge";
import { SyncStatusPanel } from "../../../features/sync-v3/SyncStatusPanel";
import { SyncV3OwnerMetadataPanel } from "../../../features/sync-v3/SyncV3OwnerMetadataPanel";
import { useOutboxResolutionIdentity } from "../../../components/use-outbox-resolution-identity";
import { useOwnerSyncDiagnostics } from "../../../features/sync-v3/use-owner-sync-diagnostics";
import { syncV3ProductEngine } from "../../../sync-v3/engine-control";
import type { SyncV3Status } from "../../../sync-v3/contract";

export default function SyncStatusSettingsScreen() {
  const router = useRouter();
  const { isAuthenticated, user } = useAuth();
  const authScope = isAuthenticated && user?.user_id ? `auth:${user.user_id}` : null;
  const [status, setStatus] = useState<SyncV3Status>(() => syncV3ProductEngine.getStatus());
  const [isFocused, setIsFocused] = useState(false);
  useEffect(() => syncV3ProductEngine.subscribe(setStatus), []);
  useFocusEffect(useCallback(() => {
    setIsFocused(true);
    return () => setIsFocused(false);
  }, []));
  const { identity } = useOutboxResolutionIdentity(Boolean(authScope), authScope ?? "", {
    active: isFocused,
    pollMs: 1000,
  });
  const currentIdentity = identity?.authScope === authScope ? identity : null;
  const ownerMetadata = useOwnerSyncDiagnostics(currentIdentity, isFocused);
  const scopes = useMemo(() => (status.scopes ?? []).map((scope) => ({
    scopeKey: scope.scopeKey,
    label: scope.scopeKey,
  })), [status.scopes]);
  const copy = (value: string) => { void Clipboard.setStringAsync(value); };

  return (
    <ScreenShell scroll style={styles.root} contentContainerStyle={styles.content}>
      {!authScope ? <Text variant="bodyMedium">ログイン後に同期状態を表示します。</Text> : null}
      <View style={styles.badge}>
        <SyncStatusBadge onPress={() => router.push("/(tabs)/settings/sync-conflicts" as never)} />
      </View>
      <SyncV3OwnerMetadataPanel metadata={ownerMetadata} />
      <LegacyMigrationPanel onCopy={copy} />
      <SyncStatusPanel
        scopes={scopes}
        onCopy={copy}
        onOpenConflicts={() => router.push("/(tabs)/settings/sync-conflicts" as never)}
      />
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#10101b" },
  content: { padding: 16, gap: 14, paddingBottom: 32 },
  badge: { alignSelf: "flex-start" },
});
