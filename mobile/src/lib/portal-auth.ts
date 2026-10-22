/**
 * AoiTalk public Gateway adapter.
 *
 * The Gateway SDK intentionally has no Expo dependency.  This module owns
 * the native adapters and exposes the small application contract consumed by
 * routing and the central API client.  The application JWT remains in the
 * existing auth store; Gateway credentials have their own SecureStore record
 * and are never treated as an application JWT.
 */

import * as SecureStore from "expo-secure-store";
import * as ExpoCrypto from "expo-crypto";
import type {
  AuthenticationResult,
  AuthStatus,
  MobileGatewayAuth,
} from "@aoi-talk/mobile-gateway-auth";
import {
  DEFAULT_GATEWAY_SERVICE_PATH,
  DEFAULT_PORTAL_URL,
  GATEWAY_CLIENT_ID,
  GATEWAY_REDIRECT_URI,
  GATEWAY_SERVICE_ID,
  isGatewayCallbackUrl,
  STORAGE_KEYS,
} from "../constants/config";
import { protectedFetch } from "./protected-fetch";
import type { SafeLifecycle } from "../stores/network";

export { isGatewayCallbackUrl } from "../constants/config";

export const GATEWAY_AUTHORIZATION_HEADER = "X-AZV-Gateway-Authorization";

/** The runtime wake is a separate bounded operation from ordinary API IO. */
export const GATEWAY_STARTUP_TIMEOUT_MS = 180_000;
export const GATEWAY_STARTUP_POLL_MS = 500;
/** A healthy service result may be reused briefly by concurrent transports. */
export const GATEWAY_READY_TTL_MS = 5_000;

export type GatewayServiceReadyState = "starting" | "ready" | "failed";

export interface GatewayServiceReadyResult {
  state: "ready";
  serviceId: string;
  started: boolean;
}

export interface GatewayServiceReadyOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  pollMs?: number;
  onState?: (state: GatewayServiceReadyState) => void;
}

type GatewayServiceReadyClient = MobileGatewayAuth & {
  awaitServiceReady?: (
    options?: GatewayServiceReadyOptions,
  ) => Promise<GatewayServiceReadyResult>;
};

type ReadyCacheEntry = {
  readyAt: number;
  result: GatewayServiceReadyResult;
  flight: Promise<GatewayServiceReadyResult> | null;
};

const gatewayReadyCache = new Map<string, ReadyCacheEntry>();

function setSafeLifecycle(
  state: SafeLifecycle,
  errorCode: string | null = null,
): void {
  // Tests and small embedders may load this adapter without the store.  The
  // lifecycle is diagnostic/UI state and must never change request semantics.
  try {
    if (typeof require !== "function") return;
    const networkModule = require("../stores/network") as {
      useNetworkStore?: { getState?: () => { setSafeLifecycle?: (state: SafeLifecycle, error?: string | null) => void } };
    };
    const setter = networkModule.useNetworkStore?.getState?.().setSafeLifecycle;
    setter?.(state, errorCode);
  } catch {
    // Do not leak a store initialization error into the auth transport.
  }
}

function safeGatewayErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" && code.trim() ? code.trim() : null;
}

function startupAbortError(): Error {
  const error = new Error("Gatewayサービスの起動待ちを中止しました");
  error.name = "AbortError";
  (error as Error & { code?: string }).code = "aborted";
  return error;
}

function awaitWithCallerAbort<T>(
  operation: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return operation;
  if (signal.aborted) return Promise.reject(startupAbortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(startupAbortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void operation.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(startupAbortError());
        else resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? startupAbortError() : error);
      },
    );
  });
}

/** SDK-compatible error shape used before the pure-ESM module is loaded. */
export class GatewayAuthError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number | null;
  readonly cause: unknown;

  constructor(
    code: string,
    message: string,
    options: { retryable?: boolean; status?: number | null; cause?: unknown } = {},
  ) {
    super(message);
    this.name = "GatewayAuthError";
    this.code = code;
    this.retryable = options.retryable === true;
    this.status = options.status ?? null;
    this.cause = options.cause;
  }
}

type GatewaySdkModule = typeof import("@aoi-talk/mobile-gateway-auth");

let gatewaySdkPromise: Promise<GatewaySdkModule> | null = null;
function loadGatewaySdk(): Promise<GatewaySdkModule> {
  if (!gatewaySdkPromise) {
    gatewaySdkPromise = import("@aoi-talk/mobile-gateway-auth");
  }
  return gatewaySdkPromise;
}

function isGatewaySdkError(error: unknown): error is { name?: string; code?: string } {
  return Boolean(error && typeof error === "object" && (
    (error as { name?: unknown }).name === "GatewayAuthError" ||
    typeof (error as { code?: unknown }).code === "string"
  ));
}

function isGatewayNetworkFailure(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const code = safeGatewayErrorCode(error);
  if (code === "transient" || code === "timeout" || code === "network_error") return true;
  const name = (error as { name?: unknown }).name;
  const message = (error as { message?: unknown }).message;
  return name === "AbortError" || name === "TypeError"
    || (typeof message === "string" && /network|timed out|timeout|failed to fetch/i.test(message));
}

function isGatewayCallerAbort(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const code = safeGatewayErrorCode(error);
  return code === "aborted" || (error as { name?: unknown }).name === "AbortError";
}

export interface PortalAuthConfig {
  /** HTTPS portal root. `/` and `/index.html` are accepted and normalized. */
  portalUrl: string;
  /** HTTPS AoiTalk service path. The production contract is `/at`. */
  servicePath: string;
  clientId: string;
  serviceId: string;
  redirectUri: string;
}

export const DEFAULT_PORTAL_AUTH_CONFIG: PortalAuthConfig = {
  portalUrl: DEFAULT_PORTAL_URL,
  servicePath: DEFAULT_GATEWAY_SERVICE_PATH,
  clientId: GATEWAY_CLIENT_ID,
  serviceId: GATEWAY_SERVICE_ID,
  redirectUri: GATEWAY_REDIRECT_URI,
};

/** An absent/expired Gateway credential must not invalidate the app JWT. */
export class GatewayReauthRequiredError extends Error {
  readonly code = "gateway_reauth_required";
  readonly cause: unknown;

  constructor(cause?: unknown) {
    super("Gateway認証が必要です。公開接続を続けるには再認証してください。");
    this.name = "GatewayReauthRequiredError";
    this.cause = cause;
  }
}

export function isGatewayReauthRequiredError(
  error: unknown,
): error is GatewayReauthRequiredError {
  return error instanceof GatewayReauthRequiredError;
}

function gatewayError(code: string, message: string): GatewayAuthError {
  // The SDK's runtime constructor accepts `(code, message)` while the
  // intentionally small declaration inherits Error's constructor overload.
  const Constructor = GatewayAuthError as unknown as new (
    code: string,
    message: string,
  ) => GatewayAuthError;
  return new Constructor(code, message);
}

/** Normalize an HTTPS portal root while retaining no path/query credentials. */
export function normalizePortalUrl(value: string = DEFAULT_PORTAL_URL): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw gatewayError("config_invalid", "portalUrl must be an absolute URL");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !["", "/", "/index.html"].includes(url.pathname)
  ) {
    throw gatewayError("config_invalid", "portalUrl must be an HTTPS portal root");
  }
  return `${url.origin}/`;
}

function normalizeServicePath(value: string, portalUrl: string): string {
  let url: URL;
  try {
    url = new URL(value, portalUrl);
  } catch {
    throw gatewayError("config_invalid", "servicePath must be a valid URL or path");
  }
  const portal = new URL(portalUrl);
  if (
    url.protocol !== "https:" ||
    url.origin !== portal.origin ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw gatewayError(
      "config_invalid",
      "servicePath must be an HTTPS path on the portal origin",
    );
  }
  const pathname = url.pathname.replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/";
  return pathname;
}

/**
 * Build the selected public service URL. A Gateway selection never silently
 * turns into the direct server origin: callers receive the explicit `/at`
 * service path and can fail closed when a different path is supplied.
 */
export function normalizeGatewayServiceUrl(
  value?: string | null,
  fallbackPortalUrl = DEFAULT_PORTAL_URL,
): string {
  const fallback = normalizePortalUrl(fallbackPortalUrl);
  const input = value?.trim() || `${fallback}${DEFAULT_GATEWAY_SERVICE_PATH}`;
  let portalUrl = fallback;
  try {
    const parsed = new URL(input, fallback);
    // A custom selected portal may use another HTTPS origin. It is still
    // constrained to its own `/at` service path and never becomes a direct
    // AoiTalk endpoint.
    if (parsed.origin !== new URL(fallback).origin) {
      portalUrl = normalizePortalUrl(`${parsed.origin}/`);
    }
  } catch {
    // normalizeServicePath below produces the stable configuration error.
  }
  const path = normalizeServicePath(input, portalUrl);
  if (path !== DEFAULT_GATEWAY_SERVICE_PATH) {
    throw gatewayError(
      "config_invalid",
      `Gateway service path must be ${DEFAULT_GATEWAY_SERVICE_PATH}`,
    );
  }
  return `${new URL(portalUrl).origin}${path}`;
}

export const getGatewayServiceUrl = normalizeGatewayServiceUrl;

function canonicalGatewayOrigin(value: URL): string {
  const protocol = value.protocol.toLowerCase();
  let host = value.hostname.toLowerCase();
  if (host.includes(":") && !host.startsWith("[")) host = `[${host}]`;
  const defaultPort = protocol === "https:" ? "443" : "80";
  const port = value.port && value.port !== defaultPort ? `:${value.port}` : "";
  return `${protocol}//${host}${port}`;
}

/**
 * Build the namespaced SecureStore key used by the pure Gateway SDK.
 *
 * Expo SecureStore accepts only `[A-Za-z0-9._-]` key characters. The issuer,
 * service path, service id, and client id remain part of the SDK binding, but
 * only their SHA-256 digest is placed in the key so custom portal origins do
 * not introduce `:` or `/` and cannot collide with another service binding.
 */
export async function getGatewayStorageKey(
  issuer: string,
  servicePath: string,
  serviceId = GATEWAY_SERVICE_ID,
  clientId = GATEWAY_CLIENT_ID,
): Promise<string> {
  const issuerUrl = new URL(normalizePortalUrl(issuer));
  const canonicalIssuer = `${canonicalGatewayOrigin(issuerUrl)}/`;
  let canonicalService: URL;
  try {
    canonicalService = new URL(normalizeGatewayServiceUrl(servicePath));
  } catch {
    throw gatewayError("config_invalid", "servicePath must be an absolute URL");
  }
  const canonicalServiceOrigin = canonicalGatewayOrigin(canonicalService);
  const canonicalServicePath = canonicalService.pathname.replace(/\/+$/, "") || "/";
  const canonicalServiceValue = `${canonicalServiceOrigin}${canonicalServicePath}`;
  if (
    canonicalService.protocol !== "https:"
    || canonicalService.username
    || canonicalService.password
    || canonicalService.search
    || canonicalService.hash
    || canonicalServiceOrigin !== canonicalGatewayOrigin(issuerUrl)
    || !serviceId.trim()
    || !clientId.trim()
  ) {
    throw gatewayError("config_invalid", "Gateway storage binding is invalid");
  }
  const digest = await ExpoCrypto.digestStringAsync(
    ExpoCrypto.CryptoDigestAlgorithm.SHA256,
    `${canonicalIssuer}\u0000${canonicalServiceValue}\u0000${serviceId}\u0000${clientId}`,
    { encoding: ExpoCrypto.CryptoEncoding.HEX },
  );
  if (!/^[0-9a-f]{64}$/i.test(digest)) {
    throw gatewayError("config_invalid", "Gateway storage digest is invalid");
  }
  return `${STORAGE_KEYS.GATEWAY_AUTH}.${digest.toLowerCase()}`;
}

function serviceUrlForTarget(target?: string | URL): string {
  if (!target) return normalizeGatewayServiceUrl();
  const url = target instanceof URL ? new URL(target.href) : new URL(target);
  if (url.protocol !== "https:" && url.protocol !== "wss:") {
    throw gatewayError("target_invalid", "Gateway target must use HTTPS");
  }
  const pathname = url.pathname.replace(/\/{2,}/g, "/");
  const marker = pathname === DEFAULT_GATEWAY_SERVICE_PATH ||
    pathname.startsWith(`${DEFAULT_GATEWAY_SERVICE_PATH}/`);
  if (!marker) {
    throw gatewayError("target_invalid", "Gateway target is outside /at");
  }
  // WSS is a transport for the same HTTPS issuer. Keep the SDK/storage
  // binding shared with HTTP requests while passing the original WSS target
  // to its credential-bound header validation.
  const protocol = url.protocol === "wss:" ? "https:" : url.protocol;
  return `${protocol}//${url.host}${DEFAULT_GATEWAY_SERVICE_PATH}`;
}

function base64ToBase64Url(value: string): string {
  return value.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function makeCryptoAdapter() {
  return {
    randomBytes: async (length: number) => {
      const crypto = await import("expo-crypto");
      return crypto.getRandomBytesAsync(length);
    },
    sha256Base64Url: async (value: string) => {
      const crypto = await import("expo-crypto");
      return base64ToBase64Url(
        await crypto.digestStringAsync(
          crypto.CryptoDigestAlgorithm.SHA256,
          value,
          { encoding: crypto.CryptoEncoding.BASE64 },
        ),
      );
    },
  };
}

const clientCache = new Map<string, Promise<MobileGatewayAuth>>();

export function createGatewayStorageAdapter() {
  return {
    get: (key: string) => SecureStore.getItemAsync(key),
    set: (key: string, value: string) => SecureStore.setItemAsync(key, value),
    delete: (key: string) => SecureStore.deleteItemAsync(key),
  };
}

function createClient(serviceUrl: string): Promise<MobileGatewayAuth> {
  const service = new URL(serviceUrl);
  const portalUrl = `${service.origin}/`;
  const cacheKey = service.href;
  const cached = clientCache.get(cacheKey);
  if (cached) return cached;

  const pending = loadGatewaySdk().then(async ({ createMobileGatewayAuth }) => {
    const storageKey = await getGatewayStorageKey(
      portalUrl,
      service.href,
      GATEWAY_SERVICE_ID,
      GATEWAY_CLIENT_ID,
    );
    return createMobileGatewayAuth(
      {
        portalOrigin: portalUrl,
        servicePath: service.href,
        serviceId: GATEWAY_SERVICE_ID,
        clientId: GATEWAY_CLIENT_ID,
        redirectUri: GATEWAY_REDIRECT_URI,
        expectedApiBasePrefix: DEFAULT_GATEWAY_SERVICE_PATH,
        issuer: portalUrl,
        storageKey,
        deviceName: "AoiTalk mobile",
        // The SDK owns only the Gateway credential. The central request
        // builder adds the captured application JWT after identity proof and
        // revision checks, preventing refresh/account races from changing it.
      },
      {
        storage: createGatewayStorageAdapter(),
        crypto: makeCryptoAdapter(),
        browser: {
          openAuthSession: (url, redirectUri) => {
            // expo-web-browser is a native module. Load it only when a Gateway
            // login is actually requested so routing/unit tests can run without
            // an installed Expo native module.
            if (typeof require !== "function") {
              throw new Error("Expo WebBrowser is unavailable");
            }
            const browser = require("expo-web-browser") as typeof import("expo-web-browser");
            return browser.openAuthSessionAsync(url, redirectUri);
          },
        },
        fetch: protectedFetch,
      },
    );
  });
  clientCache.set(cacheKey, pending);
  void pending.catch(() => {
    if (clientCache.get(cacheKey) === pending) clientCache.delete(cacheKey);
  });
  return pending;
}

function clientForTarget(target?: string | URL): Promise<MobileGatewayAuth> {
  return createClient(serviceUrlForTarget(target));
}

function normalizeReadyResult(
  result: unknown,
): GatewayServiceReadyResult {
  if (!result || typeof result !== "object") {
    throw gatewayError("protocol_error", "Gateway runtime readiness response is invalid");
  }
  const candidate = result as Partial<GatewayServiceReadyResult>;
  if (
    candidate.state !== "ready"
    || candidate.serviceId !== GATEWAY_SERVICE_ID
    || typeof candidate.started !== "boolean"
  ) {
    throw gatewayError("protocol_error", "Gateway runtime readiness service identity is invalid");
  }
  return {
    state: "ready",
    serviceId: GATEWAY_SERVICE_ID,
    started: candidate.started,
  };
}

/**
 * Wait for one selected Gateway service to be ready.
 *
 * The SDK owns the fixed `/<service>/.gateway/mobile/runtime` protocol and
 * its Gateway-only credential boundary.  This adapter only shares a short
 * ready result between transports and exposes a caller abort; it never
 * recreates the SDK's polling or stores a second credential.
 */
export async function awaitGatewayServiceReady(
  target?: string | URL,
  options: GatewayServiceReadyOptions = {},
): Promise<GatewayServiceReadyResult> {
  if (options.signal?.aborted) throw startupAbortError();
  const serviceUrl = serviceUrlForTarget(target);
  const cached = gatewayReadyCache.get(serviceUrl);
  if (cached && Date.now() - cached.readyAt <= GATEWAY_READY_TTL_MS) {
    options.onState?.("ready");
    setSafeLifecycle("ready");
    return awaitWithCallerAbort(Promise.resolve(cached.result), options.signal);
  }

  const client = await awaitWithCallerAbort(
    clientForTarget(serviceUrl),
    options.signal,
  ) as GatewayServiceReadyClient;
  const awaitReady = client.awaitServiceReady;
  if (typeof awaitReady !== "function") {
    const error = gatewayError(
      "sdk_capability_missing",
      "Gateway SDK does not expose awaitServiceReady",
    );
    setSafeLifecycle("failed", error.code);
    options.onState?.("failed");
    throw error;
  }

  setSafeLifecycle("starting");
  options.onState?.("starting");
  const timeoutMs = Math.min(
    GATEWAY_STARTUP_TIMEOUT_MS,
    Math.max(1, Math.floor(options.timeoutMs ?? GATEWAY_STARTUP_TIMEOUT_MS)),
  );
  const pollMs = Math.max(0, Math.floor(options.pollMs ?? GATEWAY_STARTUP_POLL_MS));
  try {
    const result = normalizeReadyResult(await awaitReady({
      signal: options.signal,
      timeoutMs,
      pollMs,
      onState: (state) => {
        if (state === "starting") setSafeLifecycle("starting");
        else if (state === "ready") setSafeLifecycle("ready");
        else setSafeLifecycle("failed");
        options.onState?.(state);
      },
    }));
    setSafeLifecycle("ready");
    options.onState?.("ready");
    gatewayReadyCache.set(serviceUrl, {
      readyAt: Date.now(),
      result,
      flight: null,
    });
    return result;
  } catch (error) {
    const callerAborted = isGatewayCallerAbort(error);
    if (!callerAborted) {
      setSafeLifecycle("failed", safeGatewayErrorCode(error));
      options.onState?.("failed");
    }
    if (!callerAborted && isGatewayNetworkFailure(error)) {
      clearGatewayRuntimeCaches(serviceUrl);
    }
    if (isGatewaySdkError(error) && (
      error.code === "reauth_required" ||
      error.code === "refresh_expired" ||
      error.code === "refresh_missing" ||
      error.code === "pending_missing" ||
      error.code === "revoked" ||
      error.code === "invalid_grant"
    )) {
      throw new GatewayReauthRequiredError(error);
    }
    throw error;
  }
}

/** Clear only the in-memory readiness result after a route/auth transition. */
export function clearGatewayServiceReadyCache(target?: string | URL): void {
  if (target) {
    gatewayReadyCache.delete(serviceUrlForTarget(target));
    return;
  }
  gatewayReadyCache.clear();
}

/**
 * Drop only in-memory Gateway runtime/client caches. The SDK SecureStore
 * namespace remains intact so transient failures and pending revocations can
 * recover without logging the account out silently.
 */
export function clearGatewayRuntimeCaches(target?: string | URL): void {
  const serviceUrl = serviceUrlForTarget(target);
  gatewayReadyCache.delete(serviceUrl);
  statusCache.delete(statusKey(serviceUrl));
}

export function isGatewayTarget(
  target: string | URL,
  expectedServiceUrl: string = normalizeGatewayServiceUrl(),
): boolean {
  try {
    const url = target instanceof URL ? target : new URL(target);
    if (url.protocol !== "https:" && url.protocol !== "wss:") return false;
    const service = new URL(normalizeGatewayServiceUrl(expectedServiceUrl));
    if (url.hostname !== service.hostname || url.port !== service.port) return false;
    const path = url.pathname.replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/";
    const prefix = service.pathname.replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/";
    return path === prefix || path.startsWith(`${prefix}/`);
  } catch {
    return false;
  }
}

export async function getGatewayRequestHeaders(
  target: string | URL,
  serviceUrl?: string,
): Promise<Record<string, string>> {
  if (!isGatewayTarget(target, serviceUrl)) {
    throw gatewayError("target_invalid", "target is outside the Gateway service");
  }
  try {
    const client = await clientForTarget(target);
    const headers = await client.getHeaders(target);
    rememberStatus(target, client);
    return headers;
  } catch (error) {
    if (isGatewayNetworkFailure(error)) {
      clearGatewayRuntimeCaches(target);
    }
    if (isGatewaySdkError(error) && (
      error.code === "reauth_required" ||
      error.code === "refresh_expired" ||
      error.code === "refresh_missing" ||
      error.code === "pending_missing"
    )) {
      throw new GatewayReauthRequiredError(error);
    }
    throw error;
  }
}

/** Identity probes may carry only the SDK Gateway credential, never app JWT. */
export async function getGatewayProbeHeaders(
  target: string | URL,
  serviceUrl?: string,
): Promise<Record<string, string>> {
  const headers = await getGatewayRequestHeaders(target, serviceUrl);
  const safe: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() !== "authorization") safe[name] = value;
  }
  return safe;
}

export async function refreshPortalAuth(
  target?: string | URL,
): Promise<AuthenticationResult> {
  try {
    const client = await clientForTarget(target);
    const result = await client.refresh({ force: true });
    rememberStatus(target, client);
    return result;
  } catch (error) {
    if (isGatewayNetworkFailure(error)) {
      clearGatewayRuntimeCaches(target);
    }
    if (isGatewaySdkError(error) && (
      error.code === "reauth_required" ||
      error.code === "refresh_expired" ||
      error.code === "refresh_missing" ||
      error.code === "revoked" ||
      error.code === "invalid_grant"
    )) {
      throw new GatewayReauthRequiredError(error);
    }
    throw error;
  }
}

export async function authenticatePortal(
  options: { force?: boolean; target?: string | URL } = {},
): Promise<AuthenticationResult> {
  try {
    const client = await clientForTarget(options.target);
    const result = await client.authenticate({ force: options.force });
    rememberStatus(options.target, client);
    return result;
  } catch (error) {
    if (isGatewaySdkError(error) && error.code === "reauth_required") {
      throw new GatewayReauthRequiredError(error);
    }
    throw error;
  }
}

export async function completePortalCallback(
  callback: string | URL | { url: string } | { url: URL },
  target?: string | URL,
): Promise<AuthenticationResult> {
  const rawCallback = callback instanceof URL
    ? callback.href
    : typeof callback === "string"
      ? callback
      : typeof callback?.url === "string"
        ? callback.url
        : callback?.url instanceof URL
          ? callback.url.href
          : null;
  if (!rawCallback || !isGatewayCallbackUrl(rawCallback)) {
    throw gatewayError(
      "callback_invalid",
      "Gateway callback must be the verified HTTPS fragment endpoint",
    );
  }
  const client = await clientForTarget(target);
  // Preserve the raw fragment URL. The SDK owns pending state, state/issuer
  // validation, and code exchange; this adapter never logs or persists it.
  const result = await client.completeCallback(rawCallback);
  rememberStatus(target, client);
  return result;
}

/** Retry a persisted authorization-code exchange after a transient failure. */
export async function retryPortalExchange(target?: string | URL): Promise<AuthenticationResult> {
  const client = await clientForTarget(target) as MobileGatewayAuth & {
    retryExchange?: () => Promise<AuthenticationResult>;
  };
  if (typeof client.retryExchange !== "function") {
    throw new GatewayReauthRequiredError();
  }
  const result = await client.retryExchange();
  rememberStatus(target, client);
  return result;
}

export async function revokePortalAuth(
  target?: string | URL,
): Promise<Awaited<ReturnType<MobileGatewayAuth["signOut"]>>> {
  const client = await clientForTarget(target);
  const result = await client.signOut({ revoke: true });
  clearGatewayRuntimeCaches(target);
  rememberStatus(target, client);
  return result;
}

export async function retryPortalRevocation(target?: string | URL) {
  const client = await clientForTarget(target);
  const result = await client.retryRevocation();
  if (result.status === "sent" || result.status === "none") {
    clearGatewayServiceReadyCache(target);
  }
  rememberStatus(target, client);
  return result;
}

const defaultAuthStatus: AuthStatus = {
  status: "signed_out",
  reason: null,
  expiresAt: null,
  refreshExpiresAt: null,
  grantExpiresAt: null,
};
const statusCache = new Map<string, AuthStatus>();

function rememberStatus(target: string | URL | undefined, client: MobileGatewayAuth): void {
  statusCache.set(statusKey(target), client.getStatus());
}

function statusKey(target?: string | URL): string {
  try {
    return serviceUrlForTarget(target);
  } catch {
    return normalizeGatewayServiceUrl();
  }
}

export function getPortalAuthStatus(target?: string | URL): AuthStatus {
  const key = statusKey(target);
  const cached = statusCache.get(key);
  if (!cached) {
    void clientForTarget(target).then((client) => {
      statusCache.set(key, client.getStatus());
    }).catch(() => undefined);
  }
  return cached ? { ...cached } : { ...defaultAuthStatus };
}

export async function testPortalAuth(target?: string | URL) {
  return (await clientForTarget(target)).test();
}

export function subscribePortalAuth(
  listener: (status: AuthStatus) => void,
  target?: string | URL,
): () => boolean {
  let unsubscribe: (() => boolean) | null = null;
  let active = true;
  void clientForTarget(target).then((client) => {
    if (!active) return;
    unsubscribe = client.subscribe((status) => {
      statusCache.set(statusKey(target), status);
      listener(status);
    });
  }).catch(() => undefined);
  listener(getPortalAuthStatus(target));
  return () => {
    active = false;
    return unsubscribe?.() ?? false;
  };
}

// Stable names for settings/UI workers. Keep the implementation in this
// adapter so UI and background transports share one SDK state machine.
export const authenticateGateway = authenticatePortal;
export const revokeGatewayAuth = revokePortalAuth;
export const getGatewayAuthStatus = getPortalAuthStatus;
export const testGatewayAuth = testPortalAuth;
