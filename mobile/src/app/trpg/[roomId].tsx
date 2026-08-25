import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { goBackOrReplace } from "../../lib/navigation";
import { Button, Chip, IconButton, Surface, Text, TextInput } from 'react-native-paper';
import {
  mapTrpgPlayEvent,
  mapTrpgPlayParticipant,
  mapTrpgPlaySession,
  mergeTrpgRoomSnapshot,
  trpgApi,
} from '../../lib/trpg-api';
import type {
  TrpgPlayParticipant,
  TrpgPlaySession,
  TrpgPrivateStateEntry,
} from '../../lib/trpg-api';
import { TrpgWebSocket } from '../../lib/trpg-websocket';
import type {
  TrpgImageSettings,
  TrpgLog,
  TrpgParticipant,
  TrpgPrivateMessage,
  TrpgPrivateState,
  TrpgReferenceBundle,
  TrpgReferenceStats,
  TrpgRulesetProfile,
  TrpgRoom,
} from '../../types/api';

const ACTION_KINDS = ['action', 'speech', 'ooc'] as const;
const JOIN_ROLES = ['player', 'observer'] as const;

type TrpgWsPayload = {
  type?: string;
  canonical?: boolean;
  room?: TrpgRoom;
  session?: TrpgPlaySession;
  participant?: Record<string, unknown>;
  participant_id?: string;
  participants?: Array<Record<string, unknown>>;
  private_state?: {
    participant_id?: string;
    state?: Record<string, unknown>;
    updated_at?: string | null;
  };
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

function privateStateEntries(state: Record<string, unknown> | null | undefined): Record<string, TrpgPrivateStateEntry> {
  const entries = state?.entries;
  return entries && typeof entries === 'object' && !Array.isArray(entries)
    ? entries as Record<string, TrpgPrivateStateEntry>
    : {};
}

function targetLabel(targetId: string, participants: TrpgParticipant[]): string {
  return participants.find((participant) => participant.id === targetId)?.display_name || 'Unknown';
}

function extractMentionTargets(text: string, participants: TrpgParticipant[]): string[] {
  const targets = new Set<string>();
  for (const match of text.matchAll(/@([^\s@]+)/g)) {
    const token = match[1].replace(/[、,。.!?！？]$/, '').toLowerCase();
    if (['gm', 'aigm', 'ai', 'ゲームマスター'].includes(token)) {
      const gm = participants.find((participant) => participant.role === 'gm');
      if (gm) targets.add(gm.id);
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
  const [privateStates, setPrivateStates] = useState<Record<string, {
    state: Record<string, unknown>;
    display_name?: string | null;
    updated_at?: string | null;
  }>>({});
  const [ownPrivateState, setOwnPrivateState] = useState<TrpgPrivateState | null>(null);
  const [privateStateKey, setPrivateStateKey] = useState('');
  const [privateStateValue, setPrivateStateValue] = useState('');
  const [privateStateSharedWithGm, setPrivateStateSharedWithGm] = useState(false);
  const [privateStateBusy, setPrivateStateBusy] = useState(false);
  const [imagePrompt, setImagePrompt] = useState('');
  const [imageSettingsStyle, setImageSettingsStyle] = useState('');
  const [imageBusy, setImageBusy] = useState(false);
  const [rulesets, setRulesets] = useState<TrpgRulesetProfile[]>([]);
  const [selectedRuleset, setSelectedRuleset] = useState('');
  const [referenceQuery, setReferenceQuery] = useState('');
  const [referenceBundle, setReferenceBundle] = useState<TrpgReferenceBundle | null>(null);
  const [referenceStats, setReferenceStats] = useState<TrpgReferenceStats | null>(null);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [gmRequest, setGmRequest] = useState('');
  const wsRef = useRef<TrpgWebSocket | null>(null);
  const canonicalSnapshotSeenRef = useRef(false);

  const storageKey = useMemo(() => `trpg-participant-${roomId}`, [roomId]);

  const applySnapshot = useCallback((snapshot: TrpgRoom, canonical = false) => {
    if (canonical) canonicalSnapshotSeenRef.current = true;
    setRoom((current) => mergeTrpgRoomSnapshot(current, snapshot));
    setLogs((current) => {
      if (Array.isArray(snapshot.logs)) return snapshot.logs;
      if (Array.isArray(snapshot.recent_events)) return snapshot.recent_events;
      return current;
    });
  }, []);

  const applyCanonicalSession = useCallback((session: TrpgPlaySession) => {
    applySnapshot(mapTrpgPlaySession(session), true);
  }, [applySnapshot]);

  const load = useCallback(async () => {
    if (!roomId) return;
    try {
      const snapshot = await trpgApi.getRoom(roomId, inviteCode);
      applySnapshot(snapshot, true);
    } catch (error) {
      // The canonical detail endpoint is participant-only.  A user arriving
      // with a session ID + invite code is expected to see the join form until
      // the join POST succeeds, so do not turn that pre-join 403 into an
      // unhandled promise rejection.
      console.warn('TRPG room load failed (pre-join is allowed)', error);
    }
  }, [applySnapshot, inviteCode, roomId]);

  const loadPrivateMessages = useCallback(async () => {
    if (!roomId || !participantId) {
      setPrivateMessages([]);
      return;
    }
    try {
      const messages = await trpgApi.listPrivateMessages(roomId);
      setPrivateMessages(messages);
    } catch (error) {
      console.warn('TRPG private messages load failed', error);
    }
  }, [participantId, roomId]);

  const requestSync = useCallback(() => {
    wsRef.current?.requestSync();
  }, []);

  useEffect(() => {
    let active = true;
    void AsyncStorage.getItem(storageKey).then((value) => {
      if (active) setParticipantId(value);
    });
    return () => {
      active = false;
    };
  }, [storageKey]);

  useEffect(() => {
    // The canonical WebSocket authorizes the user as an existing participant.
    // Do not connect while the join form is still pre-join.
    if (!roomId || !participantId) {
      setIsConnected(false);
      return;
    }

    let cancelled = false;
    const ws = new TrpgWebSocket();
    wsRef.current = ws;

    ws.setOnConnectionChange((connected) => {
      if (!cancelled) setIsConnected(connected);
    });

    ws.setOnMessage((payload) => {
      if (cancelled) return;
      const message = payload as TrpgWsPayload;

      switch (message.type) {
        case 'snapshot':
        case 'sync':
        case 'ended':
          if (message.session) {
            applyCanonicalSession(message.session);
          } else if (message.room) {
            applySnapshot(message.room, Boolean(message.canonical));
          }
          break;
        case 'room':
        case 'state_sync':
          if (!canonicalSnapshotSeenRef.current && message.room) applySnapshot(message.room);
          break;
        case 'join': {
          const participant = message.participant
            ? mapTrpgPlayParticipant(message.participant as unknown as TrpgPlayParticipant)
            : null;
          if (!participant) break;
          setRoom((current) => {
            if (!current) return current;
            const existing = current.participants ?? [];
            return {
              ...current,
              participants: [...existing.filter((item) => item.id !== participant.id), participant],
            };
          });
          break;
        }
        case 'leave': {
          const incomingParticipants = Array.isArray(message.participants)
            ? message.participants.map((item) => mapTrpgPlayParticipant(item as unknown as TrpgPlayParticipant))
            : null;
          const leftId = message.participant_id;
          setRoom((current) => {
            if (!current) return current;
            const participants = incomingParticipants ?? (current.participants ?? []).map((item) =>
              item.id === leftId ? { ...item, left_at: item.left_at ?? new Date().toISOString(), is_active_participant: false } : item,
            );
            return { ...current, participants };
          });
          if (leftId && leftId === participantId) {
            setPrivateStates({});
            void AsyncStorage.removeItem(storageKey);
            setParticipantId(null);
          }
          break;
        }
        case 'private_state': {
          const privateState = message.private_state;
          const stateOwner = privateState?.participant_id;
          const nextState = privateState?.state;
          if (!stateOwner || !nextState) break;
          if (stateOwner === participantId) {
            setOwnPrivateState((current) => ({
              id: current?.id ?? `ws-${stateOwner}`,
              session_id: current?.session_id ?? roomId,
              participant_id: stateOwner,
              state: nextState,
              created_at: current?.created_at,
              updated_at: privateState.updated_at,
            }));
            break;
          }
          setPrivateStates((current) => ({
            ...current,
            [stateOwner]: {
              state: nextState,
              display_name: room?.participants?.find((item) => item.id === stateOwner)?.display_name,
              updated_at: privateState.updated_at,
            },
          }));
          break;
        }
        case 'log':
          if (message.log) {
            setLogs((current) => {
              const nextLog = message.log as TrpgLog;
              const filtered = current.filter((log) => log.id !== nextLog.id);
              return [...filtered, nextLog];
            });
          }
          break;
        case 'private_message':
          void loadPrivateMessages();
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
          if (canonicalSnapshotSeenRef.current) break;
          if (message.shared_state) {
            setRoom((current) =>
              current
                ? {
                    ...current,
                    snapshot: message.shared_state,
                    shared_state: message.shared_state,
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
      if (wsRef.current === ws) wsRef.current = null;
    };
  }, [applyCanonicalSession, applySnapshot, inviteCode, load, loadPrivateMessages, participantId, requestSync, roomId, storageKey]);

  useEffect(() => {
    void loadPrivateMessages();
  }, [loadPrivateMessages]);

  const currentParticipant =
    room?.participants?.find((participant) => participant.id === participantId) ?? null;
  const isSpectator = currentParticipant?.role === 'spectator' || currentParticipant?.role === 'observer';
  const canPlay = Boolean(room?.status === 'active' && currentParticipant && !isSpectator);
  const canManage =
    Boolean(currentParticipant) &&
    !isSpectator &&
    (currentParticipant?.role === 'gm' || room?.host_user_id === currentParticipant?.user_id);
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
        (participant) =>
          participant.id !== participantId && participant.role !== 'npc' && participant.role !== 'gm',
      ),
    [activeParticipants, participantId],
  );

  const loadPrivateState = useCallback(async () => {
    if (!roomId || !participantId) {
      setOwnPrivateState(null);
      setPrivateStates({});
      return;
    }
    try {
      const own = await trpgApi.getPrivateState(roomId);
      setOwnPrivateState(own);
      if (currentParticipant?.role === 'gm') {
        const visible = await trpgApi.listGmPrivateStates(roomId);
        setPrivateStates(
          Object.fromEntries(
            visible.map((item) => [item.participant_id, {
              state: item.state,
              display_name: item.display_name,
              updated_at: item.updated_at,
            }]),
          ),
        );
      } else {
        setPrivateStates({});
      }
    } catch (error) {
      console.warn('TRPG private state load failed', error);
    }
  }, [currentParticipant?.role, participantId, roomId]);

  useEffect(() => {
    void loadPrivateState();
  }, [loadPrivateState]);

  useEffect(() => {
    let active = true;
    void trpgApi.listRulesets().then((items) => {
      if (!active) return;
      setRulesets(items);
      setSelectedRuleset((current) => current || items[0]?.key || items[0]?.ruleset_key || '');
    }).catch((error) => {
      console.warn('TRPG ruleset load failed', error);
    });
    return () => {
      active = false;
    };
  }, [roomId]);

  useEffect(() => {
    if (room?.image_settings?.style !== undefined && !imageBusy) {
      setImageSettingsStyle(String(room.image_settings.style || ''));
    }
  }, [imageBusy, room?.image_settings?.style]);

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
        target_participant_ids: targets,
        content: privateText.trim(),
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

  const handleEnd = async () => {
    if (!roomId || !canManage || room?.status === 'ended') return;
    try {
      const ended = await trpgApi.endSession(roomId);
      applySnapshot(ended, true);
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'End failed');
    }
  };

  const handlePrivateStateUpdate = async () => {
    if (!roomId || !participantId || !privateStateKey.trim()) return;
    const key = privateStateKey.trim();
    const entries = privateStateEntries(ownPrivateState?.state);
    const nextEntries: Record<string, TrpgPrivateStateEntry> = {
      ...entries,
      [key]: {
        value: parseStateValue(privateStateValue),
        shared_with_gm: privateStateSharedWithGm,
      },
    };
    try {
      setPrivateStateBusy(true);
      const updated = await trpgApi.updatePrivateState(roomId, { entries: nextEntries });
      setOwnPrivateState(updated);
      setPrivateStateKey('');
      setPrivateStateValue('');
      await loadPrivateState();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Private state update failed');
    } finally {
      setPrivateStateBusy(false);
    }
  };

  const handleImageSettingsUpdate = async (patch: TrpgImageSettings) => {
    if (!roomId || !canManage) return;
    try {
      const updated = await trpgApi.updateImageSettings(roomId, patch);
      applySnapshot(updated, true);
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Image settings update failed');
    }
  };

  const handleGenerateImage = async () => {
    if (!roomId || !canPlay || imageBusy) return;
    try {
      setImageBusy(true);
      const result = await trpgApi.generateSessionImage(roomId, imagePrompt);
      const generated = mapTrpgPlayEvent(result.event, roomId);
      setLogs((current) => [...current.filter((item) => item.id !== generated.id), generated]);
      setImagePrompt('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Image generation failed');
    } finally {
      setImageBusy(false);
    }
  };

  const handleReferenceSearch = async () => {
    if (!selectedRuleset || referenceBusy) return;
    try {
      setReferenceBusy(true);
      const [bundle, stats] = await Promise.all([
        trpgApi.searchRuleReferences(selectedRuleset, {
          query: referenceQuery.trim(),
          limit: 20,
        }),
        trpgApi.getRuleReferenceStats(selectedRuleset),
      ]);
      setReferenceBundle(bundle);
      setReferenceStats(stats);
    } catch (error) {
      Alert.alert('TRPG References', error instanceof Error ? error.message : 'Reference search failed');
    } finally {
      setReferenceBusy(false);
    }
  };

  const handleAdvance = async () => {
    if (!roomId || !participantId || !gmRequest.trim()) return;
    try {
      await trpgApi.submitAction(roomId, participantId, gmRequest.trim(), 'speech');
      setGmRequest('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Advance failed');
    }
  };

  const handleSharedStateUpdate = async () => {
    if (!roomId || !sharedStateKey.trim()) return;
    try {
      const updated = await trpgApi.updateSharedState(roomId, {
        [sharedStateKey.trim()]: parseStateValue(sharedStateValue),
      }, room?.snapshot ?? room?.shared_state ?? {});
      if (updated) applySnapshot(updated, true);
      setSharedStateKey('');
      setSharedStateValue('');
      requestSync();
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'State update failed');
    }
  };

  const handleLeave = async () => {
    if (!roomId) return;
    try {
      await trpgApi.leaveRoom(roomId);
      await AsyncStorage.removeItem(storageKey);
      setParticipantId(null);
      router.replace('/trpg');
    } catch (error) {
      Alert.alert('TRPG', error instanceof Error ? error.message : 'Leave failed');
    }
  };

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, '/trpg')} />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              {room?.title || room?.room_title || 'TRPG Room'}
            </Text>
            <Text style={styles.headerSubtext}>
              {room?.invite_code || room?.room_code ? `- ${room.invite_code || room.room_code}` : ''}{' '}
              {isConnected ? 'live' : 'reconnecting'}
            </Text>
          </View>
          <IconButton icon="refresh" iconColor="#89b4fa" onPress={() => void load()} />
          <Button
            mode="outlined"
            textColor="#f38ba8"
            compact
            onPress={() => void handleEnd()}
            disabled={!canManage || room?.status === 'ended'}
          >
            End
          </Button>
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
            <View>
              <Text style={styles.currentParticipantText}>
                You are {currentParticipant.display_name} ({currentParticipant.role})
              </Text>
              {isSpectator ? (
                <Text style={styles.currentParticipantText}>Spectator mode: read-only play actions.</Text>
              ) : null}
              <Button mode="outlined" onPress={() => void handleLeave()}>
                Leave Room
              </Button>
            </View>
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
              disabled={!canPlay}
            >
              Send
            </Button>
            <Button mode="outlined" textColor="#89b4fa" onPress={handleStart} disabled={!canManage}>
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
              selected={privateTargets.includes(activeParticipants.find((participant) => participant.role === 'gm')?.id ?? '')}
              onPress={() => {
                const gm = activeParticipants.find((participant) => participant.role === 'gm');
                if (gm) togglePrivateTarget(gm.id);
              }}
              disabled={!activeParticipants.some((participant) => participant.role === 'gm')}
              style={[
                styles.targetChip,
                privateTargets.includes(activeParticipants.find((participant) => participant.role === 'gm')?.id ?? '') &&
                  styles.targetChipActive,
              ]}
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
            disabled={!canPlay || !privateText.trim() || privateBusy}
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
          <Button mode="outlined" textColor="#89b4fa" onPress={handleAdvance} disabled={!canPlay}>
            GM Advance
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Dice</Text>
          <TextInput mode="outlined" label="Expression" value={diceExpr} onChangeText={setDiceExpr} style={styles.input} />
          <TextInput mode="outlined" label="Target (optional)" value={diceTarget} onChangeText={setDiceTarget} keyboardType="numeric" style={styles.input} />
          <TextInput mode="outlined" label="Note (optional)" value={diceNote} onChangeText={setDiceNote} style={styles.input} />
          <Button mode="outlined" textColor="#89b4fa" onPress={handleRoll} disabled={!canPlay}>
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
            disabled={!canManage || !sharedStateKey.trim()}
          >
            Update State
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Private State</Text>
          {Object.entries(privateStateEntries(ownPrivateState?.state)).map(([key, entry]) => (
            <View key={key} style={styles.stateRow}>
              <Text style={styles.stateKey}>{key}{entry.shared_with_gm ? ' · shared with GM' : ''}</Text>
              <Text style={styles.stateValue}>{formatValue(entry.value)}</Text>
            </View>
          ))}
          {ownPrivateState && Object.keys(privateStateEntries(ownPrivateState.state)).length === 0 ? (
            <Text style={styles.emptyText}>No private state yet.</Text>
          ) : null}
          {!participantId ? <Text style={styles.emptyText}>Join the room to edit private state.</Text> : null}
          <TextInput
            mode="outlined"
            label="Private State Key"
            value={privateStateKey}
            onChangeText={setPrivateStateKey}
            style={styles.input}
            disabled={!participantId}
          />
          <TextInput
            mode="outlined"
            label="Private State Value"
            value={privateStateValue}
            onChangeText={setPrivateStateValue}
            style={styles.input}
            disabled={!participantId}
          />
          <Chip
            selected={privateStateSharedWithGm}
            onPress={() => setPrivateStateSharedWithGm((value) => !value)}
            style={[styles.targetChip, privateStateSharedWithGm && styles.targetChipActive]}
            textStyle={styles.kindChipText}
            disabled={!participantId}
          >
            Share with GM
          </Chip>
          <Button
            mode="outlined"
            textColor="#89b4fa"
            onPress={handlePrivateStateUpdate}
            disabled={!participantId || !privateStateKey.trim() || privateStateBusy}
          >
            Save Private State
          </Button>
          {currentParticipant?.role === 'gm' ? (
            <View style={styles.gmPrivateStateList}>
              <Text style={styles.stateKey}>GM-visible shared states</Text>
              {Object.entries(privateStates).map(([ownerId, privateState]) => (
                <View key={ownerId} style={styles.stateRow}>
                  <Text style={styles.stateKey}>
                    {privateState.display_name || targetLabel(ownerId, room?.participants || [])}
                  </Text>
                  <Text style={styles.stateValue}>{formatValue(privateState.state)}</Text>
                </View>
              ))}
              {Object.keys(privateStates).length === 0 ? (
                <Text style={styles.emptyText}>No participant state shared with GM.</Text>
              ) : null}
            </View>
          ) : null}
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Scene Images</Text>
          <View style={styles.kindRow}>
            <Chip
              selected={Boolean(room?.image_settings?.enabled)}
              onPress={() => void handleImageSettingsUpdate({ enabled: !room?.image_settings?.enabled })}
              style={[styles.kindChip, room?.image_settings?.enabled && styles.kindChipActive]}
              textStyle={styles.kindChipText}
              disabled={!canManage}
            >
              Auto image {room?.image_settings?.enabled ? 'ON' : 'OFF'}
            </Chip>
          </View>
          <TextInput
            mode="outlined"
            label="Style (optional)"
            value={imageSettingsStyle}
            onChangeText={setImageSettingsStyle}
            style={styles.input}
            disabled={!canManage}
          />
          <Button
            mode="outlined"
            textColor="#89b4fa"
            onPress={() => void handleImageSettingsUpdate({ style: imageSettingsStyle })}
            disabled={!canManage}
          >
            Save Image Settings
          </Button>
          <TextInput
            mode="outlined"
            label="Manual image prompt (optional)"
            value={imagePrompt}
            onChangeText={setImagePrompt}
            multiline
            style={styles.input}
            disabled={!canPlay}
          />
          <Button
            mode="contained"
            buttonColor="#7c3aed"
            textColor="#cdd6f4"
            onPress={handleGenerateImage}
            disabled={!canPlay || imageBusy}
          >
            {imageBusy ? 'Generating…' : 'Generate Scene Image'}
          </Button>
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>Ruleset References</Text>
          <View style={styles.targetWrap}>
            {rulesets.map((ruleset) => {
              const key = ruleset.key || ruleset.ruleset_key || '';
              return (
                <Chip
                  key={key}
                  selected={selectedRuleset === key}
                  onPress={() => setSelectedRuleset(key)}
                  style={[styles.targetChip, selectedRuleset === key && styles.targetChipActive]}
                  textStyle={styles.kindChipText}
                >
                  {ruleset.label || ruleset.name || ruleset.display_name || key}
                </Chip>
              );
            })}
          </View>
          <TextInput
            mode="outlined"
            label="Search rules / creatures"
            value={referenceQuery}
            onChangeText={setReferenceQuery}
            style={styles.input}
          />
          <Button
            mode="outlined"
            textColor="#89b4fa"
            onPress={handleReferenceSearch}
            disabled={!selectedRuleset || referenceBusy}
          >
            {referenceBusy ? 'Searching…' : 'Search References'}
          </Button>
          {referenceBundle ? (
            <View style={styles.referenceResults}>
              <Text style={styles.stateKey}>Results · {referenceBundle.count}</Text>
              {referenceStats ? (
                <Text style={styles.stateValue}>Stats · {JSON.stringify(referenceStats)}</Text>
              ) : null}
              {[...referenceBundle.rules, ...referenceBundle.creatures].slice(0, 10).map((item, index) => (
                <Text key={`${String(item.id || item.key || index)}`} style={styles.stateValue}>
                  {String(item.name || item.title || item.key || item.text || 'Reference')}
                </Text>
              ))}
            </View>
          ) : null}
        </Surface>

        <Surface style={styles.panel} elevation={0}>
          <Text style={styles.sectionTitle}>GM Markers</Text>
          {gmMarkers ? (
            <Text style={styles.markerText}>{JSON.stringify(gmMarkers, null, 2)}</Text>
          ) : (
            <Text style={styles.emptyText}>No markers yet.</Text>
          )}
        </Surface>

        {/* Logs remain the canonical event projection; legacy state messages
            never replace this list after a canonical snapshot is received. */}
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

/*
 * Keep the style declarations close to the room screen.  The TRPG controls
 * intentionally use the same compact primitives as the existing room panels
 * so adding canonical capabilities does not change the established layout.
 */
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
  gmPrivateStateList: { marginTop: 12 },
  referenceResults: { marginTop: 12 },
  emptyText: { color: '#a6adc8' },
});
