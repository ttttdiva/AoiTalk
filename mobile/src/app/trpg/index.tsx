import React, { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useRouter, useFocusEffect } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import {
  Button,
  Dialog,
  FAB,
  IconButton,
  Portal,
  Surface,
  Text,
  TextInput,
} from 'react-native-paper';
import { scenarioApi } from '../../lib/scenario-api';
import { trpgApi } from '../../lib/trpg-api';
import type { Scenario, TrpgRoom } from '../../types/api';

export default function TrpgRoomsScreen() {
  const router = useRouter();
  const [rooms, setRooms] = useState<TrpgRoom[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [joinCode, setJoinCode] = useState('');
  const [dialogVisible, setDialogVisible] = useState(false);
  const [scenarioId, setScenarioId] = useState('');
  const [roomTitle, setRoomTitle] = useState('');

  const load = useCallback(async () => {
    const [nextRooms, nextScenarios] = await Promise.all([
      trpgApi.listRooms(),
      scenarioApi.list(),
    ]);
    setRooms(nextRooms);
    setScenarios(nextScenarios);
  }, []);

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
        room_title: roomTitle,
        is_public: true,
        gm_mode: 'ai',
      });
      setDialogVisible(false);
      router.push(`/trpg/${room.id}`);
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Create room failed');
    }
  };

  const handleJoinByCode = async () => {
    if (!joinCode.trim()) return;
    try {
      const code = joinCode.trim().toUpperCase();
      const room = await trpgApi.getRoom(code, code);
      router.push({
        pathname: '/trpg/[roomId]',
        params: { roomId: room.id, invite_code: code },
      });
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Room not found');
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/(tabs)/settings')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              TRPG
            </Text>
            <Text style={styles.headerSubtext}>ルーム一覧と入室</Text>
          </View>
        </View>
        <View style={styles.joinRow}>
          <TextInput
            mode="outlined"
            value={joinCode}
            onChangeText={setJoinCode}
            placeholder="Room code"
            style={styles.joinInput}
          />
          <Button mode="text" textColor="#89b4fa" onPress={() => router.push('/trpg/host')}>
            Host
          </Button>
          <Button mode="outlined" textColor="#89b4fa" onPress={handleJoinByCode}>
            Join
          </Button>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {rooms.map((room) => (
          <Surface key={room.id} style={styles.card} elevation={0}>
            <Text style={styles.cardTitle}>{room.room_title}</Text>
            <Text style={styles.cardMeta}>
              {room.room_code} · {room.player_count}/{room.max_players} · {room.gm_mode.toUpperCase()}
            </Text>
            <Text style={styles.cardDesc}>{room.scenario?.title || 'No scenario linked'}</Text>
            <View style={styles.actions}>
              <Button compact textColor="#89b4fa" onPress={() => router.push(`/trpg/${room.id}`)}>
                Open
              </Button>
            </View>
          </Surface>
        ))}
      </ScrollView>

      <FAB icon="plus" style={styles.fab} onPress={() => setDialogVisible(true)} color="#cdd6f4" />

      <Portal>
        <Dialog visible={dialogVisible} onDismiss={() => setDialogVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>Create TRPG Room</Dialog.Title>
          <Dialog.Content>
            <TextInput label="Room Title" value={roomTitle} onChangeText={setRoomTitle} mode="outlined" style={styles.input} />
            <Text style={styles.selectLabel}>Scenario</Text>
            <ScrollView style={styles.selectList}>
              {scenarios.map((scenario) => (
                <Button
                  key={scenario.id}
                  mode={scenarioId === scenario.id ? 'contained' : 'outlined'}
                  buttonColor={scenarioId === scenario.id ? '#7c3aed' : undefined}
                  textColor="#cdd6f4"
                  style={styles.selectButton}
                  onPress={() => setScenarioId(scenario.id)}
                >
                  {scenario.title}
                </Button>
              ))}
            </ScrollView>
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setDialogVisible(false)}>
              Cancel
            </Button>
            <Button textColor="#7c3aed" onPress={handleCreate} disabled={!scenarioId}>
              Create
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#11111b' },
  header: { paddingTop: 52, paddingHorizontal: 8, paddingBottom: 16, backgroundColor: '#1e1e2e' },
  headerRow: { flexDirection: 'row', alignItems: 'center' },
  headerTitle: { color: '#cdd6f4', fontWeight: 'bold' },
  headerSubtext: { color: '#a6adc8', marginTop: 2 },
  joinRow: { flexDirection: 'row', gap: 8, paddingHorizontal: 16, marginTop: 8, alignItems: 'center' },
  joinInput: { flex: 1 },
  content: { padding: 16, gap: 12, paddingBottom: 96 },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  cardTitle: { color: '#cdd6f4', fontSize: 16, fontWeight: '700' },
  cardMeta: { color: '#7c3aed', fontSize: 12, marginTop: 4 },
  cardDesc: { color: '#a6adc8', fontSize: 13, marginTop: 6 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', marginTop: 8 },
  fab: { position: 'absolute', right: 16, bottom: 16, backgroundColor: '#7c3aed' },
  dialog: { backgroundColor: '#1e1e2e' },
  dialogTitle: { color: '#cdd6f4' },
  input: { marginBottom: 12 },
  selectLabel: { color: '#a6adc8', marginBottom: 8 },
  selectList: { maxHeight: 240 },
  selectButton: { marginBottom: 8 },
});
