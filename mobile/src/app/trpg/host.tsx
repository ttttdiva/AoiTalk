import React, { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useFocusEffect, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import { Button, Chip, IconButton, Surface, Switch, Text, TextInput } from 'react-native-paper';
import { scenarioApi } from '../../lib/scenario-api';
import { trpgApi } from '../../lib/trpg-api';
import type { Scenario } from '../../types/api';

export default function TrpgHostScreen() {
  const router = useRouter();
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenarioId, setScenarioId] = useState('');
  const [roomTitle, setRoomTitle] = useState('');
  const [maxPlayers, setMaxPlayers] = useState('4');
  const [gmMode, setGmMode] = useState<'ai' | 'human'>('ai');
  const [isPublic, setIsPublic] = useState(true);

  const load = useCallback(async () => {
    const nextScenarios = await scenarioApi.list();
    setScenarios(nextScenarios);
    if (!scenarioId && nextScenarios[0]) {
      setScenarioId(nextScenarios[0].id);
    }
  }, [scenarioId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const handleCreate = async () => {
    if (!scenarioId) return;
    try {
      const room = await trpgApi.createRoom({
        scenario_id: scenarioId,
        room_title: roomTitle.trim() || undefined,
        max_players: Number(maxPlayers) || 4,
        gm_mode: gmMode,
        is_public: isPublic,
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
          <Text style={styles.cardTitle}>Scenario</Text>
          <View style={styles.wrap}>
            {scenarios.map((scenario) => (
              <Chip
                key={scenario.id}
                selected={scenarioId === scenario.id}
                onPress={() => setScenarioId(scenario.id)}
                style={[styles.chip, scenarioId === scenario.id && styles.chipActive]}
                textStyle={styles.chipText}
              >
                {scenario.title}
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
          <TextInput
            label="Max Players"
            value={maxPlayers}
            onChangeText={setMaxPlayers}
            mode="outlined"
            keyboardType="numeric"
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
          <View style={styles.switchRow}>
            <Text style={styles.switchLabel}>Public Room</Text>
            <Switch value={isPublic} onValueChange={setIsPublic} />
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
  switchRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 16,
  },
  switchLabel: { color: '#cdd6f4', fontSize: 14 },
});
