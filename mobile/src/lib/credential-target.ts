/**
 * Central credential-target gate for native transports.
 *
 * URL origin equality is insufficient for AoiTalk because a portal service
 * prefix and a direct API origin can route to different installations.  This
 * module binds a complete URL namespace to the durable Ed25519 server pin
 * before a caller is allowed to attach an application credential.  It never
 * creates or returns an application Authorization header.
 */

import {
  getPinnedServerIdentity,
  pinVerifiedServerIdentity,
  proofMatchesServerIdentityPin,
  type ServerIdentityProof,
  verifyServerIdentityProof,
  type Ed25519Verifier,
} from "./server-identity";

export const CREDENTIAL_IDENTITY_PATH = "/api/mobile/server-identity/challenge";

export type CredentialTargetErrorCode =
  | "invalid_target_namespace"
  | "target_namespace_mismatch"
  | "target_namespace_traversal"
  | "target_credentials_in_url"
  | "target_query_not_allowed"
  | "identity_probe_failed"
  | "identity_proof_invalid"
  | "identity_mismatch"
  | "primary_pin_missing"
  | "legacy_target_unsupported"
  | "application_auth_header_forbidden"
  | "credential_revision_changed";

export class CredentialTargetError extends Error {
  readonly code: CredentialTargetErrorCode;
  readonly targetNamespace: string | null;
  readonly status: number | null;
  readonly retryable: boolean | null;

  constructor(
    code: CredentialTargetErrorCode,
    message: string,
    options: {
      targetNamespace?: string | null;
      cause?: unknown;
      status?: number | null;
      retryable?: boolean | null;
    } = {},
  ) {
    super(message);
    this.name = "CredentialTargetError";
    this.code = code;
    this.targetNamespace = options.targetNamespace ?? null;
    this.status = options.status ?? null;
    this.retryable = options.retryable ?? null;
    if (options.cause !== undefined) {
      (this as Error & { cause?: unknown }).cause = options.cause;
    }
  }
}

export type CredentialTargetResponse = {
  status: number;
  json: () => Promise<unknown>;
};

export type CredentialTargetFetcher = (
  url: string,
  init: { method: "GET"; headers: Record<string, string> },
) => Promise<CredentialTargetResponse>;

export type CredentialTargetHeaders = (
  target: string,
) => Promise<Record<string, string>> | Record<string, string>;

export type CredentialTargetRevision = string | number | null | undefined;

export type VerifiedCredentialProofCache = {
  targetNamespace: string;
  proof: ServerIdentityProof;
  revision?: CredentialTargetRevision;
};

export type CredentialTargetOptions = {
  /** Full configured primary API namespace, including any service prefix. */
  primaryBaseUrl: string;
  /** Full candidate namespace for this request, including any service prefix. */
  targetBaseUrl: string;
  /** Optional caller-confirmed namespace. If present it must equal targetBaseUrl. */
  targetNamespace?: string;
  fetcher: CredentialTargetFetcher;
  /** Gateway-only credential builder. It must never return app Authorization. */
  gatewayHeaders?: CredentialTargetHeaders;
  /** A revision composed by the caller from auth/settings/network state. */
  revisionSnapshot?: () => CredentialTargetRevision | Promise<CredentialTargetRevision>;
  /** Optional revisioned proof cache; cache is used only when its pin matches. */
  proofCache?: VerifiedCredentialProofCache;
  proofCacheRevision?: CredentialTargetRevision;
  /** Native Ed25519 verifier, normally backed by @noble/curves. */
  verifyProof?: Ed25519Verifier;
  /** Explicit primary login/known-primary flow may pin this proof. */
  pinPrimary?: boolean;
  /** Exact configured primary may use direct legacy compatibility on identity 404. */
  allowLegacyPrimary?: boolean;
  /** Explicit root -> `/at` discovery approved by the direct legacy route. */
  approvedPrimaryAlias?: boolean;
  /** Require a signed proof; defaults to true. */
  requireProof?: boolean;
  nonceFactory?: () => string;
};

export type CredentialTargetDecision = {
  targetNamespace: string;
  primaryNamespace: string;
  identityStatus: "pinned_primary" | "pinned_alias" | "primary_pinned_now" | "legacy_primary";
  serverId: string | null;
  publicKey: string | null;
  /** Probe headers only. Callers attach app credentials after this decision. */
  probeHeaders: Record<string, string>;
  applicationCredentialAllowed: true;
};

function targetError(
  code: CredentialTargetErrorCode,
  message: string,
  targetNamespace: string | null = null,
  cause?: unknown,
  status?: number | null,
  retryable?: boolean | null,
): CredentialTargetError {
  return new CredentialTargetError(code, message, {
    targetNamespace,
    cause,
    status,
    retryable,
  });
}

function rejectCredentialHeaders(
  headers: Record<string, string>,
  targetNamespace: string,
): Record<string, string> {
  const safe: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (name.trim().toLowerCase() === "authorization") {
      throw targetError(
        "application_auth_header_forbidden",
        "identity probe cannot carry application Authorization",
        targetNamespace,
      );
    }
    safe[name] = value;
  }
  return safe;
}

function decodePathSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    throw targetError("target_namespace_traversal", "target namespace path is not decodable");
  }
}

function assertNoRawTraversal(value: string): void {
  const authorityEnd = value.indexOf("/", value.indexOf("://") + 3);
  const pathAndQuery = authorityEnd >= 0 ? value.slice(authorityEnd) : "/";
  const rawPath = pathAndQuery.split(/[?#]/, 1)[0] || "/";
  for (const rawSegment of rawPath.split("/")) {
    const decoded = decodePathSegment(rawSegment);
    if (decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\")) {
      throw targetError("target_namespace_traversal", "target namespace path traversal is not allowed");
    }
  }
}

/** Normalize protocol, authority and complete path prefix without origin-only fallback. */
export function normalizeCredentialTargetNamespace(value: string): string {
  const raw = String(value ?? "").trim();
  if (!raw) throw targetError("invalid_target_namespace", "target namespace is required");
  if (raw.includes("\\")) {
    throw targetError("target_namespace_traversal", "target namespace cannot contain backslashes");
  }
  if (!/^https?:\/\//i.test(raw)) {
    throw targetError("invalid_target_namespace", "target namespace must use HTTP(S)");
  }
  assertNoRawTraversal(raw);
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch (error) {
    throw targetError("invalid_target_namespace", "target namespace is invalid", null, error);
  }
  if (!parsed.hostname || parsed.username || parsed.password) {
    throw targetError("target_credentials_in_url", "target namespace cannot contain credentials");
  }
  if (parsed.search || parsed.hash) {
    throw targetError("target_query_not_allowed", "target namespace cannot contain query or fragment");
  }
  const protocol = parsed.protocol.toLowerCase();
  const host = parsed.hostname.toLowerCase();
  const port = parsed.port
    || (protocol === "https:" ? "443" : protocol === "http:" ? "80" : "");
  const authority = port === (protocol === "https:" ? "443" : "80")
    ? host
    : `${host}:${port}`;
  let pathname = parsed.pathname || "/";
  pathname = pathname.replace(/\/+/g, "/").replace(/\/+$/, "") || "/";
  return `${protocol}//${authority}${pathname === "/" ? "" : pathname}`;
}

function makeNonce(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.getRandomValues === "function") {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(24));
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    if (typeof globalThis.btoa === "function") {
      return globalThis.btoa(binary)
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/g, "");
    }
  }
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID().replace(/-/g, "");
  throw new CredentialTargetError("identity_probe_failed", "secure challenge nonce is unavailable");
}

function challengeUrl(targetNamespace: string, nonce: string): string {
  return `${targetNamespace}${targetNamespace.endsWith("/") ? "" : "/"}${CREDENTIAL_IDENTITY_PATH.slice(1)}?nonce=${encodeURIComponent(nonce)}&target_namespace=${encodeURIComponent(targetNamespace)}`;
}

async function sameRevision(
  revisionSnapshot: CredentialTargetOptions["revisionSnapshot"],
  expected: CredentialTargetRevision,
): Promise<boolean> {
  if (!revisionSnapshot) return true;
  return (await revisionSnapshot()) === expected;
}

function isCacheUsable(
  cache: VerifiedCredentialProofCache | undefined,
  targetNamespace: string,
  revision: CredentialTargetRevision,
): cache is VerifiedCredentialProofCache {
  return Boolean(
    cache
    && revision !== undefined
    && normalizeCredentialTargetNamespace(cache.targetNamespace) === targetNamespace
    && cache.revision === revision,
  );
}

export async function assertCredentialTargetIdentity(
  options: CredentialTargetOptions,
): Promise<CredentialTargetDecision> {
  const primaryNamespace = normalizeCredentialTargetNamespace(options.primaryBaseUrl);
  const targetNamespace = normalizeCredentialTargetNamespace(options.targetBaseUrl);
  if (options.targetNamespace !== undefined
    && normalizeCredentialTargetNamespace(options.targetNamespace) !== targetNamespace) {
    throw targetError("target_namespace_mismatch", "caller target namespace does not match URL", targetNamespace);
  }
  const isPrimary = targetNamespace === primaryNamespace;
  const beforeRevision = options.revisionSnapshot ? await options.revisionSnapshot() : undefined;
  const primaryPin = await getPinnedServerIdentity(primaryNamespace);
  const cachedProof = options.proofCache;

  let probeHeaders: Record<string, string> = {};
  let proof: ServerIdentityProof | null = null;
  if (isCacheUsable(cachedProof, targetNamespace, options.proofCacheRevision)) {
    proof = cachedProof.proof;
  } else if (options.requireProof !== false || !isPrimary || !options.allowLegacyPrimary) {
    const nonce = options.nonceFactory?.() ?? makeNonce();
    const url = challengeUrl(targetNamespace, nonce);
    try {
      if (options.gatewayHeaders) {
        probeHeaders = rejectCredentialHeaders(
          await options.gatewayHeaders(url),
          targetNamespace,
        );
      }
      const response = await options.fetcher(url, {
        method: "GET",
        headers: {
          Accept: "application/json",
          ...probeHeaders,
          "X-AoiTalk-SDK-Gateway": "identity-probe-v1",
        },
      });
      if (response.status === 404 && isPrimary && options.allowLegacyPrimary) {
        if (!(await sameRevision(options.revisionSnapshot, beforeRevision))) {
          throw targetError("credential_revision_changed", "credential target revision changed", targetNamespace);
        }
        return {
          targetNamespace,
          primaryNamespace,
          identityStatus: "legacy_primary",
          serverId: primaryPin?.serverId ?? null,
          publicKey: primaryPin?.publicKey ?? null,
          probeHeaders,
          applicationCredentialAllowed: true,
        };
      }
      if (response.status !== 200) {
        throw targetError(
          response.status === 404 ? "legacy_target_unsupported" : "identity_probe_failed",
          `identity probe failed with status ${response.status}`,
          targetNamespace,
          undefined,
          response.status,
          response.status === 408 || response.status === 429 || response.status >= 500,
        );
      }
      const payload = await response.json();
      proof = await verifyServerIdentityProof(
        payload,
        nonce,
        targetNamespace,
        options.verifyProof,
      );
    } catch (error) {
      if (error instanceof CredentialTargetError) throw error;
      throw targetError("identity_proof_invalid", "identity proof verification failed", targetNamespace, error);
    }
  }

  if (!proof) {
    throw targetError("identity_proof_invalid", "identity proof is required", targetNamespace);
  }
  if (!isPrimary && !primaryPin && !options.approvedPrimaryAlias) {
    throw targetError("primary_pin_missing", "alias identity cannot be trusted before primary pin", targetNamespace);
  }
  if (primaryPin && !proofMatchesServerIdentityPin(proof, primaryPin)) {
    throw targetError("identity_mismatch", "target identity does not match pinned primary", targetNamespace);
  }
  if (isPrimary && !primaryPin) {
    if (!options.pinPrimary) {
      throw targetError("primary_pin_missing", "primary identity must be explicitly pinned before credentials", targetNamespace);
    }
    try {
      await pinVerifiedServerIdentity(proof, primaryNamespace);
    } catch (error) {
      throw targetError("identity_proof_invalid", "primary identity pin could not be persisted", targetNamespace, error);
    }
  } else if (!isPrimary && options.approvedPrimaryAlias && !primaryPin) {
    try {
      // A known direct root -> `/at` discovery is the only alias that may
      // establish the primary anchor. Preserve both namespaces for restart
      // and later LAN/portal alias checks.
      await pinVerifiedServerIdentity(proof, primaryNamespace);
      await pinVerifiedServerIdentity(proof, targetNamespace);
    } catch (error) {
      throw targetError("identity_proof_invalid", "primary discovery identity pin could not be persisted", targetNamespace, error);
    }
  } else if (!isPrimary) {
    try {
      await pinVerifiedServerIdentity(proof, targetNamespace);
    } catch (error) {
      throw targetError("identity_proof_invalid", "alias identity pin could not be persisted", targetNamespace, error);
    }
  }
  if (!(await sameRevision(options.revisionSnapshot, beforeRevision))) {
    throw targetError("credential_revision_changed", "credential target revision changed", targetNamespace);
  }
  return {
    targetNamespace,
    primaryNamespace,
    identityStatus: isPrimary
      ? (primaryPin ? "pinned_primary" : "primary_pinned_now")
      : (options.approvedPrimaryAlias && !primaryPin ? "primary_pinned_now" : "pinned_alias"),
    serverId: proof.server_id,
    publicKey: proof.public_key,
    probeHeaders,
    applicationCredentialAllowed: true,
  };
}
