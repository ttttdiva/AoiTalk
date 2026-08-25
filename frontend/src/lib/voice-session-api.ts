/**
 * Browser-facing contract for the unified Voice Session API.
 *
 * Realtime-native sessions may still be created through `/api/live-voice/*`
 * for backward compatibility; this module is the canonical path for new
 * voice-session orchestration (pipeline, realtime + character TTS, etc.).
 */

import {
  LIVE_VOICE_END_TIMEOUT_MS,
  LiveVoiceApiError,
  MAX_LIVE_VOICE_EVENT_BYTES,
  MAX_LIVE_VOICE_SDP_BYTES,
} from "@/lib/live-voice-api";

export { LIVE_VOICE_END_TIMEOUT_MS, LiveVoiceApiError as VoiceSessionApiError };

export type VoiceSessionMode =
  | "pipeline"
  | "realtime_native"
  | "realtime_character_tts";

export type VoiceSessionCreateInput = {
  mode?: VoiceSessionMode;
  conversationSessionId?: string | null;
  projectId?: string | null;
  includeProjectContext?: boolean | null;
  characterName?: string | null;
};

export type VoiceSessionCapabilities = {
  webrtc?: boolean;
  custom_audio?: boolean;
  pipeline_status?: boolean;
};

export type VoiceSession = {
  id: string;
  voiceSessionId?: string;
  mode?: VoiceSessionMode;
  provider?: string;
  model?: string;
  conversationSessionId?: string | null;
  capabilities?: VoiceSessionCapabilities;
  [key: string]: unknown;
};

export type VoiceSessionSdpResponse = {
  sdp: string;
  callId?: string | null;
  [key: string]: unknown;
};

export type VoiceSessionEvent = {
  type: string;
  [key: string]: unknown;
};

function classifyErrorMessage(detail: string, status: number): string {
  const normalized = detail.trim().toLowerCase();
  if (
    normalized.includes("internal_api_key") ||
    normalized.includes("internal api key") ||
    (normalized.includes("internal python api") && normalized.includes("not configured")) ||
    normalized.includes("provider is not configured") ||
    normalized.includes("configuration")
  ) {
    return "音声セッションサーバーの設定が未完了です。管理者に INTERNAL_API_KEY と音声サービス設定を確認してもらってください。";
  }
  if (
    normalized.includes("レート制限") ||
    normalized.includes("利用上限") ||
    normalized.includes("rate limit") ||
    normalized.includes("quota")
  ) {
    return "音声サービスの利用上限またはレート制限に達しました。しばらく待って再試行してください。";
  }
  if (
    normalized.includes("apiキーが無効") ||
    normalized.includes("authentication") ||
    normalized.includes("認証設定")
  ) {
    return "音声サービスの認証設定を確認してください。";
  }
  if (normalized.includes("権限") || normalized.includes("permission")) {
    return "この音声サービスを利用する権限がありません。";
  }
  if (normalized.includes("モデル") || normalized.includes("model")) {
    return "指定された音声モデルを利用できません。管理者に設定を確認してもらってください。";
  }
  if (normalized.includes("タイムアウト") || normalized.includes("timeout")) {
    return "音声サービスがタイムアウトしました。しばらく待って再試行してください。";
  }
  if (status === 401) return "認証の有効期限が切れました。再度サインインしてください。";
  if (status === 403) return "この会話またはプロジェクトで音声セッションを利用する権限がありません。";
  if (status === 404 || status === 410) return "音声セッションが終了しました。もう一度開始してください。";
  if (status === 408 || status === 504) return "音声サービスがタイムアウトしました。しばらく待って再試行してください。";
  if (status === 429) return "音声サービスの利用上限に達しました。しばらく待って再試行してください。";
  if (status >= 500) return "音声サービスに接続できません。サーバー設定またはネットワークを確認して再試行してください。";
  if (status >= 400) return "音声セッションリクエストを確認して、もう一度お試しください。";
  return "音声セッションに接続できませんでした。もう一度お試しください。";
}

function normalizeErrorPayload(payload: unknown, status: number): string {
  let detail = "";
  if (payload && typeof payload === "object") {
    const value = payload as Record<string, unknown>;
    for (const key of ["detail", "error", "message"]) {
      if (typeof value[key] === "string" && value[key].trim()) {
        detail = value[key];
        break;
      }
    }
  }
  return classifyErrorMessage(detail, status);
}

async function parseResponse(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) {
    return response.json();
  }
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  headers.set("accept", "application/json, text/plain;q=0.9");
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
    cache: "no-store",
  });
  const payload = await parseResponse(response);
  if (!response.ok) {
    throw new LiveVoiceApiError(
      normalizeErrorPayload(payload, response.status),
      response.status,
    );
  }
  return payload as T;
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function assertUtf8Size(value: string, maxBytes: number, label: string): void {
  const bytes = new TextEncoder().encode(value).byteLength;
  if (bytes > maxBytes) {
    throw new Error(`${label}が大きすぎます（最大${maxBytes}バイト）`);
  }
}

const SENSITIVE_PROVIDER_KEYS = new Set([
  "secret",
  "clientsecret",
  "ephemeralsecret",
  "ephemeralclientsecret",
  "ephemeralkey",
  "ephemeraltoken",
  "token",
  "apikey",
  "apisecret",
  "openaikey",
  "openaiapikey",
  "openaisecret",
  "xapikey",
  "xopenaikey",
  "authorization",
  "authtoken",
  "accesstoken",
  "bearertoken",
]);

function isSecretFieldName(key: string): boolean {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
  return SENSITIVE_PROVIDER_KEYS.has(normalized);
}

function sanitizeProviderPayload(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeProviderPayload);
  if (!value || typeof value !== "object") return value;
  const output: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    if (isSecretFieldName(key)) continue;
    output[key] = sanitizeProviderPayload(nested);
  }
  return output;
}

/** Convert the snake_case backend response to a stable browser contract. */
export function normalizeVoiceSession(payload: unknown): VoiceSession {
  const value = (payload && typeof payload === "object"
    ? payload
    : {}) as Record<string, unknown>;
  const nested =
    value.session && typeof value.session === "object"
      ? (value.session as Record<string, unknown>)
      : {};
  const id = firstString(
    value.voice_session_id,
    nested.voice_session_id,
  );
  if (!id) throw new Error("Voice session id is missing");
  const safeValue = sanitizeProviderPayload(value) as Record<string, unknown>;
  const mode = firstString(value.mode, nested.mode) as VoiceSessionMode | null;
  return {
    ...safeValue,
    id,
    voiceSessionId: id,
    mode: mode ?? undefined,
    provider: firstString(value.provider, nested.provider) ?? undefined,
    model: firstString(value.model, nested.model) ?? undefined,
    conversationSessionId:
      firstString(
        value.conversation_session_id,
        value.conversationSessionId,
        nested.conversation_session_id,
      ) ?? null,
    capabilities:
      value.capabilities && typeof value.capabilities === "object"
        ? (value.capabilities as VoiceSessionCapabilities)
        : nested.capabilities && typeof nested.capabilities === "object"
          ? (nested.capabilities as VoiceSessionCapabilities)
          : undefined,
  };
}

export async function createVoiceSession(
  input: VoiceSessionCreateInput = {},
): Promise<VoiceSession> {
  const payload = await request<unknown>("/api/voice-sessions", {
    method: "POST",
    body: JSON.stringify({
      mode: input.mode ?? "realtime_native",
      conversation_session_id: input.conversationSessionId ?? undefined,
      project_id: input.projectId ?? undefined,
      include_project_context: input.includeProjectContext ?? undefined,
      character_name: input.characterName ?? undefined,
    }),
  });
  return normalizeVoiceSession(payload);
}

export async function getVoiceSession(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<VoiceSession> {
  const payload = await request<unknown>(
    `/api/voice-sessions/${encodeURIComponent(sessionId)}`,
    { signal: options.signal },
  );
  return normalizeVoiceSession(payload);
}

export async function exchangeVoiceSessionSdp(input: {
  sessionId: string;
  sdp: string;
}): Promise<VoiceSessionSdpResponse> {
  assertUtf8Size(input.sdp, MAX_LIVE_VOICE_SDP_BYTES, "Voice session SDP");
  const payload = await request<unknown>(
    `/api/voice-sessions/${encodeURIComponent(input.sessionId)}/webrtc`,
    {
      method: "POST",
      body: JSON.stringify({ sdp: input.sdp }),
    },
  );
  if (typeof payload === "string") {
    assertUtf8Size(payload, MAX_LIVE_VOICE_SDP_BYTES, "Voice session SDP answer");
    return { sdp: payload };
  }
  const value = (payload && typeof payload === "object"
    ? payload
    : {}) as Record<string, unknown>;
  const sdp = firstString(value.sdp, value.answer, value.session_description);
  if (!sdp) throw new Error("Voice session SDP answer is missing");
  assertUtf8Size(sdp, MAX_LIVE_VOICE_SDP_BYTES, "Voice session SDP answer");
  const safeValue = sanitizeProviderPayload(value) as Record<string, unknown>;
  return {
    ...safeValue,
    sdp,
    callId: firstString(value.call_id, value.callId),
  };
}

export async function postVoiceSessionEvent(
  sessionId: string,
  event: VoiceSessionEvent,
): Promise<unknown> {
  let body: string;
  try {
    body = JSON.stringify(sanitizeProviderPayload(event));
  } catch {
    throw new Error("音声セッションイベントをシリアライズできません");
  }
  assertUtf8Size(body, MAX_LIVE_VOICE_EVENT_BYTES, "音声セッションイベント");
  // Voice sessions share the Live Voice runtime registry; browser telemetry
  // still flows through the existing authenticated events route.
  return request(`/api/live-voice/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: "POST",
    body,
  });
}

export async function interruptVoiceSession(sessionId: string): Promise<unknown> {
  return request(`/api/voice-sessions/${encodeURIComponent(sessionId)}/interrupt`, {
    method: "POST",
  });
}

export async function endVoiceSession(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<unknown> {
  return request(`/api/voice-sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    signal: options.signal,
  });
}

/** WebSocket URL for character TTS audio transport. */
export function voiceSessionAudioWebSocketUrl(sessionId: string): string {
  if (typeof window === "undefined") {
    return `/api/voice-sessions/${encodeURIComponent(sessionId)}/audio`;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/voice-sessions/${encodeURIComponent(sessionId)}/audio`;
}
