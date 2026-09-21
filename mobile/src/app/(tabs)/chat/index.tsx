import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BackHandler,
  FlatList,
  Keyboard,
  RefreshControl,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Checkbox,
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
import { useAuth } from "../../../contexts/AuthContext";
import { useProject } from "../../../contexts/ProjectContext";
import { conversationsRepo, uploadLocalSession } from "../../../repositories";
import { chatRepo } from "../../../repositories/chat";
import { applyForegroundConversationSessions } from "../../../repositories/conversations";
import { runSync } from "../../../sync/engine";
import { scheduleSyncAfterInteractions } from "../../../lib/background-sync";
import { createCurrentCharacterSession } from "../../../features/characters/current-character";
import { characterApi } from "../../../lib/character-api";
import {
  animateNextListChange,
  areConversationListsEqual,
} from "../../../features/ui/list-transitions";
import { useReducedMotion } from "../../../features/ui/use-reduced-motion";
import { ScreenHeader } from "../../../components/screen-header";
import {
  deleteSelectedSessions,
  getVisibleSessionIds,
  retainVisibleSelection,
  toggleSessionSelection,
} from "../../../features/conversation/session-selection";
import type { ConversationSession } from "../../../types/api";
import { chatApi, type ConversationSearchResult } from "../../../lib/chat-api";

export default function ChatListScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const { selectedProjectId } = useProject();
  const reduceMotion = useReducedMotion();
  const reduceMotionRef = useRef(reduceMotion);
  reduceMotionRef.current = reduceMotion;
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());
  const [deleteTargets, setDeleteTargets] = useState<string[]>([]);
  const [deleting, setDeleting] = useState(false);
  const deletingRef = useRef(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
  const selectionEpochRef = useRef(0);
  const searchRequestRef = useRef(0);
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
  const [groupPickerVisible, setGroupPickerVisible] = useState(false);
  const [groupPickerLoading, setGroupPickerLoading] = useState(false);
  const [groupPickerError, setGroupPickerError] = useState<string | null>(null);
  const [groupCharacters, setGroupCharacters] = useState<string[]>([]);
  const [groupSelectedCharacters, setGroupSelectedCharacters] = useState<string[]>([]);
  const [groupCreating, setGroupCreating] = useState(false);
  const groupCreatingRef = useRef(false);
  const loadRequestRef = useRef(0);
  const focusedRef = useRef(false);
  const screenEpochRef = useRef(0);
  const reloadAfterDeleteRef = useRef(false);
  const syncTaskRef = useRef<(() => void) | null>(null);
  const longPressedSessionIdRef = useRef<string | null>(null);

  const showingSearch = searchVisible && Boolean(searchQuery.trim());
  const listData: Array<ConversationSession | ConversationSearchResult> =
    showingSearch ? searchResults : sessions;
  const visibleSessionIds = useMemo(() => getVisibleSessionIds(listData), [listData]);
  const selectedCount = visibleSessionIds.filter((id) => selectedSessionIds.has(id)).length;
  const allSelected = visibleSessionIds.length > 0 && selectedCount === visibleSessionIds.length;

  const clearSelection = useCallback(() => {
    selectionEpochRef.current += 1;
    setSelectionMode(false);
    setSelectedSessionIds(new Set());
    setDeleteTargets([]);
    setDeleteError(null);
    longPressedSessionIdRef.current = null;
  }, []);

  useEffect(() => {
    clearSelection();
    setSessions([]);
  }, [selectedProjectId, clearSelection]);

  useEffect(() => {
    if (deleting) return;
    setSelectedSessionIds((current) => retainVisibleSelection(current, visibleSessionIds));
    setDeleteTargets((current) => {
      const visible = new Set(visibleSessionIds);
      const next = current.filter((id) => visible.has(id));
      return next.length === current.length ? current : next;
    });
  }, [visibleSessionIds, deleting]);

  useEffect(() => {
    if (!selectionMode) return;
    const subscription = BackHandler.addEventListener("hardwareBackPress", () => {
      if (deletingRef.current) return true;
      if (deleteTargets.length > 0) setDeleteTargets([]);
      else clearSelection();
      return true;
    });
    return () => subscription.remove();
  }, [selectionMode, deleteTargets.length, clearSelection]);

  const loadSessions = useCallback(
    async (options?: { forceRefresh?: boolean }) => {
      if (!focusedRef.current) return;
      if (deletingRef.current) {
        reloadAfterDeleteRef.current = true;
        return;
      }
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
          if (requestId !== loadRequestRef.current) return;
          const list = await conversationsRepo.listSessions(
            selectedProjectId,
            options,
          );
          applyList(list);
        } catch {
          // Keep the current list visible if an explicit refresh fails.
        }
        return;
      }

      try {
        applyList(await conversationsRepo.listSessionsLocal(selectedProjectId));
      } catch {
        // A sync can still repair a failed/stale local read.
      }
      if (requestId !== loadRequestRef.current) return;
      syncTaskRef.current = scheduleSyncAfterInteractions(async () => {
        if (requestId !== loadRequestRef.current) return;
        try {
          applyList(await conversationsRepo.listSessionsLocal(selectedProjectId));
        } catch {
          // 同期後の再読込失敗でも、先に表示したローカル一覧を維持する。
        }
      });
    },
    [selectedProjectId],
  );
  const loadSessionsRef = useRef(loadSessions);
  loadSessionsRef.current = loadSessions;

  useFocusEffect(
    useCallback(() => {
      focusedRef.current = true;
      void loadSessions();
      return () => {
        focusedRef.current = false;
        screenEpochRef.current += 1;
        loadRequestRef.current += 1;
        searchRequestRef.current += 1;
        setSearchVisible(false);
        setSearchQuery("");
        setSearchResults([]);
        setSearching(false);
        setActionTarget(null);
        setRenameTarget(null);
        setSyncTarget(null);
        setGroupPickerVisible(false);
        syncTaskRef.current?.();
        syncTaskRef.current = null;
        clearSelection();
      };
    }, [loadSessions, clearSelection]),
  );

  const onRefresh = async () => {
    if (selectionMode || deletingRef.current || deleteTargets.length > 0) return;
    setRefreshing(true);
    // pull-to-refresh は明示操作なのでフル取得する。
    await loadSessions({ forceRefresh: true });
    setRefreshing(false);
  };

  const handleSync = async () => {
    if (!syncTarget || deletingRef.current) return;
    const screenEpoch = screenEpochRef.current;
    setSyncing(true);
    setSyncError(null);
    try {
      await uploadLocalSession(syncTarget.id);
      if (!focusedRef.current || screenEpoch !== screenEpochRef.current) return;
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

  const openGroupPicker = useCallback(() => {
    if (!isAuthenticated) {
      setCreateChatError("グループチャットにはログインが必要です。");
      return;
    }
    setGroupPickerVisible(true);
    setGroupPickerLoading(true);
    setGroupPickerError(null);
    setGroupCharacters([]);
    setGroupSelectedCharacters([]);
    void characterApi
      .list()
      .catch(() => characterApi.getOfflineList(false))
      .then((characters) => {
        const slugs = Array.from(
          new Set(
            characters
              .filter((character) => character.is_enabled !== false)
              .map((character) => character.slug?.trim())
              .filter((slug): slug is string => Boolean(slug)),
          ),
        );
        setGroupCharacters(slugs);
        setGroupSelectedCharacters(slugs.slice(0, 2));
      })
      .catch((error) =>
        setGroupPickerError(
          error instanceof Error
            ? error.message
            : "キャラクター一覧を取得できませんでした。",
        ),
      )
      .finally(() => setGroupPickerLoading(false));
  }, [isAuthenticated]);

  const createGroupChat = useCallback(async () => {
    if (groupCreatingRef.current) return;
    const selectedCharacters = Array.from(
      new Set(
        groupSelectedCharacters.map((item) => item.trim()).filter(Boolean),
      ),
    );
    if (!isAuthenticated) {
      setGroupPickerError("グループチャットにはログインが必要です。");
      return;
    }
    if (selectedCharacters.length < 2) {
      setGroupPickerError(
        "グループチャットには2人以上のキャラクターを選択してください。",
      );
      return;
    }

    groupCreatingRef.current = true;
    setGroupCreating(true);
    setGroupPickerError(null);
    try {
      const result = await chatApi.createGroupSession(
        selectedCharacters,
        selectedProjectId ?? null,
      );
      await applyForegroundConversationSessions([result.session]);
      setSessions((previous) => [
        result.session,
        ...previous.filter((item) => item.id !== result.session.id),
      ]);
      setGroupPickerVisible(false);
      router.push({
        pathname: "/(tabs)/chat/[sessionId]",
        params: {
          sessionId: result.session.id,
          ...(selectedProjectId ? { projectId: selectedProjectId } : {}),
        },
      });
    } catch (error) {
      setGroupPickerError(
        error instanceof Error ? error.message : "グループチャットの作成に失敗しました。",
      );
    } finally {
      groupCreatingRef.current = false;
      setGroupCreating(false);
    }
  }, [groupSelectedCharacters, isAuthenticated, router, selectedProjectId]);

  const handleDelete = async () => {
    if (deletingRef.current || !focusedRef.current) return;
    const visible = new Set(visibleSessionIds);
    const targets = deleteTargets.filter((id) => visible.has(id));
    if (targets.length === 0) {
      setDeleteTargets([]);
      return;
    }
    deletingRef.current = true;
    setDeleting(true);
    setDeleteError(null);
    setDeleteNotice(null);
    const selectionEpoch = selectionEpochRef.current;
    // 削除前に開始した一覧/検索の応答で、削除済み行を復活させない。
    loadRequestRef.current += 1;
    searchRequestRef.current += 1;
    syncTaskRef.current?.();
    syncTaskRef.current = null;
    setRefreshing(false);
    setSearching(false);
    try {
      const { deletedIds, failedIds } = await deleteSelectedSessions(
        targets,
        (id) => conversationsRepo.deleteSession(id),
      );
      const deleted = new Set(deletedIds);
      setSessions((current) => current.filter((session) => !deleted.has(session.id)));
      setSearchResults((current) => current.filter((item) => !deleted.has(item.session_id)));
      // 画面移動やスコープ変更後に、古い選択モードを再表示しない。
      if (selectionEpoch !== selectionEpochRef.current) return;
      setDeleteTargets([]);
      if (failedIds.length > 0) {
        setSelectionMode(true);
        setSelectedSessionIds(new Set(failedIds));
        setDeleteError(
          `${deletedIds.length}件削除しました。${failedIds.length}件を削除できませんでした。選択を残しています。もう一度お試しください。`,
        );
      } else {
        clearSelection();
        setDeleteNotice(`${deletedIds.length}件の会話を削除しました。`);
      }
    } finally {
      deletingRef.current = false;
      setDeleting(false);
      if (reloadAfterDeleteRef.current && focusedRef.current) {
        reloadAfterDeleteRef.current = false;
        void loadSessionsRef.current();
      }
    }
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
    const requestId = ++searchRequestRef.current;
    if (!focusedRef.current || !searchVisible || !query) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearchResults([]);
    setSearching(true);
    const timer = setTimeout(() => {
      void chatRepo
        .search(query, selectedProjectId, 50)
        .then((results) => {
          if (!cancelled && requestId === searchRequestRef.current) setSearchResults(results.slice(0, 50));
        })
        .catch(() => {
          if (!cancelled && requestId === searchRequestRef.current) setSearchResults([]);
        })
        .finally(() => {
          if (!cancelled && requestId === searchRequestRef.current) setSearching(false);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [searchQuery, searchVisible, selectedProjectId]);

  const startSelection = (sessionId: string) => {
    if (deletingRef.current) return;
    longPressedSessionIdRef.current = sessionId;
    Keyboard.dismiss();
    setActionTarget(null);
    setDeleteNotice(null);
    setSelectionMode(true);
    setSelectedSessionIds((current) => new Set(current).add(sessionId));
  };

  const pressSession = (sessionId: string) => {
    if (deletingRef.current) return;
    // 長押しと同じジェスチャーのpressだけを無視し、次のpressInで解除する。
    if (longPressedSessionIdRef.current === sessionId) {
      longPressedSessionIdRef.current = null;
      return;
    }
    if (selectionMode) {
      setSelectedSessionIds((current) => toggleSessionSelection(current, sessionId));
      return;
    }
    if (showingSearch) {
      setSearchVisible(false);
      setSearchQuery("");
    }
    router.push(`/(tabs)/chat/${sessionId}`);
  };

  const selectionCheckbox = (sessionId: string) => (
    <View
      style={styles.selectionIndicator}
      pointerEvents="none"
      importantForAccessibility="no-hide-descendants"
      accessibilityElementsHidden
    >
      <Checkbox.Android
        status={selectedSessionIds.has(sessionId) ? "checked" : "unchecked"}
        color="#c4b5fd"
        uncheckedColor="#a6adc8"
        disabled={deleting}
      />
    </View>
  );

  const sessionInteraction = (sessionId: string) => ({
    onPressIn: () => { longPressedSessionIdRef.current = null; },
    onPress: () => pressSession(sessionId),
    onLongPress: () => startSelection(sessionId),
    delayLongPress: 280,
    disabled: deleting,
    accessibilityRole: selectionMode ? "checkbox" as const : "button" as const,
    accessibilityState: {
      disabled: deleting,
      ...(selectionMode ? { checked: selectedSessionIds.has(sessionId) } : {}),
    },
    accessibilityHint: selectionMode
      ? "タップして選択を切り替えます"
      : "タップして会話を開きます。長押しで複数選択できます",
  });

  const renderSearchItem = ({ item }: { item: ConversationSearchResult }) => (
    <List.Item
      {...sessionInteraction(item.session_id)}
      testID={`chat-search-result-${item.id}`}
      title={item.title || "無題の会話"}
      description={`${item.snippet}${item.character_name ? ` · ${item.character_name}` : ""}`}
      left={(props) => selectionMode
        ? selectionCheckbox(item.session_id)
        : <List.Icon {...props} icon="text-search" color="#89b4fa" />}
      style={[styles.listItem, selectionMode && selectedSessionIds.has(item.session_id) && styles.selectedItem]}
    />
  );

  const renderItem = ({ item }: { item: ConversationSession }) => (
    <List.Item
      {...sessionInteraction(item.id)}
      testID={`chat-session-${item.id}`}
      title={item.title || "New conversation"}
      titleStyle={styles.sessionTitle}
      description={
        item.last_activity
          ? `${format(new Date(item.last_activity), "MM/dd HH:mm")} | ${item.message_count} messages`
          : `${item.message_count} messages`
      }
      descriptionStyle={styles.sessionDesc}
      left={(props) => selectionMode
        ? selectionCheckbox(item.id)
        : <List.Icon {...props} icon="chat" color="#7c3aed" />}
      right={selectionMode ? undefined : () => (
        <IconButton
          icon="dots-vertical"
          iconColor="#a6adc8"
          size={22}
          accessibilityLabel={`${item.title || "無題の会話"}の操作`}
          onPress={(event) => {
            event?.stopPropagation();
            setActionTarget(item);
          }}
        />
      )}
      style={[styles.listItem, selectionMode && selectedSessionIds.has(item.id) && styles.selectedItem]}
    />
  );

  return (
    <View style={styles.container}>
      <ScreenHeader
        title={selectionMode ? `${selectedCount}件選択` : "Chat"}
        subtitle={selectionMode && showingSearch ? `検索結果の${visibleSessionIds.length}件の会話が対象` : undefined}
        showMenu={!selectionMode && !deleting}
        showSettings={!selectionMode && !deleting}
        right={selectionMode ? (
          <IconButton
            icon="close"
            iconColor="#cdd6f4"
            accessibilityLabel="複数選択を終了"
            disabled={deleting}
            onPress={() => { if (!deletingRef.current) clearSelection(); }}
          />
        ) : (
          <View style={styles.headerActions}>
            <IconButton
              icon="account-multiple-plus-outline"
              iconColor="#cdd6f4"
              accessibilityLabel="新規グループチャット"
              disabled={!isAuthenticated || deleting}
              onPress={openGroupPicker}
            />
            <IconButton
              icon={searchVisible ? "magnify-close" : "magnify"}
              iconColor="#cdd6f4"
              accessibilityLabel="会話を検索"
              disabled={deleting}
              onPress={() => {
                setSearchVisible((visible) => !visible);
                if (searchVisible) setSearchQuery("");
              }}
            />
          </View>
        )}
      />
      {selectionMode ? (
        <View style={styles.selectionToolbar}>
          <Checkbox.Item
            label={allSelected ? "全選択を解除" : "全選択"}
            accessibilityLabel={allSelected ? "全選択を解除" : "全選択"}
            status={allSelected ? "checked" : selectedCount > 0 ? "indeterminate" : "unchecked"}
            mode="android"
            position="leading"
            color="#c4b5fd"
            uncheckedColor="#a6adc8"
            labelStyle={styles.sessionTitle}
            style={styles.selectAll}
            disabled={deleting || visibleSessionIds.length === 0}
            onPress={() => {
              if (!deletingRef.current) setSelectedSessionIds(allSelected ? new Set() : new Set(visibleSessionIds));
            }}
          />
          <Button
            icon="delete-outline"
            textColor="#f38ba8"
            accessibilityLabel="選択した会話を削除"
            disabled={deleting || selectedCount === 0}
            loading={deleting}
            onPress={() => setDeleteTargets(visibleSessionIds.filter((id) => selectedSessionIds.has(id)))}
          >
            削除
          </Button>
        </View>
      ) : null}
      {deleteError ? (
        <Text style={styles.deleteError} accessibilityRole="alert">{deleteError}</Text>
      ) : null}
      {searchVisible ? (
        <TextInput
          value={searchQuery}
          onChangeText={setSearchQuery}
          mode="flat"
          dense
          autoFocus
          placeholder="会話を検索（最大50件）"
          disabled={selectionMode || deleting || deleteTargets.length > 0}
          style={styles.searchInput}
        />
      ) : null}

      <FlatList<ConversationSession | ConversationSearchResult>
        data={listData}
        extraData={{ selectionMode, selectedSessionIds, deleting }}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) =>
          "session_id" in item
            ? renderSearchItem({ item })
            : renderItem({ item })
        }
        ItemSeparatorComponent={() => <Divider style={styles.divider} />}
        refreshControl={selectionMode || deleting || deleteTargets.length > 0 ? undefined : (
          <RefreshControl
            refreshing={refreshing}
            onRefresh={onRefresh}
            tintColor="#7c3aed"
          />
        )}
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

      {!selectionMode && !deleting ? (
        <FAB
          icon="plus"
          style={styles.fab}
          onPress={() => void createRegularChat()}
          disabled={creatingChat}
          loading={creatingChat}
          color="#cdd6f4"
          accessibilityLabel="新しいチャット"
        />
      ) : null}

      <Portal>
        <Dialog
          visible={groupPickerVisible}
          onDismiss={() => {
            if (!groupPickerLoading && !groupCreating) {
              setGroupPickerVisible(false);
            }
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>Group Chat</Dialog.Title>
          <Dialog.ScrollArea>
            <ScrollView contentContainerStyle={styles.groupDialogContent}>
              <Text style={styles.dialogText}>
                参加するキャラクターを2人以上選択してください。
              </Text>
              {groupPickerLoading ? (
                <Text style={styles.dialogText}>読み込み中…</Text>
              ) : null}
              {groupPickerError ? (
                <Text style={styles.groupErrorText}>{groupPickerError}</Text>
              ) : null}
              {!groupPickerLoading && groupCharacters.length === 0 ? (
                <Text style={styles.dialogText}>
                  利用可能なキャラクターがありません。
                </Text>
              ) : null}
              {!groupPickerLoading
                ? groupCharacters.map((slug) => {
                    const selected = groupSelectedCharacters.includes(slug);
                    return (
                      <Button
                        key={slug}
                        mode={selected ? "contained" : "outlined"}
                        buttonColor={selected ? "#7c3aed" : undefined}
                        textColor="#cdd6f4"
                        style={styles.groupCharacterButton}
                        onPress={() =>
                          setGroupSelectedCharacters((current) =>
                            selected
                              ? current.filter((item) => item !== slug)
                              : [...current, slug],
                          )
                        }
                        disabled={groupCreating}
                      >
                        {slug}
                      </Button>
                    );
                  })
                : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button
              onPress={() => setGroupPickerVisible(false)}
              disabled={groupPickerLoading || groupCreating}
            >
              閉じる
            </Button>
            <Button
              onPress={() => void createGroupChat()}
              loading={groupCreating}
              disabled={
                groupPickerLoading ||
                groupCreating ||
                groupSelectedCharacters.length < 2
              }
            >
              作成
            </Button>
          </Dialog.Actions>
        </Dialog>

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
                setDeleteTargets(actionTarget ? [actionTarget.id] : []);
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
          visible={deleteTargets.length > 0}
          dismissable={!deleting}
          onDismiss={() => {
            if (!deletingRef.current) setDeleteTargets([]);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.dialogTitle}>
            {deleteTargets.length}件の会話を削除
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogText}>
              選択した{deleteTargets.length}件の会話をチャット一覧から削除します。
              サーバー上の会話は接続中のみ削除できます。ローカル専用の会話はオフラインでも削除できます。
            </Text>
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={() => { if (!deletingRef.current) setDeleteTargets([]); }} textColor="#a6adc8" disabled={deleting}>
              キャンセル
            </Button>
            <Button
              onPress={() => void handleDelete()}
              textColor="#f38ba8"
              accessibilityLabel="削除を確定"
              loading={deleting}
              disabled={deleting}
            >
              {deleting ? "削除中…" : "削除する"}
            </Button>
          </Dialog.Actions>
        </Dialog>
        <Snackbar
          visible={!!deleteNotice}
          onDismiss={() => setDeleteNotice(null)}
          duration={4000}
          style={styles.snackbar}
        >
          {deleteNotice}
        </Snackbar>
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
  headerActions: { flexDirection: "row", alignItems: "center" },
  searchInput: { backgroundColor: "#1e1e2e", marginHorizontal: 12, marginBottom: 6 },
  listItem: { backgroundColor: "#11111b", paddingVertical: 4 },
  selectedItem: { backgroundColor: "#302445" },
  selectionIndicator: { marginLeft: 12, justifyContent: "center" },
  selectionToolbar: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 8,
    backgroundColor: "#1e1e2e",
  },
  selectAll: { flex: 1, paddingVertical: 4 },
  deleteError: { color: "#f38ba8", paddingHorizontal: 16, paddingVertical: 8 },
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
  groupDialogContent: { paddingVertical: 8, gap: 8 },
  groupCharacterButton: { marginTop: 6 },
  groupErrorText: { color: "#f38ba8", fontSize: 13, marginTop: 8 },
  snackbar: { backgroundColor: "#313244" },
});
