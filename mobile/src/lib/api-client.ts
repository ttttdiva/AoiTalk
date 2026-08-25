/**
 * APIクライアント — Bearer token自動付与 + リフレッシュ
 */

import {
  getApiUrl,
  getTokenSnapshot,
  saveTokenIfRevision,
} from "./auth";
import { DEFAULT_API_URL, API_TIMEOUT } from '../constants/config';
import {
  clearNetworkEndpointRoutingCache,
  resolveApiUrlForCurrentNetwork,
} from './connection-routing';
import { looksLikeHtml, normalizeApiUrl } from './api-url';
import { useNetworkStore } from '../stores/network';

export class ApiHttpError extends Error {
  readonly status: number;
  readonly responseBody: string;

  constructor(status: number, responseBody: string, message?: string) {
    super(message ?? `API Error ${status}: ${responseBody.trim().slice(0, 500)}`);
    this.name = 'ApiHttpError';
    this.status = status;
    this.responseBody = responseBody;
  }
}

class AuthError extends ApiHttpError {
  constructor(responseBody = '') {
    super(401, responseBody, '認証が必要です');
    this.name = 'AuthError';
  }
}

type AuthInvalidatedListener = (error: AuthError) => void;
const authInvalidatedListeners = new Set<AuthInvalidatedListener>();

/**
 * Subscribe to a terminal 401 (after token refresh/retry is exhausted).
 * The API client stays UI-agnostic; AuthContext can expose a reauth state
 * without introducing a circular import from this low-level module.
 */
export function subscribeAuthInvalidated(
  listener: AuthInvalidatedListener,
): () => void {
  authInvalidatedListeners.add(listener);
  return () => authInvalidatedListeners.delete(listener);
}

function notifyAuthInvalidated(error: AuthError): void {
  for (const listener of authInvalidatedListeners) {
    try {
      listener(error);
    } catch {
      // A UI subscriber must never change the API request's failure semantics.
    }
  }
}

function throwAuthInvalidated(): never {
  const error = new AuthError();
  notifyAuthInvalidated(error);
  throw error;
}

/**
 * クライアント側のtimeoutで打ち切った通信。
 *
 * 「サーバーへ届かなかった」ことは意味しない。サーバーは受理して処理を続けている
 * 可能性があるため、通信不能と同じ自動再送へ回すと二重取り込みになる。
 */
export class ApiTimeoutError extends Error {
  readonly timeoutMs: number;

  constructor(timeoutMs: number) {
    super(`処理が${Math.round(timeoutMs / 1000)}秒以内に終わりませんでした`);
    this.name = 'ApiTimeoutError';
    this.timeoutMs = timeoutMs;
  }
}

export function isApiTimeoutError(error: unknown): error is ApiTimeoutError {
  return error instanceof ApiTimeoutError;
}

let cachedApiUrl: string | null = null;
// 同時に走る別APIの成功を、あとから完了した古い通信失敗で上書きしない。
let reachabilitySuccessRevision = 0;
let reachabilityEndpointRevision = 0;

/** 現在のAPI URLを取得 */
export async function getBaseUrl(): Promise<string> {
  if (!cachedApiUrl) {
    const stored = await getApiUrl();
    cachedApiUrl = normalizeApiUrl(stored || DEFAULT_API_URL);
  }
  return resolveApiUrlForCurrentNetwork(cachedApiUrl);
}

function formatApiError(status: number, text: string): ApiHttpError {
  if (status === 404 && looksLikeHtml(text)) {
    return new ApiHttpError(
      status,
      text,
      'API Error 404: 接続先がAoiTalk APIではなくWeb UIの404を返しました。Connection settings の API URL がモバイルAPIを返すエンドポイントを指しているか確認してください。'
    );
  }

  const body = text.trim();
  return new ApiHttpError(status, text, `API Error ${status}: ${body.slice(0, 500)}`);
}

export function isApiHttpError(error: unknown): error is ApiHttpError {
  return (
    error instanceof ApiHttpError ||
    (error instanceof Error &&
      typeof (error as Error & { status?: unknown }).status === 'number')
  );
}

/** fetch 自体が応答を受け取れなかった通信不能・タイムアウトだけを判定する。 */
export function isApiConnectionError(error: unknown): boolean {
  if (isApiHttpError(error)) return false;
  if (isApiTimeoutError(error)) return false;
  if (!(error instanceof Error)) return false;
  if (error.name === 'AbortError') return true;
  return /abort|timeout|timed out|network request failed|failed to fetch|networkerror|connection refused/i.test(
    error.message,
  );
}

/**
 * API のHTTP応答有無をサーバー到達性へ反映する。
 *
 * 4xx/5xx はリクエスト自体の失敗だが、サーバーは応答しているためオンライン。
 * fetch不能・timeout のときだけ到達不能として記録する。
 */
async function fetchWithReachability(
  input: RequestInfo | URL,
  init?: RequestInit,
  isClientTimeout?: () => boolean,
): Promise<Response> {
  const successRevisionAtStart = reachabilitySuccessRevision;
  const endpointRevisionAtStart = reachabilityEndpointRevision;
  try {
    const response = await fetch(input, init);
    if (reachabilityEndpointRevision === endpointRevisionAtStart) {
      reachabilitySuccessRevision += 1;
      useNetworkStore.getState().setServerReachable(true);
    }
    return response;
  } catch (error) {
    // 自前timeoutでの打ち切りは「サーバーが応答しない」とは限らない
    // （処理が長いだけのことがある）ので、未到達として記録しない。
    if (
      !isClientTimeout?.() &&
      isApiConnectionError(error) &&
      reachabilityEndpointRevision === endpointRevisionAtStart &&
      reachabilitySuccessRevision === successRevisionAtStart
    ) {
      useNetworkStore.getState().setServerReachable(false);
    }
    throw error;
  }
}

/** API URLキャッシュをクリア（設定変更時） */
export function clearApiUrlCache(): void {
  cachedApiUrl = null;
  reachabilityEndpointRevision += 1;
  clearNetworkEndpointRoutingCache();
}

type TokenSnapshot = Awaited<ReturnType<typeof getTokenSnapshot>>;

const refreshInFlightByRevision = new Map<number, Promise<boolean>>();

async function refreshTokenOnce(snapshot: TokenSnapshot): Promise<boolean> {
  try {
    const { token, revision } = snapshot;
    if (!token) return false;

    const baseUrl = await getBaseUrl();
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT);
    const res = await fetchWithReachability(`${baseUrl}/api/auth/refresh`, {
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
      return saveTokenIfRevision(data.access_token, revision);
    }
    return false;
  } catch {
    return false;
  }
}

/** トークンリフレッシュ試行。同じ認証世代の並行401だけを1リクエストへ集約する。 */
export async function tryRefreshToken(
  expectedSnapshot?: TokenSnapshot,
): Promise<boolean> {
  const snapshot = expectedSnapshot ?? (await getTokenSnapshot());
  if (!snapshot.token) return false;

  const current = await getTokenSnapshot();
  if (
    current.revision !== snapshot.revision ||
    current.token !== snapshot.token
  ) {
    return false;
  }

  const inFlight = refreshInFlightByRevision.get(snapshot.revision);
  if (inFlight) return inFlight;

  const refresh = refreshTokenOnce(snapshot);
  refreshInFlightByRevision.set(snapshot.revision, refresh);
  void refresh.finally(() => {
    if (refreshInFlightByRevision.get(snapshot.revision) === refresh) {
      refreshInFlightByRevision.delete(snapshot.revision);
    }
  });
  return refresh;
}

/** 汎用API呼び出し */
async function fetchApiInternal<T>(
  path: string,
  options: RequestInit,
  timeout: number,
  authRetryBudget: number,
  parseResponse: (response: Response) => Promise<T> = async (response) =>
    response.json() as Promise<T>,
): Promise<T> {
  const tokenSnapshot = await getTokenSnapshot();
  const token = tokenSnapshot.token;
  const baseUrl = await getBaseUrl();
  const url = `${baseUrl}${path}`;

  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);

  try {
    const res = await fetchWithReachability(
      url,
      {
        ...options,
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(options.headers || {}),
        },
      },
      () => timedOut,
    );

    if (res.status === 401) {
      if (authRetryBudget <= 0) {
        throwAuthInvalidated();
      }

      const currentSnapshot = await getTokenSnapshot();
      const tokenChanged =
        currentSnapshot.revision !== tokenSnapshot.revision ||
        currentSnapshot.token !== tokenSnapshot.token;
      if (tokenChanged) {
        if (!currentSnapshot.token) throwAuthInvalidated();
        return fetchApiInternal(path, options, timeout, authRetryBudget - 1, parseResponse);
      }

      const refreshed = await tryRefreshToken(tokenSnapshot);
      if (!refreshed) {
        const afterRefreshSnapshot = await getTokenSnapshot();
        if (
          afterRefreshSnapshot.token &&
          (afterRefreshSnapshot.revision !== tokenSnapshot.revision ||
            afterRefreshSnapshot.token !== tokenSnapshot.token)
        ) {
          return fetchApiInternal(path, options, timeout, authRetryBudget - 1, parseResponse);
        }
        throwAuthInvalidated();
      }
      // refresh後も401なら再帰せず、認証エラーとして呼び出し元へ返す。
      return fetchApiInternal(path, options, timeout, 0, parseResponse);
    }

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw formatApiError(res.status, text);
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    return parseResponse(res);
  } catch (error) {
    // 自前のtimeoutで打ち切ったabortは、通信不能と区別できるようにして投げる。
    if (timedOut && error instanceof Error && error.name === 'AbortError') {
      throw new ApiTimeoutError(timeout);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function fetchApi<T>(
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT,
): Promise<T> {
  // token切替後の再試行1回 + refresh後の再試行1回を上限にする。
  return fetchApiInternal(path, options, timeout, 2);
}

/**
 * 汎用API呼び出し（text/plain）。
 *
 * Story export などJSON以外のcanonical responseでも、通常のfetchApiと
 * 同じBearer付与・401 refresh・timeout・到達性判定を維持する。
 */
export async function fetchApiText(
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT,
): Promise<string> {
  return fetchApiInternal(path, options, timeout, 2, (response) => response.text());
}

export { AuthError };
