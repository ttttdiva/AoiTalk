import React, { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import {
  Button,
  Dialog,
  IconButton,
  Portal,
  Surface,
  Switch,
  Text,
  TextInput,
} from 'react-native-paper';
import { ProjectColorPicker } from '../../components/project-color-picker';
import {
  getProjectColor,
  DEFAULT_PROJECT_COLOR,
  normalizeProjectColor,
} from '../../lib/project-colors';
import { projectApi } from '../../lib/project-api';
import { taskApi } from '../../lib/task-api';
import type {
  Project,
  ProjectMember,
  ProjectNotificationSetting,
  ProjectStorageUsage,
} from '../../types/api';

export default function ProjectDetailScreen() {
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [usage, setUsage] = useState<ProjectStorageUsage | null>(null);
  const [notifications, setNotifications] = useState<ProjectNotificationSetting | null>(null);
  const [dialogVisible, setDialogVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [color, setColor] = useState(DEFAULT_PROJECT_COLOR);
  const [allowJoinRequests, setAllowJoinRequests] = useState(false);
  const [storageQuotaMb, setStorageQuotaMb] = useState('');

  const load = useCallback(async () => {
    if (!id) return;
    const [nextProject, nextMembers, nextUsage, nextNotifications] = await Promise.all([
      projectApi.get(id),
      projectApi.listMembers(id),
      projectApi.getStorageUsage(id),
      taskApi.getNotificationSettings(id),
    ]);
    setProject(nextProject);
    setMembers(nextMembers);
    setUsage(nextUsage);
    setNotifications(nextNotifications);
    setName(nextProject.name);
    setDescription(nextProject.description ?? '');
    setColor(getProjectColor(nextProject));
    setAllowJoinRequests(Boolean(nextProject.allow_join_requests));
    setStorageQuotaMb(
      nextProject.storage_quota_mb != null ? String(nextProject.storage_quota_mb) : '',
    );
  }, [id]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const handleSave = async () => {
    if (!id || !name.trim()) return;
    setSaving(true);
    try {
      const updated = await projectApi.update(id, {
        name: name.trim(),
        description: description.trim() || null,
        project_metadata: {
          ...(project?.metadata ?? {}),
          color: normalizeProjectColor(color),
        },
        allow_join_requests: allowJoinRequests,
        storage_quota_mb: storageQuotaMb.trim() ? Number(storageQuotaMb) : undefined,
      });
      setProject(updated);
      setDialogVisible(false);
    } catch (error) {
      Alert.alert('Project', error instanceof Error ? error.message : 'Update failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/projects')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              {project?.name || 'Project'}
            </Text>
            <Text style={styles.headerSubtext}>{project?.slug || ''}</Text>
          </View>
          <Button compact mode="outlined" textColor="#89b4fa" onPress={() => setDialogVisible(true)}>
            Edit
          </Button>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Overview</Text>
          <Text style={styles.bodyText}>{project?.description || 'No description'}</Text>
          <View style={styles.colorRow}>
            <View
              style={[
                styles.colorDot,
                { backgroundColor: getProjectColor(project) },
              ]}
            />
            <Text style={styles.metaText}>Color: {getProjectColor(project)}</Text>
          </View>
          <Text style={styles.metaText}>
            Join requests: {project?.allow_join_requests ? 'Enabled' : 'Disabled'}
          </Text>
          <Text style={styles.metaText}>Quota: {project?.storage_quota_mb ?? 0} MB</Text>
          <Text style={styles.metaText}>Used: {project?.storage_used_mb?.toFixed(2) ?? '0.00'} MB</Text>
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Storage</Text>
          <Text style={styles.metaText}>Used: {usage?.usage?.total_mb?.toFixed(2) || '0.00'} MB</Text>
          <Text style={styles.metaText}>Files: {usage?.usage?.file_count ?? 0}</Text>
          <Text style={styles.metaText}>Folders: {usage?.usage?.directory_count ?? 0}</Text>
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Notifications</Text>
          <Text style={styles.metaText}>
            Webhook: {notifications?.discord_webhook_url ? 'Configured' : 'Not set'}
          </Text>
          <Text style={styles.metaText}>
            Default reminders: {(notifications?.default_reminder_offsets || []).join(', ') || 'None'}
          </Text>
          <Text style={styles.metaText}>
            Overdue alerts: {notifications?.notify_overdue ? 'Enabled' : 'Disabled'}
          </Text>
          <Button mode="outlined" textColor="#89b4fa" onPress={() => router.push('/(tabs)/settings/connection')}>
            Open Notification Settings
          </Button>
        </Surface>

        <Surface style={styles.card} elevation={0}>
          <Text style={styles.cardTitle}>Members</Text>
          {members.map((member) => (
            <View key={member.id} style={styles.memberRow}>
              <Text style={styles.memberName}>
                {member.display_name || member.username || member.user_id}
              </Text>
              <Text style={styles.memberRole}>{member.role || 'member'}</Text>
            </View>
          ))}
          {members.length === 0 ? <Text style={styles.bodyText}>No members listed.</Text> : null}
        </Surface>
      </ScrollView>

      <Portal>
        <Dialog visible={dialogVisible} onDismiss={() => setDialogVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>Edit Project</Dialog.Title>
          <Dialog.Content>
            <TextInput label="Name" value={name} onChangeText={setName} mode="outlined" style={styles.input} />
            <TextInput
              label="Description"
              value={description}
              onChangeText={setDescription}
              mode="outlined"
              multiline
              style={styles.input}
            />
            <ProjectColorPicker value={color} onChange={setColor} />
            <TextInput
              label="Storage Quota (MB)"
              value={storageQuotaMb}
              onChangeText={setStorageQuotaMb}
              mode="outlined"
              keyboardType="numeric"
              style={styles.input}
            />
            <View style={styles.switchRow}>
              <Text style={styles.switchLabel}>Allow Join Requests</Text>
              <Switch value={allowJoinRequests} onValueChange={setAllowJoinRequests} />
            </View>
          </Dialog.Content>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setDialogVisible(false)}>
              Cancel
            </Button>
            <Button textColor="#7c3aed" onPress={() => void handleSave()} disabled={saving || !name.trim()}>
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
  headerRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  headerTitle: { color: '#cdd6f4', fontWeight: 'bold' },
  headerSubtext: { color: '#a6adc8', marginTop: 2 },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  cardTitle: { color: '#7c3aed', fontSize: 13, fontWeight: '700', marginBottom: 10 },
  bodyText: { color: '#cdd6f4', fontSize: 13, lineHeight: 19 },
  metaText: { color: '#a6adc8', fontSize: 13, marginBottom: 6 },
  colorRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 10 },
  colorDot: {
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 1,
    borderColor: '#cdd6f4',
  },
  memberRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 6,
    borderBottomWidth: 1,
    borderBottomColor: '#313244',
  },
  memberName: { color: '#cdd6f4', fontSize: 13, flex: 1 },
  memberRole: { color: '#89b4fa', fontSize: 12, marginLeft: 8 },
  dialog: { backgroundColor: '#1e1e2e' },
  dialogTitle: { color: '#cdd6f4' },
  input: { marginBottom: 12 },
  switchRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginTop: 4,
  },
  switchLabel: { color: '#cdd6f4', fontSize: 14 },
});
