import React, { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useFocusEffect, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import { Button, Chip, IconButton, Surface, Text, TextInput } from 'react-native-paper';
import { storyApi } from '../../lib/story-api';
import storyRepo from '../../repositories/story';
import { trpgApi } from '../../lib/trpg-api';
import type { StoryWork } from '../../types/api';

export default function TrpgHostScreen() {
  const router = useRouter();
  const [works, setWorks] = useState<StoryWork[]>([]);
  const [workId, setWorkId] = useState('');
  const [roomTitle, setRoomTitle] = useState('');
  const [gmMode, setGmMode] = useState<'ai' | 'human'>('ai');

  const load = useCallback(async () => {
    let allWorks: StoryWork[];
    try {
      allWorks = await storyApi.listWorks();
    } catch {
      // Keep the selector usable offline after the last successful Story
      // list has been cached locally.
      allWorks = await storyRepo.listWorks();
    }
    const nextWorks = allWorks.filter((work) => work.kind === 'trpg');
    setWorks(nextWorks);
    setWorkId((current) => (current && nextWorks.some((work) => work.id === current) ? current : nextWorks[0]?.id ?? ''));
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const handleCreate = async () => {
    if (!workId) return;
    try {
      const room = await trpgApi.createRoom({
        // trpgApi serializes this selected Story Work ID as canonical
        // `work_id` for POST /api/trpg/sessions.
        work_id: workId,
        room_title: roomTitle.trim() || undefined,
        gm_mode: gmMode,
      });
      router.replace(`/trpg/${room.id}`);
    } catch (error) {
      Alert.alert('TRPG Host', error instanceof Error ? error.message : 'Create failed');
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/trpg')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Host Room
            </Text>
            <Text style={styles.headerSubtext}>Create a room with GM-focused defaults.</Text>
          </View>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Story Work (TRPG)</Text>
          <View style={styles.wrap}>
            {works.map((work) => (
              <Chip
                key={work.id}
                selected={workId === work.id}
                onPress={() => setWorkId(work.id)}
                style={[styles.chip, workId === work.id && styles.chipActive]}
                textStyle={styles.chipText}
              >
                {work.title}
              </Chip>
            ))}
          </View>
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <TextInput
            label="Room Title"
            value={roomTitle}
            onChangeText={setRoomTitle}
            mode="outlined"
            style={styles.input}
          />
          <Text style={styles.cardTitle}>GM Mode</Text>
          <View style={styles.wrap}>
            {(['ai', 'human'] as const).map((value) => (
              <Chip
                key={value}
                selected={gmMode === value}
                onPress={() => setGmMode(value)}
                style={[styles.chip, gmMode === value && styles.chipActive]}
                textStyle={styles.chipText}
              >
                {value.toUpperCase()}
              </Chip>
            ))}
          </View>
          <Button mode="contained" buttonColor="#7c3aed" textColor="#cdd6f4" onPress={handleCreate}>
            Create Room
          </Button>
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
  wrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  chip: { backgroundColor: '#313244' },
  chipActive: { backgroundColor: '#4c1d95' },
  chipText: { color: '#cdd6f4' },
  input: { marginBottom: 12 },
});
