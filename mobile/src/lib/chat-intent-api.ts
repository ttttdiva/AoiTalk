import { fetchApi, ApiHttpError } from "./api-client";

import type { components } from "../types/api-types.gen";
export type ChatIntentCandidate = components["schemas"]["ChatIntentCandidate"];
export type ChatIntentResponse = components["schemas"]["ChatIntentResponse"];


export class ChatIntentLookupError extends Error {}

function lookupError(status: number, body: unknown): ChatIntentLookupError {
  const code = (body as { detail?: { code?: unknown } } | null)?.detail?.code;
  const message = code === "chat_intent_privacy_blocked" || code === "chat_intent_privacy_unavailable"
    ? "現在の保護設定では意図を外部サービスで推定できません。＋メニューで意図確認をOFFにすると通常送信できます。"
    : status === 401 ? "意図候補の取得にはログインが必要です。"
    : status === 413 || status === 422 ? "意図推定は4000文字以内の本文に対応します。本文と会話の指定を確認してください。"
    : status === 429 ? "意図候補の取得が混み合っています。少し待って再試行してください。"
    : status === 504 ? "意図候補の取得がタイムアウトしました。下書きは保持されています。"
    : status === 403 ? "この会話で意図候補を取得できません。アクセス権と保護設定を確認してください。"
    : "意図候補を取得できませんでした。接続とサーバーのJEV設定を確認してください。";
  return new ChatIntentLookupError(message);
}

export function validateChatIntentResponse(value: unknown): ChatIntentResponse {
  const result = value as ChatIntentResponse;
  if (!result || typeof result.model !== "string" || !Array.isArray(result.candidates)
      || result.candidates.length < 2 || result.candidates.length > 32
      || result.candidates.some((item) => !item || typeof item.id !== "string"
        || !/^[a-z_]{1,40}$/.test(item.id) || typeof item.label !== "string"
        || !item.label.trim() || item.label.length > 80 || /[\[\]\r\n]/.test(item.label)
        || typeof item.score !== "number" || !Number.isFinite(item.score)
        || item.score < 0 || item.score > 1)
      || new Set(result.candidates.map((item) => item.id)).size !== result.candidates.length) {
    throw new Error("意図候補の応答が不正です。もう一度お試しください。");
  }
  return { model: result.model, candidates: [...result.candidates].sort((a, b) => b.score - a.score) };
}

export function appendChatIntent(content: string, candidate: ChatIntentCandidate): string {
  return `${content}\n\n[意図: ${candidate.label}]`;
}

export async function fetchChatIntentCandidates(text: string, signal: AbortSignal, scope: { session_id?: string | null; project_id?: string | null } = {}): Promise<ChatIntentResponse> {
  try {
    return validateChatIntentResponse(await fetchApi<unknown>("/api/chat/intent-candidates", {
      method: "POST", signal, body: JSON.stringify({ text, ...scope }),
    }, 20_000));
  } catch (error) {
    // Decode only known codes; provider bodies and secrets never reach the UI.
    let body: unknown = null;
    if (error instanceof ApiHttpError) {
      try { body = JSON.parse(error.responseBody); } catch { /* intentionally ignored */ }
    }
    throw lookupError(error instanceof ApiHttpError ? error.status : 0, body);
  }
}
