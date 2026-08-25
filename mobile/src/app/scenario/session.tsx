import React, { useCallback, useMemo, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { Button, Chip, IconButton, Surface, Text } from "react-native-paper";
import { goBackOrReplace } from "../../lib/navigation";
import { storyApi } from "../../lib/story-api";
import { chatApi } from "../../lib/chat-api";
import type { StoryContextPreview, StoryEpisode, StoryOverview, StoryWritingSession } from "../../types/api";

/** Canonical Story writing launcher. It creates StoryWritingSession before Chat navigation. */
export default function StoryWritingScreen() {
  const router = useRouter();
  const { workId, episodeId, conversationSessionId } = useLocalSearchParams<{ workId?: string; episodeId?: string; conversationSessionId?: string }>();
  const [overview, setOverview] = useState<StoryOverview | null>(null);
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | undefined>(episodeId);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [existingWriting, setExistingWriting] = useState<StoryWritingSession | null>(null);
  const [contextPreview, setContextPreview] = useState<StoryContextPreview | null>(null);
  const [contextBusy, setContextBusy] = useState(false);

  const load = useCallback(async () => {
    if (!workId) return;
    try {
      const next = await storyApi.getOverview(workId);
      setOverview(next);
      setSelectedEpisodeId((current) => current && next.graph.episodes.some((item) => item.id === current) ? current : (next.work.start_episode_id || next.graph.episodes[0]?.id));
    } catch (error) {
      Alert.alert("Story", error instanceof Error ? error.message : "作品を読み込めませんでした。");
    } finally {
      setLoading(false);
    }
  }, [workId]);

  useFocusEffect(useCallback(() => { void load(); }, [load]));

  useFocusEffect(useCallback(() => {
    if (!conversationSessionId) {
      setExistingWriting(null);
      return;
    }
    let active = true;
    void storyApi.getWritingSessionByConversation(conversationSessionId).then((result) => {
      if (active) setExistingWriting(result);
    }).catch(() => {
      if (active) setExistingWriting(null);
    });
    return () => { active = false; };
  }, [conversationSessionId]));

  const selectedEpisode = useMemo<StoryEpisode | null>(() => overview?.graph.episodes.find((item) => item.id === selectedEpisodeId) ?? null, [overview, selectedEpisodeId]);

  const start = async () => {
    if (!workId || starting) return;
    setStarting(true);
    try {
      // Chat has no Story-specific session creation endpoint. Link a normal
      // conversation to the canonical StoryWritingSession in one API call.
      const chat = await chatApi.createSession("project_manager");
      const writing = await storyApi.startWriting(workId, {
        episode_id: selectedEpisodeId || null,
        conversation_session_id: chat.id,
      });
      const sessionId = writing.conversation_session_id || chat.id;
      router.replace({ pathname: "/(tabs)/chat/[sessionId]", params: { sessionId } });
    } catch (error) {
      Alert.alert("Story", error instanceof Error ? error.message : "執筆セッションを開始できませんでした。");
    } finally {
      setStarting(false);
    }
  };

  const resume = () => {
    const sessionId = existingWriting?.conversation_session_id || conversationSessionId;
    if (!sessionId) return;
    router.replace({ pathname: "/(tabs)/chat/[sessionId]", params: { sessionId } });
  };

  const previewContext = async () => {
    if (!selectedEpisode) return;
    setContextBusy(true);
    try {
      setContextPreview(await storyApi.contextPreview(selectedEpisode.id));
    } catch (error) {
      Alert.alert("Story", error instanceof Error ? error.message : "コンテキストを取得できませんでした。");
    } finally {
      setContextBusy(false);
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}><View style={styles.headerRow}><IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, workId ? `/scenario/${workId}` : "/scenarios")} /><View style={styles.headerText}><Text variant="titleLarge" style={styles.headerTitle}>Story Writing</Text><Text style={styles.headerSubtext}>{overview?.work.title || ""}</Text></View></View></Surface>
      <ScrollView contentContainerStyle={styles.content}>
        {loading ? <Text style={styles.description}>Loading…</Text> : null}
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.sectionTitle}>Target episode</Text>
          <View style={styles.wrap}>
            <Chip selected={!selectedEpisodeId} onPress={() => setSelectedEpisodeId(undefined)} style={!selectedEpisodeId ? styles.chipActive : styles.chip} textStyle={styles.chipText}>Work context</Chip>
            {(overview?.graph.episodes ?? []).map((episode) => <Chip key={episode.id} selected={selectedEpisodeId === episode.id} onPress={() => setSelectedEpisodeId(episode.id)} style={selectedEpisodeId === episode.id ? styles.chipActive : styles.chip} textStyle={styles.chipText}>{episode.title}</Chip>)}
          </View>
          {selectedEpisode ? <View style={styles.selection}><Text style={styles.cardTitle}>{selectedEpisode.title}</Text><Text style={styles.description}>{selectedEpisode.summary || selectedEpisode.plot || "Summary unset"}</Text></View> : null}
        </Surface>
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.sectionTitle}>Canonical context</Text>
          <Text style={styles.description}>Characters, rulebooks, notes, and the selected route are resolved by the server when Chat starts. Add the writing instruction in the Chat composer so it is durable in the conversation.</Text>
          <View style={styles.actions}><Button mode="outlined" textColor="#c4b5fd" loading={contextBusy} disabled={!selectedEpisode} onPress={() => void previewContext()}>Preview context</Button>{existingWriting || conversationSessionId ? <Button mode="outlined" textColor="#89b4fa" onPress={resume}>Resume Story Chat</Button> : null}<Button mode="contained" buttonColor="#7c3aed" loading={starting} disabled={starting || !workId} onPress={() => void start()}>Start Story Chat</Button></View>
          {contextPreview ? <Surface style={styles.preview} elevation={0}><Text style={styles.cardTitle}>{contextPreview.estimated_chars} chars · {contextPreview.resolved_model || "canonical model"}</Text><Text style={styles.description}>{contextPreview.prompt}</Text></Surface> : null}
        </Surface>
      </ScrollView>
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
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: "#1e1e2e", borderRadius: 12, padding: 16, gap: 10 },
  sectionTitle: { color: "#c4b5fd", fontSize: 14, fontWeight: "700" },
  cardTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  description: { color: "#a6adc8", fontSize: 13, lineHeight: 19 },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { backgroundColor: "#313244" },
  chipActive: { backgroundColor: "#4c1d95" },
  chipText: { color: "#cdd6f4" },
  selection: { padding: 12, backgroundColor: "#181825", borderRadius: 8, gap: 6 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  preview: { padding: 10, backgroundColor: "#181825", borderRadius: 8, gap: 6 },
});
