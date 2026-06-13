import React, { useCallback, useState } from "react";
import {
  Alert,
  FlatList,
  RefreshControl,
  StyleSheet,
  View,
} from "react-native";
import {
  Button,
  Dialog,
  Divider,
  FAB,
  IconButton,
  List,
  Portal,
  Surface,
  Text,
} from "react-native-paper";
import { useRouter, useFocusEffect } from "expo-router";
import { format } from "date-fns";
import { useProject } from "../../../contexts/ProjectContext";
import { conversationsRepo } from "../../../repositories";
import { runSync } from "../../../sync/engine";
import { getDefaultCharacterName } from "../../../lib/preferences";
import type { ConversationSession } from "../../../types/api";

export default function ChatListScreen() {
  const router = useRouter();
  const { selectedProjectId } = useProject();
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      await runSync();
      const list = await conversationsRepo.listSessions();
      setSessions(list);
    } catch {
      // Keep the current list visible if a refresh fails.
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      void loadSessions();
    }, [loadSessions]),
  );

  const onRefresh = async () => {
    setRefreshing(true);
    await loadSessions();
    setRefreshing(false);
  };

  const handleCreate = async () => {
    try {
      const characterName = await getDefaultCharacterName();
      const session = await conversationsRepo.createSession(
        characterName,
        selectedProjectId ?? undefined,
      );
      router.push({
        pathname: "/(tabs)/chat/[sessionId]",
        params: { sessionId: session.id },
      });
    } catch (error) {
      Alert.alert(
        "Chat",
        error instanceof Error ? error.message : "チャット開始に失敗しました。",
      );
    }
  };

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

  const renderItem = ({ item }: { item: ConversationSession }) => (
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
      onPress={() => router.push(`/(tabs)/chat/${item.id}`)}
      style={styles.listItem}
    />
  );

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <Text variant="titleLarge" style={styles.headerTitle}>
          Chat
        </Text>
      </Surface>

      <FlatList
        data={sessions}
        keyExtractor={(item) => item.id}
        renderItem={renderItem}
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
            <Text style={styles.emptyText}>No conversations yet.</Text>
            <Text style={styles.emptySubtext}>
              Create a session to start chatting.
            </Text>
          </View>
        }
        contentContainerStyle={
          sessions.length === 0 ? styles.emptyContainer : undefined
        }
      />

      <FAB
        icon="plus"
        style={styles.fab}
        onPress={handleCreate}
        color="#cdd6f4"
      />

      <Portal>
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
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: { padding: 16, paddingTop: 56, backgroundColor: "#1e1e2e" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
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
});
