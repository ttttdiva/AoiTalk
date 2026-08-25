import { buildPlayWebSocketUrl } from "@/lib/websocket-url";

type MessageHandler = (payload: Record<string, unknown>) => void;

export class TrpgPlayWebSocket {
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private onMessage: MessageHandler | null = null;
  private onConnectionChange: ((connected: boolean) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  setOnMessage(handler: MessageHandler): void {
    this.onMessage = handler;
  }

  setOnConnectionChange(handler: (connected: boolean) => void): void {
    this.onConnectionChange = handler;
  }

  connect(sessionId: string): void {
    this.sessionId = sessionId;
    this.disconnect();
    this.ws = new WebSocket(buildPlayWebSocketUrl(sessionId));

    this.ws.onopen = () => {
      this.onConnectionChange?.(true);
      this.requestSync();
    };
    this.ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        this.onMessage?.(payload);
      } catch {
        // ignore malformed payloads
      }
    };
    this.ws.onclose = () => {
      this.onConnectionChange?.(false);
      if (this.sessionId) {
        this.reconnectTimer = setTimeout(() => {
          if (this.sessionId) this.connect(this.sessionId);
        }, 5000);
      }
    };
  }

  requestSync(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "request_sync" }));
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
