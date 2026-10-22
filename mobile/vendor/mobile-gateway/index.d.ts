export type AuthStatusName =
  | "signed_out"
  | "authorizing"
  | "exchanging"
  | "authenticated"
  | "refreshing"
  | "reauth_required"
  | "revoked"
  | "cancelled"
  | "transient";

export interface AuthStatus {
  status: AuthStatusName;
  reason: string | null;
  /** Absolute Unix time in milliseconds. */
  expiresAt: number | null;
  /** Absolute Unix time in milliseconds. */
  refreshExpiresAt: number | null;
  /** Absolute Unix time in milliseconds. */
  grantExpiresAt: number | null;
}

export interface MobileGatewayConfig {
  portalOrigin: string;
  servicePath: string;
  serviceId: string;
  clientId: string;
  /** Registered HTTPS App Link without query or fragment. */
  redirectUri: string;
  deviceName?: string;
  storageKey?: string;
  expectedApiBasePrefix?: string;
  pendingTtlMs?: number;
  expirySkewMs?: number;
  timeoutMs?: number;
  maxAccessLifetimeSec?: number;
  maxGrantLifetimeSec?: number;
  minGrantLifetimeSec?: number;
  minRefreshLifetimeSec?: number;
  browserCallbackGraceMs?: number;
  issuer?: string;
  issuerConfig?: Partial<MobileGatewayConfig> & { issuer?: string };
  appAuthorization?:
    | string
    | Record<string, string>
    | ((target: string) => string | Record<string, string> | null | undefined | Promise<string | Record<string, string> | null | undefined>);
}

export interface StorageAdapter {
  get(key: string): string | null | undefined | Promise<string | null | undefined>;
  set(key: string, value: string): void | Promise<void>;
  delete(key: string): void | Promise<void>;
}

export interface CryptoAdapter {
  randomBytes(length: number): Uint8Array | ArrayBuffer | number[] | Promise<Uint8Array | ArrayBuffer | number[]>;
  sha256Base64Url(value: string): string | Promise<string>;
}

export interface BrowserAdapter {
  openAuthSession(url: string, redirectUri: string): unknown | Promise<unknown>;
}

export interface MobileGatewayAdapters {
  storage: StorageAdapter;
  crypto: CryptoAdapter;
  browser: BrowserAdapter;
  /** Native adapter must honor redirect: "manual"; do not rely on global fetch. */
  fetch: typeof fetch;
  timer?: {
    setTimeout(callback: () => void, delayMs: number): unknown;
    clearTimeout(timerId: unknown): void;
  };
}

export interface AuthenticationResult {
  status: "authenticated" | "cancelled" | "pending";
  reason?: string;
  requestId?: string;
  expiresAt?: number;
  refreshExpiresAt?: number | null;
  grantExpiresAt?: number | null;
  deviceId?: string | null;
  serviceId?: string;
  apiBaseUrl?: string;
}

export type RuntimeReadyState = "starting" | "ready" | "failed";

export interface AwaitServiceReadyOptions {
  /** Abort this caller's wait without cancelling other callers sharing the request. */
  signal?: AbortSignal;
  /** Maximum total wait in milliseconds. Values above 180000 are capped. */
  timeoutMs?: number;
  /** Delay between state polls in milliseconds. Defaults to 500. */
  pollMs?: number;
  /** Receives safe runtime states for this caller only. */
  onState?: (state: RuntimeReadyState) => void;
}

export interface ServiceReadyResult {
  state: "ready";
  serviceId: string;
  /** True when this call submitted the asynchronous activation request. */
  started: boolean;
}

export interface SignOutResult {
  status: "signed_out";
  revocation: "sent" | "pending" | "skipped";
  error?: { code: string; retryable: boolean };
}

export class GatewayAuthError extends Error {
  constructor(code: string, message: string, options?: {
    retryable?: boolean;
    status?: number | null;
    cause?: unknown;
  });
  readonly name: "GatewayAuthError";
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number | null;
  readonly cause: unknown;
}

/** True only for a 401 carrying the Worker-owned Gateway auth marker. */
export function isGatewayAuthResponse(response: Response): boolean;

export interface MobileGatewayAuth {
  authenticate(options?: { force?: boolean }): Promise<AuthenticationResult>;
  completeCallback(callback: string | URL | { url: string } | { url: URL }): Promise<AuthenticationResult>;
  resumePendingExchange(): Promise<AuthenticationResult>;
  retryExchange(): Promise<AuthenticationResult>;
  awaitServiceReady(options?: AwaitServiceReadyOptions): Promise<ServiceReadyResult>;
  getHeaders(target: string | URL): Promise<Record<string, string>>;
  refresh(options?: { force?: boolean }): Promise<AuthenticationResult>;
  signOut(options?: { revoke?: boolean }): Promise<SignOutResult>;
  retryRevocation(): Promise<{ status: "none" | "sent" | "pending"; error?: { code: string; retryable: boolean } }>;
  getStatus(): AuthStatus;
  subscribe(listener: (status: AuthStatus) => void): () => boolean;
  test(): Promise<{
    ok: true;
    storage: true;
    crypto: true;
    fetch: true;
    storageKey: string;
    portalOrigin: string;
    servicePath: string;
    redirectUri: string;
    hasCredential: boolean;
  }>;
}

export function createMobileGatewayAuth(config: MobileGatewayConfig, adapters: MobileGatewayAdapters): MobileGatewayAuth;
export const createAuthClient: typeof createMobileGatewayAuth;
export default createMobileGatewayAuth;
