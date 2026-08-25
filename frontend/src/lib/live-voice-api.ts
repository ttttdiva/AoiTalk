/**
 * Browser-facing contract for Live Voice.
 *
 * The browser only talks to the authenticated AoiTalk routes in this module.
 * In particular, neither the normal provider API key nor an ephemeral client
 * secret is accepted, persisted, or returned here. WebRTC setup always uses
 * the authenticated server-side unified call route.
 */

export type LiveVoiceSessionCreateInput = {
  conversationSessionId?: string | null;
  projectId?: string | null;
  includeProjectContext?: boolean | null;
  characterName?: string | null;
};

export type LiveVoiceSession = {
  id: string;
  provider?: string;
  model?: string;
  conversationSessionId?: string | null;
  [key: string]: unknown;
};

export type LiveVoiceSdpResponse = {
  sdp: string;
  callId?: string | null;
  [key: string]: unknown;
};

export type LiveVoiceEvent = {
  type: string;
  [key: string]: unknown;
};

export class LiveVoiceApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "LiveVoiceApiError";
    this.status = status;
  }
}

/** Best-effort end requests must never hold local media teardown hostage. */
export const LIVE_VOICE_END_TIMEOUT_MS = 5000;

// SDP should be a small offer/answer. Event payloads are metadata/transcripts;
// keeping a hard browser-side bound prevents accidental blob/base64 uploads
// from reaching the authenticated proxy even if a provider event changes.
export const MAX_LIVE_VOICE_SDP_BYTES = 256 * 1024;
export const MAX_LIVE_VOICE_EVENT_BYTES = 128 * 1024;

function classifyErrorMessage(detail: string, status: number): string {
  const normalized = detail.trim().toLowerCase();
  // The proxy/backend may include a diagnostic detail. Never echo it to the
  // browser unless it is one of the deliberately stable, non-secret classes
  // below; provider response bodies can contain credential/header data.
  if (
    normalized.includes("internal_api_key") ||
    normalized.includes("internal api key") ||
    (normalized.includes("internal python api") && normalized.includes("not configured")) ||
    normalized.includes("provider is not configured") ||
    normalized.includes("configuration")
  ) {
    return "Live Voiceサーバーの設定が未完了です。管理者に INTERNAL_API_KEY と音声サービス設定を確認してもらってください。";
  }
  // These phrases are emitted by the server-side generation classifier. Match
  // only the category keywords and return our own stable text; never echo the
  // provider's full response body.
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
  if (status === 403) return "この会話またはプロジェクトでLive Voiceを利用する権限がありません。";
  if (status === 404 || status === 410) return "Live Voiceセッションが終了しました。もう一度開始してください。";
  if (status === 408 || status === 504) return "音声サービスがタイムアウトしました。しばらく待って再試行してください。";
  if (status === 429) return "音声サービスの利用上限に達しました。しばらく待って再試行してください。";
  if (status >= 500) return "音声サービスに接続できません。サーバー設定またはネットワークを確認して再試行してください。";
  if (status >= 400) return "Live Voiceリクエストを確認して、もう一度お試しください。";
  return "Live Voiceに接続できませんでした。もう一度お試しください。";
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

/**
 * Provider payload keys that must never cross the browser contract boundary.
 *
 * Keep this check canonicalized so snake_case, camelCase, casing, and header
 * spellings (for example ``x-api-key``) share one exact deny-list entry. Do
 * not use substring matching here: Realtime usage counters such as
 * ``input_tokens``/``token_count`` and metadata such as ``secretarial`` or
 * ``authorization_url`` are not credentials and must remain visible.
 */
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
export function normalizeLiveVoiceSession(payload: unknown): LiveVoiceSession {
  const value = (payload && typeof payload === "object"
    ? payload
    : {}) as Record<string, unknown>;
  const nested =
    value.session && typeof value.session === "object"
      ? (value.session as Record<string, unknown>)
      : {};
  // ``session_id`` in the backend response is the durable ConversationSession
  // id. The short-lived browser runtime id is ``live_session_id``/``id``.
  const id = firstString(
    value.live_session_id,
    value.live_voice_session_id,
    value.id,
    nested.live_session_id,
    nested.live_voice_session_id,
    nested.id,
  );
  if (!id) throw new Error("Live Voice session id is missing");
  // Keep only a sanitized session snapshot. This prevents accidental
  // rendering/logging of legacy provider secret fields by callers that spread
  // the returned session into UI state.
  const safeValue = sanitizeProviderPayload(value) as Record<string, unknown>;
  return {
    ...safeValue,
    id,
    provider: firstString(value.provider, nested.provider) ?? undefined,
    model: firstString(value.model, nested.model) ?? undefined,
    conversationSessionId:
      firstString(
        value.conversation_session_id,
        value.conversationSessionId,
        nested.conversation_session_id,
        nested.session_id,
        value.session_id,
      ) ?? null,
  };
}

export async function createLiveVoiceSession(
  input: LiveVoiceSessionCreateInput = {},
): Promise<LiveVoiceSession> {
  const payload = await request<unknown>("/api/live-voice/sessions", {
    method: "POST",
    body: JSON.stringify({
      conversation_session_id: input.conversationSessionId ?? undefined,
      project_id: input.projectId ?? undefined,
      include_project_context: input.includeProjectContext ?? undefined,
      character_name: input.characterName ?? undefined,
      provider: "openai_realtime",
    }),
  });
  return normalizeLiveVoiceSession(payload);
}

export async function getLiveVoiceSession(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<LiveVoiceSession> {
  const payload = await request<unknown>(
    `/api/live-voice/sessions/${encodeURIComponent(sessionId)}`,
    { signal: options.signal },
  );
  return normalizeLiveVoiceSession(payload);
}

export async function exchangeLiveVoiceSdp(input: {
  sessionId: string;
  sdp: string;
  /** Deprecated browser compatibility input; accepted but intentionally ignored. */
  clientSecret?: string | null;
}): Promise<LiveVoiceSdpResponse> {
  assertUtf8Size(input.sdp, MAX_LIVE_VOICE_SDP_BYTES, "Live Voice SDP");
  const payload = await request<unknown>("/api/live-voice/sdp", {
    method: "POST",
    body: JSON.stringify({
      session_id: input.sessionId,
      sdp: input.sdp,
    }),
  });
  if (typeof payload === "string") {
    assertUtf8Size(payload, MAX_LIVE_VOICE_SDP_BYTES, "Live Voice SDP answer");
    return { sdp: payload };
  }
  const value = (payload && typeof payload === "object"
    ? payload
    : {}) as Record<string, unknown>;
  const sdp = firstString(value.sdp, value.answer, value.session_description);
  if (!sdp) throw new Error("Live Voice SDP answer is missing");
  assertUtf8Size(sdp, MAX_LIVE_VOICE_SDP_BYTES, "Live Voice SDP answer");
  const safeValue = sanitizeProviderPayload(value) as Record<string, unknown>;
  return {
    ...safeValue,
    sdp,
    callId: firstString(value.call_id, value.callId),
  };
}

export async function postLiveVoiceEvent(
  sessionId: string,
  event: LiveVoiceEvent,
): Promise<unknown> {
  let body: string;
  try {
    body = JSON.stringify(sanitizeProviderPayload(event));
  } catch {
    throw new Error("Live Voiceイベントをシリアライズできません");
  }
  assertUtf8Size(body, MAX_LIVE_VOICE_EVENT_BYTES, "Live Voiceイベント");
  return request(`/api/live-voice/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: "POST",
    body,
  });
}

export async function endLiveVoiceSession(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<unknown> {
  return request(`/api/live-voice/sessions/${encodeURIComponent(sessionId)}/end`, {
    method: "POST",
    signal: options.signal,
  });
}
