import { DEFAULT_API_URL, DEFAULT_PORTAL_URL } from "../constants/config";
import { getApiUrl } from "./auth";
import { getCredentialIdentityRevision } from "./api-client";
import {
  getNetworkEndpointRoutingConfig,
  getNetworkEndpointRoutingRevision,
  getObservedNetworkRouteSnapshot,
} from "./connection-routing";
import {
  getRequestAuthHeaders,
  type RequestAuthHeaderOptions,
} from "./auth-headers";
import {
  CredentialTargetError,
  normalizeCredentialTargetNamespace,
} from "./credential-target";
import { isGatewayTarget, normalizeGatewayServiceUrl } from "./portal-auth";

export class TransportCredentialRevisionError extends Error {
  readonly code = "credential_revision_changed";

  constructor() {
    super("認証または接続設定が変わったため送信を破棄しました");
    this.name = "TransportCredentialRevisionError";
  }
}

function assertNetworkSnapshotStable(
  expected: ReturnType<typeof getObservedNetworkRouteSnapshot>,
): void {
  if (!expected || typeof getObservedNetworkRouteSnapshot !== "function") return;
  const current = getObservedNetworkRouteSnapshot();
  if (current && current.networkKey !== expected.networkKey) {
    throw new TransportCredentialRevisionError();
  }
}

type TransportAuthOptions = Omit<
  RequestAuthHeaderOptions,
  "primaryBaseUrl" | "targetBaseUrl"
> & {
  primaryBaseUrl?: string;
  targetBaseUrl?: string;
};

function routeContextError(message: string): CredentialTargetError {
  return new CredentialTargetError("invalid_target_namespace", message);
}

function transportAbortError(): Error {
  const error = new Error("認証付き通信を中止しました");
  error.name = "AbortError";
  return error;
}

function awaitTransport<T>(operation: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return operation;
  if (signal.aborted) return Promise.reject(transportAbortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(transportAbortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    void operation.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(transportAbortError());
        else resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? transportAbortError() : error);
      },
    );
  });
}

function assertActualTargetWithinCapturedBase(
  target: string | URL,
  targetBaseUrl: string,
): void {
  const baseNamespace = normalizeCredentialTargetNamespace(targetBaseUrl);
  const base = new URL(baseNamespace);
  const actual = target instanceof URL ? new URL(target.href) : new URL(target);
  if (actual.username || actual.password) {
    throw new CredentialTargetError(
      "target_credentials_in_url",
      "transport target cannot contain URL credentials",
    );
  }
  if (actual.hash) {
    throw new CredentialTargetError(
      "target_query_not_allowed",
      "transport target cannot contain a URL fragment",
    );
  }
  const protocol = actual.protocol === "wss:"
    ? "https:"
    : actual.protocol === "ws:"
      ? "http:"
      : actual.protocol;
  if (protocol !== "http:" && protocol !== "https:") {
    throw routeContextError("transport target must use HTTP(S) or WS(S)");
  }
  const actualNamespace = normalizeCredentialTargetNamespace(
    `${protocol}//${actual.host}${actual.pathname || "/"}`,
  );
  const actualUrl = new URL(actualNamespace);
  const basePath = base.pathname.replace(/\/+$/, "") || "/";
  const actualPath = actualUrl.pathname.replace(/\/+$/, "") || "/";
  if (
    actualUrl.origin !== base.origin ||
    !(basePath === "/" || actualPath === basePath || actualPath.startsWith(`${basePath}/`))
  ) {
    throw routeContextError("transport target is outside the captured route namespace");
  }
}

/**
 * Resolve the selected Gateway namespace once for this transport attempt.
 * `auth-headers` deliberately has no UI routing dependency, so non-fetch
 * callers must provide the selected service binding here. A target that is
 * LAN/direct/foreign stays a direct target even while Gateway mode is stored.
 */
async function resolveTransportRouteContext(
  targetBaseUrl: string,
  options: TransportAuthOptions,
): Promise<{
  gateway: boolean;
  gatewayServiceUrl?: string;
  routingRevision: number;
}> {
  const routingRevisionBefore = getNetworkEndpointRoutingRevision();
  const routing = await getNetworkEndpointRoutingConfig();
  const routingRevision = getNetworkEndpointRoutingRevision();
  if (routingRevision !== routingRevisionBefore) {
    throw new TransportCredentialRevisionError();
  }
  const selectedGatewayServiceUrl = routing.mode === "gateway"
    ? normalizeGatewayServiceUrl(
      undefined,
      routing.portalUrl || DEFAULT_PORTAL_URL,
    )
    : undefined;
  const explicitGatewayServiceUrl = options.gatewayServiceUrl
    ? normalizeGatewayServiceUrl(options.gatewayServiceUrl)
    : undefined;

  if (explicitGatewayServiceUrl && explicitGatewayServiceUrl !== selectedGatewayServiceUrl) {
    throw routeContextError("Gateway service does not match the selected connection route");
  }

  const targetIsSelectedGateway = Boolean(
    selectedGatewayServiceUrl &&
    isGatewayTarget(targetBaseUrl, selectedGatewayServiceUrl),
  );
  if (options.gateway !== undefined && options.gateway !== targetIsSelectedGateway) {
    throw routeContextError("Gateway flag does not match the selected connection route");
  }

  const gateway = options.gateway ?? targetIsSelectedGateway;
  if (gateway && !selectedGatewayServiceUrl) {
    throw routeContextError("Gateway authentication is unavailable for the selected route");
  }

  return {
    gateway,
    gatewayServiceUrl: gateway
      ? explicitGatewayServiceUrl ?? selectedGatewayServiceUrl
      : undefined,
    routingRevision,
  };
}

/**
 * Route-aware auth for transports that cannot use fetchApi (multipart,
 * WebSocket and native media). Every caller goes through the strict
 * credential-target proof and the selected route namespace.
 */
export async function getTransportAuthHeaders(
  target: string | URL,
  options: TransportAuthOptions = {},
): Promise<Record<string, string>> {
  const networkSnapshot = getObservedNetworkRouteSnapshot?.() ?? null;
  const primaryBaseUrl =
    options.primaryBaseUrl ?? (await getApiUrl()) ?? DEFAULT_API_URL;
  // Callers capture the resolved base URL before constructing the resource
  // path. Never infer a namespace from origin or an assumed `/at` prefix: a
  // direct service may legitimately live below another path.
  if (!options.targetBaseUrl) {
    throw routeContextError("resolved transport target namespace is required");
  }
  const targetBaseUrl = options.targetBaseUrl;
  assertActualTargetWithinCapturedBase(target, targetBaseUrl);
  const route = await resolveTransportRouteContext(targetBaseUrl, options);
  const headers = await getRequestAuthHeaders(target, {
    ...options,
    primaryBaseUrl,
    targetBaseUrl,
    gateway: route.gateway,
    ...(route.gatewayServiceUrl
      ? { gatewayServiceUrl: route.gatewayServiceUrl }
      : { gatewayServiceUrl: undefined }),
    identityRevisionSnapshot: () => getCredentialIdentityRevision(),
  });
  if (getNetworkEndpointRoutingRevision() !== route.routingRevision) {
    throw new TransportCredentialRevisionError();
  }
  assertNetworkSnapshotStable(networkSnapshot);
  return headers;
}

/**
 * Atomic non-fetch transport boundary. `send` is invoked synchronously after
 * the final revision check, so WebSocket construction/native fetch cannot be
 * separated from the credential snapshot by another await.
 */
export async function sendWithTransportAuth<T>(
  target: string | URL,
  options: TransportAuthOptions,
  send: (headers: Record<string, string>) => T,
): Promise<T> {
  const networkSnapshot = getObservedNetworkRouteSnapshot?.() ?? null;
  const before = await awaitTransport(
    getCredentialIdentityRevision(),
    options.signal,
  );
  const headers = await getTransportAuthHeaders(target, options);
  const after = await awaitTransport(
    getCredentialIdentityRevision(),
    options.signal,
  );
  if (options.signal?.aborted) throw transportAbortError();
  assertNetworkSnapshotStable(networkSnapshot);
  if (before !== after) throw new TransportCredentialRevisionError();
  return send(headers);
}
