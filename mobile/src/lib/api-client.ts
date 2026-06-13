/**
 * APIクライアント — Bearer token自動付与 + リフレッシュ
 */

import { getToken, saveToken, getApiUrl } from './auth';
import { DEFAULT_API_URL, API_TIMEOUT } from '../constants/config';
import {
  clearNetworkEndpointRoutingCache,
  resolveApiUrlForCurrentNetwork,
} from './connection-routing';
import { looksLikeHtml, normalizeApiUrl } from './api-url';

class AuthError extends Error {
  constructor() {
    super('認証が必要です');
    this.name = 'AuthError';
  }
}

let cachedApiUrl: string | null = null;

/** 現在のAPI URLを取得 */
export async function getBaseUrl(): Promise<string> {
  if (!cachedApiUrl) {
    const stored = await getApiUrl();
    cachedApiUrl = normalizeApiUrl(stored || DEFAULT_API_URL);
  }
  return resolveApiUrlForCurrentNetwork(cachedApiUrl);
}

function formatApiError(status: number, text: string): Error {
  if (status === 404 && looksLikeHtml(text)) {
    return new Error(
      'API Error 404: 接続先がAoiTalk APIではなくWeb UIの404を返しました。Connection settings の API URL がモバイルAPIを返すエンドポイントを指しているか確認してください。'
    );
  }

  const body = text.trim();
  return new Error(`API Error ${status}: ${body.slice(0, 500)}`);
}

/** API URLキャッシュをクリア（設定変更時） */
export function clearApiUrlCache(): void {
  cachedApiUrl = null;
  clearNetworkEndpointRoutingCache();
}

/** トークンリフレッシュ試行 */
export async function tryRefreshToken(): Promise<boolean> {
  try {
    const token = await getToken();
    if (!token) return false;

    const baseUrl = await getBaseUrl();
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT);
    const res = await fetch(`${baseUrl}/api/auth/refresh`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
    }).finally(() => clearTimeout(timeoutId));

    if (!res.ok) return false;
    const data = await res.json();
    if (data.access_token) {
      await saveToken(data.access_token);
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

/** 汎用API呼び出し */
export async function fetchApi<T>(
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT
): Promise<T> {
  const token = await getToken();
  const baseUrl = await getBaseUrl();
  const url = `${baseUrl}${path}`;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
    });

    if (res.status === 401) {
      const refreshed = await tryRefreshToken();
      if (!refreshed) {
        throw new AuthError();
      }
      // リトライ
      return fetchApi(path, options, timeout);
    }

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw formatApiError(res.status, text);
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    return res.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

export { AuthError };
