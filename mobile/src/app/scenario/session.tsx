import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useCallback, useMemo, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import { Button, Chip, IconButton, Surface, Text, TextInput } from 'react-native-paper';
import { scenarioApi } from '../../lib/scenario-api';
import type { ScenarioDetail } from '../../types/api';

type SessionDraft = {
  targetEpisodeId?: string;
  targetSceneId?: string;
  prompt: string;
};

function getDraftKey(scenarioId: string): string {
  return `scenario-session-draft:${scenarioId}`;
}

export default function ScenarioSessionScreen() {
  const router = useRouter();
  const { scenarioId } = useLocalSearchParams<{ scenarioId: string }>();
  const [detail, setDetail] = useState<ScenarioDetail | null>(null);
  const [targetEpisodeId, setTargetEpisodeId] = useState<string | undefined>();
  const [targetSceneId, setTargetSceneId] = useState<string | undefined>();
  const [prompt, setPrompt] = useState('');
  const [savingDraft, setSavingDraft] = useState(false);

  const load = useCallback(async () => {
    if (!scenarioId) return;
    const [nextDetail, rawDraft] = await Promise.all([
      scenarioApi.get(scenarioId),
      AsyncStorage.getItem(getDraftKey(scenarioId)),
    ]);
    setDetail(nextDetail);
    if (rawDraft) {
      const draft = JSON.parse(rawDraft) as SessionDraft;
      setTargetEpisodeId(draft.targetEpisodeId);
      setTargetSceneId(draft.targetSceneId);
      setPrompt(draft.prompt || '');
    }
  }, [scenarioId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const selectedEpisode = useMemo(
    () => (detail?.episodes || []).find((episode) => episode.id === targetEpisodeId) ?? null,
    [detail, targetEpisodeId],
  );

  const selectedScene = useMemo(
    () => (detail?.scenes || []).find((scene) => scene.id === targetSceneId) ?? null,
    [detail, targetSceneId],
  );

  const promptSuggestions = useMemo(() => {
    const suggestions = [
      'Continue this scene in the current tone and keep the canon consistent.',
      'Write the next beat with stronger sensory detail and clearer character intent.',
      'Draft a short exchange first, then expand into full prose.',
      'Revise for pacing, clarity, and emotional escalation.',
    ];
    if (selectedEpisode?.synopsis_sentence) {
      suggestions.unshift(`Write toward this episode goal: ${selectedEpisode.synopsis_sentence}`);
    }
    if (selectedScene?.gm_instructions) {
      suggestions.unshift(`Follow these scene constraints: ${selectedScene.gm_instructions}`);
    }
    return suggestions;
  }, [selectedEpisode, selectedScene]);

  const persistDraft = useCallback(async () => {
    if (!scenarioId) return;
    setSavingDraft(true);
    try {
      await AsyncStorage.setItem(
        getDraftKey(scenarioId),
        JSON.stringify({
          targetEpisodeId,
          targetSceneId,
          prompt,
        } satisfies SessionDraft),
      );
    } finally {
      setSavingDraft(false);
    }
  }, [prompt, scenarioId, targetEpisodeId, targetSceneId]);

  const clearDraft = useCallback(async () => {
    if (!scenarioId) return;
    await AsyncStorage.removeItem(getDraftKey(scenarioId));
    setTargetEpisodeId(undefined);
    setTargetSceneId(undefined);
    setPrompt('');
  }, [scenarioId]);

  const handleStart = async () => {
    if (!scenarioId) return;
    try {
      const session = await scenarioApi.startWritingSession(scenarioId, {
        target_episode_id: targetEpisodeId,
        target_scene_id: targetSceneId,
        writing_prompt: prompt.trim(),
      });
      await AsyncStorage.removeItem(getDraftKey(scenarioId));
      if (session.conversation_session_id) {
        router.push(`/(tabs)/chat/${session.conversation_session_id}`);
      }
    } catch (error) {
      Alert.alert('Scenario Session', error instanceof Error ? error.message : 'Start failed');
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/scenarios')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Writing Session
            </Text>
            <Text style={styles.headerSubtext}>{detail?.title || ''}</Text>
          </View>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Target Episode</Text>
          <View style={styles.wrap}>
            <Chip
              selected={!targetEpisodeId}
              onPress={() => setTargetEpisodeId(undefined)}
              style={[styles.chip, !targetEpisodeId && styles.chipActive]}
              textStyle={styles.chipText}
            >
              None
            </Chip>
            {(detail?.episodes || []).map((episode) => (
              <Chip
                key={episode.id}
                selected={targetEpisodeId === episode.id}
                onPress={() => setTargetEpisodeId(episode.id)}
                style={[styles.chip, targetEpisodeId === episode.id && styles.chipActive]}
                textStyle={styles.chipText}
              >
                {episode.title}
              </Chip>
            ))}
          </View>
          {selectedEpisode ? (
            <View style={styles.selectionBox}>
              <Text style={styles.selectionTitle}>{selectedEpisode.title}</Text>
              <Text style={styles.selectionBody}>
                {selectedEpisode.synopsis_full || selectedEpisode.synopsis_paragraph || selectedEpisode.synopsis_sentence || 'No summary'}
              </Text>
            </View>
          ) : null}
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Target Scene</Text>
          <View style={styles.wrap}>
            <Chip
              selected={!targetSceneId}
              onPress={() => setTargetSceneId(undefined)}
              style={[styles.chip, !targetSceneId && styles.chipActive]}
              textStyle={styles.chipText}
            >
              None
            </Chip>
            {(detail?.scenes || []).map((scene) => (
              <Chip
                key={scene.id}
                selected={targetSceneId === scene.id}
                onPress={() => setTargetSceneId(scene.id)}
                style={[styles.chip, targetSceneId === scene.id && styles.chipActive]}
                textStyle={styles.chipText}
              >
                {scene.title}
              </Chip>
            ))}
          </View>
          {selectedScene ? (
            <View style={styles.selectionBox}>
              <Text style={styles.selectionTitle}>{selectedScene.title}</Text>
              <Text style={styles.selectionBody}>{selectedScene.description || 'No description'}</Text>
              {selectedScene.gm_instructions ? (
                <Text style={styles.selectionMeta}>GM: {selectedScene.gm_instructions}</Text>
              ) : null}
            </View>
          ) : null}
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Prompt</Text>
          <View style={styles.wrap}>
            {promptSuggestions.map((suggestion) => (
              <Chip
                key={suggestion}
                onPress={() => setPrompt((current) => (current ? `${current}\n${suggestion}` : suggestion))}
                style={styles.promptChip}
                textStyle={styles.chipText}
              >
                Use
              </Chip>
            ))}
          </View>
          <TextInput
            mode="outlined"
            multiline
            label="Writing Prompt"
            value={prompt}
            onChangeText={setPrompt}
            style={styles.input}
          />
          <View style={styles.buttonRow}>
            <Button mode="outlined" textColor="#89b4fa" onPress={() => void persistDraft()}>
              {savingDraft ? 'Saving...' : 'Save Draft'}
            </Button>
            <Button mode="outlined" textColor="#a6adc8" onPress={() => void clearDraft()}>
              Clear Draft
            </Button>
            <Button mode="contained" buttonColor="#7c3aed" textColor="#cdd6f4" onPress={() => void handleStart()}>
              Start Session
            </Button>
          </View>
        </Surface>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#11111b' },
  header: { paddingTop: 52, paddingHorizontal: 8, paddingBottom: 16, backgroundColor: '#1e1e2e' },
  headerRow: { flexDirection: 'row', alignItems: 'center' },
  headerTitle: { color: '#cdd6f4', fontWeight: 'bold' },
  headerSubtext: { color: '#a6adc8', marginTop: 2 },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  cardTitle: { color: '#7c3aed', fontSize: 13, fontWeight: '700', marginBottom: 10 },
  wrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { backgroundColor: '#313244' },
  chipActive: { backgroundColor: '#4c1d95' },
  chipText: { color: '#cdd6f4' },
  promptChip: { backgroundColor: '#313244' },
  selectionBox: {
    marginTop: 12,
    padding: 12,
    borderRadius: 8,
    backgroundColor: '#181825',
  },
  selectionTitle: { color: '#cdd6f4', fontSize: 13, fontWeight: '700' },
  selectionBody: { color: '#a6adc8', fontSize: 13, marginTop: 6 },
  selectionMeta: { color: '#89b4fa', fontSize: 12, marginTop: 6 },
  input: { marginTop: 12, marginBottom: 12 },
  buttonRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
});
