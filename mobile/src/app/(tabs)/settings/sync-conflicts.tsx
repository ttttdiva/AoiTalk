import React from "react";
import { StyleSheet } from "react-native";
import * as Clipboard from "expo-clipboard";
import { Text } from "react-native-paper";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { SyncConflictsScreen } from "../../../features/sync-v3/SyncConflictsScreen";

export default function SyncConflictsSettingsScreen() {
  const { isAuthenticated, user } = useAuth();
  const authScope = isAuthenticated && user?.user_id ? `auth:${user.user_id}` : null;
  return (
    <ScreenShell scroll style={styles.root} contentContainerStyle={styles.content}>
      {authScope ? (
        <SyncConflictsScreen authScope={authScope} onCopy={(value) => { void Clipboard.setStringAsync(value); }} />
      ) : (
        <Text variant="bodyMedium">ログイン後に競合項目を表示します。</Text>
      )}
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#10101b" },
  content: { padding: 16, gap: 14, paddingBottom: 32 },
});
