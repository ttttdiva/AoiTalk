import { NextRequest } from "next/server";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";

/** Credential authority selected by the user at the login boundary. */
export type CredentialSource = "local" | "active_directory";
export type AuthSource = "local" | "ad";

export type CanonicalAuthUser = {
  id: string;
  username: string;
  role: string | null;
  display_name: string | null;
  password_reset_required: boolean;
  session_version: number;
  auth_source: AuthSource;
};

export type CanonicalAuthResult = {
  authenticated: true;
  user: CanonicalAuthUser;
  session_version: number;
  auth_source: AuthSource;
};

/** Stable error codes emitted by the canonical password-authentication service. */
export type CanonicalAuthErrorCode =
  | "credential_source_required"
  | "unsupported_credential_source"
  | "invalid_credentials"
  | "account_disabled"
  | "ad_configuration_unavailable"
  | "ad_unreachable"
  | "ad_tls_failure"
  | "ad_identity_lookup_failed"
  | "ad_provisioning_conflict"
  | "authentication_backend_unavailable"
  | "unknown";

export class CanonicalAuthError extends Error {
  readonly code: CanonicalAuthErrorCode;
  readonly status: number;
  readonly detail: string;

  constructor(
    code: CanonicalAuthErrorCode,
    detail: string,
    status: number,
  ) {
    super(detail);
    this.name = "CanonicalAuthError";
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function numberValue(value: unknown, fallback = 1): number {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isInteger(numeric) && numeric > 0 ? numeric : fallback;
}

/**
 * Accept the wire spelling used by FastAPI while keeping aliases at the BFF
 * boundary.  The browser never sends an ambiguous/empty source once the login
 * form is rendered; omission remains compatible with local-only deployments.
 */
export function normalizeCredentialSource(value: unknown): CredentialSource | null {
  if (value === "local") return "local";
  if (value === "active_directory" || value === "ad") {
    return "active_directory";
  }
  return null;
}

export function normalizeAuthSource(value: unknown): AuthSource | null {
  if (value === "local") return "local";
  if (value === "ad" || value === "active_directory") return "ad";
  return null;
}

function canonicalErrorCode(value: unknown): CanonicalAuthErrorCode {
  const code = stringValue(value);
  switch (code) {
    case "credential_source_required":
    case "unsupported_credential_source":
    case "invalid_credentials":
    case "account_disabled":
    case "ad_configuration_unavailable":
    case "ad_unreachable":
    case "ad_tls_failure":
    case "ad_identity_lookup_failed":
    case "ad_provisioning_conflict":
    case "authentication_backend_unavailable":
      return code;
    default:
      return "unknown";
  }
}

function statusForCode(code: CanonicalAuthErrorCode, status: number): number {
  if (code === "account_disabled") return 403;
  if (code === "ad_provisioning_conflict") return 409;
  if (
    code === "ad_configuration_unavailable" ||
    code === "ad_unreachable" ||
    code === "ad_tls_failure" ||
    code === "ad_identity_lookup_failed" ||
    code === "authentication_backend_unavailable"
  ) {
    return status >= 500 ? status : 503;
  }
  if (code === "credential_source_required" || code === "unsupported_credential_source") {
    return 400;
  }
  return status >= 400 && status < 600 ? status : 401;
}

function userFromEnvelope(
  envelope: UnknownRecord,
  requestedUsername: string,
): CanonicalAuthUser | null {
  const rawUser = isRecord(envelope.user) ? envelope.user : envelope;
  const id = stringValue(rawUser.id) ?? stringValue(rawUser.user_id);
  const username = stringValue(rawUser.username) ?? requestedUsername.trim();
  if (!id || !username) return null;
  const authSource = normalizeAuthSource(
    rawUser.auth_source ?? rawUser.authSource ?? envelope.auth_source ?? envelope.source,
  );
  // The canonical service always returns the selected source. Do not infer AD
  // from a display label or from a nullable password hash in the BFF.
  if (!authSource) return null;
  return {
    id,
    username,
    role: stringValue(rawUser.role),
    display_name:
      stringValue(rawUser.display_name) ?? stringValue(rawUser.displayName),
    password_reset_required:
      rawUser.password_reset_required === true || rawUser.is_password_reset_required === true,
    session_version: numberValue(
      rawUser.session_version ?? rawUser.sessionVersion ?? envelope.session_version,
    ),
    auth_source: authSource,
  };
}

function errorFromEnvelope(envelope: unknown, status: number): CanonicalAuthError {
  const body = isRecord(envelope) ? envelope : {};
  const detail =
    stringValue(body.detail) ??
    stringValue(body.message) ??
    "Authentication failed";
  const code = canonicalErrorCode(body.code ?? body.error_code ?? body.error);
  return new CanonicalAuthError(code, detail, statusForCode(code, status));
}

/**
 * Invoke the FastAPI-owned password authentication service.  This is the only
 * frontend credential seam: no LDAP client, password hash, or user lookup is
 * present in Next.js.  The internal adapter intentionally has no audit,
 * throttle, or cookie side effects; callers retain those existing semantics.
 */
export async function authenticatePasswordViaPython(
  request: NextRequest,
  input: {
    username: string;
    password: string;
    credentialSource?: CredentialSource;
  },
): Promise<CanonicalAuthResult> {
  let upstream: Response;
  try {
    upstream = await fetchPythonApi("/internal/auth/password-login", {
      method: "POST",
      user: {},
      headers: {
        "content-type": "application/json",
        // Exact adapter marker is required by FastAPI. It also lets the
        // backend distinguish this call from the public browser login route.
        "x-aoitalk-auth-adapter": "password-v1",
        // The original browser request remains the audit/throttle authority in
        // Next. This hint is informational and must never be trusted as an IP.
        "x-forwarded-origin": request.nextUrl.origin,
      },
      body: JSON.stringify({
        username: input.username,
        password: input.password,
        ...(input.credentialSource
          ? { credential_source: input.credentialSource }
          : {}),
      }),
    });
  } catch {
    throw new CanonicalAuthError(
      "authentication_backend_unavailable",
      "Authentication service is unavailable",
      503,
    );
  }

  let envelope: unknown = null;
  try {
    envelope = await upstream.json();
  } catch {
    // Keep a stable error taxonomy even if a proxy emits an empty body.
  }
  if (!upstream.ok) throw errorFromEnvelope(envelope, upstream.status);
  if (!isRecord(envelope) || envelope.authenticated !== true) {
    throw errorFromEnvelope(envelope, upstream.status || 401);
  }

  const user = userFromEnvelope(envelope, input.username);
  if (!user) {
    throw new CanonicalAuthError(
      "authentication_backend_unavailable",
      "Authentication service returned an invalid user record",
      503,
    );
  }
  return {
    authenticated: true,
    user,
    session_version: user.session_version,
    auth_source: user.auth_source,
  };
}

export function isUnavailableAuthError(error: unknown): boolean {
  return error instanceof CanonicalAuthError && error.status >= 500;
}

export function authFailureReason(error: unknown): string {
  if (error instanceof CanonicalAuthError) return error.code;
  return "authentication_backend_unavailable";
}
