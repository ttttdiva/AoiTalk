import React, { useCallback, useMemo, useRef, useState } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { List, Searchbar, Text } from "react-native-paper";
import { ScreenHeader } from "../../../components/screen-header";
import { OutboxDiagnostics } from "../../../components/outbox-diagnostics";
import { OutboxResolutionHistory } from "../../../components/outbox-resolution-history";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { filterMobileSettingsItems, MOBILE_SETTINGS_CATEGORIES } from "../../../lib/mobile-settings-registry";
import { useSyncStatusStore } from "../../../stores/sync-status";
import {
  outboxResolutionNamespaceKey,
  useOutboxResolutionIdentity,
} from "../../../components/use-outbox-resolution-identity";
import { isMobileReadOnlyDiagnosticsEnabled } from "../../../lib/read-only-diagnostics";

export default function SettingsScreen() {
  const router = useRouter();
  const { user, isAuthenticated, isAnonymous } = useAuth();
  const currentAuthScope = useSyncStatusStore((value) => value.authScope);
  const outboxState = useSyncStatusStore((value) => value.outboxState);
  const readOnlyDiagnostics = isMobileReadOnlyDiagnosticsEnabled();
  const identityHint = typeof user?.user_id === "string" ? user.user_id : "";
  const [focusGeneration, setFocusGeneration] = useState(0);
  const [isFocused, setIsFocused] = useState(false);
  const { identity: resolutionIdentity } = useOutboxResolutionIdentity(
    isAuthenticated && !isAnonymous,
    `${identityHint}|focus:${focusGeneration}`,
    { active: isFocused },
  );
  const lastIdentityRef = useRef<typeof resolutionIdentity>(null);
  if (resolutionIdentity) lastIdentityRef.current = resolutionIdentity;
  const [query, setQuery] = useState("");
  const [showResolutionHistory, setShowResolutionHistory] = useState(false);
  const [historyRequest, setHistoryRequest] = useState(0);
  useFocusEffect(useCallback(() => {
    setIsFocused(true);
    setFocusGeneration((value) => value + 1);
    return () => {
      // Settings can remain mounted while a child route is focused. Drop all
      // resolution children on blur so a later return cannot reveal an old
      // server snapshot or leave an adoption confirmation actionable.
      setFocusGeneration((value) => value + 1);
      setIsFocused(false);
      setShowResolutionHistory(false);
      setHistoryRequest(0);
    };
  }, []));
  const stableIdentity = resolutionIdentity ?? lastIdentityRef.current;
  const ownAuthScope = stableIdentity?.authScope ?? null;
  const identityReady = Boolean(resolutionIdentity);
  const showOutboxDiagnostics = Boolean(
    identityReady && ownAuthScope && (readOnlyDiagnostics
      || (currentAuthScope === ownAuthScope && outboxState !== "empty")),
  );
  const showResolutionHistoryEntry = Boolean(
    !readOnlyDiagnostics && isFocused && identityReady && ownAuthScope,
  );
  const shouldMountHistory = Boolean(stableIdentity && ownAuthScope);

  const items = useMemo(
    () => filterMobileSettingsItems(query, {
      isAuthenticated,
      isAnonymous,
      isAdmin: user?.role === "admin",
    }),
    [isAnonymous, isAuthenticated, query, user?.role],
  );

  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={<ScreenHeader title="Settings" showSettings={false} />}
    >
      <View style={styles.searchWrap}>
        <Searchbar
          accessibilityLabel="設定を検索"
          placeholder="設定を検索"
          value={query}
          onChangeText={setQuery}
          style={styles.search}
          inputStyle={styles.searchInput}
        />
      </View>

      {MOBILE_SETTINGS_CATEGORIES.map((category) => {
        const categoryItems = items.filter((item) => item.category === category.id);
        if (categoryItems.length === 0) return null;
        return (
          <View key={category.id} style={styles.section}>
            <Text style={styles.sectionTitle}>{category.label}</Text>
            <View style={styles.rows}>
              {categoryItems.map((item, index) => (
                <List.Item
                  key={item.id}
                  title={item.label}
                  titleStyle={styles.itemTitle}
                  style={[styles.row, index < categoryItems.length - 1 ? styles.rowBorder : null]}
                  left={(props) => <List.Icon {...props} icon={item.icon} color="#a78bfa" />}
                  right={(props) => <List.Icon {...props} icon="chevron-right" color="#777294" />}
                  onPress={() => router.push(item.route as never)}
                />
              ))}
            </View>
          </View>
        );
      })}

      {showOutboxDiagnostics ? (
        <View style={styles.outboxDiagnostics} testID="sync-outbox-diagnostics">
          <Text style={styles.itemTitle}>送信待ちの詳細</Text>
          <OutboxDiagnostics
            key={`outbox-diagnostics-${focusGeneration}`}
            resolutionIdentity={resolutionIdentity}
          />
        </View>
      ) : null}

      {shouldMountHistory && !readOnlyDiagnostics ? (
        <View
          style={showResolutionHistoryEntry ? styles.resolutionHistory : styles.historyHidden}
          testID="sync-outbox-resolution-history"
        >
          <Pressable
            accessibilityRole="button"
            accessibilityState={{ expanded: showResolutionHistory }}
            style={styles.historyButton}
            onPress={() => {
              setShowResolutionHistory((value) => {
                if (!value) setHistoryRequest((request) => request + 1);
                return !value;
              });
            }}
          >
            <Text style={styles.historyButtonText}>保存した原値と下書きを確認</Text>
          </Pressable>
          {ownAuthScope ? (
            <View style={showResolutionHistory ? styles.historyPanelVisible : styles.historyPanelHidden}>
              <OutboxResolutionHistory
                authScope={ownAuthScope}
                loadRequest={historyRequest}
                active={showResolutionHistoryEntry && showResolutionHistory}
                namespaceKey={stableIdentity ? outboxResolutionNamespaceKey(stableIdentity) : undefined}
              />
            </View>
          ) : null}
        </View>
      ) : null}

      {items.length === 0 ? (
        <View style={styles.empty}>
          <Text style={styles.emptyTitle}>該当する設定はありません</Text>
          <Text style={styles.emptyText}>別のキーワードで検索してください。</Text>
        </View>
      ) : null}
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#10101b" },
  content: { paddingBottom: 32 },
  searchWrap: { paddingHorizontal: 16, paddingTop: 10, paddingBottom: 4 },
  search: { backgroundColor: "#1b1a2b", borderWidth: 1, borderColor: "#343149", minHeight: 44 },
  searchInput: { color: "#f0efff", fontSize: 14, minHeight: 44 },
  section: { marginTop: 18 },
  sectionTitle: { color: "#8f8aa8", fontSize: 12, fontWeight: "600", marginHorizontal: 22, marginBottom: 5 },
  outboxDiagnostics: { marginHorizontal: 12, marginTop: 18, padding: 16, borderRadius: 12, backgroundColor: "#1b1a2b" },
  resolutionHistory: { marginHorizontal: 12, marginTop: 12, padding: 16, borderRadius: 12, backgroundColor: "#1b1a2b" },
  historyHidden: { display: "none" },
  historyButton: { minHeight: 44, justifyContent: "center", borderWidth: 1, borderColor: "#777294", borderRadius: 8, paddingHorizontal: 12 },
  historyButtonText: { color: "#f0efff", fontSize: 14, fontWeight: "500" },
  historyPanelVisible: { display: "flex" },
  historyPanelHidden: { display: "none" },
  rows: { marginHorizontal: 12, overflow: "hidden", borderRadius: 12 },
  row: { minHeight: 52, paddingHorizontal: 4, paddingVertical: 1, backgroundColor: "transparent" },
  rowBorder: { borderBottomWidth: 1, borderBottomColor: "#26243a" },
  itemTitle: { color: "#f0efff", fontSize: 14, fontWeight: "500" },
  empty: { alignItems: "center", paddingHorizontal: 24, paddingVertical: 40 },
  emptyTitle: { color: "#cdd6f4", fontSize: 15, fontWeight: "600" },
  emptyText: { color: "#8f8aa8", fontSize: 13, marginTop: 6 },
});
