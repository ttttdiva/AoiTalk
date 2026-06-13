import React, { useCallback, useMemo, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter, useFocusEffect } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import {
  Button,
  Chip,
  Dialog,
  FAB,
  IconButton,
  Portal,
  Surface,
  Text,
  TextInput,
} from 'react-native-paper';
import { scenarioApi } from '../../lib/scenario-api';
import type {
  CanonEntry,
  ScenarioCharacter,
  ScenarioDetail,
  ScenarioEpisode,
  ScenarioScene,
} from '../../types/api';

type SectionKey = 'episodes' | 'characters' | 'scenes' | 'canon';
type ItemType = ScenarioEpisode | ScenarioCharacter | ScenarioScene | CanonEntry;

const SECTION_KEYS: SectionKey[] = ['episodes', 'characters', 'scenes', 'canon'];

function getTitle(item: ItemType): string {
  if ('fact' in item) return item.fact;
  if ('name' in item) return item.name;
  return item.title;
}

function getBody(item: ItemType): string {
  if ('description' in item) return item.description || 'No description';
  if ('fact' in item) return item.fact;
  if ('synopsis_sentence' in item) {
    return item.synopsis_full || item.synopsis_paragraph || 'No details';
  }
  return 'No details';
}

export default function ScenarioDetailScreen() {
  const router = useRouter();
  const { scenarioId } = useLocalSearchParams<{ scenarioId: string }>();
  const [detail, setDetail] = useState<ScenarioDetail | null>(null);
  const [canonEntries, setCanonEntries] = useState<CanonEntry[]>([]);
  const [section, setSection] = useState<SectionKey>('episodes');
  const [dialogVisible, setDialogVisible] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [title, setTitle] = useState('');
  const [subtitle, setSubtitle] = useState('');
  const [body, setBody] = useState('');
  const [statusValue, setStatusValue] = useState('');
  const [sortOrder, setSortOrder] = useState('');
  const [gmInstructions, setGmInstructions] = useState('');
  const [imagePrompt, setImagePrompt] = useState('');
  const [sourceSceneId, setSourceSceneId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!scenarioId) return;
    const [nextDetail, nextCanon] = await Promise.all([
      scenarioApi.get(scenarioId),
      scenarioApi.listCanonEntries(scenarioId),
    ]);
    setDetail(nextDetail);
    setCanonEntries(nextCanon);
  }, [scenarioId]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const currentItems = useMemo(() => {
    if (!detail) return [];
    if (section === 'episodes') return detail.episodes ?? [];
    if (section === 'characters') return detail.characters;
    if (section === 'scenes') return detail.scenes;
    return canonEntries;
  }, [canonEntries, detail, section]);

  const openCreate = () => {
    setEditingId(null);
    setTitle('');
    setSubtitle('');
    setBody('');
    setStatusValue('');
    setSortOrder('');
    setGmInstructions('');
    setImagePrompt('');
    setSourceSceneId(null);
    setDialogVisible(true);
  };

  const openEdit = (item: ItemType) => {
    setEditingId(item.id);
    if (section === 'episodes') {
      const episode = item as ScenarioEpisode;
      setTitle(episode.title);
      setSubtitle(episode.synopsis_sentence ?? '');
      setBody(episode.synopsis_full ?? episode.synopsis_paragraph ?? '');
      setStatusValue(episode.status ?? '');
      setSortOrder(episode.sort_order != null ? String(episode.sort_order) : '');
      setGmInstructions('');
      setImagePrompt('');
      setSourceSceneId(null);
    } else if (section === 'characters') {
      const character = item as ScenarioCharacter;
      setTitle(character.name);
      setSubtitle(character.role ?? '');
      setBody(character.description ?? '');
      setStatusValue('');
      setSortOrder('');
      setGmInstructions('');
      setImagePrompt('');
      setSourceSceneId(null);
    } else if (section === 'scenes') {
      const scene = item as ScenarioScene;
      setTitle(scene.title);
      setSubtitle(scene.scene_type ?? '');
      setBody(scene.description ?? '');
      setStatusValue(scene.status ?? '');
      setSortOrder(scene.sort_order != null ? String(scene.sort_order) : '');
      setGmInstructions(scene.gm_instructions ?? '');
      setImagePrompt(scene.image_prompt ?? '');
      setSourceSceneId(null);
    } else {
      const canon = item as CanonEntry;
      setTitle(canon.category);
      setSubtitle('');
      setBody(canon.fact);
      setStatusValue('');
      setSortOrder('');
      setGmInstructions('');
      setImagePrompt('');
      setSourceSceneId(canon.source_scene_id ?? null);
    }
    setDialogVisible(true);
  };

  const handleSave = async () => {
    if (!scenarioId || !title.trim()) return;
    const parsedSortOrder = sortOrder.trim() ? Number(sortOrder) : undefined;
    try {
      if (section === 'episodes') {
        if (editingId) {
          await scenarioApi.updateEpisode(editingId, {
            title: title.trim(),
            synopsis_sentence: subtitle.trim(),
            synopsis_full: body.trim(),
            status: statusValue.trim() || undefined,
            sort_order: Number.isFinite(parsedSortOrder) ? parsedSortOrder : undefined,
          });
        } else {
          await scenarioApi.createEpisode(scenarioId, {
            title: title.trim(),
            synopsis_sentence: subtitle.trim(),
            synopsis_full: body.trim(),
            status: statusValue.trim() || undefined,
            sort_order: Number.isFinite(parsedSortOrder) ? parsedSortOrder : undefined,
          });
        }
      } else if (section === 'characters') {
        if (editingId) {
          await scenarioApi.updateCharacter(scenarioId, editingId, {
            name: title.trim(),
            role: subtitle.trim() || 'npc',
            description: body.trim(),
          });
        } else {
          await scenarioApi.createCharacter(scenarioId, {
            name: title.trim(),
            role: subtitle.trim() || 'npc',
            description: body.trim(),
          });
        }
      } else if (section === 'scenes') {
        if (editingId) {
          await scenarioApi.updateScene(scenarioId, editingId, {
            title: title.trim(),
            scene_type: subtitle.trim() || 'dialogue',
            description: body.trim(),
            gm_instructions: gmInstructions.trim() || undefined,
            image_prompt: imagePrompt.trim() || undefined,
            status: statusValue.trim() || undefined,
            sort_order: Number.isFinite(parsedSortOrder) ? parsedSortOrder : undefined,
          });
        } else {
          await scenarioApi.createScene(scenarioId, {
            title: title.trim(),
            scene_type: subtitle.trim() || 'dialogue',
            description: body.trim(),
            gm_instructions: gmInstructions.trim() || undefined,
            image_prompt: imagePrompt.trim() || undefined,
            sort_order: Number.isFinite(parsedSortOrder) ? parsedSortOrder : undefined,
          });
        }
      } else {
        if (editingId) {
          await scenarioApi.updateCanonEntry(editingId, {
            category: title.trim(),
            fact: body.trim(),
            source_scene_id: sourceSceneId,
          });
        } else {
          await scenarioApi.createCanonEntry(scenarioId, {
            category: title.trim(),
            fact: body.trim(),
            source_scene_id: sourceSceneId,
          });
        }
      }
      setDialogVisible(false);
      await load();
    } catch (error) {
      Alert.alert('Scenario', error instanceof Error ? error.message : 'Save failed');
    }
  };

  const handleDelete = (item: ItemType) => {
    Alert.alert('Delete', `${getTitle(item)} を削除しますか？`, [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete',
        style: 'destructive',
        onPress: async () => {
          try {
            if (!scenarioId) return;
            if (section === 'episodes') {
              await scenarioApi.deleteEpisode(item.id);
            } else if (section === 'characters') {
              await scenarioApi.deleteCharacter(scenarioId, item.id);
            } else if (section === 'scenes') {
              await scenarioApi.deleteScene(scenarioId, item.id);
            } else {
              await scenarioApi.deleteCanonEntry(item.id);
            }
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
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/scenarios')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              {detail?.title || 'Scenario'}
            </Text>
            <Text style={styles.headerSubtext}>{detail?.genre || detail?.description || ''}</Text>
          </View>
        </View>
        <View style={styles.sectionRow}>
          {SECTION_KEYS.map((key) => (
            <Chip
              key={key}
              selected={section === key}
              onPress={() => setSection(key)}
              style={[styles.sectionChip, section === key && styles.sectionChipActive]}
              textStyle={styles.sectionChipText}
            >
              {key}
            </Chip>
          ))}
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {currentItems.map((item) => (
          <Surface key={item.id} style={styles.card} elevation={0}>
            <Text style={styles.cardTitle}>{getTitle(item)}</Text>
            {'role' in item ? <Text style={styles.cardMeta}>{item.role}</Text> : null}
            {'scene_type' in item ? <Text style={styles.cardMeta}>{item.scene_type}</Text> : null}
            {'category' in item ? <Text style={styles.cardMeta}>{item.category}</Text> : null}
            {'status' in item && item.status ? <Text style={styles.cardMeta}>Status: {item.status}</Text> : null}
            {'sort_order' in item && item.sort_order != null ? (
              <Text style={styles.cardMeta}>Order: {item.sort_order}</Text>
            ) : null}
            {'source_scene_id' in item && item.source_scene_id ? (
              <Text style={styles.cardMeta}>Source Scene: {item.source_scene_id}</Text>
            ) : null}
            {'synopsis_sentence' in item && item.synopsis_sentence ? (
              <Text style={styles.cardMeta}>{item.synopsis_sentence}</Text>
            ) : null}
            <Text style={styles.cardBody}>
              {getBody(item)}
            </Text>
            {'gm_instructions' in item && item.gm_instructions ? (
              <Text style={styles.cardSubBody}>GM: {item.gm_instructions}</Text>
            ) : null}
            {'image_prompt' in item && item.image_prompt ? (
              <Text style={styles.cardSubBody}>Image: {item.image_prompt}</Text>
            ) : null}
            <View style={styles.actions}>
              <Button compact textColor="#a6adc8" onPress={() => openEdit(item)}>
                Edit
              </Button>
              <Button compact textColor="#f38ba8" onPress={() => handleDelete(item)}>
                Delete
              </Button>
            </View>
          </Surface>
        ))}

        {currentItems.length === 0 ? (
          <Surface style={styles.card} elevation={0}>
            <Text style={styles.emptyText}>No items in this section.</Text>
          </Surface>
        ) : null}
      </ScrollView>

      <FAB icon="plus" style={styles.fab} onPress={openCreate} color="#cdd6f4" />

      <Portal>
        <Dialog visible={dialogVisible} onDismiss={() => setDialogVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>
            {editingId ? `Edit ${section}` : `Create ${section}`}
          </Dialog.Title>
          <Dialog.Content>
            <TextInput
              label={section === 'characters' ? 'Name' : section === 'canon' ? 'Category' : 'Title'}
              value={title}
              onChangeText={setTitle}
              mode="outlined"
              style={styles.input}
            />
            <TextInput
              label={
                section === 'episodes'
                  ? 'One-line Summary'
                  : section === 'characters'
                    ? 'Role'
                    : section === 'scenes'
                      ? 'Scene Type'
                      : 'Optional'
              }
              value={subtitle}
              onChangeText={setSubtitle}
              mode="outlined"
              style={styles.input}
            />
            {section !== 'characters' ? (
              <TextInput
                label="Status"
                value={statusValue}
                onChangeText={setStatusValue}
                mode="outlined"
                style={styles.input}
              />
            ) : null}
            {(section === 'episodes' || section === 'scenes') ? (
              <TextInput
                label="Sort Order"
                value={sortOrder}
                onChangeText={setSortOrder}
                mode="outlined"
                keyboardType="numeric"
                style={styles.input}
              />
            ) : null}
            <TextInput
              label={
                section === 'episodes'
                  ? 'Details'
                  : section === 'canon'
                    ? 'Fact'
                    : 'Description'
              }
              value={body}
              onChangeText={setBody}
              mode="outlined"
              multiline
              style={styles.input}
            />
            {section === 'scenes' ? (
              <>
                <TextInput
                  label="GM Instructions"
                  value={gmInstructions}
                  onChangeText={setGmInstructions}
                  mode="outlined"
                  multiline
                  style={styles.input}
                />
                <TextInput
                  label="Image Prompt"
                  value={imagePrompt}
                  onChangeText={setImagePrompt}
                  mode="outlined"
                  multiline
                  style={styles.input}
                />
              </>
            ) : null}
            {section === 'canon' ? (
              <View style={styles.linkSection}>
                <Text style={styles.linkLabel}>Source Scene</Text>
                <View style={styles.linkRow}>
                  <Chip
                    selected={!sourceSceneId}
                    onPress={() => setSourceSceneId(null)}
                    style={[styles.linkChip, !sourceSceneId && styles.linkChipActive]}
                    textStyle={styles.sectionChipText}
                  >
                    None
                  </Chip>
                  {(detail?.scenes ?? []).map((scene) => (
                    <Chip
                      key={scene.id}
                      selected={sourceSceneId === scene.id}
                      onPress={() => setSourceSceneId(scene.id)}
                      style={[styles.linkChip, sourceSceneId === scene.id && styles.linkChipActive]}
                      textStyle={styles.sectionChipText}
                    >
                      {scene.title}
                    </Chip>
                  ))}
                </View>
              </View>
            ) : null}
          </Dialog.Content>
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
  sectionRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16, marginTop: 8 },
  sectionChip: { backgroundColor: '#313244' },
  sectionChipActive: { backgroundColor: '#4c1d95' },
  sectionChipText: { color: '#cdd6f4' },
  content: { padding: 16, gap: 12, paddingBottom: 96 },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  cardTitle: { color: '#cdd6f4', fontSize: 15, fontWeight: '700' },
  cardMeta: { color: '#7c3aed', fontSize: 12, marginTop: 4 },
  cardBody: { color: '#a6adc8', fontSize: 13, marginTop: 8 },
  cardSubBody: { color: '#bac2de', fontSize: 12, marginTop: 6 },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: 8, marginTop: 8 },
  emptyText: { color: '#a6adc8' },
  fab: { position: 'absolute', right: 16, bottom: 16, backgroundColor: '#7c3aed' },
  dialog: { backgroundColor: '#1e1e2e' },
  dialogTitle: { color: '#cdd6f4' },
  input: { marginBottom: 12 },
  linkSection: { marginBottom: 12 },
  linkLabel: { color: '#a6adc8', marginBottom: 8 },
  linkRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  linkChip: { backgroundColor: '#313244' },
  linkChipActive: { backgroundColor: '#4c1d95' },
});
