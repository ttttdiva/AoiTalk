/** Central request authentication header builder for every mobile transport. */

import { getApiUrl, getTokenSnapshot, readAuthActor } from "./auth";
import { DEFAULT_API_URL } from "../constants/config";
import {
  getNetworkEndpointRoutingConfig,
  type NetworkEndpointRoutingConfig,
} from "./connection-routing";
import { normalizeApiUrl } from "./api-url";
import {
  assertCredentialTargetIdentity,
  CredentialTargetError,
  normalizeCredentialTargetNamespace,
  type CredentialTargetOptions,
  type CredentialTargetRevision,
} from "./credential-target";
import { nativeEd25519Verifier, nativeIdentityNonce } from "./native-identity";
import { protectedFetch } from "./protected-fetch";
import {
  awaitGatewayServiceReady,
  getGatewayRequestHeaders,
  getGatewayProbeHeaders,
  isGatewayTarget,
} from "./portal-auth";

export const APPLICATION_AUTHORIZATION_HEADER = "Authorization";
export const GATEWAY_AUTHORIZATION_HEADER = "X-AZV-Gateway-Authorization";

export { CredentialTargetError } from "./credential-target";

export interface RequestAuthHeaderOptions {
  /** Explicit route kind from central routing. */
  gateway?: boolean;
  /** Token snapshot captured at the start of one API attempt. */
  applicationToken?: string | null;
  /** Selected Gateway service URL used to bind origin and path. */
  gatewayServiceUrl?: string;
  /** Configured primary namespace used for signed server identity binding. */
  primaryBaseUrl?: string;
  /** Resolved route namespace used for signed server identity binding. */
  targetBaseUrl?: string;
  /** Explicit primary TOFU/login decision. */
  pinPrimary?: boolean;
  /** Allow the exact legacy primary 404 compatibility path. */
  allowLegacyPrimary?: boolean;
  /** Root -> `/at` mapping approved by the direct route probe. */
  approvedPrimaryAlias?: boolean;
  /** Revision captured by the caller; proof is discarded if it changes. */
  identityRevision?: CredentialTargetRevision;
  identityRevisionSnapshot?: () => CredentialTargetRevision | Promise<CredentialTargetRevision>;
  /** Caller cancellation is propagated through Gateway startup/identity work. */
  signal?: AbortSignal;
  /** Separate Gateway cold-start budget; ordinary HTTP timeout stays intact. */
  startupTimeoutMs?: number;
}
function toHeaderRecord(headers: HeadersInit | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  if (!headers) return result;
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    headers.forEach((value, key) => {
      result[key] = value;
    });
    return result;
  }
  if (Array.isArray(headers)) {
    for (const [name, value] of headers) result[name] = value;
    return result;
  }
  for (const [name, value] of Object.entries(headers)) {
    if (typeof value === "string") result[name] = value;
  }
  return result;
}
function lowerHeaderNames(headers: Record<string, string>): Set<string> {
  return new Set(Object.keys(headers).map((name) => name.toLowerCase()));
}
function targetWithinBase(target: URL, baseValue: string): boolean {
  try {
    const base = new URL(normalizeApiUrl(baseValue));
    const targetOrigin = target.protocol === "wss:"
      ? `https://${target.host}`
      : target.protocol === "ws:"
        ? `http://${target.host}`
        : target.origin;
    if (targetOrigin !== base.origin) return false;
    const prefix = base.pathname.replace(/\/+$/, "") || "/";
    const path = target.pathname.replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/";
    return prefix === "/" || path === prefix || path.startsWith(`${prefix}/`);
  } catch {
    return false;
  }
}

async function isConfiguredDirectTarget(
  target: URL,
  routing: NetworkEndpointRoutingConfig,
): Promise<boolean> {
  const configured = await getApiUrl();
  const values = [configured || DEFAULT_API_URL];
  // A direct LAN route is a valid application target while it is selected;
  // the public Gateway route is handled by the SDK branch above.
  values.push(routing.wifiApiUrl);
  if (routing.mode !== "gateway") values.push(routing.cellularApiUrl);
  return values.filter(Boolean).some((value) => targetWithinBase(target, value));
}

async function assertTargetIdentity(
  target: URL,
  routing: NetworkEndpointRoutingConfig,
  options: RequestAuthHeaderOptions,
): Promise<void> {
  const primaryBaseUrl = options.primaryBaseUrl || await getApiUrl() || DEFAULT_API_URL;
  const targetBaseUrl = options.targetBaseUrl || `${target.origin}${target.pathname}`;
  const gateway = Boolean(
    options.gateway && options.gatewayServiceUrl &&
    isGatewayTarget(targetBaseUrl, options.gatewayServiceUrl),
  );
  let actorBoundPrimary = false;
  if (!options.pinPrimary) {
    try {
      const actorResult = await readAuthActor();
      if (actorResult.readable && actorResult.actor) {
        actorBoundPrimary = normalizeCredentialTargetNamespace(
          actorResult.actor.serverFingerprint,
        ) === normalizeCredentialTargetNamespace(primaryBaseUrl);
      }
    } catch {
      actorBoundPrimary = false;
    }
  }
  await assertCredentialTargetIdentity({
    primaryBaseUrl,
    targetBaseUrl,
    fetcher: async (url, init) => {
      const response = await protectedFetch(url, {
        ...init,
        ...(options.signal ? { signal: options.signal } : {}),
      });
      return { status: response.status, json: () => response.json() };
    },
    gatewayHeaders: gateway
      ? (probeTarget) => getGatewayProbeHeaders(probeTarget, options.gatewayServiceUrl)
      : undefined,
    revisionSnapshot: options.identityRevisionSnapshot
      ?? (options.identityRevision === undefined
        ? undefined
        : async () => options.identityRevision),
    pinPrimary: options.pinPrimary === true || actorBoundPrimary,
    allowLegacyPrimary: options.allowLegacyPrimary ?? routing.mode !== "gateway",
    approvedPrimaryAlias: options.approvedPrimaryAlias === true,
    requireProof: true,
    verifyProof: nativeEd25519Verifier,
    nonceFactory: nativeIdentityNonce,
  });
}

/**
 * Merge caller-owned non-auth headers with the centrally generated values.
 * Supplying either auth header is rejected instead of allowing a stale or
 * user supplied credential to shadow the selected route's credential.
 */
export function mergeRequestAuthHeaders(
  callerHeaders: HeadersInit | undefined,
  authHeaders: Record<string, string>,
): Record<string, string> {
  const caller = toHeaderRecord(callerHeaders);
  const names = lowerHeaderNames(caller);
  if (
    names.has(APPLICATION_AUTHORIZATION_HEADER.toLowerCase()) ||
    names.has(GATEWAY_AUTHORIZATION_HEADER.toLowerCase())
  ) {
    throw new Error("認証ヘッダーは中央API経路から設定してください");
  }
  return { ...caller, ...authHeaders };
}

export async function bootstrapPrimaryIdentity(
  primaryBaseUrl: string,
  options: { gatewayServiceUrl?: string; revisionSnapshot?: CredentialTargetOptions["revisionSnapshot"] } = {},
) {
  const target = normalizeApiUrl(primaryBaseUrl);
  const gateway = options.gatewayServiceUrl
    ? isGatewayTarget(target, options.gatewayServiceUrl)
    : false;
  return assertCredentialTargetIdentity({
    primaryBaseUrl: target,
    targetBaseUrl: target,
    fetcher: async (url, init) => {
      const response = await protectedFetch(url, init);
      return { status: response.status, json: () => response.json() };
    },
    gatewayHeaders: gateway
      ? (probeTarget) => getGatewayProbeHeaders(probeTarget, options.gatewayServiceUrl)
      : undefined,
    revisionSnapshot: options.revisionSnapshot,
    pinPrimary: true,
    allowLegacyPrimary: false,
    requireProof: true,
    verifyProof: nativeEd25519Verifier,
    nonceFactory: nativeIdentityNonce,
  });
}

/**
 * Build application and, when selected, Gateway authentication headers for a
 * single absolute target URL. The app JWT is intentionally retained alongside
 * the Gateway header; a Gateway re-auth error never removes that JWT.
 */
export async function getRequestAuthHeaders(
  target?: string | URL,
  options: RequestAuthHeaderOptions = {},
): Promise<Record<string, string>> {
  // A credential without a resolved, origin/prefix-bound target is unsafe.
  // Callers must resolve the route first and pass that exact URL.
  if (!target) return {};
  const targetUrl = target instanceof URL ? new URL(target.href) : new URL(target);
  if (options.gateway ?? isGatewayTarget(targetUrl, options.gatewayServiceUrl)) {
    if (typeof awaitGatewayServiceReady === "function") {
      await awaitGatewayServiceReady(targetUrl, {
        signal: options.signal,
        timeoutMs: options.startupTimeoutMs,
      });
    }
    if (options.primaryBaseUrl || options.targetBaseUrl) {
      const routing = await getNetworkEndpointRoutingConfig();
      await assertTargetIdentity(targetUrl, routing, options);
    }
    const gatewayHeaders = options.gatewayServiceUrl
      ? getGatewayRequestHeaders(target, options.gatewayServiceUrl)
      : getGatewayRequestHeaders(target);
    const resolvedGatewayHeaders = await gatewayHeaders;
    const revisionBeforeGateway = options.identityRevisionSnapshot
      ? await options.identityRevisionSnapshot()
      : options.identityRevision;
    const token = options.applicationToken === undefined
      ? (await getTokenSnapshot()).token
      : options.applicationToken;
    const revisionAfterGateway = options.identityRevisionSnapshot
      ? await options.identityRevisionSnapshot()
      : options.identityRevision;
    if (revisionBeforeGateway !== revisionAfterGateway) {
      throw new CredentialTargetError(
        "credential_revision_changed",
        "credential target revision changed during Gateway header refresh",
        { targetNamespace: targetUrl.href },
      );
    }
    return token
      ? { ...resolvedGatewayHeaders, [APPLICATION_AUTHORIZATION_HEADER]: `Bearer ${token}` }
      : resolvedGatewayHeaders;
  }

  if (!options.primaryBaseUrl && !options.targetBaseUrl) {
    throw new CredentialTargetError(
      "invalid_target_namespace",
      "primary and resolved target namespaces are required before app credentials",
      { targetNamespace: targetUrl.href },
    );
  }

  const routing = await getNetworkEndpointRoutingConfig();
  if (!(await isConfiguredDirectTarget(targetUrl, routing))) {
    throw new CredentialTargetError(
      "invalid_target_namespace",
      "認証先URLが現在の接続設定の境界外です",
      { targetNamespace: targetUrl.href },
    );
  }

  await assertTargetIdentity(targetUrl, routing, options);

  const token = options.applicationToken === undefined
    ? (await getTokenSnapshot()).token
    : options.applicationToken;
  return token
    ? { [APPLICATION_AUTHORIZATION_HEADER]: `Bearer ${token}` }
    : {};
}
