/**
 * 既存Python APIへのプロキシ用クライアント
 * サーバーサイド専用（process.env.PYTHON_API_URL を参照）
 */

const getBaseUrl = () =>
  process.env.PYTHON_API_URL || "http://127.0.0.1:3000";

async function fetchApi<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
  const res = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    ...options,
  });

  if (!res.ok) {
    throw new Error(
      `Python API error: ${res.status} ${res.statusText} (${path})`
    );
  }

  return res.json() as Promise<T>;
}

// ─── LLM関連 ───

export async function getLlmMode() {
  return fetchApi("/api/llm/mode");
}

export async function setLlmMode(mode: string) {
  return fetchApi("/api/llm/mode", {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}

export async function getLlmEngine() {
  return fetchApi("/api/llm/engine");
}

export async function setLlmEngine(engine: string) {
  return fetchApi("/api/llm/engine", {
    method: "POST",
    body: JSON.stringify({ engine }),
  });
}

// ─── 音声関連 ───

export async function getVoiceStatus() {
  return fetchApi("/api/voice/status");
}

// ─── キャラクター ───

export async function getCharacters() {
  return fetchApi("/api/characters");
}

export async function switchCharacter(characterName: string) {
  return fetchApi("/api/characters/switch", {
    method: "POST",
    body: JSON.stringify({ character_name: characterName }),
  });
}

// ─── 設定 ───

export async function getConfig() {
  return fetchApi("/api/config");
}

export async function getSettings() {
  return fetchApi("/api/settings");
}

export async function updateSetting(key: string, value: unknown) {
  return fetchApi("/api/settings", {
    method: "POST",
    body: JSON.stringify({ key, value }),
  });
}

export async function getRuntimeFeatures() {
  return fetchApi("/api/runtime/features");
}

export async function setRuntimeFeature(feature: string, enabled: boolean) {
  return fetchApi("/api/runtime/features", {
    method: "PATCH",
    body: JSON.stringify({ feature, enabled }),
  });
}
