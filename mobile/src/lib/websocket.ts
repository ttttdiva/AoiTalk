/**
 * WebSocketマネージャー — チャット通信
 */

import { getToken } from './auth';
import { getBaseUrl } from './api-client';
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
  private sessionId: string | null = null;

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
    this.sessionId = sessionId;
    this.disconnect();

    const token = await getToken();
    const apiUrl = await getBaseUrl();

    // HTTP(S) → WS(S) 変換
    const protocol = apiUrl.startsWith('https') ? 'wss' : 'ws';
    const host = apiUrl.replace(/^https?:\/\//, '');
    const params = new URLSearchParams({ session_id: sessionId });
    if (token) params.set('token', token);

    const url = `${protocol}://${host}/ws?${params.toString()}`;

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.onConnectionChange?.(true);
    };

    this.ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);
        this.onMessage?.(msg);
      } catch {
        // JSON解析失敗は無視
      }
    };

    this.ws.onclose = () => {
      this.onConnectionChange?.(false);
      // 自動再接続（5秒後）
      if (this.sessionId) {
        this.reconnectTimer = setTimeout(() => {
          if (this.sessionId) this.connect(this.sessionId);
        }, 5000);
      }
    };

    this.ws.onerror = () => {
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

  /** 切断 */
  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null; // 再接続を防止
      this.ws.close();
      this.ws = null;
    }
    this.onConnectionChange?.(false);
  }

  /** 接続中かどうか */
  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
