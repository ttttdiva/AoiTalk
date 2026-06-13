import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import { Button, Chip, IconButton, Surface, Text, TextInput } from 'react-native-paper';
import { trpgApi } from '../../lib/trpg-api';
import { TrpgWebSocket } from '../../lib/trpg-websocket';
import type { TrpgLog, TrpgParticipant, TrpgPrivateMessage, TrpgRoom } from '../../types/api';

const ACTION_KINDS = ['action', 'speech', 'ooc'] as const;
const JOIN_ROLES = ['player', 'observer'] as const;

type TrpgWsPayload = {
  type?: string;
  room?: TrpgRoom;
  log?: TrpgLog;
  shared_state?: Record<string, unknown>;
  markers?: unknown;
};

function parseStateValue(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  try {
    return JSON.parse(trimmed);
  } catch {
    return trimmed;
  }
}

function formatValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function targetLabel(targetId: string, participants: TrpgParticipant[]): string {
  if (targetId === 'gm') return 'AI GM';
  return participants.find((participant) => participant.id === targetId)?.display_name || 'Unknown';
}

function extractMentionTargets(text: string, participants: TrpgParticipant[]): string[] {
  const targets = new Set<string>();
  for (const match of text.matchAll(/@([^\s@]+)/g)) {
    const token = match[1].replace(/[、,。.!?！？]$/, '').toLowerCase();
    if (['gm', 'aigm', 'ai', 'ゲームマスター'].includes(token)) {
      targets.add('gm');
      continue;
    }
    const participant = participants.find((item) => {
      const name = item.display_name.toLowerCase();
      return name === token || name.startsWith(token);
    });
    if (participant) targets.add(participant.id);
  }
  return Array.from(targets);
}

export default function TrpgRoomScreen() {
  const router = useRouter();
  const { roomId, invite_code: inviteCodeParam } = useLocalSearchParams<{
    roomId: string;
    invite_code?: string;
  }>();
  const inviteCode = Array.isArray(inviteCodeParam)
    ? inviteCodeParam[0] ?? ''
    : inviteCodeParam ?? '';
  const [room, setRoom] = useState<TrpgRoom | null>(null);
  const [logs, setLogs] = useState<TrpgLog[]>([]);
  const [privateMessages, setPrivateMessages] = useState<TrpgPrivateMessage[]>([]);
  const [participantId, setParticipantId] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [joinRole, setJoinRole] = useState<(typeof JOIN_ROLES)[number]>('player');
  const [actionText, setActionText] = useState('');
  const [actionKind, setActionKind] = useState<(typeof ACTION_KINDS)[number]>('action');
  const [privateText, setPrivateText] = useState('');
  const [privateTargets, setPrivateTargets] = useState<string[]>([]);
  const [privateBusy, setPrivateBusy] = useState(false);
  const [diceExpr, setDiceExpr] = useState('1d100');
  const [diceTarget, setDiceTarget] = useState('');
  const [diceNote, setDiceNote] = useState('');
  const [isConnected, setIsConnected] = useState(false);
  const [sharedStateKey, setSharedStateKey] = useState('');
  const [sharedStateValue, setSharedStateValue] = useState('');
  const [gmMarkers, setGmMarkers] = useState<unknown>(null);
  const [gmRequest, setGmRequest] = useState('');
  const wsRef = useRef<TrpgWebSocket | null>(null);

  const storageKey = useMemo(() => `trpg-participant-${roomId}`, [roomId]);

  const applySnapshot = useCallback((snapshot: TrpgRoom) => {
    setRoom(snapshot);
    setLogs(Array.isArray(snapshot.logs) ? snapshot.logs : []);
  }, []);

  const load = useCallback(async () => {
    if (!roomId) return;
    const snapshot = await trpgApi.getRoom(roomId, inviteCode);
    applySnapshot(snapshot);
  }, [applySnapshot, inviteCode, roomId]);

  const loadPrivateMessages = useCallback(async () => {
    if (!roomId || !participantId) {
      setPrivateMessages([]);
      return;
    }
    try {
      const messages = await trpgApi.listPrivateMessages(roomId, participantId);
      setPrivateMessages(messages);
    } catch (error) {
      console.warn('TRPG private messages load failed', error);
    }
  }, [participantId, roomId]);

  const requestSync = useCallback(() => {
    wsRef.current?.requestSync();
  }, []);

  useEffect(() => {
    if (!roomId) return;

    let cancelled = false;
    const ws = new TrpgWebSocket();
    wsRef.current = ws;

    void AsyncStorage.getItem(storageKey).then((value) => {
      if (!cancelled && value) {
        setParticipantId(value);
      }
    });

    ws.setOnConnectionChange((connected) => {
      if (!cancelled) {
        setIsConnected(connected);
      }
    });

    ws.setOnMessage((payload) => {
      if (cancelled) return;
      const message = payload as TrpgWsPayload;

      switch (message.type) {
        case 'state_sync':
          if (message.room) {
            applySnapshot(message.room);
          }
          break;
        case 'log_append':
          if (message.log) {
            setLogs((current) => {
              const filtered = current.filter((log) => log.id !== message.log?.id);
              return [...filtered, message.log!];
            });
          }
          requestSync();
          break;
        case 'participant_update':
        case 'turn_change':
        case 'scene_change':
        case 'shared_state':
          if (message.shared_state) {
            setRoom((current) =>
              current
                ? {
                    ...current,
                    shared_state: message.shared_state ?? current.shared_state,
                  }
                : current,
            );
          }
          requestSync();
          break;
        case 'gm_markers':
          setGmMarkers(message.markers ?? null);
          requestSync();
          break;
        case 'private_refresh':
          void loadPrivateMessages();
          break;
        default:
          break;
      }
    });

    void load();
    void ws.connect(roomId, inviteCode);

    return () => {
      cancelled = true;
      ws.disconnect();
      if (wsRef.current === ws) {
        wsRef.current = null;
      }
    };
  }, [applySnapshot, inviteCode, load, loadPrivateMessages, requestSync, roomId, storageKey]);

  useEffect(() => {
    void loadPrivateMessages();
  }, [loadPrivateMessages]);

  const currentParticipant =
    room?.participants?.find((participant) => participant.id === participantId) ?? null;
  const activeParticipants = useMemo(
    () =>
      (room?.participants || [])
        .filter((participant) => participant.is_active_participant !== false)
        .sort((a, b) => (a.seat_index ?? 0) - (b.seat_index ?? 0)),
    [room?.participants],
  );
  const privateTargetParticipants = useMemo(
    () =>
      activeParticipants.filter(
        (participant) => participant.id !== participantId && participant.role !== 'npc',
      ),
    [activeParticipants, participantId],
  );

  const togglePrivateTarget = useCallback((targetId: string) => {
    setPrivateTargets((current) =>
      current.includes(targetId)
        ? current.filter((id) => id !== targetId)
        : [...current, targetId],
    );
  }, []);

  const handleJoin = async () => {
    if (!roomId || !displayName.trim()) return;
    try {
      const participant = await trpgApi.joinRoom(roomId, {
        display_name: displayName.trim(),
        role: joinRole,
        invite_code: inviteCode || undefined,
      });
      await AsyncStorage.setItem(storageKey, participant.id);
      setParticipantId(participant.id);
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Join failed');
    }
  };

  const handleSendPrivateMessage = async () => {
    if (!roomId || !participantId || !privateText.trim()) return;
    const mentioned = extractMentionTargets(privateText, activeParticipants);
    const targets = Array.from(new Set([...privateTargets, ...mentioned])).filter(
      (target) => target !== participantId,
    );
    if (targets.length === 0) {
      Alert.alert('TRPG', 'Select at least one private chat target, or mention someone with @name.');
      return;
    }

    try {
      setPrivateBusy(true);
      await trpgApi.sendPrivateMessage(roomId, {
        sender_participant_id: participantId,
        target_participant_ids: targets,
        content: privateText.trim(),
        message_type: targets.includes('gm') ? 'gm' : 'private',
        request_gm_reply: true,
      });
      setPrivateText('');
      await loadPrivateMessages();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Private message failed');
    } finally {
      setPrivateBusy(false);
    }
  };

  const handleSubmitAction = async () => {
    if (!roomId || !participantId || !actionText.trim()) return;
    try {
      await trpgApi.submitAction(roomId, participantId, actionText.trim(), actionKind);
      setActionText('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Action failed');
    }
  };

  const handleRoll = async () => {
    if (!roomId || !diceExpr.trim()) return;
    try {
      await trpgApi.rollDice(
        roomId,
        participantId,
        diceExpr.trim(),
        diceTarget.trim() ? Number(diceTarget) : null,
        diceNote.trim() || undefined,
      );
      setDiceNote('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Dice roll failed');
    }
  };

  const handleStart = async () => {
    if (!roomId) return;
    try {
      await trpgApi.startSession(roomId);
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Start failed');
    }
  };

  const handleAdvance = async () => {
    if (!roomId) return;
    try {
      await trpgApi.advanceGm(roomId, gmRequest.trim());
      setGmRequest('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Advance failed');
    }
  };

  const handleSharedStateUpdate = async () => {
    if (!roomId || !sharedStateKey.trim()) return;
    try {
      await trpgApi.updateSharedState(roomId, {
        [sharedStateKey.trim()]: parseStateValue(sharedStateValue),
      });
      setSharedStateKey('');
      setSharedStateValue('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'State update failed');
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/trpg')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              {room?.room_title || 'TRPG Room'}
            </Text>
            <Text style={styles.headerSubtext}>
              {room?.scenario?.title || ''} {room?.room_code ? `- ${room.room_code}` : ''}{' '}
              {isConnected ? 'live' : 'reconnecting'}
            </Text>
          </View>
          <IconButton icon="refresh" iconColor="#89b4fa" onPress={() => void load()} />
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Participants</Text>
          <View style={styles.participantWrap}>
            {(room?.participants || []).map((participant: TrpgParticipant) => (
              <Chip
                key={participant.id}
                style={[
                  styles.participantChip,
                  participant.id === participantId && styles.participantChipActive,
                ]}
                textStyle={styles.participantChipText}
              >
                {participant.display_name} - {participant.role}
              </Chip>
            ))}
          </View>
          {!currentParticipant ? (
            <View style={styles.joinBox}>
              <TextInput
                mode="outlined"
                label="Display Name"
                value={displayName}
                onChangeText={setDisplayName}
                style={styles.input}
              />
              <View style={styles.kindRow}>
                {JOIN_ROLES.map((role) => (
                  <Chip
                    key={role}
                    selected={joinRole === role}
                    onPress={() => setJoinRole(role)}
                    style={[styles.kindChip, joinRole === role && styles.kindChipActive]}
                    textStyle={styles.kindChipText}
                  >
                    {role}
                  </Chip>
                ))}
              </View>
              <Button
                mode="contained"
                buttonColor="#7c3aed"
                textColor="#cdd6f4"
                onPress={handleJoin}
              >
                Join Room
              </Button>
            </View>
          ) : (
            <Text style={styles.currentParticipantText}>
              You are {currentParticipant.display_name} ({currentParticipant.role})
            </Text>
          )}
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Scene</Text>
          <Text style={styles.sceneTitle}>{room?.current_scene?.title || 'No active scene'}</Text>
          {room?.current_scene?.description ? (
            <Text style={styles.sceneBody}>{room.current_scene.description}</Text>
          ) : null}
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Actions</Text>
          <View style={styles.kindRow}>
            {ACTION_KINDS.map((kind) => (
              <Chip
                key={kind}
                selected={actionKind === kind}
                onPress={() => setActionKind(kind)}
                style={[styles.kindChip, actionKind === kind && styles.kindChipActive]}
                textStyle={styles.kindChipText}
              >
                {kind}
              </Chip>
            ))}
          </View>
          <TextInput
            mode="outlined"
            label="Action"
            value={actionText}
            onChangeText={setActionText}
            multiline
            style={styles.input}
          />
          <View style={styles.buttonRow}>
            <Button
              mode="contained"
              buttonColor="#7c3aed"
              textColor="#cdd6f4"
              onPress={handleSubmitAction}
              disabled={!participantId}
            >
              Send
            </Button>
            <Button mode="outlined" textColor="#89b4fa" onPress={handleStart}>
              Start
            </Button>
          </View>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Private Chat</Text>
          <View style={styles.privateMessageList}>
            {privateMessages.map((message) => {
              const mine = message.sender_participant_id === participantId;
              return (
                <View
                  key={message.id}
                  style={[styles.privateMessageRow, mine && styles.privateMessageMine]}
                >
                  <View style={styles.privateMessageHeader}>
                    <Text style={styles.privateSender}>{message.sender_label || 'AI GM'}</Text>
                    <Text style={styles.privateTargets}>
                      To {message.target_participant_ids.map((id) => targetLabel(id, room?.participants || [])).join(', ')}
                    </Text>
                  </View>
                  <Text style={styles.privateMessageText}>{message.content}</Text>
                </View>
              );
            })}
            {participantId && privateMessages.length === 0 ? (
              <Text style={styles.emptyText}>No private messages yet.</Text>
            ) : null}
            {!participantId ? (
              <Text style={styles.emptyText}>Join the room to use private chat.</Text>
            ) : null}
          </View>
          <View style={styles.targetWrap}>
            <Chip
              selected={privateTargets.includes('gm')}
              onPress={() => togglePrivateTarget('gm')}
              style={[styles.targetChip, privateTargets.includes('gm') && styles.targetChipActive]}
              textStyle={styles.kindChipText}
            >
              AI GM
            </Chip>
            {privateTargetParticipants.map((participant) => (
              <Chip
                key={participant.id}
                selected={privateTargets.includes(participant.id)}
                onPress={() => togglePrivateTarget(participant.id)}
                style={[
                  styles.targetChip,
                  privateTargets.includes(participant.id) && styles.targetChipActive,
                ]}
                textStyle={styles.kindChipText}
              >
                {participant.display_name}
              </Chip>
            ))}
          </View>
          <TextInput
            mode="outlined"
            label="Private message"
            value={privateText}
            onChangeText={setPrivateText}
            multiline
            style={styles.input}
          />
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            onPress={handleSendPrivateMessage}
            disabled={!participantId || !privateText.trim() || privateBusy}
          >
            Send Private
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>GM Controls</Text>
          <TextInput
            mode="outlined"
            label="Advance Request"
            value={gmRequest}
            onChangeText={setGmRequest}
            multiline
            style={styles.input}
          />
          <Button mode="outlined" textColor="#89b4fa" onPress={handleAdvance}>
            GM Advance
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Dice</Text>
          <TextInput mode="outlined" label="Expression" value={diceExpr} onChangeText={setDiceExpr} style={styles.input} />
          <TextInput mode="outlined" label="Target (optional)" value={diceTarget} onChangeText={setDiceTarget} keyboardType="numeric" style={styles.input} />
          <TextInput mode="outlined" label="Note (optional)" value={diceNote} onChangeText={setDiceNote} style={styles.input} />
          <Button mode="outlined" textColor="#89b4fa" onPress={handleRoll}>
            Roll
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Shared State</Text>
          {Object.entries(room?.shared_state || {}).map(([key, value]) => (
            <View key={key} style={styles.stateRow}>
              <Text style={styles.stateKey}>{key}</Text>
              <Text style={styles.stateValue}>{formatValue(value)}</Text>
            </View>
          ))}
          {room?.shared_state && Object.keys(room.shared_state).length === 0 ? (
            <Text style={styles.emptyText}>No shared state yet.</Text>
          ) : null}
          <TextInput mode="outlined" label="State Key" value={sharedStateKey} onChangeText={setSharedStateKey} style={styles.input} />
          <TextInput mode="outlined" label="State Value" value={sharedStateValue} onChangeText={setSharedStateValue} style={styles.input} />
          <Button
            mode="outlined"
            textColor="#89b4fa"
            onPress={handleSharedStateUpdate}
            disabled={!sharedStateKey.trim()}
          >
            Update State
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>GM Markers</Text>
          {gmMarkers ? (
            <Text style={styles.markerText}>{JSON.stringify(gmMarkers, null, 2)}</Text>
          ) : (
            <Text style={styles.emptyText}>No markers yet.</Text>
          )}
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Logs</Text>
          {logs.map((log) => (
            <View key={log.id} style={styles.logRow}>
              <Text style={styles.logType}>{log.log_type}</Text>
              <Text style={styles.logText}>{log.content || JSON.stringify(log.metadata || {})}</Text>
            </View>
          ))}
          {logs.length === 0 ? <Text style={styles.emptyText}>No logs yet.</Text> : null}
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
  panel: { backgroundColor: '#1e1e2e', borderRadius: 12, padding: 16 },
  sectionTitle: { color: '#7c3aed', fontSize: 13, fontWeight: '700', marginBottom: 10 },
  participantWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  participantChip: { backgroundColor: '#313244' },
  participantChipActive: { backgroundColor: '#4c1d95' },
  participantChipText: { color: '#cdd6f4' },
  sceneTitle: { color: '#cdd6f4', fontSize: 16, fontWeight: '700' },
  sceneBody: { color: '#a6adc8', marginTop: 8, lineHeight: 20 },
  joinBox: { marginTop: 12 },
  currentParticipantText: { color: '#a6adc8', marginTop: 8 },
  input: { marginBottom: 12 },
  kindRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  kindChip: { backgroundColor: '#313244' },
  kindChipActive: { backgroundColor: '#4c1d95' },
  kindChipText: { color: '#cdd6f4' },
  targetWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  targetChip: { backgroundColor: '#313244' },
  targetChipActive: { backgroundColor: '#4c1d95' },
  privateMessageList: { gap: 8, marginBottom: 12 },
  privateMessageRow: {
    backgroundColor: '#181825',
    borderColor: '#313244',
    borderRadius: 10,
    borderWidth: 1,
    padding: 10,
  },
  privateMessageMine: { backgroundColor: '#2e244f', borderColor: '#7c3aed' },
  privateMessageHeader: { gap: 4, marginBottom: 6 },
  privateSender: { color: '#cdd6f4', fontWeight: '700' },
  privateTargets: { color: '#89b4fa', fontSize: 11 },
  privateMessageText: { color: '#cdd6f4', lineHeight: 19 },
  buttonRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  stateRow: { paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: '#313244' },
  stateKey: { color: '#89b4fa', fontSize: 12, textTransform: 'uppercase' },
  stateValue: { color: '#cdd6f4', marginTop: 4 },
  markerText: { color: '#cdd6f4', fontFamily: 'monospace' },
  logRow: { paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: '#313244' },
  logType: { color: '#7c3aed', fontSize: 11, textTransform: 'uppercase' },
  logText: { color: '#cdd6f4', fontSize: 13, marginTop: 3 },
  emptyText: { color: '#a6adc8' },
});
