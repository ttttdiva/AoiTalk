import React, { useCallback, useMemo, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import {
  ActivityIndicator,
  Button,
  Chip,
  Dialog,
  IconButton,
  Portal,
  Surface,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { format } from "date-fns";
import { useAuth } from "../../../contexts/AuthContext";
import { memoryApi } from "../../../lib/memory-api";
import type { UserMemory } from "../../../types/api";

type EditingState = {
  mode: "create" | "edit";
  memory?: UserMemory;
};

function formatMemoryDate(value?: string | null): string {
  if (!value) return "日時不明";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "日時不明";
  return format(date, "yyyy/MM/dd HH:mm");
}

export default function SettingsMemoryScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [memories, setMemories] = useState<UserMemory[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<EditingState | null>(null);
  const [draftContent, setDraftContent] = useState("");
  const [draftCategory, setDraftCategory] = useState("general");

  const activeCount = useMemo(
    () => memories.filter((memory) => memory.is_active).length,
    [memories],
  );

  const loadMemories = useCallback(async () => {
    if (!isAuthenticated) {
      setMemories([]);
      return;
    }
    setLoading(true);
    try {
      setMemories(await memoryApi.list());
    } catch (error) {
      Alert.alert(
        "User Memory",
        error instanceof Error
          ? error.message
          : "メモリ一覧の取得に失敗しました。",
      );
    } finally {
      setLoading(false);
    }
  }, [isAuthenticated]);

  useFocusEffect(
    useCallback(() => {
      void loadMemories();
    }, [loadMemories]),
  );

  const openCreateDialog = useCallback(() => {
    setDraftContent("");
    setDraftCategory("general");
    setEditing({ mode: "create" });
  }, []);

  const openEditDialog = useCallback((memory: UserMemory) => {
    setDraftContent(memory.content);
    setDraftCategory(memory.category || "general");
    setEditing({ mode: "edit", memory });
  }, []);

  const closeDialog = useCallback(() => {
    if (saving) return;
    setEditing(null);
  }, [saving]);

  const saveMemory = useCallback(async () => {
    const content = draftContent.trim();
    const category = draftCategory.trim() || "general";
    if (!content) {
      Alert.alert("User Memory", "メモリ内容を入力してください。");
      return;
    }
    if (!editing) return;

    setSaving(true);
    try {
      if (editing.mode === "create") {
        const created = await memoryApi.create({ content, category });
        setMemories((prev) => [created, ...prev]);
      } else if (editing.memory) {
        const updated = await memoryApi.update(editing.memory.id, {
          content,
          category,
        });
        setMemories((prev) =>
          prev.map((memory) =>
            memory.id === updated.id ? updated : memory,
          ),
        );
      }
      setEditing(null);
    } catch (error) {
      Alert.alert(
        "User Memory",
        error instanceof Error ? error.message : "メモリの保存に失敗しました。",
      );
    } finally {
      setSaving(false);
    }
  }, [draftCategory, draftContent, editing]);

  const toggleMemory = useCallback(async (memory: UserMemory) => {
    try {
      const updated = await memoryApi.toggle(memory.id);
      setMemories((prev) =>
        prev.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (error) {
      Alert.alert(
        "User Memory",
        error instanceof Error
          ? error.message
          : "メモリ状態の切り替えに失敗しました。",
      );
    }
  }, []);

  const deleteMemory = useCallback((memory: UserMemory) => {
    Alert.alert("User Memory", "このメモリを削除しますか？", [
      { text: "キャンセル", style: "cancel" },
      {
        text: "削除",
        style: "destructive",
        onPress: () => {
          void (async () => {
            try {
              await memoryApi.delete(memory.id);
              setMemories((prev) => prev.filter((item) => item.id !== memory.id));
            } catch (error) {
              Alert.alert(
                "User Memory",
                error instanceof Error
                  ? error.message
                  : "メモリの削除に失敗しました。",
              );
            }
          })();
        },
      },
    ]);
  }, []);

  const deleteAllMemories = useCallback(() => {
    Alert.alert(
      "User Memory",
      "全てのメモリを削除しますか？この操作は取り消せません。",
      [
        { text: "キャンセル", style: "cancel" },
        {
          text: "全削除",
          style: "destructive",
          onPress: () => {
            void (async () => {
              try {
                await memoryApi.deleteAll();
                setMemories([]);
              } catch (error) {
                Alert.alert(
                  "User Memory",
                  error instanceof Error
                    ? error.message
                    : "メモリの全削除に失敗しました。",
                );
              }
            })();
          },
        },
      ],
    );
  }, []);

  return (
    <>
      <ScrollView style={styles.container} contentContainerStyle={styles.content}>
        <Surface style={styles.header} elevation={1}>
          <View style={styles.headerRow}>
            <IconButton
              icon="arrow-left"
              iconColor="#cdd6f4"
              onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
            />
            <View style={{ flex: 1 }}>
              <Text variant="titleLarge" style={styles.headerTitle}>
                User Memory
              </Text>
              <Text style={styles.headerSubtext}>
                会話から保存されたユーザー情報を確認・編集します。
              </Text>
            </View>
          </View>
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <View style={styles.sectionHeader}>
            <View>
              <Text style={styles.cardTitle}>Saved Memories</Text>
              <Text style={styles.helperText}>
                有効なメモリはサーバー側チャットのプロンプトへ簡潔に挿入されます。
              </Text>
            </View>
            {memories.length > 0 ? (
              <Chip compact style={styles.countChip} textStyle={styles.countChipText}>
                {activeCount}/{memories.length} active
              </Chip>
            ) : null}
          </View>

          {!isAuthenticated ? (
            <View style={styles.emptyBlock}>
              <Text style={styles.emptyText}>
                ユーザーメモリはサーバーログイン中のみ利用できます。
              </Text>
              <Button
                mode="outlined"
                textColor="#89b4fa"
                onPress={() => router.push("/(auth)/login")}
              >
                サーバーにログイン
              </Button>
            </View>
          ) : loading ? (
            <ActivityIndicator color="#7c3aed" style={styles.loader} />
          ) : memories.length === 0 ? (
            <View style={styles.emptyBlock}>
              <Text style={styles.emptyText}>
                メモリはまだありません。会話を通じて自動的に記憶されます。
              </Text>
              <Button mode="contained" buttonColor="#7c3aed" onPress={openCreateDialog}>
                手動で追加
              </Button>
            </View>
          ) : (
            <View style={styles.memoryList}>
              {memories.map((memory) => (
                <Surface key={memory.id} style={styles.memoryCard} elevation={0}>
                  <View style={styles.memoryTopRow}>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.memoryText}>{memory.content}</Text>
                      <View style={styles.metaRow}>
                        <Chip compact style={styles.metaChip} textStyle={styles.metaChipText}>
                          {memory.source === "manual" ? "manual" : "auto"}
                        </Chip>
                        <Chip compact style={styles.metaChip} textStyle={styles.metaChipText}>
                          {memory.category || "general"}
                        </Chip>
                      </View>
                    </View>
                    <Switch
                      value={memory.is_active}
                      onValueChange={() => {
                        void toggleMemory(memory);
                      }}
                    />
                  </View>
                  <Text style={styles.memoryDate}>
                    Updated {formatMemoryDate(memory.updated_at || memory.created_at)}
                  </Text>
                  <View style={styles.actions}>
                    <Button
                      compact
                      mode="outlined"
                      textColor="#89b4fa"
                      onPress={() => openEditDialog(memory)}
                    >
                      編集
                    </Button>
                    <Button
                      compact
                      mode="text"
                      textColor="#f38ba8"
                      onPress={() => deleteMemory(memory)}
                    >
                      削除
                    </Button>
                  </View>
                </Surface>
              ))}
            </View>
          )}

          {isAuthenticated ? (
            <View style={styles.footerActions}>
              <Button
                mode="contained"
                buttonColor="#7c3aed"
                onPress={openCreateDialog}
              >
                メモリを追加
              </Button>
              <Button mode="outlined" textColor="#89b4fa" onPress={loadMemories}>
                更新
              </Button>
              {memories.length > 0 ? (
                <Button mode="text" textColor="#f38ba8" onPress={deleteAllMemories}>
                  全削除
                </Button>
              ) : null}
            </View>
          ) : null}
        </Surface>
      </ScrollView>

      <Portal>
        <Dialog visible={!!editing} onDismiss={closeDialog} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>
            {editing?.mode === "create" ? "メモリを追加" : "メモリを編集"}
          </Dialog.Title>
          <Dialog.Content>
            <Text style={styles.dialogLabel}>内容</Text>
            <TextInput
              mode="outlined"
              value={draftContent}
              onChangeText={setDraftContent}
              multiline
              style={styles.dialogInput}
              outlineColor="#585b70"
              activeOutlineColor="#7c3aed"
              textColor="#cdd6f4"
            />
            <Text style={styles.dialogLabel}>カテゴリ</Text>
            <TextInput
              mode="outlined"
              value={draftCategory}
              onChangeText={setDraftCategory}
              autoCapitalize="none"
              style={styles.dialogInput}
              outlineColor="#585b70"
              activeOutlineColor="#7c3aed"
              textColor="#cdd6f4"
            />
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={closeDialog} disabled={saving} textColor="#a6adc8">
              キャンセル
            </Button>
            <Button onPress={saveMemory} loading={saving} textColor="#89b4fa">
              保存
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { paddingBottom: 32 },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 4 },
  card: {
    backgroundColor: "#1e1e2e",
    borderRadius: 16,
    padding: 16,
    margin: 16,
  },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    marginBottom: 12,
  },
  cardTitle: { color: "#cdd6f4", fontSize: 18, fontWeight: "700" },
  helperText: { color: "#a6adc8", marginTop: 4, lineHeight: 20 },
  countChip: { backgroundColor: "#313244", alignSelf: "flex-start" },
  countChipText: { color: "#cdd6f4", fontSize: 11 },
  loader: { marginVertical: 32 },
  emptyBlock: { gap: 16, paddingVertical: 16 },
  emptyText: { color: "#a6adc8", lineHeight: 20 },
  memoryList: { gap: 12 },
  memoryCard: {
    backgroundColor: "#181825",
    borderColor: "#313244",
    borderRadius: 12,
    borderWidth: 1,
    padding: 12,
  },
  memoryTopRow: { flexDirection: "row", gap: 12, alignItems: "flex-start" },
  memoryText: { color: "#cdd6f4", fontSize: 15, lineHeight: 22 },
  metaRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 10 },
  metaChip: { backgroundColor: "#313244" },
  metaChipText: { color: "#a6adc8", fontSize: 11 },
  memoryDate: { color: "#6c7086", fontSize: 12, marginTop: 10 },
  actions: { flexDirection: "row", justifyContent: "flex-end", gap: 8, marginTop: 8 },
  footerActions: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 16 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogLabel: { color: "#a6adc8", marginBottom: 6, marginTop: 8 },
  dialogInput: { backgroundColor: "#181825", marginBottom: 8 },
});
