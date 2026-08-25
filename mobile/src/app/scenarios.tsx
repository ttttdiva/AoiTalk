import React, { useCallback, useState } from "react";
import { Alert, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import {
  ActivityIndicator,
  Button,
  Dialog,
  FAB,
  IconButton,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { goBackOrReplace } from "../lib/navigation";
import { storyApi } from "../lib/story-api";
import { chatApi } from "../lib/chat-api";
import type { StoryWork, StoryWorkKind } from "../types/api";

/** Canonical Story work list. */
export default function StoryWorksScreen() {
  const router = useRouter();
  const [works, setWorks] = useState<StoryWork[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [dialogVisible, setDialogVisible] = useState(false);
  const [title, setTitle] = useState("");
  const [synopsis, setSynopsis] = useState("");
  const [kind, setKind] = useState<StoryWorkKind>("novel");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const nextWorks = await storyApi.listWorks();
      setWorks(nextWorks);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "作品一覧を取得できませんでした。");
    } finally {
      setLoading(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const refresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const resetEditor = () => {
    setTitle("");
    setSynopsis("");
    setKind("novel");
  };

  const createWork = async () => {
    const value = title.trim();
    if (!value) return;
    try {
      const work = await storyApi.createWork({ title: value, synopsis: synopsis.trim() || null, kind });
      setDialogVisible(false);
      resetEditor();
      setWorks((items) => [work, ...items.filter((item) => item.id !== work.id)]);
      router.push(`/scenario/${work.id}`);
    } catch (error) {
      Alert.alert("Story", error instanceof Error ? error.message : "作品を作成できませんでした。");
    }
  };

  const archiveWork = (work: StoryWork) => {
    Alert.alert("作品をアーカイブ", `${work.title} をアーカイブしますか？`, [
      { text: "キャンセル", style: "cancel" },
      {
        text: "アーカイブ",
        style: "destructive",
        onPress: async () => {
          try {
            await storyApi.archiveWork(work.id);
            setWorks((items) => items.filter((item) => item.id !== work.id));
          } catch (error) {
            Alert.alert("Story", error instanceof Error ? error.message : "アーカイブに失敗しました。");
          }
        },
      },
    ]);
  };

  const startWriting = async (work: StoryWork) => {
    try {
      // Link one canonical StoryWritingSession to the chat the user opens.
      const chat = await chatApi.createSession("project_manager");
      const writing = await storyApi.startWriting(work.id, { conversation_session_id: chat.id });
      const sessionId = writing.conversation_session_id || chat.id;
      router.push({ pathname: "/(tabs)/chat/[sessionId]", params: { sessionId } });
    } catch (error) {
      Alert.alert("Story", error instanceof Error ? error.message : "執筆チャットを開始できませんでした。");
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, "/(tabs)/settings")} />
          <View style={styles.headerText}>
            <Text variant="titleLarge" style={styles.headerTitle}>Story</Text>
            <Text style={styles.headerSubtext}>作品・エピソード・設定資料</Text>
          </View>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content} refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void refresh()} tintColor="#7c3aed" />}>
        {loading ? <ActivityIndicator accessibilityLabel="Storyを読み込み中" color="#7c3aed" /> : null}
        {!loading && loadError ? (
          <Surface style={styles.errorCard} elevation={0}>
            <Text style={styles.errorTitle}>作品一覧を読み込めませんでした</Text>
            <Text style={styles.errorText}>{loadError}</Text>
            <Button mode="outlined" textColor="#c4b5fd" onPress={() => void load()}>再試行</Button>
          </Surface>
        ) : null}
        {!loading && !loadError && works.length === 0 ? <View style={styles.empty}><Text style={styles.emptyText}>作品がありません。右下から作成してください。</Text></View> : null}
        {works.map((work) => (
          <Surface key={work.id} style={styles.card} elevation={0}>
            <Text style={styles.cardTitle}>{work.title}</Text>
            <Text style={styles.cardMeta}>{work.kind === "trpg" ? "TRPG" : "Novel"} · {work.status}</Text>
            <Text style={styles.cardDescription}>{work.synopsis || "概要未設定"}</Text>
            <Text style={styles.cardStats}>{work.episode_count ?? 0} episodes · {work.char_count ?? work.total_chars ?? 0} chars</Text>
            <View style={styles.actions}>
              <Button compact textColor="#89b4fa" onPress={() => void startWriting(work)}>Chat</Button>
              <Button compact textColor="#f9e2af" onPress={() => router.push(`/scenario/session?workId=${work.id}`)}>Write</Button>
              <Button compact textColor="#cdd6f4" onPress={() => router.push(`/scenario/${work.id}`)}>Open</Button>
              <Button compact textColor="#f38ba8" onPress={() => archiveWork(work)}>Archive</Button>
            </View>
          </Surface>
        ))}
      </ScrollView>

      <FAB icon="plus" color="#cdd6f4" style={styles.fab} onPress={() => setDialogVisible(true)} />

      <Portal>
        <Dialog visible={dialogVisible} onDismiss={() => setDialogVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>Create Story Work</Dialog.Title>
          <Dialog.Content>
            <TextInput label="Title" mode="outlined" value={title} onChangeText={setTitle} style={styles.input} />
            <TextInput label="Synopsis" mode="outlined" multiline value={synopsis} onChangeText={setSynopsis} style={styles.input} />
            <View style={styles.kindRow}>
              <Button mode={kind === "novel" ? "contained" : "outlined"} buttonColor={kind === "novel" ? "#4c1d95" : undefined} textColor="#cdd6f4" onPress={() => setKind("novel")}>Novel</Button>
              <Button mode={kind === "trpg" ? "contained" : "outlined"} buttonColor={kind === "trpg" ? "#4c1d95" : undefined} textColor="#cdd6f4" onPress={() => setKind("trpg")}>TRPG</Button>
            </View>
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setDialogVisible(false)}>Cancel</Button>
            <Button textColor="#c4b5fd" disabled={!title.trim()} onPress={() => void createWork()}>Create</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: { paddingTop: 52, paddingHorizontal: 8, paddingBottom: 16, backgroundColor: "#1e1e2e" },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerText: { flex: 1 },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  content: { padding: 16, gap: 12, paddingBottom: 96 },
  card: { backgroundColor: "#1e1e2e", borderRadius: 12, padding: 16 },
  cardTitle: { color: "#cdd6f4", fontSize: 17, fontWeight: "700" },
  cardMeta: { color: "#c4b5fd", fontSize: 12, marginTop: 4 },
  cardDescription: { color: "#a6adc8", fontSize: 13, marginTop: 8 },
  cardStats: { color: "#6c7086", fontSize: 12, marginTop: 6 },
  actions: { flexDirection: "row", justifyContent: "flex-end", flexWrap: "wrap", gap: 4, marginTop: 10 },
  errorCard: { backgroundColor: "#1e1e2e", borderRadius: 12, padding: 16, gap: 8 },
  errorTitle: { color: "#f38ba8", fontSize: 15, fontWeight: "700" },
  errorText: { color: "#a6adc8", fontSize: 13 },
  empty: { alignItems: "center", paddingTop: 80 },
  emptyText: { color: "#a6adc8", textAlign: "center" },
  fab: { position: "absolute", right: 16, bottom: 16, backgroundColor: "#7c3aed" },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  input: { marginBottom: 12 },
  kindRow: { flexDirection: "row", gap: 8 },
});
