import { getBaseUrl } from './api-client';
import { getToken } from './auth';

type MessageHandler = (payload: Record<string, unknown>) => void;

export class TrpgWebSocket {
  private ws: WebSocket | null = null;
  private roomId: string | null = null;
  private onMessage: MessageHandler | null = null;
  private onConnectionChange: ((connected: boolean) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  setOnMessage(handler: MessageHandler): void {
    this.onMessage = handler;
  }

  setOnConnectionChange(handler: (connected: boolean) => void): void {
    this.onConnectionChange = handler;
  }

  async connect(roomId: string, inviteCode?: string | null): Promise<void> {
    this.roomId = roomId;
    this.disconnect();
    const apiUrl = await getBaseUrl();
    const token = await getToken();
    const protocol = apiUrl.startsWith('https') ? 'wss' : 'ws';
    const host = apiUrl.replace(/^https?:\/\//, '');
    const params = new URLSearchParams();
    if (token) params.set('token', token);
    if (inviteCode) params.set('invite_code', inviteCode);
    const query = params.toString() ? `?${params.toString()}` : '';
    this.ws = new WebSocket(`${protocol}://${host}/ws/trpg/${roomId}${query}`);

    this.ws.onopen = () => {
      this.onConnectionChange?.(true);
    };
    this.ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        this.onMessage?.(payload);
      } catch {
        // ignore
      }
    };
    this.ws.onclose = () => {
      this.onConnectionChange?.(false);
      if (this.roomId) {
        this.reconnectTimer = setTimeout(() => {
          if (this.roomId) void this.connect(this.roomId, inviteCode);
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
    this.onConnectionChange?.(false);
  }
}
