import React from "react";
import { StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { Button, IconButton, Surface, Text } from "react-native-paper";
import { useAuth } from "../../../contexts/AuthContext";

export default function SettingsProfileScreen() {
  const router = useRouter();
  const { user, logout, isAuthenticated } = useAuth();

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <Text variant="titleLarge" style={styles.headerTitle}>
            Account details
          </Text>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>
          {user?.username || "Anonymous"}
        </Text>
        <Text style={styles.cardDescription}>
          {isAuthenticated
            ? "サーバー同期・サーバー側モデル・ユーザー設定が利用できます。"
            : "匿名モードです。会話や設定はこの端末のローカルデータとして扱われます。"}
        </Text>
        <View style={styles.detailRow}>
          <Text style={styles.label}>Mode</Text>
          <Text style={styles.value}>
            {isAuthenticated ? "Server login" : "Anonymous"}
          </Text>
        </View>
        <View style={styles.detailRow}>
          <Text style={styles.label}>Role</Text>
          <Text style={styles.value}>{user?.role || "-"}</Text>
        </View>
        <View style={styles.detailRow}>
          <Text style={styles.label}>User ID</Text>
          <Text style={styles.value} numberOfLines={2}>
            {user?.user_id || "-"}
          </Text>
        </View>
      </Surface>

      <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Session</Text>
        <Text style={styles.cardDescription}>
          ログアウトしても端末内のローカルデータは削除しません。サーバー連携だけを切断します。
        </Text>
        {isAuthenticated ? (
          <Button
            mode="outlined"
            textColor="#f38ba8"
            style={styles.dangerButton}
            onPress={async () => {
              await logout();
              router.replace("/(tabs)/chat");
            }}
          >
            Log out from server
          </Button>
        ) : (
          <Button
            mode="contained-tonal"
            buttonColor="#313244"
            textColor="#89b4fa"
            onPress={() => router.push("/(auth)/login")}
          >
            サーバーにログイン
          </Button>
        )}
      </Surface>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b", paddingBottom: 24 },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  card: {
    backgroundColor: "#1e1e2e",
    borderColor: "#313244",
    borderWidth: 1,
    borderRadius: 14,
    padding: 16,
    marginHorizontal: 16,
    marginTop: 16,
  },
  cardTitle: { color: "#cdd6f4", fontSize: 18, fontWeight: "bold" },
  cardDescription: {
    color: "#a6adc8",
    fontSize: 13,
    lineHeight: 19,
    marginTop: 6,
    marginBottom: 12,
  },
  detailRow: {
    borderTopWidth: 1,
    borderTopColor: "#313244",
    paddingTop: 12,
    marginTop: 12,
  },
  label: { color: "#a6adc8", fontSize: 12, marginBottom: 4 },
  value: { color: "#cdd6f4", fontSize: 14 },
  dangerButton: { borderColor: "#f38ba8" },
});
