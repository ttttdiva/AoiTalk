/**
 * APIクライアント — Bearer token自動付与 + リフレッシュ
 */

import {
  getApiUrl,
  getTokenSnapshot,
  getTokenAuthScope,
  readAuthActor,
  saveTokenIfRevision,
} from "./auth";
import { DEFAULT_API_URL, DEFAULT_PORTAL_URL, API_TIMEOUT } from '../constants/config';
import {
  ApiEndpointUnavailableError,
  clearNetworkEndpointRoutingCache,
  getNetworkEndpointRoutingRevision,
  getNetworkEndpointRoutingConfig,
  resolveApiUrlForCurrentNetwork,
  getObservedNetworkRouteSnapshot,
  type NetworkRouteSnapshot,
} from './connection-routing';
import {
  CredentialTargetError,
  getRequestAuthHeaders,
  mergeRequestAuthHeaders,
} from './auth-headers';
import { protectedFetch } from './protected-fetch';
import {
  GatewayReauthRequiredError,
  GATEWAY_STARTUP_TIMEOUT_MS,
  awaitGatewayServiceReady,
  clearGatewayRuntimeCaches,
  clearGatewayServiceReadyCache,
  getGatewayProbeHeaders,
  isGatewayTarget,
  normalizeGatewayServiceUrl,
  refreshPortalAuth,
} from './portal-auth';
import { isGatewayAuthResponse } from '@aoi-talk/mobile-gateway-auth';

export { protectedFetch } from './protected-fetch';

function isMutationMethod(method: string | undefined): boolean {
  const normalized = (method ?? "GET").toUpperCase();
  return normalized !== "GET" && normalized !== "HEAD" && normalized !== "OPTIONS";
}
export {
  bootstrapPrimaryIdentity,
  getRequestAuthHeaders,
  mergeRequestAuthHeaders,
} from "./auth-headers";
export { isGatewayTarget, GatewayReauthRequiredError } from "./portal-auth";
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

/**
 * A bounded, non-secret identifier for correlating one logical API call with
 * the server access log.  Callers may provide their own safe identifier; the
 * default is generated locally and never contains auth or request content.
 */
let nextApiRequestSequence = 0;

export function createApiRequestId(prefix = "mobile"): string {
  const randomUuid = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : null;
  const suffix = randomUuid
    ?? `${Date.now().toString(36)}-${(++nextApiRequestSequence).toString(36)}`;
  return `${prefix}-${suffix}`.slice(0, 96);
}

export type ApiRequestLifecycle = {
  requestId: string;
  /** Called immediately before every fetch attempt, including retries. */
  onAttempt?: (event: { requestId: string }) => void;
  /** Called for every HTTP response; auth retries keep the same requestId. */
  onResponse?: (event: { requestId: string; status: number }) => void;
};

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
  readonly requestMethod?: string;
  readonly requestPath?: string;

  constructor(timeoutMs: number, request?: { method: string; path: string }) {
    // Only retain a path without query parameters. Never log request bodies,
    // auth headers or an absolute URL when reporting the failed boundary.
    const path = request?.path.split("?")[0];
    const method = request?.method.toUpperCase();
    const boundary = method && path ? `（${method} ${path}）` : "";
    super(`処理が${Math.round(timeoutMs / 1000)}秒以内に終わりませんでした${boundary}`);
    this.name = 'ApiTimeoutError';
    this.timeoutMs = timeoutMs;
    this.requestMethod = method;
    this.requestPath = path;
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

/** A durable request must not retry against a route changed mid-flight. */
export class ApiRoutingChangedError extends Error {
  readonly expectedRevision: number;
  readonly currentRevision: number;

  constructor(expectedRevision: number, currentRevision: number) {
    super(`API network route changed from revision ${expectedRevision} to ${currentRevision}`);
    this.name = "ApiRoutingChangedError";
    this.expectedRevision = expectedRevision;
    this.currentRevision = currentRevision;
  }
}

export class ApiNetworkChangedError extends Error {
  readonly expectedNetworkKey: string;
  readonly currentNetworkKey: string;

  constructor(expectedNetworkKey: string, currentNetworkKey: string) {
    super(`API network changed from ${expectedNetworkKey} to ${currentNetworkKey}`);
    this.name = "ApiNetworkChangedError";
    this.code = "network_changed";
    this.expectedNetworkKey = expectedNetworkKey;
    this.currentNetworkKey = currentNetworkKey;
  }

  readonly code: "network_changed";
}

export { ApiEndpointUnavailableError } from './connection-routing';
export function isGatewayReauthRequiredError(
  error: unknown,
): error is GatewayReauthRequiredError {
  return error instanceof GatewayReauthRequiredError || (
    error instanceof Error &&
    (error as Error & { code?: unknown }).code === "gateway_reauth_required"
  );
}

export function isApiServerChangedError(
  error: unknown,
): error is ApiServerChangedError {
  return error instanceof ApiServerChangedError;
}

export function isApiRoutingChangedError(
  error: unknown,
): error is ApiRoutingChangedError {
  return error instanceof ApiRoutingChangedError;
}

/**
 * Stable identity for the configured API namespace. Network-specific LAN /
 * public routing is intentionally not included, while an explicit service
 * path remains part of the namespace. Credentials, query parameters and
 * fragments are never persisted in a journal key.
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

/**
 * Validate an already-resolved route while preserving its configured service
 * path. Persistent settings use normalizeApiUrl() and therefore retain the
 * selected `/at` or direct service prefix.
 */
function requireResolvedApiUrl(value: string): string {
  requireConfiguredApiUrl(value);
  const parsed = new URL(value.trim());
  parsed.search = "";
  parsed.hash = "";
  return parsed.toString().replace(/\/+$/, "");
}

/** Append a canonical `/api/*` route without duplicating a configured prefix. */
function appendApiPath(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/+$/, "");
  try {
    const parsed = new URL(base);
    const prefix = parsed.pathname.replace(/\/+$/, "") || "/";
    if (
      prefix !== "/" &&
      path === prefix
    ) {
      return `${base}${path.slice(prefix.length)}` || base;
    }
    if (
      prefix !== "/" &&
      path.startsWith(`${prefix}/`)
    ) {
      return `${base}${path.slice(prefix.length)}`;
    }
  } catch {
    // requireResolvedApiUrl validates the base before this helper is called.
  }
  return `${base}${path}`;
}
/** Persist/replay identity of the currently configured API server. */
export async function getConfiguredApiServerFingerprint(): Promise<string> {
  const stored = await getApiUrl();
  return normalizeApiServerFingerprint(stored || DEFAULT_API_URL);
}

/** Read the selected Gateway binding once for one route/request operation. */
async function getSelectedGatewayServiceUrl(): Promise<string | null> {
  const routing = await getNetworkEndpointRoutingConfig();
  if (routing.mode !== "gateway") return null;
  return normalizeGatewayServiceUrl(`${routing.portalUrl || DEFAULT_PORTAL_URL}/at`);
}

export async function getCredentialIdentityRevision(): Promise<string> {
  const [fingerprint, snapshot] = await Promise.all([
    getConfiguredApiServerFingerprint(),
    getTokenSnapshot(),
  ]);
  return `${fingerprint}|${getNetworkEndpointRoutingRevision()}|${snapshot.revision}`;
}

const legacyGatewayAliases = new Map<string, string>();

function rememberLegacyGatewayAlias(primary: string, resolved: string): void {
  legacyGatewayAliases.set(
    normalizeApiServerFingerprint(resolved),
    normalizeApiServerFingerprint(primary),
  );
}

function hasApprovedLegacyGatewayAlias(primary: string, resolved: string): boolean {
  return legacyGatewayAliases.get(normalizeApiServerFingerprint(resolved))
    === normalizeApiServerFingerprint(primary);
}

let cachedApiUrl: string | null = null;
// 同時に走る別APIの成功を、あとから完了した古い通信失敗で上書きしない。
let reachabilitySuccessRevision = 0;
let reachabilityEndpointRevision = 0;

/** 現在のAPI URLを取得 */
export interface BaseUrlOptions {
  /** Abort this caller while a route or Gateway service is starting. */
  signal?: AbortSignal;
  /** Capture the selected network before a Gateway readiness wait. */
  onResolution?: (resolution: import("./connection-routing").NetworkEndpointResolution) => void;
}

export async function getBaseUrl(options: BaseUrlOptions = {}): Promise<string> {
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
  const gatewayServiceUrl = await getSelectedGatewayServiceUrl();
  const routed = await resolveApiUrlForCurrentNetwork(cachedApiUrl, {
      probe: (endpoint, timeoutMs, signal) =>
        probeApiEndpoint(endpoint, timeoutMs ?? 3_500, gatewayServiceUrl, signal),
    gatewayApiUrl: gatewayServiceUrl ?? undefined,
      signal: options.signal,
    onResolution: options.onResolution,
  });
  if (
    endpointRevisionAtStart !== reachabilityEndpointRevision ||
    routingRevisionAtStart !== getNetworkEndpointRoutingRevision()
  ) {
    // clearApiUrlCache() can race a route probe after that probe has populated
    // its short-lived cache.  Invalidate the route cache before retrying so a
    // stale endpoint cannot win the second resolution.
    clearNetworkEndpointRoutingCache();
    return getBaseUrl(options);
  }
  return requireResolvedApiUrl(routed);
}

async function getBaseUrlForServerFingerprint(
  expectedFingerprint: string,
  options: BaseUrlOptions = {},
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
    const gatewayServiceUrl = await getSelectedGatewayServiceUrl();
    const routed = await resolveApiUrlForCurrentNetwork(expected, {
      probe: (endpoint, timeoutMs, signal) =>
        probeApiEndpoint(endpoint, timeoutMs ?? 3_500, gatewayServiceUrl, signal),
      gatewayApiUrl: gatewayServiceUrl ?? undefined,
      signal: options.signal,
      onResolution: options.onResolution,
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
    return requireResolvedApiUrl(routed);
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

/** Gateway 401s are identified by the Worker-owned response marker. */
async function isGatewayAuthFailure(response: Response): Promise<boolean> {
  return isGatewayAuthResponse(response);
}

function headerValue(
  headers: HeadersInit | undefined,
  name: string,
): string | null {
  const lowerName = name.toLowerCase();
  if (!headers) return null;
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    const value = headers.get(name);
    return value?.trim() || null;
  }
  if (Array.isArray(headers)) {
    const entry = headers.find(([key]) => key.toLowerCase() === lowerName);
    return entry?.[1]?.trim() || null;
  }
  const record = headers as Record<string, string | undefined>;
  const key = Object.keys(record).find((candidate) => candidate.toLowerCase() === lowerName);
  return key ? record[key]?.trim() || null : null;
}

function addRequestIdHeader(
  headers: HeadersInit | undefined,
  requestId: string,
): Record<string, string> {
  const result: Record<string, string> = {};
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    headers.forEach((value, key) => {
      result[key] = value;
    });
  } else if (Array.isArray(headers)) {
    for (const [key, value] of headers) result[key] = value;
  } else if (headers) {
    Object.assign(result, headers);
  }
  if (!headerValue(headers, "x-request-id")) result["X-Request-ID"] = requestId;
  return result;
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
    const response = await protectedFetch(input, init);
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
  timeoutMs = 3_500,
  gatewayServiceUrl: string | null = null,
  callerSignal?: AbortSignal,
): Promise<string | false> {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  const endpointRevisionAtStart = reachabilityEndpointRevision;
  let abortCaller: (() => void) | null = null;

  const fetchHealth = async (
    baseUrl: string,
    signal: AbortSignal,
  ): Promise<{ endpoint: string | null; gatewayRouteMissing: boolean }> => {
    const target = appendApiPath(baseUrl, "/api/health");
    let authHeaders: Record<string, string> = {
      // Keep the health probe independent of the application account in
      // direct mode.  Gateway mode replaces this with the SDK header below.
      Authorization: "Bearer aoitalk-route-probe",
    };
    if (gatewayServiceUrl && isGatewayTarget(baseUrl, gatewayServiceUrl)) {
      // A Gateway candidate is the only route allowed to wake a service.
      // Keep this startup budget separate from the ordinary health probe's
      // short timeout so a cold service can become ready without turning a
      // normal 10s API deadline into an accidental retry loop.
      if (typeof awaitGatewayServiceReady !== "function") {
        throw new GatewayReauthRequiredError();
      }
      try {
        await awaitGatewayServiceReady(baseUrl, {
          signal: callerSignal,
          timeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
        });
      } catch (error) {
        if (error instanceof GatewayReauthRequiredError) {
          return { endpoint: baseUrl, gatewayRouteMissing: false };
        }
        throw error;
      }
      try {
        // Health/identity probes carry only the SDK Gateway credential. The
        // application JWT is attached after the target identity is verified.
        authHeaders = await getGatewayProbeHeaders(target, gatewayServiceUrl);
      } catch (error) {
        // A missing Gateway credential is a re-auth boundary, not evidence
        // that the selected portal is down. Return the candidate so the real
        // request surfaces the dedicated reauth error without falling back to
        // a direct server.
        if (error instanceof GatewayReauthRequiredError) return {
          endpoint: baseUrl,
          gatewayRouteMissing: false,
        };
        throw error;
      }
    }
    const response = await protectedFetch(target, {
      method: 'GET',
      signal,
      headers: { Accept: "application/json", ...authHeaders },
    });

    if (response.status === 404) {
      let body = "";
      try {
        body = await response.text();
      } catch {
        // A body read failure is not evidence that this is the known Gateway.
      }
      return {
        endpoint: null,
        gatewayRouteMissing: body.trim() === "Gateway route not found",
      };
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    return {
      endpoint: isAoiTalkHealthPayload(payload) ? baseUrl : null,
      gatewayRouteMissing: false,
    };
  };

  try {
    const baseUrl = requireResolvedApiUrl(endpoint);
    const controller = new AbortController();
    abortCaller = () => controller.abort();
    callerSignal?.addEventListener("abort", abortCaller, { once: true });
    timeoutId = setTimeout(() => {
      controller.abort();
    }, timeoutMs);

    let result = await awaitRequest(fetchHealth(baseUrl, controller.signal), controller.signal);
    if (
      !result.endpoint &&
      result.gatewayRouteMissing &&
      !gatewayServiceUrl &&
      !new URL(baseUrl).pathname.replace(/\/+$/, "").endsWith("/at")
    ) {
      const prefixedBaseUrl = `${baseUrl}/at`;
      const prefixed = await awaitRequest(fetchHealth(prefixedBaseUrl, controller.signal), controller.signal);
      if (prefixed.endpoint) rememberLegacyGatewayAlias(baseUrl, prefixed.endpoint);
      result = prefixed;
    }

    const reachableEndpoint = result.endpoint;
    if (
      reachableEndpoint &&
      reachabilityEndpointRevision === endpointRevisionAtStart
    ) {
      reachabilitySuccessRevision += 1;
      useNetworkStore.getState().setServerReachable(true);
    }
    return reachableEndpoint ?? false;
  } catch (error) {
    if (callerSignal?.aborted) {
      throw error;
    }
    // Route selection owns the aggregate failure decision. One failed endpoint
    // probe must not flip the whole app offline when another route or a
    // confirmation probe can still succeed.
    return false;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
    // The callback is attached after the URL is validated, so a caller abort
    // never outlives this probe. (The event target may be absent in tiny test
    // doubles, hence the optional calls.)
    if (abortCaller) callerSignal?.removeEventListener("abort", abortCaller);
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
  const controller = new AbortController();
  const deadline = Date.now() + Math.max(1, timeoutMs);
  const timeoutId = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  try {
    const baseUrl = await awaitRequest(
      getBaseUrl({ signal: controller.signal }),
      controller.signal,
    );
    const gatewayServiceUrl = await awaitRequest(
      getSelectedGatewayServiceUrl(),
      controller.signal,
    );
    const remaining = Math.max(1, deadline - Date.now());
    const reachable = Boolean(await probeApiEndpoint(
      baseUrl,
      remaining,
      gatewayServiceUrl,
      controller.signal,
    ));
    if (!reachable) useNetworkStore.getState().setServerReachable(false);
    return reachable;
  } catch (error) {
    if (isApiConnectionError(error) || isInvalidApiUrlError(error)) {
      useNetworkStore.getState().setServerReachable(false);
      return false;
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

/** API URLキャッシュをクリア（設定変更時） */
export function clearApiUrlCache(): void {
  cachedApiUrl = null;
  legacyGatewayAliases.clear();
  if (typeof clearGatewayServiceReadyCache === "function") {
    clearGatewayServiceReadyCache();
  }
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

  const controller = new AbortController();
  let timedOut = false;
  let timeoutId: ReturnType<typeof setTimeout> | null = startRequestDeadline(
    controller,
    API_TIMEOUT,
    (value) => { timedOut = value; },
  );

  try {
    const routingConfigSnapshot = await awaitRequest(
      getNetworkEndpointRoutingConfig(),
      controller.signal,
    );
    if (routingConfigSnapshot.mode === "gateway") {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    const baseUrl = await awaitRequest(
      expectedServerFingerprint
        ? getBaseUrlForServerFingerprint(expectedServerFingerprint, {
          signal: controller.signal,
        })
        : getBaseUrl({ signal: controller.signal }),
      controller.signal,
    );
    checkRequestSignal(controller.signal);
    const beforeRefreshAuth = await awaitRequest(getTokenSnapshot(), controller.signal);
    if (
      beforeRefreshAuth.revision !== revision ||
      beforeRefreshAuth.token !== token
    ) {
      return false;
    }
    const gatewayServiceUrl = await awaitRequest(
      getSelectedGatewayServiceUrl(),
      controller.signal,
    );
    if (gatewayServiceUrl && isGatewayTarget(baseUrl, gatewayServiceUrl)) {
      if (typeof awaitGatewayServiceReady === "function") {
        await awaitRequest(
          awaitGatewayServiceReady(baseUrl, {
            signal: controller.signal,
            timeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
          }),
          controller.signal,
        );
      }
    }
    if (timeoutId === null) {
      timeoutId = startRequestDeadline(
        controller,
        API_TIMEOUT,
        (value) => { timedOut = value; },
      );
    }
    const primaryBaseUrl = (await awaitRequest(getApiUrl(), controller.signal))
      || DEFAULT_API_URL;
    const refreshTarget = appendApiPath(baseUrl, "/api/auth/refresh");
    const refreshAuthHeaders = await getRequestAuthHeaders(refreshTarget, {
      gateway: Boolean(gatewayServiceUrl && isGatewayTarget(baseUrl, gatewayServiceUrl)),
      gatewayServiceUrl: gatewayServiceUrl ?? undefined,
      applicationToken: token,
      primaryBaseUrl,
      targetBaseUrl: baseUrl,
      approvedPrimaryAlias: hasApprovedLegacyGatewayAlias(primaryBaseUrl, baseUrl),
      identityRevisionSnapshot: () => getCredentialIdentityRevision(),
      signal: controller.signal,
      startupTimeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
    });
    const res = await awaitRequest(fetchWithReachability(refreshTarget, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...refreshAuthHeaders,
      },
    }, () => timedOut), controller.signal);

    // 認証拒否が確定した場合だけfalseを返す。通信断・一時的なHTTP障害を
    // falseにすると呼び出し元が認証失効を通知し、保存済みtokenを削除してしまう。
    if (res.status === 401 || res.status === 403) return false;
    if (!res.ok) {
      throw formatApiError(res.status, await awaitRequest(res.text(), controller.signal));
    }
    const data: unknown = await awaitRequest(res.json(), controller.signal);
    const accessToken = data && typeof data === 'object'
      ? (data as { access_token?: unknown }).access_token
      : undefined;
    if (typeof accessToken !== 'string' || !accessToken.trim()) {
      throw new Error('認証更新APIが有効なaccess_tokenを返しませんでした');
    }
    // SecureStore writes are serialized behind the auth mutation queue and
    // may wait indefinitely when another token mutation is stalled. Keep the
    // refresh flight's own deadline around this await so its shared entry can
    // be released even though the underlying mutation is still finishing.
    return await awaitRequest(
      saveTokenIfRevision(accessToken, revision),
      controller.signal,
    );
  } catch (error) {
    if (timedOut && error instanceof Error && error.name === 'AbortError') {
      throw new ApiTimeoutError(API_TIMEOUT);
    }
    // 一時障害や保存失敗は元の原因を保持し、次の同期で再試行できるようにする。
    throw error;
  } finally {
    // ヘッダー受信時点で解除すると、本文が停止したrefreshが全同期を塞ぐ。
    if (timeoutId !== null) clearTimeout(timeoutId);
  }
}

async function getTokenSnapshotWithDeadline(): Promise<TokenSnapshot> {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, API_TIMEOUT);
  try {
    return await awaitRequest(getTokenSnapshot(), controller.signal);
  } catch (error) {
    if (timedOut && error instanceof Error && error.name === "AbortError") {
      throw new ApiTimeoutError(API_TIMEOUT);
    }
    throw error;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
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
  const snapshot = expectedSnapshot ?? (await getTokenSnapshotWithDeadline());
  if (!snapshot.token) return false;

  // AuthContext calls tryRefreshToken directly, without fetchApiInternal's
  // caller-owned AbortSignal. Bound this second storage read as well so a
  // stalled SecureStore/token queue cannot leave startup refresh pending.
  const current = await getTokenSnapshotWithDeadline();
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

/** Stop only this caller, never a shared authentication refresh. */
function requestAborted(): Error {
  const error = new Error('リクエストを中止しました。');
  error.name = 'AbortError';
  return error;
}

function checkRequestSignal(signal?: AbortSignal | null): void {
  if (signal?.aborted) throw requestAborted();
}

function awaitRequest<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => { signal.removeEventListener('abort', onAbort); reject(requestAborted()); };
    signal.addEventListener('abort', onAbort, { once: true });
    // Consume a late rejection even when the caller has already cancelled.
    void operation.then((value) => {
      signal.removeEventListener('abort', onAbort);
      if (signal.aborted) reject(requestAborted());
      else resolve(value);
    }, (error: unknown) => {
      signal.removeEventListener('abort', onAbort);
      reject(signal.aborted ? requestAborted() : error);
    });
    if (signal.aborted) onAbort();
  });
}

function startRequestDeadline(
  controller: AbortController,
  timeout: number,
  setTimedOut: (value: boolean) => void,
): ReturnType<typeof setTimeout> {
  return setTimeout(() => {
    setTimedOut(true);
    controller.abort();
  }, timeout);
}

function tokenAuthScope(value: string | null): string {
  if (typeof getTokenAuthScope === "function") return getTokenAuthScope(value);
  return value ? "auth:present" : "anonymous";
}

function makeCredentialRevisionError(message: string, targetNamespace: string): Error {
  if (typeof CredentialTargetError === "function") {
    return new CredentialTargetError(
      "credential_revision_changed",
      message,
      { targetNamespace },
    );
  }
  const error = new Error(message) as Error & { code?: string; targetNamespace?: string };
  error.code = "credential_revision_changed";
  error.targetNamespace = targetNamespace;
  return error;
}

function revisionExceptToken(value: string): string {
  const separator = value.lastIndexOf("|");
  return separator < 0 ? value : value.slice(0, separator);
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
  expectedAuthScope: string | null = null,
  lifecycle?: ApiRequestLifecycle,
  requestId?: string,
  expectedRoutingRevision: number | null = null,
): Promise<T> {
  checkRequestSignal(options.signal);
  const effectiveRequestId = requestId
    ?? headerValue(options.headers, "x-request-id")
    ?? lifecycle?.requestId
    ?? createApiRequestId();
  const controller = new AbortController();
  const onCallerAbort = () => controller.abort();
  options.signal?.addEventListener('abort', onCallerAbort, { once: true });
  if (options.signal?.aborted) onCallerAbort();
  let timedOut = false;
  // Route probing may include a Gateway cold-start wait (up to 180s). Start
  // the ordinary request deadline only after that preflight has selected a
  // route; direct/LAN probes retain their own short health timeout.
  let timeoutId: ReturnType<typeof setTimeout> | null = startRequestDeadline(
    controller,
    timeout,
    (value) => { timedOut = value; },
  );

  try {
    // Storage, network permission and route probing are part of this request's
    // wait budget. Race them without cancelling shared route/auth work.
    const tokenSnapshot = await awaitRequest(getTokenSnapshot(), controller.signal);
    checkRequestSignal(controller.signal);
    const token = tokenSnapshot.token;
    const requestAuthScope = tokenAuthScope(token);
    const actorSnapshot = await awaitRequest(
      typeof readAuthActor === "function"
        ? readAuthActor()
        : Promise.resolve({ readable: true, actor: null }),
      controller.signal,
    );
    const actorIdentitySnapshot = actorSnapshot.actor
      ? `${actorSnapshot.actor.serverFingerprint}|${actorSnapshot.actor.user_id}|${actorSnapshot.actor.role}`
      : actorSnapshot.readable ? "none" : "unreadable";
    const requestRoutingRevision = expectedRoutingRevision
      ?? getNetworkEndpointRoutingRevision();
    const primaryBaseUrlSnapshot = (await awaitRequest(getApiUrl(), controller.signal))
      || DEFAULT_API_URL;
    const requestServerFingerprint = normalizeApiServerFingerprint(
      expectedServerFingerprint ?? primaryBaseUrlSnapshot,
    );
    const routingConfigSnapshot = await awaitRequest(
      getNetworkEndpointRoutingConfig(),
      controller.signal,
    );
    const networkSnapshotRef: { value: NetworkRouteSnapshot | null } = { value: null };
    if (routingConfigSnapshot.mode === "gateway") {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    const checkOwner = (value: string | null) => {
      if (expectedAuthScope !== null && (!value || tokenAuthScope(value) !== expectedAuthScope)) {
        throw new Error('API read owner changed');
      }
    };
    checkOwner(token);
    const baseUrl = await awaitRequest(
      getBaseUrlForServerFingerprint(requestServerFingerprint, {
        signal: controller.signal,
        onResolution: (resolution) => {
          if (
            typeof getObservedNetworkRouteSnapshot === "function" &&
            (routingConfigSnapshot.mode === "gateway" || routingConfigSnapshot.enabled)
          ) {
            networkSnapshotRef.value = getObservedNetworkRouteSnapshot();
          }
        },
      }),
      controller.signal,
    );
    checkRequestSignal(controller.signal);
    const currentRoutingRevision = getNetworkEndpointRoutingRevision();
    if (currentRoutingRevision !== requestRoutingRevision) {
      throw new ApiRoutingChangedError(requestRoutingRevision, currentRoutingRevision);
    }
    const expectedNetworkSnapshot = networkSnapshotRef.value;
    if (
      expectedNetworkSnapshot &&
      typeof getObservedNetworkRouteSnapshot === "function"
    ) {
      const networkAfterReady = getObservedNetworkRouteSnapshot();
      if (!networkAfterReady) {
        // There was no observable network identity to compare.
        networkSnapshotRef.value = null;
      } else if (networkAfterReady.networkKey !== expectedNetworkSnapshot.networkKey) {
        throw new ApiNetworkChangedError(
          expectedNetworkSnapshot.networkKey,
          networkAfterReady.networkKey,
        );
      }
    }
    if (expectedAuthScope !== null) {
      checkOwner((await awaitRequest(getTokenSnapshot(), controller.signal)).token);
    }
    const url = appendApiPath(baseUrl, path);
    const gatewayServiceUrl = await awaitRequest(
      getSelectedGatewayServiceUrl(),
      controller.signal,
    );
    const gatewayRequest = Boolean(
      gatewayServiceUrl && isGatewayTarget(baseUrl, gatewayServiceUrl),
    );
    if (gatewayRequest) {
      // Route health normally fills the short ready cache. This second
      // boundary handles a cache expiry between route selection and auth
      // header construction while keeping startup outside the HTTP deadline.
      if (typeof awaitGatewayServiceReady === "function") {
        await awaitRequest(
          awaitGatewayServiceReady(baseUrl, {
            signal: controller.signal,
            timeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
          }),
          controller.signal,
        );
      }
    }
    // The timeout starts after route selection/ready wait.  Auth proof and the
    // actual HTTP request remain under the existing 10/30/60s caller budget.
    if (timeoutId === null) {
      timeoutId = startRequestDeadline(
        controller,
        timeout,
        (value) => { timedOut = value; },
      );
    }
    const primaryBaseUrl = primaryBaseUrlSnapshot;
    const identityRevisionBeforeHeaders = await awaitRequest(
      getCredentialIdentityRevision(),
      controller.signal,
    );
    let authHeaders = await awaitRequest(
      getRequestAuthHeaders(url, {
        gateway: gatewayRequest,
        gatewayServiceUrl: gatewayServiceUrl ?? undefined,
        applicationToken: token,
        primaryBaseUrl,
        targetBaseUrl: baseUrl,
        approvedPrimaryAlias: hasApprovedLegacyGatewayAlias(primaryBaseUrl, baseUrl),
        identityRevisionSnapshot: () => getCredentialIdentityRevision(),
        signal: controller.signal,
        startupTimeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
      }),
      controller.signal,
    );
    let identityRevisionAfterHeaders = await awaitRequest(
      getCredentialIdentityRevision(),
      controller.signal,
    );
    const actorAfterHeaders = await awaitRequest(
      typeof readAuthActor === "function"
        ? readAuthActor()
        : Promise.resolve({ readable: true, actor: null }),
      controller.signal,
    );
    let tokenAfterHeaders = await awaitRequest(
      getTokenSnapshot(),
      controller.signal,
    );
    checkRequestSignal(controller.signal);
    const tokenPrincipalChanged =
      tokenAuthScope(tokenAfterHeaders.token) !== requestAuthScope;
    if (tokenPrincipalChanged) {
      throw makeCredentialRevisionError(
        "authenticated principal changed before request send",
        baseUrl,
      );
    }
    if (
      tokenAfterHeaders.revision !== tokenSnapshot.revision
      || tokenAfterHeaders.token !== token
    ) {
      // A refresh for the same principal may complete while auth headers are
      // being built. Rebuild headers with the fresh token before the first
      // HTTP dispatch; account changes still fail closed above.
      authHeaders = await awaitRequest(
        getRequestAuthHeaders(url, {
          gateway: gatewayRequest,
          gatewayServiceUrl: gatewayServiceUrl ?? undefined,
          applicationToken: tokenAfterHeaders.token,
          primaryBaseUrl,
          targetBaseUrl: baseUrl,
          approvedPrimaryAlias: hasApprovedLegacyGatewayAlias(primaryBaseUrl, baseUrl),
          identityRevisionSnapshot: () => getCredentialIdentityRevision(),
          signal: controller.signal,
          startupTimeoutMs: GATEWAY_STARTUP_TIMEOUT_MS,
        }),
        controller.signal,
      );
      identityRevisionAfterHeaders = await awaitRequest(
        getCredentialIdentityRevision(),
        controller.signal,
      );
      tokenAfterHeaders = await awaitRequest(getTokenSnapshot(), controller.signal);
      if (tokenAuthScope(tokenAfterHeaders.token) !== requestAuthScope) {
        throw makeCredentialRevisionError(
          "authenticated principal changed before request send",
          baseUrl,
        );
      }
    }
    const actorIdentityAfterHeaders = actorAfterHeaders.actor
      ? `${actorAfterHeaders.actor.serverFingerprint}|${actorAfterHeaders.actor.user_id}|${actorAfterHeaders.actor.role}`
      : actorAfterHeaders.readable ? "none" : "unreadable";
    if (actorIdentitySnapshot !== actorIdentityAfterHeaders) {
      throw makeCredentialRevisionError(
        "authenticated actor changed before request send",
        baseUrl,
      );
    }
    const currentServerFingerprint = await awaitRequest(
      getConfiguredApiServerFingerprint(),
      controller.signal,
    );
    if (currentServerFingerprint !== requestServerFingerprint) {
      throw new ApiServerChangedError(requestServerFingerprint, currentServerFingerprint);
    }
    if (
      identityRevisionBeforeHeaders !== identityRevisionAfterHeaders &&
      revisionExceptToken(identityRevisionBeforeHeaders) !== revisionExceptToken(identityRevisionAfterHeaders)
    ) {
      throw makeCredentialRevisionError(
        "credential target revision changed before request send",
        baseUrl,
      );
    }
    if (
      isMutationMethod(options.method) &&
      getNetworkEndpointRoutingRevision() !== requestRoutingRevision
    ) {
      throw new ApiRoutingChangedError(
        requestRoutingRevision,
        getNetworkEndpointRoutingRevision(),
      );
    }
    if (
      expectedNetworkSnapshot &&
      typeof getObservedNetworkRouteSnapshot === "function"
    ) {
      const networkBeforeDispatch = getObservedNetworkRouteSnapshot();
      if (networkBeforeDispatch && networkBeforeDispatch.networkKey !== expectedNetworkSnapshot.networkKey) {
        throw new ApiNetworkChangedError(
          expectedNetworkSnapshot.networkKey,
          networkBeforeDispatch.networkKey,
        );
      }
    }
    try {
      lifecycle?.onAttempt?.({ requestId: effectiveRequestId });
    } catch {
      // Diagnostics are observers and must never change request semantics.
    }
    let res: Response;
    try {
    const requestHeaders = mergeRequestAuthHeaders(
      addRequestIdHeader(options.headers, effectiveRequestId),
      authHeaders,
    );
    // React Native's FormData implementation must supply its own multipart
    // boundary.  The JSON default is useful for ordinary requests, but an
    // explicit application/json header makes file uploads unreadable by
    // FastAPI.  Keep auth and request-id headers while letting fetch set the
    // multipart content type.
    const isMultipartBody = typeof FormData !== "undefined" && options.body instanceof FormData;
    if (isMultipartBody) {
      for (const name of Object.keys(requestHeaders)) {
        if (name.toLowerCase() === "content-type") delete requestHeaders[name];
      }
    }
    res = await awaitRequest(fetchWithReachability(
        url,
        {
          ...options,
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            ...requestHeaders,
          },
        },
        // Cancelling voice must not mark every other API/sync client offline.
        () => timedOut || Boolean(options.signal?.aborted),
      ), controller.signal);
    } catch (error) {
      if (
        gatewayRequest &&
        !options.signal?.aborted &&
        isApiConnectionError(error)
      ) {
        if (typeof clearGatewayRuntimeCaches === "function") {
          clearGatewayRuntimeCaches(baseUrl);
        }
        if (typeof clearGatewayServiceReadyCache === "function") {
          clearGatewayServiceReadyCache(baseUrl);
        }
      }
      throw error;
    }

    try {
      lifecycle?.onResponse?.({ requestId: effectiveRequestId, status: res.status });
    } catch {
      // Diagnostics are observers and must never change request semantics.
    }

    if (res.status === 401 && gatewayRequest && await isGatewayAuthFailure(res)) {
      if (authRetryBudget <= 0) {
        throw new GatewayReauthRequiredError();
      }
      await awaitRequest(refreshPortalAuth(baseUrl), controller.signal);
      return fetchApiInternal(
        path,
        options,
        timeout,
        authRetryBudget - 1,
        expectedServerFingerprint,
        parseResponse,
        expectedAuthScope,
        lifecycle,
        effectiveRequestId,
        requestRoutingRevision,
      );
    }

    if (res.status === 401) {
      if (expectedAuthScope !== null) {
        checkOwner((await awaitRequest(getTokenSnapshot(), controller.signal)).token);
      }
      if (authRetryBudget <= 0) {
        throwAuthInvalidated();
      }

      const currentSnapshot = await awaitRequest(getTokenSnapshot(), controller.signal);
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
          expectedAuthScope,
          lifecycle,
          effectiveRequestId,
          requestRoutingRevision,
        );
      }

      const refreshed = await awaitRequest(tryRefreshToken(
        tokenSnapshot,
        expectedServerFingerprint,
      ), controller.signal);
      if (!refreshed) {
        const afterRefreshSnapshot = await awaitRequest(getTokenSnapshot(), controller.signal);
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
            expectedAuthScope,
            lifecycle,
            effectiveRequestId,
            requestRoutingRevision,
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
        expectedAuthScope,
        lifecycle,
        effectiveRequestId,
        requestRoutingRevision,
      );
    }

    if (!res.ok) {
      const text = await awaitRequest(res.text(), controller.signal);
      throw formatApiError(res.status, text);
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    // 本文の受信・解析完了までtimeoutとcatchを有効にする。awaitを省くと
    // finallyが先に実行され、止まった本文がsyncのsingle-flightを占有し続ける。
    const result = await awaitRequest(parseResponse(res), controller.signal);
    if (expectedAuthScope !== null) {
      checkOwner((await awaitRequest(getTokenSnapshot(), controller.signal)).token);
    }
    return result;
  } catch (error) {
    // 自前のtimeoutで打ち切ったabortは、通信不能と区別できるようにして投げる。
    if (timedOut && error instanceof Error && error.name === 'AbortError') {
      throw new ApiTimeoutError(timeout, { method: options.method ?? "GET", path });
    }
    throw error;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
    options.signal?.removeEventListener('abort', onCallerAbort);
  }
}

/** Read-only request that cannot retry using a different authenticated owner. */
export async function fetchApiReadForAuthScope<T>(
  authScope: string,
  path: string,
  timeout: number = API_TIMEOUT,
): Promise<T> {
  return fetchApiInternal<T>(path, { method: 'GET' }, timeout, 2, null,
    async (response) => response.json() as Promise<T>, authScope);
}

/**
 * Read-only request pinned to both the authenticated owner and the configured
 * server namespace captured by a resolution preview.  Resolution flows must
 * use this adapter for a live current read; the ordinary read helper allows
 * route selection to resolve against the current server and therefore cannot
 * prove that a delayed response belongs to the preview's server.
 */
export async function fetchApiReadForAuthScopeAtServer<T>(
  authScope: string,
  serverFingerprint: string,
  path: string,
  timeout: number = API_TIMEOUT,
): Promise<T> {
  return fetchApiInternal<T>(
    path,
    { method: 'GET' },
    timeout,
    2,
    normalizeApiServerFingerprint(serverFingerprint),
    (response) => response.json() as Promise<T>,
    authScope,
  );
}

export async function fetchApi<T>(
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT,
  lifecycle?: ApiRequestLifecycle,
): Promise<T> {
  // token切替後の再試行1回 + refresh後の再試行1回を上限にする。
  return fetchApiInternal(path, options, timeout, 2, null, undefined, null, lifecycle);
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
 * Docs export などJSON以外のcanonical responseでも、通常のfetchApiと
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

/**
 * Authenticated binary response with the same route, refresh and timeout
 * behaviour as fetchApi.  Callers still own persistence and must not open a
 * server-provided URL directly; this is the byte transport boundary for
 * scoped file downloads.
 */
export async function fetchApiBytes(
  path: string,
  options: RequestInit = {},
  timeout: number = API_TIMEOUT,
): Promise<ArrayBuffer> {
  return fetchApiInternal(
    path,
    options,
    timeout,
    2,
    null,
    (response) => response.arrayBuffer(),
  );
}

export { AuthError };
