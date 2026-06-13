import { fetchApi } from './api-client';
import type { TrpgLog, TrpgParticipant, TrpgPrivateMessage, TrpgRoom } from '../types/api';

type CreateRoomPayload = {
  scenario_id: string;
  room_title?: string;
  max_players?: number;
  gm_mode?: 'ai' | 'human';
  is_public?: boolean;
};

type JoinRoomPayload = {
  display_name: string;
  character_id?: string | null;
  role?: string;
  avatar_url?: string;
  as_npc?: boolean;
  invite_code?: string | null;
};

type SendPrivateMessagePayload = {
  sender_participant_id: string;
  target_participant_ids: string[];
  content: string;
  message_type?: 'private' | 'gm' | 'mention';
  metadata?: Record<string, unknown>;
  request_gm_reply?: boolean;
};

export const trpgApi = {
  async listRooms(): Promise<TrpgRoom[]> {
    const data = await fetchApi<{ rooms: TrpgRoom[]; count: number }>('/api/trpg/rooms');
    return data.rooms;
  },

  async getRoom(roomIdOrCode: string, inviteCode?: string | null): Promise<TrpgRoom> {
    const query = inviteCode ? `?invite_code=${encodeURIComponent(inviteCode)}` : '';
    return fetchApi<TrpgRoom>(`/api/trpg/rooms/${roomIdOrCode}${query}`);
  },

  async createRoom(payload: CreateRoomPayload): Promise<TrpgRoom> {
    return fetchApi<TrpgRoom>('/api/trpg/rooms', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async joinRoom(roomIdOrCode: string, payload: JoinRoomPayload): Promise<TrpgParticipant> {
    return fetchApi<TrpgParticipant>(`/api/trpg/rooms/${roomIdOrCode}/join`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async listLogs(roomId: string): Promise<TrpgLog[]> {
    const data = await fetchApi<{ logs: TrpgLog[]; count: number }>(`/api/trpg/rooms/${roomId}/logs`);
    return data.logs;
  },

  async listPrivateMessages(roomId: string, viewerParticipantId: string): Promise<TrpgPrivateMessage[]> {
    const query = `?viewer_participant_id=${encodeURIComponent(viewerParticipantId)}`;
    const data = await fetchApi<{ messages: TrpgPrivateMessage[]; count: number }>(
      `/api/trpg/rooms/${roomId}/private-messages${query}`,
    );
    return data.messages;
  },

  async sendPrivateMessage(
    roomId: string,
    payload: SendPrivateMessagePayload,
  ): Promise<{ message: TrpgPrivateMessage; gm_reply?: TrpgPrivateMessage | null }> {
    return fetchApi(`/api/trpg/rooms/${roomId}/private-messages`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async startSession(roomId: string): Promise<Record<string, unknown>> {
    return fetchApi(`/api/trpg/rooms/${roomId}/start`, { method: 'POST' });
  },

  async advanceGm(roomId: string, userRequest = ''): Promise<Record<string, unknown>> {
    return fetchApi(`/api/trpg/rooms/${roomId}/gm/advance`, {
      method: 'POST',
      body: JSON.stringify({ user_request: userRequest }),
    });
  },

  async updateSharedState(roomId: string, updates: Record<string, unknown>): Promise<Record<string, unknown>> {
    return fetchApi(`/api/trpg/rooms/${roomId}/shared_state`, {
      method: 'PUT',
      body: JSON.stringify({ updates }),
    });
  },

  async submitAction(
    roomId: string,
    participantId: string,
    actionText: string,
    actionKind: 'action' | 'speech' | 'ooc' = 'action',
  ): Promise<Record<string, unknown>> {
    return fetchApi(`/api/trpg/rooms/${roomId}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        participant_id: participantId,
        action_text: actionText,
        action_kind: actionKind,
      }),
    });
  },

  async rollDice(
    roomId: string,
    participantId: string | null,
    expression: string,
    target?: number | null,
    note?: string,
  ): Promise<TrpgLog> {
    return fetchApi<TrpgLog>(`/api/trpg/rooms/${roomId}/dice`, {
      method: 'POST',
      body: JSON.stringify({
        participant_id: participantId,
        expression,
        target: target ?? null,
        note: note ?? '',
      }),
    });
  },
};
