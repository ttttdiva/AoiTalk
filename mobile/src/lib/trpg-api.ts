import { fetchApi } from './api-client';
import type {
  TrpgGmPrivateState,
  TrpgImageSettings,
  TrpgLog,
  TrpgParticipant,
  TrpgPrivateMessage,
  TrpgPrivateState,
  TrpgReferenceBundle,
  TrpgReferenceStats,
  TrpgRulesetProfile,
  TrpgRoom,
} from '../types/api';

// Keep the historical lib import path as a type-only re-export while the
// definitions themselves live in the shared API contract module.
export type {
  TrpgGmPrivateState,
  TrpgImageSettings,
  TrpgLog,
  TrpgParticipant,
  TrpgPrivateMessage,
  TrpgPrivateState,
  TrpgReferenceBundle,
  TrpgReferenceStats,
  TrpgRulesetProfile,
  TrpgRoom,
} from '../types/api';

export type TrpgPlayParticipant = {
  id: string;
  session_id: string;
  user_id?: string | null;
  display_name: string;
  role: string;
  story_character_id?: string | null;
  is_npc?: boolean;
  joined_at?: string | null;
  left_at?: string | null;
};

export type TrpgPlayEvent = {
  id: string;
  session_id: string;
  actor_participant_id?: string | null;
  kind: string;
  body: string;
  meta?: Record<string, unknown> | null;
  created_at?: string | null;
  actor_display_name?: string | null;
};

export type TrpgPlayWhisper = {
  id: string;
  session_id: string;
  sender_participant_id: string;
  body: string;
  recipient_participant_ids: string[];
  created_at?: string | null;
};

export type TrpgImageGenerationResult = {
  event: TrpgPlayEvent;
  media_id?: string | null;
};

export type TrpgPrivateStateEntry = {
  value?: unknown;
  shared_with_gm?: boolean;
};

export type TrpgPrivateStatePayload = Omit<TrpgPrivateState, 'state'> & {
  state: {
    entries?: Record<string, TrpgPrivateStateEntry>;
    [key: string]: unknown;
  };
};

export type TrpgReferenceSearchOptions = {
  query?: string;
  kind?: string;
  mechanicKey?: string;
  ruleDomain?: string;
  creatureType?: string;
  limit?: number;
};

export type TrpgPlaySession = {
  id: string;
  work_id: string;
  host_user_id: string;
  title: string;
  gm_mode: string;
  status: string;
  invite_code?: string | null;
  snapshot?: Record<string, unknown> | null;
  image_settings?: TrpgImageSettings | null;
  participants?: TrpgPlayParticipant[];
  recent_events?: TrpgPlayEvent[];
  created_at?: string | null;
  updated_at?: string | null;
  ended_at?: string | null;
};

export function mapTrpgPlayParticipant(participant: TrpgPlayParticipant): TrpgParticipant {
  return {
    ...participant,
    play_session_id: participant.session_id,
    character_id: participant.story_character_id,
    is_active_participant: participant.left_at == null,
  };
}

export function mapTrpgPlayEvent(event: TrpgPlayEvent, sessionId?: string): TrpgLog {
  return {
    id: event.id,
    play_session_id: sessionId ?? event.session_id,
    participant_id: event.actor_participant_id,
    log_type: event.kind,
    content: event.body,
    metadata: event.meta,
    created_at: event.created_at,
    participant_name: event.actor_display_name,
  };
}

export function mapTrpgPlaySession(session: TrpgPlaySession): TrpgRoom {
  const hasParticipants = Array.isArray(session.participants);
  const hasRecentEvents = Array.isArray(session.recent_events);
  const logs = hasRecentEvents
    ? (session.recent_events ?? []).map((event) => mapTrpgPlayEvent(event, session.id))
    : undefined;
  const participants = hasParticipants
    ? (session.participants ?? []).map(mapTrpgPlayParticipant)
    : undefined;
  const hasSnapshot = Object.prototype.hasOwnProperty.call(session, 'snapshot');
  return {
    id: session.id,
    work_id: session.work_id,
    title: session.title,
    room_code: session.invite_code || '',
    room_title: session.title,
    status: session.status,
    gm_mode: session.gm_mode,
    host_user_id: session.host_user_id,
    invite_code: session.invite_code,
    ...(Object.prototype.hasOwnProperty.call(session, 'image_settings')
      ? { image_settings: session.image_settings }
      : {}),
    ...(hasSnapshot
      ? { snapshot: session.snapshot, shared_state: session.snapshot }
      : {}),
    ...(participants ? { participants } : {}),
    ...(logs ? { recent_events: logs, logs } : {}),
    created_at: session.created_at,
    updated_at: session.updated_at,
    ended_at: session.ended_at,
  };
}

/**
 * A snapshot event can omit participant/event projections because the backend
 * broadcasts the session row after a snapshot-only mutation. Preserve those
 * projections from the last complete room detail instead of clearing them.
 */
export function mergeTrpgRoomSnapshot(
  current: TrpgRoom | null,
  incoming: TrpgRoom,
): TrpgRoom {
  if (!current) return incoming;

  const participants = incoming.participants ?? current.participants;
  const logs = incoming.logs ?? incoming.recent_events ?? current.logs ?? current.recent_events;
  const recentEvents = incoming.recent_events ?? incoming.logs ?? current.recent_events ?? current.logs;
  const snapshot = incoming.snapshot !== undefined ? incoming.snapshot : current.snapshot;
  const sharedState = incoming.shared_state !== undefined
    ? incoming.shared_state
    : snapshot !== undefined
      ? snapshot
      : current.shared_state;
  const definedIncoming = Object.fromEntries(
    Object.entries(incoming).filter(([, value]) => value !== undefined),
  ) as TrpgRoom;

  return {
    ...current,
    ...definedIncoming,
    ...(participants !== undefined ? { participants } : {}),
    ...(logs !== undefined ? { logs } : {}),
    ...(recentEvents !== undefined ? { recent_events: recentEvents } : {}),
    ...(snapshot !== undefined ? { snapshot } : {}),
    ...(sharedState !== undefined ? { shared_state: sharedState } : {}),
  };
}

type CreateSessionPayload = {
  work_id: string;
  gm_mode?: 'ai' | 'human';
  title?: string;
};

type JoinSessionPayload = {
  invite_code: string;
  display_name: string;
  role?: string;
  story_character_id?: string | null;
};

function withQuery(
  path: string,
  values: Record<string, string | number | boolean | null | undefined>,
): string {
  const query = Object.entries(values)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join('&');
  return query ? `${path}?${query}` : path;
}

function mapTrpgPrivateState(payload: TrpgPrivateStatePayload): TrpgPrivateStatePayload {
  const state = payload.state && typeof payload.state === 'object'
    ? payload.state
    : { entries: {} };
  return {
    ...payload,
    state: {
      ...state,
      entries: state.entries && typeof state.entries === 'object' ? state.entries : {},
    },
  };
}

export const trpgApi = {
  async listRooms(): Promise<TrpgRoom[]> {
    const data = await fetchApi<{ sessions: TrpgPlaySession[] }>('/api/trpg/sessions');
    return data.sessions.map(mapTrpgPlaySession);
  },

  async listRulesets(includeDisabled = false): Promise<TrpgRulesetProfile[]> {
    const data = await fetchApi<{ rulesets?: TrpgRulesetProfile[] }>(
      withQuery('/api/trpg/rulesets', { include_disabled: includeDisabled || undefined }),
    );
    return Array.isArray(data.rulesets) ? data.rulesets : [];
  },

  /** Alias kept explicit for callers that use the reference-domain name. */
  async listReferenceRulesets(includeDisabled = false): Promise<TrpgRulesetProfile[]> {
    return trpgApi.listRulesets(includeDisabled);
  },

  async searchRuleReferences(
    rulesetKey: string,
    options: TrpgReferenceSearchOptions = {},
  ): Promise<TrpgReferenceBundle> {
    return fetchApi<TrpgReferenceBundle>(
      withQuery(`/api/trpg/rulesets/${encodeURIComponent(rulesetKey)}/references`, {
        query: options.query,
        kind: options.kind,
        mechanic_key: options.mechanicKey,
        rule_domain: options.ruleDomain,
        creature_type: options.creatureType,
        limit: options.limit,
      }),
    );
  },

  /** Short alias for reference search UI code. */
  async searchReferences(
    rulesetKey: string,
    options: TrpgReferenceSearchOptions = {},
  ): Promise<TrpgReferenceBundle> {
    return trpgApi.searchRuleReferences(rulesetKey, options);
  },

  async getRuleReferenceStats(rulesetKey: string): Promise<TrpgReferenceStats> {
    return fetchApi<TrpgReferenceStats>(
      `/api/trpg/rulesets/${encodeURIComponent(rulesetKey)}/reference-stats`,
    );
  },

  async getReferenceStats(rulesetKey: string): Promise<TrpgReferenceStats> {
    return trpgApi.getRuleReferenceStats(rulesetKey);
  },

  async getRoom(sessionId: string, inviteCode?: string | null): Promise<TrpgRoom> {
    const session = await fetchApi<TrpgPlaySession>(`/api/trpg/sessions/${sessionId}`);
    if (inviteCode && session.invite_code && inviteCode.toUpperCase() !== session.invite_code.toUpperCase()) {
      throw new Error('招待コードが正しくありません');
    }
    return mapTrpgPlaySession(session);
  },

  async createRoom(payload: { work_id: string; room_title?: string; gm_mode?: 'ai' | 'human' }): Promise<TrpgRoom> {
    const session = await fetchApi<TrpgPlaySession>('/api/trpg/sessions', {
      method: 'POST',
      body: JSON.stringify({
        work_id: payload.work_id,
        gm_mode: payload.gm_mode ?? 'ai',
        title: payload.room_title,
      } satisfies CreateSessionPayload),
    });
    return mapTrpgPlaySession(session);
  },

  async joinRoom(sessionId: string, payload: {
    display_name: string;
    character_id?: string | null;
    role?: string;
    invite_code?: string | null;
  }): Promise<TrpgParticipant> {
    const normalizedRole =
      payload.role === 'observer' || payload.role === 'spectator' ? 'spectator' : payload.role;
    const participant = await fetchApi<TrpgPlayParticipant>(`/api/trpg/sessions/${sessionId}/join`, {
      method: 'POST',
      body: JSON.stringify({
        invite_code: payload.invite_code || '',
        display_name: payload.display_name,
        role: normalizedRole,
        story_character_id: payload.character_id,
      } satisfies JoinSessionPayload),
    });
    return mapTrpgPlayParticipant(participant);
  },

  async listLogs(sessionId: string): Promise<TrpgLog[]> {
    const data = await fetchApi<{ events: TrpgPlayEvent[] }>(`/api/trpg/sessions/${sessionId}/events`);
    return data.events.map((event) => ({
      id: event.id,
      play_session_id: event.session_id,
      participant_id: event.actor_participant_id,
      log_type: event.kind,
      content: event.body,
      metadata: event.meta,
      created_at: event.created_at,
      participant_name: event.actor_display_name,
    }));
  },

  async listPrivateMessages(sessionId: string): Promise<TrpgPrivateMessage[]> {
    const data = await fetchApi<{ whispers: TrpgPlayWhisper[] }>(`/api/trpg/sessions/${sessionId}/whispers`);
    return data.whispers.map((whisper) => ({
      id: whisper.id,
      play_session_id: whisper.session_id,
      sender_participant_id: whisper.sender_participant_id,
      sender_label: whisper.sender_participant_id,
      target_participant_ids: whisper.recipient_participant_ids,
      content: whisper.body,
      created_at: whisper.created_at,
    }));
  },

  async sendPrivateMessage(
    sessionId: string,
    payload: {
      target_participant_ids: string[];
      content: string;
    },
  ): Promise<{ message: TrpgPrivateMessage }> {
    const whisper = await fetchApi<TrpgPlayWhisper>(`/api/trpg/sessions/${sessionId}/whispers`, {
      method: 'POST',
      body: JSON.stringify({
        body: payload.content,
        recipient_participant_ids: payload.target_participant_ids,
      }),
    });
    const message: TrpgPrivateMessage = {
      id: whisper.id,
      play_session_id: whisper.session_id,
      sender_participant_id: whisper.sender_participant_id,
      sender_label: whisper.sender_participant_id,
      target_participant_ids: whisper.recipient_participant_ids,
      content: whisper.body,
      created_at: whisper.created_at,
    };
    return { message };
  },

  async startSession(sessionId: string): Promise<TrpgRoom> {
    const session = await fetchApi<TrpgPlaySession>(`/api/trpg/sessions/${sessionId}/start`, { method: 'POST' });
    return mapTrpgPlaySession(session);
  },

  async endSession(sessionId: string): Promise<TrpgRoom> {
    const session = await fetchApi<TrpgPlaySession>(`/api/trpg/sessions/${sessionId}/end`, { method: 'POST' });
    return mapTrpgPlaySession(session);
  },

  async updateSharedState(
    sessionId: string,
    updates: Record<string, unknown>,
    currentSnapshot?: Record<string, unknown> | null,
  ): Promise<TrpgRoom> {
    // The canonical endpoint replaces the complete JSON snapshot. Read the
    // current value when the caller did not already provide its latest room
    // snapshot, then apply only the requested key updates locally.
    const baseSnapshot = currentSnapshot === undefined
      ? ((await trpgApi.getRoom(sessionId)).snapshot ?? {})
      : (currentSnapshot ?? {});
    const snapshot = { ...baseSnapshot, ...updates };
    const session = await fetchApi<TrpgPlaySession>(`/api/trpg/sessions/${sessionId}/snapshot`, {
      method: 'PATCH',
      body: JSON.stringify({ snapshot }),
    });
    return mapTrpgPlaySession(session);
  },

  async getPrivateState(sessionId: string): Promise<TrpgPrivateStatePayload> {
    const payload = await fetchApi<TrpgPrivateStatePayload>(
      `/api/trpg/sessions/${sessionId}/private-state`,
    );
    return mapTrpgPrivateState(payload);
  },

  async updatePrivateState(
    sessionId: string,
    state: Record<string, unknown>,
  ): Promise<TrpgPrivateStatePayload> {
    const payload = await fetchApi<TrpgPrivateStatePayload>(
      `/api/trpg/sessions/${sessionId}/private-state`,
      {
        method: 'PATCH',
        body: JSON.stringify({ state }),
      },
    );
    return mapTrpgPrivateState(payload);
  },

  async patchPrivateState(
    sessionId: string,
    state: Record<string, unknown>,
  ): Promise<TrpgPrivateStatePayload> {
    return trpgApi.updatePrivateState(sessionId, state);
  },

  async listGmPrivateStates(sessionId: string): Promise<TrpgGmPrivateState[]> {
    const data = await fetchApi<{ private_states?: TrpgGmPrivateState[] }>(
      `/api/trpg/sessions/${sessionId}/private-states`,
    );
    return Array.isArray(data.private_states) ? data.private_states : [];
  },

  async updateImageSettings(
    sessionId: string,
    imageSettings: TrpgImageSettings,
  ): Promise<TrpgRoom> {
    const session = await fetchApi<TrpgPlaySession>(
      `/api/trpg/sessions/${sessionId}/image-settings`,
      {
        method: 'PATCH',
        body: JSON.stringify({ image_settings: imageSettings }),
      },
    );
    return mapTrpgPlaySession(session);
  },

  async patchImageSettings(
    sessionId: string,
    imageSettings: TrpgImageSettings,
  ): Promise<TrpgRoom> {
    return trpgApi.updateImageSettings(sessionId, imageSettings);
  },

  async generateSessionImage(
    sessionId: string,
    prompt?: string,
  ): Promise<TrpgImageGenerationResult> {
    const data = await fetchApi<TrpgImageGenerationResult>(
      `/api/trpg/sessions/${sessionId}/images/generate`,
      {
        method: 'POST',
        body: JSON.stringify({ prompt: prompt?.trim() || null }),
      },
    );
    return data;
  },

  async generateImage(sessionId: string, prompt?: string): Promise<TrpgImageGenerationResult> {
    return trpgApi.generateSessionImage(sessionId, prompt);
  },

  async submitAction(
    sessionId: string,
    _participantId: string,
    actionText: string,
    actionKind: 'action' | 'speech' | 'ooc' = 'action',
  ): Promise<Record<string, unknown>> {
    const data = await fetchApi<{ events: TrpgPlayEvent[] }>(`/api/trpg/sessions/${sessionId}/actions`, {
      method: 'POST',
      body: JSON.stringify({ kind: actionKind, text: actionText }),
    });
    return { events: data.events };
  },

  async rollDice(
    sessionId: string,
    _participantId: string | null,
    expression: string,
    _target?: number | null,
    note?: string,
  ): Promise<TrpgLog> {
    const data = await fetchApi<{ event: TrpgPlayEvent }>(`/api/trpg/sessions/${sessionId}/dice`, {
      method: 'POST',
      body: JSON.stringify({ expression, note: note ?? '' }),
    });
    const event = data.event;
    return {
      id: event.id,
      play_session_id: event.session_id,
      participant_id: event.actor_participant_id,
      log_type: event.kind,
      content: event.body,
      metadata: event.meta,
      created_at: event.created_at,
      participant_name: event.actor_display_name,
    };
  },

  async leaveRoom(sessionId: string): Promise<TrpgParticipant> {
    const participant = await fetchApi<TrpgPlayParticipant>(`/api/trpg/sessions/${sessionId}/leave`, {
      method: 'POST',
    });
    return mapTrpgPlayParticipant(participant);
  },
};
