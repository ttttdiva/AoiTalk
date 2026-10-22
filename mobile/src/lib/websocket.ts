/**
 * WebSocketマネージャー — チャット通信
 */

import { getBaseUrl } from './api-client';
import { sendWithTransportAuth } from './transport-auth';
import { getApiUrl } from './auth';
import { conversationPerformanceDiagnostics } from '../features/conversation/performance-diagnostics';
import type {
  ChatResponseModelSelection,
  WSMessage,
  UserMessagePayload,
} from '../types/api';

type MessageHandler = (msg: WSMessage) => void;

const PERMANENT_PRECONNECT_CODES = new Set([
  "gateway_reauth_required",
  "reauth_required",
  "signed_out",
  "config_invalid",
  "target_invalid",
  "invalid_target_namespace",
  "target_namespace_mismatch",
  "target_namespace_traversal",
  "target_credentials_in_url",
  "target_query_not_allowed",
  "application_auth_header_forbidden",
  "primary_pin_missing",
  "identity_mismatch",
  "identity_probe_failed",
  "legacy_target_unsupported",
]);

const DIAGNOSTIC_ERROR_CODES = new Set([
  ...PERMANENT_PRECONNECT_CODES,
  "credential_revision_changed",
  "identity_proof_invalid",
  "transient",
  "timeout",
  "network_error",
  "api_endpoint_unavailable",
]);

function errorCode(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" && code.trim() ? code.trim() : null;
}

function isTransientCause(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const name = (error as { name?: unknown }).name;
  const code = errorCode(error);
  return name === "TypeError" || name === "AbortError" || name === "TimeoutError"
    || code === "transient" || code === "timeout" || code === "network_error"
    || code === "api_endpoint_unavailable";
}

function shouldRetryPreconnect(error: unknown): boolean {
  const code = errorCode(error);
  if (code === "credential_revision_changed") return true;
  if (code === "identity_probe_failed") {
    return (error as { retryable?: unknown }).retryable === true;
  }
  if (code && PERMANENT_PRECONNECT_CODES.has(code)) return false;
  if (code === "identity_proof_invalid") {
    return isTransientCause((error as { cause?: unknown }).cause);
  }
  if (error && typeof error === "object") {
    const retryable = (error as { retryable?: unknown }).retryable;
    if (typeof retryable === "boolean") return retryable;
    if ((error as { name?: unknown }).name === "InvalidApiUrlError") return false;
  }
  // Route probes, SecureStore/network reads, and unknown transport failures
  // are transient. They use the same bounded reconnect path as onclose.
  return true;
}

function diagnosticCode(error: unknown): string {
  const code = errorCode(error);
  return code && DIAGNOSTIC_ERROR_CODES.has(code) ? code : "unknown";
}

function websocketAbortError(): Error {
  const error = new Error("WebSocket接続を中止しました");
  error.name = "AbortError";
  return error;
}

function awaitConnect<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(websocketAbortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(websocketAbortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void operation.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(websocketAbortError());
        else resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? websocketAbortError() : error);
      },
    );
  });
}

export class ChatWebSocket {
  private ws: WebSocket | null = null;
  private onMessage: MessageHandler | null = null;
  private onConnectionChange: ((connected: boolean) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopTrackingActiveSocket: (() => void) | null = null;
  private stopTrackingReconnectTimer: (() => void) | null = null;
  private sessionId: string | null = null;
  private connectionEpoch = 0;
  private connectAbortController: AbortController | null = null;

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

  private scheduleReconnect(epoch: number, sessionId: string): void {
    if (epoch !== this.connectionEpoch || this.sessionId !== sessionId) return;
    this.cancelReconnectTimer();
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
    this.connectAbortController?.abort();
    const connectAbortController = new AbortController();
    this.connectAbortController = connectAbortController;
    this.cancelReconnectTimer();
    this.closeActiveSocket();
    this.sessionId = sessionId;

    try {
    const primaryApiUrl = await getApiUrl();
    const apiUrl = await awaitConnect(
      getBaseUrl({ signal: connectAbortController.signal }),
      connectAbortController.signal,
    );
    if (
      epoch !== this.connectionEpoch ||
      this.sessionId !== sessionId
    ) {
      return;
    }

    // HTTP(S) → WS(S) 変換
    const protocol = apiUrl.startsWith('https') ? 'wss' : 'ws';
    const target = new URL(`${apiUrl}/ws`);
    target.protocol = protocol + ':';
    const params = new URLSearchParams({ session_id: sessionId });
    const url = `${target.toString()}?${params.toString()}`;
    // React Native's third constructor argument carries request headers. Keep
    // the JWT/Gateway credential out of the query string and therefore out of
    // proxy logs, reconnect URLs, and crash reports.
    const NativeWebSocket = WebSocket as unknown as new (
      url: string,
      protocols?: string | string[],
      options?: { headers: Record<string, string> },
    ) => WebSocket;
    const socket = await sendWithTransportAuth(url, {
      primaryBaseUrl: primaryApiUrl || apiUrl,
      targetBaseUrl: apiUrl,
      signal: connectAbortController.signal,
    }, (headers) => {
      // The route/auth awaits above may outlive a session switch or
      // disconnect. Check the epoch in the same synchronous callback that
      // constructs the native socket so an obsolete handshake never reaches
      // the platform networking stack.
      if (epoch !== this.connectionEpoch || this.sessionId !== sessionId) {
        return null;
      }
      return new NativeWebSocket(url, undefined, { headers });
    });
    if (!socket) return;
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
      this.scheduleReconnect(epoch, sessionId);
    };

    socket.onerror = () => {
      // onclose が呼ばれるのでここでは何もしない
    };
    } catch (error) {
      if (epoch !== this.connectionEpoch || this.sessionId !== sessionId) return;
      conversationPerformanceDiagnostics.increment(
        "socket",
        `connect-failed.${diagnosticCode(error)}`,
      );
      this.onConnectionChange?.(false);
      if (shouldRetryPreconnect(error)) {
        this.scheduleReconnect(epoch, sessionId);
      }
    }
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

  sendHumanInteractionResponse(requestId: string, sessionId: string, result: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || sessionId !== this.sessionId) return false;
    this.ws.send(JSON.stringify({ type: "human_interaction_response",
      data: { ...result, request_id: requestId, session_id: sessionId } }));
    return true;
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
    this.connectAbortController?.abort();
    this.connectAbortController = null;
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
