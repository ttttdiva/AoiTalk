import { Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useState } from "react";
import { useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { Button, Surface, Text } from "react-native-paper";
import { ScreenHeader } from "../../../components/screen-header";
import { useAuth } from "../../../contexts/AuthContext";
import {
  AUTH_RECOVERY_DIRTY_DOCS_TABLES,
  readAuthRecoveryInventory,
  type AuthRecoveryInventory,
} from "../../../lib/auth-recovery";

function formatRecoveryCount(value: number | null): string {
  return value === null ? "不明" : String(value);
}

function recoveryCategoryLabel(
  category: AuthRecoveryInventory["outbox"],
): string {
  if (!category.readable || category.pendingCount === null) return "読み取り不明";
  if (category.hasUnknownOwner) {
    return `${category.pendingCount}件（所有者未確認あり）`;
  }
  return `${category.pendingCount}件`;
}

export default function SettingsProfileScreen() {
  const router = useRouter();
  const [showAuthDetails, setShowAuthDetails] = useState(false);
  const [recoveryInventory, setRecoveryInventory] =
    useState<AuthRecoveryInventory | null>(null);
  const [recoveryReadAt, setRecoveryReadAt] = useState<string | null>(null);
  const [recoveryReading, setRecoveryReading] = useState(false);
  const {
    user,
    logout,
    isAuthenticated,
    isAnonymous,
    reauthRequired,
    reauthIdentityUnknown,
    authState,
    actualAuthMode,
    tokenPresent,
  } = useAuth();
  const isReauthRequired =
    reauthRequired
    || authState === "reauth_required"
    || authState === "reauth_required_unknown";
  const isUnknownReauth =
    reauthIdentityUnknown || authState === "reauth_required_unknown";
  const isAnonymousMode = authState
    ? authState === "anonymous"
    : isAnonymous || (!isAuthenticated && !isReauthRequired);

  const inspectRecoveryInventory = async () => {
    if (recoveryReading) return;
    setRecoveryReading(true);
    try {
      const inventory = await readAuthRecoveryInventory();
      setRecoveryInventory(inventory);
      setRecoveryReadAt(new Date().toISOString());
    } finally {
      setRecoveryReading(false);
    }
  };

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Account details"
        onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
      />

      <ScrollView
        testID="account-details-scroll"
        style={styles.scroll}
        contentContainerStyle={styles.scrollContent}
        keyboardShouldPersistTaps="handled"
      >
        <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>
          {user?.username || (isReauthRequired ? "Session recovery needed" : "Anonymous")}
        </Text>
        <Text style={styles.cardDescription}>
          {isUnknownReauth
            ? "前回のサーバーアカウントを確認できません。元のユーザーで再ログインしてください。未送信データの所有者を確認できない間は、別ユーザーへ切り替えません。"
            : isReauthRequired
              ? "サーバーの認証の有効期限が切れました。同じユーザーで再ログインすると、未送信の変更を保持したまま同期を再開できます。"
            : isAuthenticated
              ? "サーバー同期・サーバー側モデル・ユーザー設定が利用できます。"
              : "匿名モードです。会話や設定はこの端末のローカルデータとして扱われます。"}
        </Text>
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ expanded: showAuthDetails }}
          onPress={() => setShowAuthDetails((value) => !value)}
          style={styles.detailsToggle}
        >
          <Text style={styles.detailsToggleText}>
            認証の詳細 {showAuthDetails ? "▲" : "▼"}
          </Text>
        </Pressable>
        {showAuthDetails ? (
          <View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>モード</Text>
              <Text style={styles.value}>
                {isUnknownReauth
                  ? "再ログインが必要（前回アカウント不明）"
                  : isReauthRequired
                    ? "再ログインが必要"
                    : isAuthenticated
                      ? "サーバーログイン"
                      : isAnonymousMode
                        ? "匿名モード"
                        : "サインアウト"}
              </Text>
            </View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>認証状態</Text>
              <Text style={styles.value}>
                {isUnknownReauth
                  ? "前回アカウント不明"
                  : isReauthRequired
                    ? "認証の有効期限切れ"
                    : isAuthenticated
                      ? "認証済み"
                      : "匿名ローカルモード"}
              </Text>
            </View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>資格情報</Text>
              <Text style={styles.value}>
                {tokenPresent ? "保存済み（値は非表示）" : "保存なし"}
              </Text>
            </View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>実際の認証モード</Text>
              <Text style={styles.value}>
                {actualAuthMode === "authenticated" ? "認証済みアカウント" : "匿名"}
              </Text>
            </View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>権限</Text>
              <Text style={styles.value}>{user?.role || "-"}</Text>
            </View>
            <View style={styles.detailRow}>
              <Text style={styles.label}>ユーザーID</Text>
              <Text style={styles.value} numberOfLines={2}>
                {user?.user_id || "-"}
              </Text>
            </View>
          </View>
        ) : null}
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>端末ローカルの保存データ診断</Text>
          <Text style={styles.cardDescription}>
            端末ローカルに残る未送信データを、端末全体の読み取り専用件数として確認できます。確認はこのボタンを押した時だけ行われます。
          </Text>
          <Button
            mode="outlined"
            loading={recoveryReading}
            disabled={recoveryReading}
            onPress={() => void inspectRecoveryInventory()}
            style={styles.recoveryButton}
          >
            未送信データを確認
          </Button>
          {recoveryInventory ? (
            <View style={styles.recoverySummary}>
              <Text style={styles.recoverySummaryTitle}>端末内の保存データ診断</Text>
              <Text style={styles.recoverySummaryText}>
                確認時刻: {recoveryReadAt ?? "-"}
              </Text>
              <Text style={styles.recoverySummaryText}>
                Outbox: {recoveryCategoryLabel(recoveryInventory.outbox)}
              </Text>
              <Text style={styles.recoverySummaryText}>
                保存済みComposer下書き: {recoveryCategoryLabel(recoveryInventory.composerDrafts)}
              </Text>
              <Text style={styles.recoverySummaryText}>Docs dirty:</Text>
              {AUTH_RECOVERY_DIRTY_DOCS_TABLES.map((tableName) => (
                <Text key={tableName} style={styles.recoverySummaryText}>
                  {tableName}: {formatRecoveryCount(recoveryInventory.dirtyDocs.tableCounts[tableName])}
                </Text>
              ))}
              <Text style={styles.recoverySummaryText}>
                Docs dirty 合計: {recoveryCategoryLabel(recoveryInventory.dirtyDocs)}
              </Text>
              <Text style={styles.recoverySummaryNote}>
                件数は端末全体の読み取り専用診断です。現在アカウント別の件数ではありません。
                所有者未確認や読み取り不明は0件として扱っていません。
              </Text>
              <Text style={styles.recoverySummaryNote}>
                メモリ上だけの未保存Docs下書きは対象外です。
              </Text>
            </View>
          ) : null}
        </Surface>

        <Surface style={styles.card} elevation={0}>
        <Text style={styles.cardTitle}>Session</Text>
        <Text style={styles.cardDescription}>
          ログアウトすると未同期のローカル同期キャッシュは破棄されます。サーバー上のデータは削除されません。
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
            {isUnknownReauth
              ? "元のユーザーで再ログイン"
              : isReauthRequired
                ? "再ログイン"
                : "サーバーにログイン"}
          </Button>
        )}
        </Surface>
      </ScrollView>

    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b", paddingBottom: 24 },
  scroll: { flex: 1 },
  scrollContent: { paddingBottom: 32 },
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
  recoveryButton: { marginTop: 12 },
  recoverySummary: {
    backgroundColor: "#181825",
    borderRadius: 8,
    marginTop: 10,
    padding: 10,
  },
  recoverySummaryTitle: { color: "#cdd6f4", fontSize: 13, fontWeight: "600" },
  recoverySummaryText: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  recoverySummaryNote: { color: "#7f849c", fontSize: 11, lineHeight: 17, marginTop: 6 },
  detailsToggle: {
    borderTopWidth: 1,
    borderTopColor: "#313244",
    marginTop: 12,
    paddingTop: 12,
  },
  detailsToggleText: { color: "#89b4fa", fontSize: 13, fontWeight: "600" },
  dangerButton: { borderColor: "#f38ba8" },
});
