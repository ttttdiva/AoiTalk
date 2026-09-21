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
  ApiEndpointUnavailableError,
  clearNetworkEndpointRoutingCache,
  getNetworkEndpointRoutingRevision,
  resolveApiUrlForCurrentNetwork,
} from './connection-routing';
import {
  isInvalidApiUrlError,
  looksLikeHtml,
  normalizeApiUrl,
  requireConfiguredApiUrl,
} from './api-url';
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

/**
 * A durable operation was created for one configured AoiTalk server but the
 * user changed API settings before the request/retry completed.  Callers must
 * strand the operation instead of replaying it against the new server.
 */
export class ApiServerChangedError extends Error {
  readonly expectedFingerprint: string;
  readonly currentFingerprint: string;

  constructor(expectedFingerprint: string, currentFingerprint: string) {
    super(
      `API server changed from ${expectedFingerprint} to ${currentFingerprint}`,
    );
    this.name = 'ApiServerChangedError';
    this.expectedFingerprint = expectedFingerprint;
    this.currentFingerprint = currentFingerprint;
  }
}

export { ApiEndpointUnavailableError } from './connection-routing';

export function isApiServerChangedError(
  error: unknown,
): error is ApiServerChangedError {
  return error instanceof ApiServerChangedError;
}

/**
 * Stable identity for the configured API endpoint.  Network-specific LAN /
 * public routing is intentionally not included in this value.  Credentials,
 * query parameters and fragments are never persisted in a journal key.
 */
export function normalizeApiServerFingerprint(value: string): string {
  const normalized = normalizeApiUrl(value);
  try {
    const parsed = new URL(normalized);
    parsed.username = '';
    parsed.password = '';
    parsed.search = '';
    parsed.hash = '';
    const pathname = parsed.pathname.replace(/\/+$/, '');
    return (
      `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}`
      + (pathname && pathname !== '/' ? pathname : '')
    );
  } catch {
    return normalized.replace(/\/+$/, '');
  }
}

/** Persist/replay identity of the currently configured API server. */
export async function getConfiguredApiServerFingerprint(): Promise<string> {
  const stored = await getApiUrl();
  return normalizeApiServerFingerprint(stored || DEFAULT_API_URL);
}

let cachedApiUrl: string | null = null;
// 同時に走る別APIの成功を、あとから完了した古い通信失敗で上書きしない。
let reachabilitySuccessRevision = 0;
let reachabilityEndpointRevision = 0;

/** 現在のAPI URLを取得 */
export async function getBaseUrl(): Promise<string> {
  if (cachedApiUrl === null) {
    for (;;) {
      const revisionAtStart = reachabilityEndpointRevision;
      const stored = await getApiUrl();
      if (revisionAtStart !== reachabilityEndpointRevision) continue;
      cachedApiUrl = normalizeApiUrl(stored || DEFAULT_API_URL);
      break;
    }
  }

  const endpointRevisionAtStart = reachabilityEndpointRevision;
  const routingRevisionAtStart = getNetworkEndpointRoutingRevision();
  const routed = await resolveApiUrlForCurrentNetwork(cachedApiUrl, {
    probe: probeApiEndpoint,
  });
  if (
    endpointRevisionAtStart !== reachabilityEndpointRevision ||
    routingRevisionAtStart !== getNetworkEndpointRoutingRevision()
  ) {
    // clearApiUrlCache() can race a route probe after that probe has populated
    // its short-lived cache.  Invalidate the route cache before retrying so a
    // stale endpoint cannot win the second resolution.
    clearNetworkEndpointRoutingCache();
    return getBaseUrl();
  }
  return requireConfiguredApiUrl(routed);
}

async function getBaseUrlForServerFingerprint(
  expectedFingerprint: string,
): Promise<string> {
  const expected = normalizeApiServerFingerprint(expectedFingerprint);
  for (;;) {
    const endpointRevisionAtStart = reachabilityEndpointRevision;
    const routingRevisionAtStart = getNetworkEndpointRoutingRevision();
    const current = await getConfiguredApiServerFingerprint();
    if (current !== expected) {
      throw new ApiServerChangedError(expected, current);
    }
    // The configured server may still resolve to a LAN/public endpoint for the
    // current network; that routing does not change durable server identity.
    const routed = await resolveApiUrlForCurrentNetwork(expected, {
      probe: probeApiEndpoint,
    });
    // Routing can await network/storage state. Re-check after that await so a
    // settings write racing the resolution cannot send the operation to a new
    // configured server.
    const afterRouting = await getConfiguredApiServerFingerprint();
    if (afterRouting !== expected) {
      throw new ApiServerChangedError(expected, afterRouting);
    }
    if (
      endpointRevisionAtStart !== reachabilityEndpointRevision ||
      routingRevisionAtStart !== getNetworkEndpointRoutingRevision()
    ) {
      continue;
    }
    return requireConfiguredApiUrl(routed);
  }
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
  if (
    typeof ApiEndpointUnavailableError !== "undefined" &&
    error instanceof ApiEndpointUnavailableError
  ) {
    return true;
  }
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
  clientTimeoutCountsAsUnreachable = false,
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
    const clientTimedOut = Boolean(isClientTimeout?.());
    // 自前timeoutでの打ち切りは「サーバーが応答しない」とは限らない
    // （処理が長いだけのことがある）ので、未到達として記録しない。
    // 明示的なread-only reachability probeだけは例外として未到達扱いできる。
    if (
      (!clientTimedOut || clientTimeoutCountsAsUnreachable) &&
      isApiConnectionError(error) &&
      reachabilityEndpointRevision === endpointRevisionAtStart &&
      reachabilitySuccessRevision === successRevisionAtStart
    ) {
      useNetworkStore.getState().setServerReachable(false);
    }
    throw error;
  }
}

function isAoiTalkHealthPayload(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const payload = value as { status?: unknown; boot_id?: unknown };
  // FastAPI's /api/health returns {status: "ok"|"degraded", boot_id}.
  // Requiring both fields prevents a generic JSON endpoint from being treated
  // as an AoiTalk server merely because it returned HTTP 200.
  if (payload.status !== "ok" && payload.status !== "degraded") return false;
  return typeof payload.boot_id === "string" && payload.boot_id.trim().length > 0;
}

/**
 * Probe one endpoint without routing through getBaseUrl().  This is used by
 * route selection, so it must not recursively resolve the same route.
 *
 * The Caddy boundary requires a non-empty Authorization header before it
 * forwards /api/* to FastAPI.  The value below is deliberately non-secret and
 * is accepted by the unauthenticated FastAPI health route; it is never used
 * for an application request or persisted as a credential.
 */
async function probeApiEndpoint(
  endpoint: string,
  timeoutMs = 2_000,
): Promise<boolean> {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  const endpointRevisionAtStart = reachabilityEndpointRevision;
  const successRevisionAtStart = reachabilitySuccessRevision;
  let timedOut = false;

  try {
    const baseUrl = requireConfiguredApiUrl(endpoint);
    const controller = new AbortController();
    timeoutId = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    const response = await fetch(`${baseUrl}/api/health`, {
      method: 'GET',
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        Authorization: "Bearer aoitalk-route-probe",
      },
    });
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    const reachable = isAoiTalkHealthPayload(payload);

    if (reachabilityEndpointRevision === endpointRevisionAtStart) {
      if (reachable) {
        reachabilitySuccessRevision += 1;
        useNetworkStore.getState().setServerReachable(true);
      } else if (reachabilitySuccessRevision === successRevisionAtStart) {
        useNetworkStore.getState().setServerReachable(false);
      }
    }
    return reachable;
  } catch (error) {
    if (
      reachabilityEndpointRevision === endpointRevisionAtStart &&
      reachabilitySuccessRevision === successRevisionAtStart &&
      ((!timedOut && isApiConnectionError(error)) || timedOut)
    ) {
      useNetworkStore.getState().setServerReachable(false);
    }
    return false;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
  }
}

/**
 * Read-only AoiTalk API reachability probe.
 *
 * A probe timeout/failure is safe to classify as temporarily unreachable
 * because GET /api/health has no ambiguous server-side write outcome.  A
 * response must have AoiTalk's health shape; an HTML/JSON response from an
 * unrelated web server is not considered a usable API endpoint.
 */
export async function probeApiReachability(
  timeoutMs = 2_000,
): Promise<boolean> {
  try {
    const baseUrl = await getBaseUrl();
    return probeApiEndpoint(baseUrl, timeoutMs);
  } catch (error) {
    if (isApiConnectionError(error) || isInvalidApiUrlError(error)) return false;
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

const refreshInFlightByRevision = new Map<string, Promise<boolean>>();

async function refreshTokenOnce(
  snapshot: TokenSnapshot,
  expectedServerFingerprint: string | null,
): Promise<boolean> {
  const { token, revision } = snapshot;
  if (!token) return false;

  const baseUrl = expectedServerFingerprint
    ? await getBaseUrlForServerFingerprint(expectedServerFingerprint)
    : await getBaseUrl();
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, API_TIMEOUT);

  try {
    const res = await fetchWithReachability(`${baseUrl}/api/auth/refresh`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
    }, () => timedOut);

    // 認証拒否が確定した場合だけfalseを返す。通信断・一時的なHTTP障害を
    // falseにすると呼び出し元が認証失効を通知し、保存済みtokenを削除してしまう。
    if (res.status === 401 || res.status === 403) return false;
    if (!res.ok) {
      throw formatApiError(res.status, await res.text());
    }
    const data: unknown = await res.json();
    const accessToken = data && typeof data === 'object'
      ? (data as { access_token?: unknown }).access_token
      : undefined;
    if (typeof accessToken !== 'string' || !accessToken.trim()) {
      throw new Error('認証更新APIが有効なaccess_tokenを返しませんでした');
    }
    return await saveTokenIfRevision(accessToken, revision);
  } catch (error) {
    if (timedOut && error instanceof Error && error.name === 'AbortError') {
      throw new ApiTimeoutError(API_TIMEOUT);
    }
    // 一時障害や保存失敗は元の原因を保持し、次の同期で再試行できるようにする。
    throw error;
  } finally {
    // ヘッダー受信時点で解除すると、本文が停止したrefreshが全同期を塞ぐ。
    clearTimeout(timeoutId);
  }
}

/**
 * 同じ認証世代の並行401を1リクエストへ集約する。
 * falseは認証拒否・token不在・世代変更。一時的な通信/HTTP/保存障害はrejectし、
 * 呼び出し元が保存済み認証を失効させず再試行できるよう区別する。
 */
export async function tryRefreshToken(
  expectedSnapshot?: TokenSnapshot,
  expectedServerFingerprint: string | null = null,
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

  const normalizedFingerprint = expectedServerFingerprint
    ? normalizeApiServerFingerprint(expectedServerFingerprint)
    : null;
  const refreshKey = `${snapshot.revision}:${normalizedFingerprint ?? '*'}`;
  const inFlight = refreshInFlightByRevision.get(refreshKey);
  if (inFlight) return inFlight;

  const refresh = refreshTokenOnce(snapshot, normalizedFingerprint);
  refreshInFlightByRevision.set(refreshKey, refresh);
  // `finally()` would create a second rejected promise when a pinned refresh
  // detects a server change.  Attach both fulfillment/rejection handlers so
  // cleanup never surfaces as an unhandled rejection; the original promise
  // still rejects to the request awaiting it.
  void refresh.then(() => {
    if (refreshInFlightByRevision.get(refreshKey) === refresh) {
      refreshInFlightByRevision.delete(refreshKey);
    }
  }, () => {
    if (refreshInFlightByRevision.get(refreshKey) === refresh) {
      refreshInFlightByRevision.delete(refreshKey);
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
  expectedServerFingerprint: string | null,
  parseResponse: (response: Response) => Promise<T> = async (response) =>
    response.json() as Promise<T>,
): Promise<T> {
  const tokenSnapshot = await getTokenSnapshot();
  const token = tokenSnapshot.token;
  const baseUrl = expectedServerFingerprint
    ? await getBaseUrlForServerFingerprint(expectedServerFingerprint)
    : await getBaseUrl();
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
        return fetchApiInternal(
          path,
          options,
          timeout,
          authRetryBudget - 1,
          expectedServerFingerprint,
          parseResponse,
        );
      }

      const refreshed = await tryRefreshToken(
        tokenSnapshot,
        expectedServerFingerprint,
      );
      if (!refreshed) {
        const afterRefreshSnapshot = await getTokenSnapshot();
        if (
          afterRefreshSnapshot.token &&
          (afterRefreshSnapshot.revision !== tokenSnapshot.revision ||
            afterRefreshSnapshot.token !== tokenSnapshot.token)
        ) {
          return fetchApiInternal(
            path,
            options,
            timeout,
            authRetryBudget - 1,
            expectedServerFingerprint,
            parseResponse,
          );
        }
        throwAuthInvalidated();
      }
      // refresh後も401なら再帰せず、認証エラーとして呼び出し元へ返す。
      return fetchApiInternal(
        path,
        options,
        timeout,
        0,
        expectedServerFingerprint,
        parseResponse,
      );
    }

    if (!res.ok) {
      const text = await res.text();
      throw formatApiError(res.status, text);
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    // 本文の受信・解析完了までtimeoutとcatchを有効にする。awaitを省くと
    // finallyが先に実行され、止まった本文がsyncのsingle-flightを占有し続ける。
    return await parseResponse(res);
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
  return fetchApiInternal(path, options, timeout, 2, null);
}

/** Durable mutation/read pinned to the configured server captured at intent. */
export async function fetchApiAtServerFingerprint<T>(
  serverFingerprint: string,
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT,
): Promise<T> {
  return fetchApiInternal(
    path,
    options,
    timeout,
    2,
    normalizeApiServerFingerprint(serverFingerprint),
  );
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
  return fetchApiInternal(
    path,
    options,
    timeout,
    2,
    null,
    (response) => response.text(),
  );
}

export { AuthError };
