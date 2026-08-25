/**
 * WebSocketマネージャー — チャット通信
 */

import { getToken } from './auth';
import { getBaseUrl } from './api-client';
import { conversationPerformanceDiagnostics } from '../features/conversation/performance-diagnostics';
import type {
  ChatResponseModelSelection,
  WSMessage,
  UserMessagePayload,
} from '../types/api';

type MessageHandler = (msg: WSMessage) => void;

export class ChatWebSocket {
  private ws: WebSocket | null = null;
  private onMessage: MessageHandler | null = null;
  private onConnectionChange: ((connected: boolean) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopTrackingActiveSocket: (() => void) | null = null;
  private stopTrackingReconnectTimer: (() => void) | null = null;
  private sessionId: string | null = null;
  private connectionEpoch = 0;

  private cancelReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.stopTrackingReconnectTimer?.();
    this.stopTrackingReconnectTimer = null;
  }

  private closeActiveSocket(): void {
    const socket = this.ws;
    this.ws = null;
    this.stopTrackingActiveSocket?.();
    this.stopTrackingActiveSocket = null;
    if (socket) {
      socket.onclose = null;
      socket.close();
    }
  }

  /** メッセージハンドラ設定 */
  setOnMessage(handler: MessageHandler): void {
    this.onMessage = handler;
  }

  /** 接続状態変更ハンドラ設定 */
  setOnConnectionChange(handler: (connected: boolean) => void): void {
    this.onConnectionChange = handler;
  }

  /** WebSocket接続 */
  async connect(sessionId: string): Promise<void> {
    const epoch = ++this.connectionEpoch;
    this.cancelReconnectTimer();
    this.closeActiveSocket();
    this.sessionId = sessionId;

    const token = await getToken();
    const apiUrl = await getBaseUrl();
    if (
      epoch !== this.connectionEpoch ||
      this.sessionId !== sessionId
    ) {
      return;
    }

    // HTTP(S) → WS(S) 変換
    const protocol = apiUrl.startsWith('https') ? 'wss' : 'ws';
    const host = apiUrl.replace(/^https?:\/\//, '');
    const params = new URLSearchParams({ session_id: sessionId });
    if (token) params.set('token', token);

    const url = `${protocol}://${host}/ws?${params.toString()}`;

    const socket = new WebSocket(url);
    const stopTrackingSocket = conversationPerformanceDiagnostics.trackActive(
      'socket',
      'conversation',
    );
    let socketTrackingStopped = false;
    const stopSocketTracking = () => {
      if (socketTrackingStopped) return;
      socketTrackingStopped = true;
      stopTrackingSocket();
      if (this.stopTrackingActiveSocket === stopSocketTracking) {
        this.stopTrackingActiveSocket = null;
      }
    };
    this.ws = socket;
    this.stopTrackingActiveSocket = stopSocketTracking;

    socket.onopen = () => {
      if (
        epoch !== this.connectionEpoch ||
        this.sessionId !== sessionId ||
        this.ws !== socket
      ) {
        return;
      }
      this.onConnectionChange?.(true);
    };

    socket.onmessage = (event) => {
      if (
        epoch !== this.connectionEpoch ||
        this.sessionId !== sessionId ||
        this.ws !== socket
      ) {
        return;
      }
      try {
        const msg: WSMessage = JSON.parse(event.data);
        if (msg.type === 'stream_token') {
          conversationPerformanceDiagnostics.increment(
            'stream',
            'received-token',
          );
        }
        this.onMessage?.(msg);
      } catch {
        // JSON解析失敗は無視
      }
    };

    socket.onclose = () => {
      stopSocketTracking();
      if (
        epoch !== this.connectionEpoch ||
        this.sessionId !== sessionId ||
        this.ws !== socket
      ) {
        return;
      }
      this.ws = null;
      this.onConnectionChange?.(false);
      // 自動再接続（5秒後）
      const stopTrackingTimer = conversationPerformanceDiagnostics.trackActive(
        'timer',
        'websocket-reconnect',
      );
      let timerTrackingStopped = false;
      const stopTimerTracking = () => {
        if (timerTrackingStopped) return;
        timerTrackingStopped = true;
        stopTrackingTimer();
        if (this.stopTrackingReconnectTimer === stopTimerTracking) {
          this.stopTrackingReconnectTimer = null;
        }
      };
      const reconnectTimer = setTimeout(() => {
        if (this.reconnectTimer === reconnectTimer) {
          this.reconnectTimer = null;
        }
        stopTimerTracking();
        if (
          epoch !== this.connectionEpoch ||
          this.sessionId !== sessionId
        ) {
          return;
        }
        void this.connect(sessionId);
      }, 5000);
      this.reconnectTimer = reconnectTimer;
      this.stopTrackingReconnectTimer = stopTimerTracking;
    };

    socket.onerror = () => {
      // onclose が呼ばれるのでここでは何もしない
    };
  }

  /** メッセージ送信 */
  sendMessage(
    content: string,
    projectId?: string,
    agentMode?: string,
    includeProjectContext?: boolean,
    editMessageId?: string,
    responseModel?: ChatResponseModelSelection,
  ): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.sessionId) return;

    const payload: UserMessagePayload = {
      type: 'user_message',
      data: {
        message: content,
        session_id: this.sessionId,
        ...(projectId ? { project_id: projectId } : {}),
        ...(agentMode ? { agent_mode: agentMode } : {}),
        ...(editMessageId ? { edit_message_id: editMessageId } : {}),
        ...(responseModel ? { response_model: responseModel } : {}),
        include_project_context: includeProjectContext === true,
      },
    };

    this.ws.send(JSON.stringify(payload));
  }

  sendPermissionResponse(requestId: string, approved: boolean): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: 'external_llm_permission_response',
        data: {
          request_id: requestId,
          approved,
        },
      }),
    );
  }

  /** サーバー側で進行中の応答生成を停止する。 */
  stopGeneration(): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.sessionId) {
      return false;
    }
    this.ws.send(
      JSON.stringify({
        type: "stop_generation",
        data: { session_id: this.sessionId },
      }),
    );
    return true;
  }

  /** 切断 */
  disconnect(): void {
    this.connectionEpoch += 1;
    this.sessionId = null;
    this.cancelReconnectTimer();
    this.closeActiveSocket();
    this.onConnectionChange?.(false);
  }

  /** 接続中かどうか */
  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
