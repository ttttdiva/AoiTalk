/**
 * Docs 一覧画面
 *
 * トップレベルページ（parent_id null / archived_at null）をフラットリスト表示する。
 * 各行タップで詳細（アウトライン編集）へ。FAB で新規ページ作成、ヘッダから検索とデイリーページ。
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  AppState,
  FlatList,
  RefreshControl,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Divider,
  FAB,
  IconButton,
  List,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useRouter } from "expo-router";
import { ScreenHeader } from "../../../components/screen-header";
import { RichTitle } from "../../../components/docs/rich-title";
import { ClipIngestDialog } from "../../../components/docs/clip-ingest-dialog";
import { docsRepo } from "../../../repositories/docs";
import { docsApi } from "../../../lib/docs-api";
import { useNetworkStore } from "../../../stores/network";
import { runSync } from "../../../sync/engine";
import type { DocsNode } from "../../../types/api";

type SearchResult = { id: string; title: string; subtitle?: string };

export default function DocsListScreen() {
  const router = useRouter();
  const online = useNetworkStore((state) => state.online);
  const [pages, setPages] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [searchVisible, setSearchVisible] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [busyToday, setBusyToday] = useState(false);
  const [clipIngestVisible, setClipIngestVisible] = useState(false);
  const focusedRef = useRef(false);
  const loadRequestRef = useRef(0);
  const revalidateFlightRef = useRef<Promise<void> | null>(null);

  const loadPages = useCallback(async () => {
    const requestId = ++loadRequestRef.current;
    try {
      const nextPages = await docsRepo.listPages();
      if (requestId === loadRequestRef.current) setPages(nextPages);
    } catch {
      if (requestId === loadRequestRef.current) setPages([]);
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false);
    }
  }, []);

  const revalidatePages = useCallback((): Promise<void> => {
    if (!online) return loadPages();
    if (revalidateFlightRef.current) return revalidateFlightRef.current;

    const flight = runSync()
      .then(loadPages)
      .catch(() => {
        // ローカル先読みは維持する。次のfocus/foreground/手動更新で再試行する。
      })
      .finally(() => {
        if (revalidateFlightRef.current === flight) {
          revalidateFlightRef.current = null;
        }
      });
    revalidateFlightRef.current = flight;
    return flight;
  }, [loadPages, online]);

  useFocusEffect(
    useCallback(() => {
      focusedRef.current = true;
      void loadPages();
      void revalidatePages();
      return () => {
        focusedRef.current = false;
        loadRequestRef.current += 1;
      };
    }, [loadPages, revalidatePages]),
  );

  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && focusedRef.current) {
        void revalidatePages();
      }
    });
    return () => subscription.remove();
  }, [revalidatePages]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await revalidatePages();
    } finally {
      setRefreshing(false);
    }
  };

  const openNode = useCallback(
    (nodeId: string) => {
      router.push(`/(tabs)/docs/${nodeId}`);
    },
    [router],
  );

  const runSearch = useCallback(
    async (value: string) => {
      const q = value.trim();
      if (!q) {
        setResults([]);
        return;
      }
      try {
        if (online) {
          const hits = await docsApi.search(q);
          setResults(
            hits.map((hit) => ({
              id: hit.id,
              title: hit.title,
              subtitle: hit.parent_title ?? undefined,
            })),
          );
        } else {
          const rows = await docsRepo.searchLocal(q);
          setResults(
            rows.map((row) => ({
              id: row.id,
              title: row.title,
              subtitle: row.description ?? undefined,
            })),
          );
        }
      } catch {
        // オフライン等の失敗時はローカル検索へフォールバック
        try {
          const rows = await docsRepo.searchLocal(q);
          setResults(
            rows.map((row) => ({ id: row.id, title: row.title })),
          );
        } catch {
          setResults([]);
        }
      }
    },
    [online],
  );

  const handleQueryChange = (value: string) => {
    setQuery(value);
    void runSearch(value);
  };

  const createPage = async () => {
    try {
      const node = await docsRepo.createNode({ title: "" });
      router.push({
        pathname: "/(tabs)/docs/[nodeId]",
        params: { nodeId: node.id, created: "1" },
      });
    } catch {
      // 作成失敗時は何もしない
    }
  };

  const openToday = async () => {
    if (busyToday) return;
    setBusyToday(true);
    try {
      const result = await docsApi.today();
      openNode(result.node.id);
    } catch {
      // オフライン時はローカルキャッシュ済みの Day ノードへフォールバックする。
      // 新規作成はサーバ ensure が重複排除を担うためオフラインでは行わない。
      const now = new Date();
      const today = [
        now.getFullYear(),
        String(now.getMonth() + 1).padStart(2, "0"),
        String(now.getDate()).padStart(2, "0"),
      ].join("-");
      const cached = await docsRepo.findDayNode(today).catch(() => null);
      if (cached) {
        openNode(cached.id);
      } else {
        Alert.alert(
          "デイリーページ",
          "オフラインのため今日のデイリーページを作成できません。接続後に再度お試しください。",
        );
      }
    } finally {
      setBusyToday(false);
    }
  };

  const searching = searchVisible && query.trim().length > 0;

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Docs"
        right={
          <>
            <IconButton
              icon="tray-arrow-down"
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              accessibilityLabel="クリップ取り込み"
              onPress={() => setClipIngestVisible(true)}
            />
            <IconButton
              icon={searchVisible ? "magnify-close" : "magnify"}
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              onPress={() => {
                setSearchVisible((prev) => !prev);
                if (searchVisible) {
                  setQuery("");
                  setResults([]);
                }
              }}
            />
            <IconButton
              icon="calendar-today"
              size={22}
              iconColor="#a6adc8"
              style={styles.headerIcon}
              disabled={busyToday}
              onPress={() => void openToday()}
            />
          </>
        }
      />

      {searchVisible ? (
        <View style={styles.searchRow}>
          <TextInput
            value={query}
            onChangeText={handleQueryChange}
            mode="outlined"
            dense
            placeholder="Docs を検索"
            autoCorrect={false}
            left={<TextInput.Icon icon="magnify" />}
            right={
              query ? (
                <TextInput.Icon icon="close" onPress={() => handleQueryChange("")} />
              ) : undefined
            }
          />
        </View>
      ) : null}

      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#7c3aed" />
        </View>
      ) : searching ? (
        <FlatList
          data={results}
          keyExtractor={(item) => item.id}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          renderItem={({ item }) => (
            <List.Item
              title={() => (
                <RichTitle text={item.title || "無題"} numberOfLines={1} />
              )}
              description={item.subtitle}
              descriptionStyle={styles.itemDesc}
              left={(props) => (
                <List.Icon {...props} icon="text-search" color="#7c3aed" />
              )}
              onPress={() => openNode(item.id)}
              style={styles.listItem}
            />
          )}
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={styles.emptyText}>該当するページがありません</Text>
            </View>
          }
        />
      ) : (
        <FlatList
          data={pages}
          keyExtractor={(item) => item.id}
          ItemSeparatorComponent={() => <Divider style={styles.divider} />}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor="#7c3aed"
            />
          }
          renderItem={({ item }) => (
            <List.Item
              title={() => (
                <RichTitle text={item.title || "無題のページ"} numberOfLines={1} />
              )}
              description={item.description ?? undefined}
              descriptionStyle={styles.itemDesc}
              descriptionNumberOfLines={1}
              left={(props) => (
                <List.Icon {...props} icon="file-document-outline" color="#7c3aed" />
              )}
              onPress={() => openNode(item.id)}
              style={styles.listItem}
            />
          )}
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={styles.emptyText}>ページがありません</Text>
              <Text style={styles.emptySubtext}>
                右下の + から新規ページを作成できます
              </Text>
            </View>
          }
          contentContainerStyle={
            pages.length === 0 ? styles.emptyContainer : undefined
          }
        />
      )}

      <FAB
        icon="plus"
        style={styles.fab}
        onPress={() => void createPage()}
        color="#cdd6f4"
      />
      <ClipIngestDialog
        visible={clipIngestVisible}
        onDismiss={() => setClipIngestVisible(false)}
        onOpenNode={(nodeId) => {
          setClipIngestVisible(false);
          openNode(nodeId);
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  headerIcon: { margin: 0 },
  searchRow: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    backgroundColor: "#181825",
  },
  center: { flex: 1, alignItems: "center", justifyContent: "center", paddingTop: 60 },
  emptyContainer: { flexGrow: 1 },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  emptySubtext: { color: "#585b70", fontSize: 13, marginTop: 4 },
  listItem: { backgroundColor: "#11111b", paddingVertical: 4 },
  itemDesc: { color: "#a6adc8", fontSize: 12 },
  divider: { backgroundColor: "#313244" },
  fab: {
    position: "absolute",
    right: 16,
    bottom: 16,
    backgroundColor: "#7c3aed",
  },
});
