import { getBaseUrl } from './api-client';
import { getToken } from './auth';
import {
  mapTrpgPlayEvent,
  mapTrpgPlaySession,
} from './trpg-api';
import type { TrpgPlayEvent, TrpgPlaySession, TrpgPlayWhisper } from './trpg-api';

type MessageHandler = (payload: Record<string, unknown>) => void;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function asSession(value: unknown): TrpgPlaySession | null {
  return isRecord(value) && typeof value.id === 'string' && typeof value.status === 'string'
    ? value as unknown as TrpgPlaySession
    : null;
}

function asEvent(value: unknown): TrpgPlayEvent | null {
  return isRecord(value) && typeof value.id === 'string' && typeof value.session_id === 'string'
    ? value as unknown as TrpgPlayEvent
    : null;
}

function asWhisper(value: unknown): TrpgPlayWhisper | null {
  return isRecord(value) && typeof value.id === 'string' && typeof value.session_id === 'string'
    ? value as unknown as TrpgPlayWhisper
    : null;
}

export class TrpgWebSocket {
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private onMessage: MessageHandler | null = null;
  private onConnectionChange: ((connected: boolean) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Once a canonical session envelope is seen, stale legacy room envelopes
   * must never be allowed to overwrite its snapshot/projections. */
  private canonicalSnapshotSeen = false;

  setOnMessage(handler: MessageHandler): void {
    this.onMessage = handler;
  }

  setOnConnectionChange(handler: (connected: boolean) => void): void {
    this.onConnectionChange = handler;
  }

  async connect(sessionId: string, _inviteCode?: string | null): Promise<void> {
    this.disconnect();
    this.sessionId = sessionId;
    this.canonicalSnapshotSeen = false;
    const apiUrl = await getBaseUrl();
    const token = await getToken();
    const protocol = apiUrl.startsWith('https') ? 'wss' : 'ws';
    const host = apiUrl.replace(/^https?:\/\//, '');
    const params = new URLSearchParams();
    if (token) params.set('token', token);
    const query = params.toString() ? `?${params.toString()}` : '';
    this.ws = new WebSocket(`${protocol}://${host}/ws/play/${sessionId}${query}`);

    this.ws.onopen = () => {
      this.onConnectionChange?.(true);
      this.requestSync();
    };
    this.ws.onmessage = (event) => {
      try {
        const payload: unknown = JSON.parse(event.data);
        if (!isRecord(payload)) return;
        const eventPayload = asEvent(payload.event);
        if (payload.type === 'event' && eventPayload) {
          const log = mapTrpgPlayEvent(eventPayload);
          this.onMessage?.({
            type: 'log',
            event: eventPayload,
            log,
          });
          return;
        }
        const whisperPayload = asWhisper(payload.whisper);
        if (payload.type === 'whisper' && whisperPayload) {
          const whisper = whisperPayload;
          this.onMessage?.({
            type: 'private_message',
            whisper,
            message: {
              id: whisper.id,
              play_session_id: whisper.session_id,
              sender_participant_id: whisper.sender_participant_id,
              target_participant_ids: whisper.recipient_participant_ids,
              content: whisper.body,
              created_at: whisper.created_at,
            },
          });
          return;
        }
        const session = asSession(payload.session);
        if ((payload.type === 'sync' || payload.type === 'snapshot' || payload.type === 'ended') && session) {
          this.canonicalSnapshotSeen = true;
          const room = mapTrpgPlaySession(session);
          this.onMessage?.({
            ...payload,
            // `sync` is the initial transport envelope; expose it to the
            // screen as the same canonical snapshot event as HTTP/WS updates.
            type: payload.type === 'sync' ? 'snapshot' : payload.type,
            canonical: true,
            session,
            room,
          });
          return;
        }
        // A few historical deployments emitted room/state_sync envelopes.
        // Keep them as a pre-canonical compatibility fallback only; after the
        // canonical session snapshot arrives they are stale by definition.
        if (payload.type === 'room' || payload.type === 'state_sync') {
          if (!this.canonicalSnapshotSeen && isRecord(payload.room)) {
            this.onMessage?.({ ...payload, canonical: false });
          }
          return;
        }
        if (payload.type === 'shared_state' && this.canonicalSnapshotSeen) {
          return;
        }
        this.onMessage?.(payload);
      } catch {
        // ignore
      }
    };
    this.ws.onclose = () => {
      this.onConnectionChange?.(false);
      const closedSessionId = this.sessionId;
      if (closedSessionId) {
        this.reconnectTimer = setTimeout(() => {
          if (this.sessionId === closedSessionId) void this.connect(closedSessionId);
        }, 5000);
      }
    };
  }

  requestSync(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'request_sync' }));
    }
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.sessionId = null;
    this.canonicalSnapshotSeen = false;
    this.onConnectionChange?.(false);
  }
}
