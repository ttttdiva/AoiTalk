import React, { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useRouter, useFocusEffect } from 'expo-router';
import { goBackOrReplace } from "../lib/navigation";
import { Button, Dialog, FAB, IconButton, Portal, Surface, Text, TextInput } from 'react-native-paper';
import { scenarioApi } from '../lib/scenario-api';
import type { Scenario } from '../types/api';

export default function ScenariosScreen() {
  const router = useRouter();
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [dialogVisible, setDialogVisible] = useState(false);
  const [editing, setEditing] = useState<Scenario | null>(null);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [genre, setGenre] = useState('');
  const [setting, setSetting] = useState('');
  const [openingText, setOpeningText] = useState('');

  const load = useCallback(async () => {
    const next = await scenarioApi.list();
    setScenarios(next);
  }, []);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const openEditor = (scenario?: Scenario) => {
    setEditing(scenario ?? null);
    setTitle(scenario?.title ?? '');
    setDescription(scenario?.description ?? '');
    setGenre(scenario?.genre ?? '');
    setSetting(scenario?.setting ?? '');
    setOpeningText(scenario?.opening_text ?? '');
    setDialogVisible(true);
  };

  const handleSave = async () => {
    if (!title.trim()) return;
    try {
      if (editing) {
        await scenarioApi.update(editing.id, {
          title: title.trim(),
          description,
          genre,
          setting,
          opening_text: openingText,
        });
      } else {
        await scenarioApi.create({
          title: title.trim(),
          description,
          genre,
          setting,
          opening_text: openingText,
        });
      }
      setDialogVisible(false);
      await load();
    } catch (error) {
      Alert.alert('Scenario', error instanceof Error ? error.message : 'Save failed');
    }
  };

  const handlePlay = async (scenarioId: string) => {
    try {
      const result = await scenarioApi.startPlay(scenarioId);
      router.push(`/(tabs)/chat/${result.conversation_session_id}`);
    } catch (error) {
      Alert.alert('Scenario', error instanceof Error ? error.message : 'Play failed');
    }
  };

  const handleDelete = (scenario: Scenario) => {
    Alert.alert('Delete Scenario', `${scenario.title} を削除しますか？`, [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete',
        style: 'destructive',
        onPress: async () => {
          try {
            await scenarioApi.delete(scenario.id);
            await load();
          } catch (error) {
            Alert.alert('Scenario', error instanceof Error ? error.message : 'Delete failed');
          }
        },
      },
    ]);
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/(tabs)/settings')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              Scenarios
            </Text>
            <Text style={styles.headerSubtext}>シナリオ管理とプレイ開始</Text>
          </View>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {scenarios.map((scenario) => (
          <Surface key={scenario.id} style={styles.card} elevation={0}>
            <Text style={styles.cardTitle}>{scenario.title}</Text>
            <Text style={styles.cardMeta}>{scenario.genre || 'genre unset'}</Text>
            <Text style={styles.cardDesc}>{scenario.description || scenario.setting || 'No summary'}</Text>
            <View style={styles.actions}>
              <Button compact textColor="#89b4fa" onPress={() => handlePlay(scenario.id)}>
                Play
              </Button>
              <Button
                compact
                textColor="#f9e2af"
                onPress={() => router.push(`/scenario/session?scenarioId=${scenario.id}`)}
              >
                Write
              </Button>
              <Button compact textColor="#a6adc8" onPress={() => router.push(`/scenario/${scenario.id}`)}>
                Manage
              </Button>
              <Button compact textColor="#f38ba8" onPress={() => handleDelete(scenario)}>
                Delete
              </Button>
            </View>
          </Surface>
        ))}

        {scenarios.length === 0 ? (
          <View style={styles.empty}>
            <Text style={styles.emptyText}>No scenarios available.</Text>
          </View>
        ) : null}
      </ScrollView>

      <FAB icon="plus" style={styles.fab} onPress={() => openEditor()} color="#cdd6f4" />

      <Portal>
        <Dialog visible={dialogVisible} onDismiss={() => setDialogVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>
            {editing ? 'Edit Scenario' : 'Create Scenario'}
          </Dialog.Title>
          <Dialog.ScrollArea style={{ maxHeight: 460 }}>
            <View style={{ padding: 4 }}>
              <TextInput label="Title" value={title} onChangeText={setTitle} mode="outlined" style={styles.input} />
              <TextInput label="Description" value={description} onChangeText={setDescription} mode="outlined" multiline style={styles.input} />
              <TextInput label="Genre" value={genre} onChangeText={setGenre} mode="outlined" style={styles.input} />
              <TextInput label="Setting" value={setting} onChangeText={setSetting} mode="outlined" multiline style={styles.input} />
              <TextInput
                label="Opening Text"
                value={openingText}
                onChangeText={setOpeningText}
                mode="outlined"
                multiline
                style={styles.input}
              />
            </View>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setDialogVisible(false)}>
              Cancel
            </Button>
            <Button textColor="#7c3aed" onPress={handleSave} disabled={!title.trim()}>
              Save
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
  content: { padding: 16, gap: 12, paddingBottom: 96 },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  cardTitle: { color: '#cdd6f4', fontSize: 16, fontWeight: '700' },
  cardMeta: { color: '#7c3aed', fontSize: 12, marginTop: 4 },
  cardDesc: { color: '#a6adc8', fontSize: 13, marginTop: 6 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: 8, marginTop: 10 },
  empty: { alignItems: 'center', paddingTop: 80 },
  emptyText: { color: '#a6adc8' },
  fab: { position: 'absolute', right: 16, bottom: 16, backgroundColor: '#7c3aed' },
  dialog: { backgroundColor: '#1e1e2e' },
  dialogTitle: { color: '#cdd6f4' },
  input: { marginBottom: 12 },
});
