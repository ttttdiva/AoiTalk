import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  FlatList,
  RefreshControl,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  Divider,
  FAB,
  IconButton,
  List,
  Portal,
  Snackbar,
  Text,
  TextInput,
} from "react-native-paper";
import * as Clipboard from "expo-clipboard";
import { useRouter, useFocusEffect } from "expo-router";
import { format } from "date-fns";
import { useProject } from "../../../contexts/ProjectContext";
import { conversationsRepo, uploadLocalSession } from "../../../repositories";
import { chatRepo } from "../../../repositories/chat";
import { runSync } from "../../../sync/engine";
import { scheduleSyncAfterInteractions } from "../../../lib/background-sync";
import { createCurrentCharacterSession } from "../../../features/characters/current-character";
import {
  animateNextListChange,
  areConversationListsEqual,
} from "../../../features/ui/list-transitions";
import { useReducedMotion } from "../../../features/ui/use-reduced-motion";
import { ScreenHeader } from "../../../components/screen-header";
import type { ConversationSession } from "../../../types/api";
import type { ConversationSearchResult } from "../../../lib/chat-api";

export default function ChatListScreen() {
  const router = useRouter();
  const { selectedProjectId } = useProject();
  const reduceMotion = useReducedMotion();
  const reduceMotionRef = useRef(reduceMotion);
  reduceMotionRef.current = reduceMotion;
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [syncTarget, setSyncTarget] = useState<ConversationSession | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [actionTarget, setActionTarget] =
    useState<ConversationSession | null>(null);
  const [renameTarget, setRenameTarget] =
    useState<ConversationSession | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [copiedNotice, setCopiedNotice] = useState(false);
  const [searchVisible, setSearchVisible] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<ConversationSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const creatingChatRef = useRef(false);
  const [creatingChat, setCreatingChat] = useState(false);
  const [createChatError, setCreateChatError] = useState<string | null>(null);
  const loadRequestRef = useRef(0);
  const syncTaskRef = useRef<(() => void) | null>(null);
  const longPressedSessionIdRef = useRef<string | null>(null);

  const loadSessions = useCallback(
    async (options?: { forceRefresh?: boolean }) => {
      const requestId = ++loadRequestRef.current;
      syncTaskRef.current?.();
      syncTaskRef.current = null;

      const applyList = (list: ConversationSession[]) => {
        if (requestId !== loadRequestRef.current) return;
        setSessions((previous) => {
          if (areConversationListsEqual(previous, list)) return previous;
          animateNextListChange(reduceMotionRef.current);
          return list;
        });
      };

      if (options?.forceRefresh) {
        try {
          await runSync();
          const list = await conversationsRepo.listSessions(
            undefined,
            options,
          );
          applyList(list);
        } catch {
          // Keep the current list visible if an explicit refresh fails.
        }
        return;
      }

      try {
        applyList(await conversationsRepo.listSessionsLocal());
      } catch {
        // A sync can still repair a failed/stale local read.
      }
      if (requestId !== loadRequestRef.current) return;

      if (requestId !== loadRequestRef.current) return;
      syncTaskRef.current = scheduleSyncAfterInteractions(async () => {
        if (requestId !== loadRequestRef.current) return;
        try {
          applyList(await conversationsRepo.listSessionsLocal());
        } catch {
          // 同期後の再読込失敗でも、先に表示したローカル一覧を維持する。
        }
      });
    },
    [],
  );

  useFocusEffect(
    useCallback(() => {
      void loadSessions();
      return () => {
        loadRequestRef.current += 1;
        syncTaskRef.current?.();
        syncTaskRef.current = null;
      };
    }, [loadSessions]),
  );

  const onRefresh = async () => {
    setRefreshing(true);
    // pull-to-refresh は明示操作なのでフル取得する。
    await loadSessions({ forceRefresh: true });
    setRefreshing(false);
  };

  const handleSync = async () => {
    if (!syncTarget) return;
    setSyncing(true);
    setSyncError(null);
    try {
      await uploadLocalSession(syncTarget.id);
      setSyncTarget(null);
      // 同期直後はサーバーの最新（message_count 等）を反映するためフル取得する。
      await loadSessions({ forceRefresh: true });
    } catch (error) {
      setSyncError(
        error instanceof Error ? error.message : "サーバー同期に失敗しました。",
      );
    } finally {
      setSyncing(false);
    }
  };

  const createRegularChat = useCallback(async () => {
    if (creatingChatRef.current) return;

    creatingChatRef.current = true;
    setCreatingChat(true);
    setCreateChatError(null);

    try {
      const session = await createCurrentCharacterSession(selectedProjectId, {
        localFirst: true,
      });

      setSessions((previous) => [
        session,
        ...previous.filter((item) => item.id !== session.id),
      ]);

      router.push({
        pathname: "/(tabs)/chat/[sessionId]",
        params: {
          sessionId: session.id,
          ...(selectedProjectId ? { projectId: selectedProjectId } : {}),
        },
      });
    } catch (error) {
      setCreateChatError(
        error instanceof Error
          ? error.message
          : "チャットを開始できませんでした。",
      );
    } finally {
      creatingChatRef.current = false;
      setCreatingChat(false);
    }
  }, [router, selectedProjectId]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await conversationsRepo.deleteSession(deleteTarget);
      setSessions((prev) =>
        prev.filter((session) => session.id !== deleteTarget),
      );
    } catch {
      // ignore
    }
    setDeleteTarget(null);
  };

  const handleCopySessionId = async () => {
    if (!actionTarget) return;
    await Clipboard.setStringAsync(actionTarget.id);
    setActionTarget(null);
    setCopiedNotice(true);
  };

  const openRename = () => {
    if (!actionTarget) return;
    setRenameTarget(actionTarget);
    setRenameDraft(actionTarget.title || "");
    setRenameError(null);
    setActionTarget(null);
  };

  const handleRename = async () => {
    if (!renameTarget) return;
    const title = renameDraft.trim();
    if (!title) {
      setRenameError("タイトルを入力してください。");
      return;
    }
    setRenaming(true);
    setRenameError(null);
    try {
      await conversationsRepo.updateTitle(renameTarget.id, title, {
        requireServerSuccess: true,
      });
      setSessions((previous) =>
        previous.map((session) =>
          session.id === renameTarget.id ? { ...session, title } : session,
        ),
      );
      setRenameTarget(null);
    } catch (error) {
      setRenameError(
        error instanceof Error ? error.message : "タイトルの更新に失敗しました。",
      );
    } finally {
      setRenaming(false);
    }
  };

  useEffect(() => {
    const query = searchQuery.trim();
    if (!searchVisible || !query) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = setTimeout(() => {
      void chatRepo
        .search(query, selectedProjectId, 50)
        .then((results) => {
          if (!cancelled) setSearchResults(results.slice(0, 50));
        })
        .catch(() => {
          if (!cancelled) setSearchResults([]);
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [searchQuery, searchVisible, selectedProjectId]);

  const renderSearchItem = ({ item }: { item: ConversationSearchResult }) => (
    <List.Item
      title={item.title || "無題の会話"}
      description={`${item.snippet}${item.character_name ? ` · ${item.character_name}` : ""}`}
      left={(props) => <List.Icon {...props} icon="text-search" color="#89b4fa" />}
      onPress={() => {
        setSearchVisible(false);
        setSearchQuery("");
        router.push(`/(tabs)/chat/${item.session_id}`);
      }}
      style={styles.listItem}
    />
  );

  const showingSearch = searchVisible && Boolean(searchQuery.trim());
  const listData: Array<ConversationSession | ConversationSearchResult> =
    showingSearch ? searchResults : sessions;

  const renderItem = ({ item }: { item: ConversationSession }) => {
    return (
      <List.Item
        title={item.title || "New conversation"}
        titleStyle={styles.sessionTitle}
        description={
          item.last_activity
            ? `${format(new Date(item.last_activity), "MM/dd HH:mm")} | ${item.message_count} messages`
            : `${item.message_count} messages`
        }
        descriptionStyle={styles.sessionDesc}
        left={(props) => <List.Icon {...props} icon="chat" color="#7c3aed" />}
        right={() => (
          <IconButton
            icon="delete-outline"
            iconColor="#f38ba8"
            size={20}
            onPress={() => setDeleteTarget(item.id)}
          />
        )}
        onPressIn={() => {
          longPressedSessionIdRef.current = null;
        }}
        onPress={() => {
          if (longPressedSessionIdRef.current === item.id) {
            longPressedSessionIdRef.current = null;
            return;
          }
          router.push(`/(tabs)/chat/${item.id}`);
        }}
        onLongPress={() => {
          longPressedSessionIdRef.current = item.id;
          setActionTarget(item);
        }}
        delayLongPress={280}
        style={styles.listItem}
      />
    );
  };

  return (
    <View style={styles.container}>
      <ScreenHeader
        title="Chat"
        right={
          <IconButton
            icon={searchVisible ? "magnify-close" : "magnify"}
            iconColor="#cdd6f4"
            accessibilityLabel="会話を検索"
            onPress={() => {
              setSearchVisible((visible) => !visible);
              if (searchVisible) setSearchQuery("");
            }}
          />
        }
      />
      {searchVisible ? (
        <TextInput
          value={searchQuery}
          onChangeText={setSearchQuery}
          mode="flat"
          dense
          autoFocus
          placeholder="会話を検索（最大50件）"
          style={styles.searchInput}
        />
      ) : null}

      <FlatList<ConversationSession | ConversationSearchResult>
        data={listData}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) =>
          "session_id" in item
            ? renderSearchItem({ item })
            : renderItem({ item })
        }
        ItemSeparatorComponent={() => <Divider style={styles.divider} />}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor="#7c3aed"
          />
        }
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyText}>
              {searching
                ? "検索中…"
                : searchVisible && searchQuery.trim()
                  ? "検索結果はありません。"
                  : "No conversations yet."}
            </Text>
            <Text style={styles.emptySubtext}>
              Create a session to start chatting.
            </Text>
          </View>
        }
        contentContainerStyle={
          listData.length === 0 ? styles.emptyContainer : undefined
        }
      />

      <FAB
        icon="plus"
        style={styles.fab}
        onPress={() => void createRegularChat()}
        disabled={creatingChat}
        loading={creatingChat}
        color="#cdd6f4"
        accessibilityLabel="新しいチャット"
      />

      <Portal>
        <Dialog
          visible={!!actionTarget}
          onDismiss={() => setActionTarget(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>セッション操作</Dialog.Title>
          <Dialog.Content style={styles.actionDialogContent}>
            <List.Item
              title="タイトルを編集"
              left={(props) => <List.Icon {...props} icon="pencil-outline" />}
              onPress={openRename}
            />
            <List.Item
              title="セッションIDをコピー"
              left={(props) => <List.Icon {...props} icon="content-copy" />}
              onPress={() => void handleCopySessionId()}
            />
            {actionTarget?.user_id === "" ? (
              <List.Item
                title="サーバーへ同期"
                left={(props) => <List.Icon {...props} icon="cloud-upload-outline" />}
                onPress={() => {
                  setSyncTarget(actionTarget);
                  setActionTarget(null);
                }}
              />
            ) : null}
            <List.Item
              title="削除"
              titleStyle={styles.dangerText}
              left={(props) => (
                <List.Icon {...props} icon="delete-outline" color="#f38ba8" />
              )}
              onPress={() => {
                setDeleteTarget(actionTarget?.id ?? null);
                setActionTarget(null);
              }}
            />
          </Dialog.Content>
        </Dialog>

        <Dialog
          visible={!!renameTarget}
          onDismiss={() => {
            if (!renaming) setRenameTarget(null);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            セッションタイトルを変更
          </Dialog.Title>
          <Dialog.Content>
            <TextInput
              value={renameDraft}
              onChangeText={setRenameDraft}
              mode="outlined"
              label="タイトル"
              maxLength={200}
              autoFocus
              disabled={renaming}
              onSubmitEditing={() => void handleRename()}
            />
            {renameError ? (
              <Text style={styles.renameError}>{renameError}</Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setRenameTarget(null)} disabled={renaming}>
              キャンセル
            </Button>
            <Button
              onPress={() => void handleRename()}
              loading={renaming}
              disabled={renaming || !renameDraft.trim()}
            >
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={!!syncTarget}
          onDismiss={() => {
            if (syncing) return;
            setSyncTarget(null);
            setSyncError(null);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>サーバーへ同期</Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>
              このローカルチャットをサーバーへ保存し、他の端末やログインでも
              参照できるようにします。よろしいですか？
            </Text>
            {syncing ? (
              <ActivityIndicator
                color="#7c3aed"
                style={styles.syncIndicator}
              />
            ) : null}
            {syncError ? (
              <Text style={styles.syncErrorText}>{syncError}</Text>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button
              onPress={() => {
                setSyncTarget(null);
                setSyncError(null);
              }}
              textColor="#a6adc8"
              disabled={syncing}
            >
              キャンセル
            </Button>
            <Button
              onPress={handleSync}
              textColor="#7c3aed"
              disabled={syncing}
            >
              同期
            </Button>
          </Dialog.Actions>
        </Dialog>

        <Dialog
          visible={!!deleteTarget}
          onDismiss={() => setDeleteTarget(null)}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            Delete conversation
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>
              This session will be removed from the chat list.
            </Text>
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => setDeleteTarget(null)} textColor="#a6adc8">
              Cancel
            </Button>
            <Button onPress={handleDelete} textColor="#f38ba8">
              Delete
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Snackbar
          visible={copiedNotice}
          onDismiss={() => setCopiedNotice(false)}
          duration={2200}
          style={styles.snackbar}
        >
          セッションIDをコピーしました
        </Snackbar>
        <Snackbar
          visible={!!createChatError}
          onDismiss={() => setCreateChatError(null)}
          duration={4000}
          style={styles.snackbar}
        >
          {createChatError}
        </Snackbar>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  searchInput: { backgroundColor: "#1e1e2e", marginHorizontal: 12, marginBottom: 6 },
  listItem: { backgroundColor: "#11111b", paddingVertical: 4 },
  sessionTitle: { color: "#cdd6f4" },
  sessionDesc: { color: "#a6adc8", fontSize: 12 },
  divider: { backgroundColor: "#313244" },
  fab: {
    position: "absolute",
    right: 16,
    bottom: 16,
    backgroundColor: "#7c3aed",
  },
  empty: { alignItems: "center", paddingTop: 60 },
  emptyContainer: { flexGrow: 1 },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  emptySubtext: { color: "#585b70", fontSize: 13, marginTop: 4 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogText: { color: "#a6adc8" },
  syncIndicator: { marginTop: 12, alignSelf: "flex-start" },
  syncErrorText: { color: "#f38ba8", fontSize: 13, marginTop: 10 },
  actionDialogContent: { paddingHorizontal: 0 },
  dangerText: { color: "#f38ba8" },
  renameError: { color: "#f38ba8", fontSize: 12, marginTop: 8 },
  snackbar: { backgroundColor: "#313244" },
});
