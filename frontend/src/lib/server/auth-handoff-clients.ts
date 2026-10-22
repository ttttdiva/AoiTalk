import { createHash, timingSafeEqual } from "node:crypto";

/**
 * An operator registered browser handoff recipient.
 *
 * The value in `secretSha256` is the SHA-256 digest of the recipient's
 * Basic-auth secret.  The raw secret is deliberately never persisted or
 * configured in this process; only its digest is accepted here.
 */
export type AuthHandoffClient = {
  clientId: string;
  origin: string;
  callbackPath: string;
  audience: string;
  secretSha256?: string;
};

export const AUTH_HANDOFF_CLIENTS_ENV = "AOITALK_AUTH_HANDOFF_CLIENTS";
export const AUTH_HANDOFF_ISSUER_ENV = "AOITALK_AUTH_HANDOFF_ISSUER";
export const DEFAULT_AUTH_HANDOFF_ISSUER = "https://az-vnetspace.com";
export const LEGACY_AUTH_HANDOFF_CLIENT_ID = "aoitalk-legacy";
export const LEGACY_AUTH_HANDOFF_ORIGIN = "https://nk-h01.f5.si";
export const LEGACY_AUTH_HANDOFF_CALLBACK_PATH =
  "/at/api/auth/handoff/callback";
export const LEGACY_AUTH_HANDOFF_AUDIENCE = LEGACY_AUTH_HANDOFF_ORIGIN;

const CLIENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const HASH_RE = /^[a-f0-9]{64}$/u;
const MAX_CLIENTS = 100;
const CALLBACK_PATH_MAX_LENGTH = 512;
const ORIGIN_MAX_LENGTH = 255;

// This is a marker, rather than a usable credential.  `authenticate...`
// rejects the legacy row before comparing a supplied secret, so this value
// cannot accidentally turn the compatibility client into an S2S client.
const LEGACY_CLIENT: AuthHandoffClient = Object.freeze({
  clientId: LEGACY_AUTH_HANDOFF_CLIENT_ID,
  origin: LEGACY_AUTH_HANDOFF_ORIGIN,
  callbackPath: LEGACY_AUTH_HANDOFF_CALLBACK_PATH,
  audience: LEGACY_AUTH_HANDOFF_AUDIENCE,
});

type ConfiguredClient = AuthHandoffClient & { secretSha256: string };

export function isLegacyAuthHandoffClient(client: AuthHandoffClient): boolean {
  return client.clientId === LEGACY_AUTH_HANDOFF_CLIENT_ID;
}

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]) {
  const allowed = new Set(keys);
  return Object.keys(value).every((key) => allowed.has(key));
}

/** Canonical HTTPS origins are exact strings, not URL-normalized aliases. */
export function isCanonicalHttpsOrigin(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > ORIGIN_MAX_LENGTH ||
    value.includes("*")
  ) {
    return false;
  }
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      parsed.pathname === "/" &&
      parsed.search === "" &&
      parsed.hash === "" &&
      parsed.origin === value
    );
  } catch {
    return false;
  }
}

/**
 * Callback paths are compared byte-for-byte.  In particular, accepting an
 * encoded slash or a dot segment here would make a proxy's path rewrite part
 * of the trust decision.
 */
export function isExactCallbackPath(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > CALLBACK_PATH_MAX_LENGTH ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("//") ||
    value.includes("\\") ||
    value.includes("%") ||
    value.includes("?") ||
    value.includes("#") ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    return false;
  }
  const segments = value.split("/");
  if (segments.some((segment) => segment === "." || segment === "..")) {
    return false;
  }
  try {
    const parsed = new URL(value, "https://callback.invalid");
    return parsed.pathname === value && parsed.search === "" && parsed.hash === "";
  } catch {
    return false;
  }
}

/**
 * Audience is an exact service binding, rather than an origin alias.  It can
 * be an origin, a service-specific HTTPS URL, or an opaque URI such as
 * `urn:aoitalk:handoff:novel`; it is never normalized or resolved by the
 * provider.
 */
export function isExactAudience(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > ORIGIN_MAX_LENGTH ||
    /[\u0000-\u0020\u007f]/u.test(value) ||
    value.includes("\\") ||
    value.includes("#")
  ) {
    return false;
  }
  if (/^urn:[A-Za-z0-9][A-Za-z0-9:._~-]{0,253}$/u.test(value)) return true;
  if (!value.startsWith("https://")) return false;
  try {
    const parsed = new URL(value);
    return (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.hostname.includes("*") &&
      parsed.search === "" &&
      parsed.hash === "" &&
      value === (parsed.pathname === "/" ? parsed.origin : `${parsed.origin}${parsed.pathname}`)
    );
  } catch {
    return false;
  }
}

function validateClient(value: unknown): ConfiguredClient | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entry = value as Record<string, unknown>;
  if (
    !hasOnlyKeys(entry, [
      "clientId",
      "origin",
      "callbackPath",
      "audience",
      "secretSha256",
    ]) ||
    typeof entry.clientId !== "string" ||
    !CLIENT_ID_RE.test(entry.clientId) ||
    entry.clientId === LEGACY_AUTH_HANDOFF_CLIENT_ID ||
    !isCanonicalHttpsOrigin(entry.origin) ||
    !isExactCallbackPath(entry.callbackPath) ||
    !isExactAudience(entry.audience) ||
    typeof entry.secretSha256 !== "string" ||
    !HASH_RE.test(entry.secretSha256) ||
    entry.secretSha256 === "0".repeat(64)
  ) {
    return null;
  }

  return {
    clientId: entry.clientId,
    origin: entry.origin,
    callbackPath: entry.callbackPath,
    audience: entry.audience,
    secretSha256: entry.secretSha256,
  };
}

function readRawConfig(raw?: string): { configured: boolean; value: unknown } {
  const source = raw === undefined ? process.env[AUTH_HANDOFF_CLIENTS_ENV] : raw;
  if (source === undefined || source.trim() === "") {
    return { configured: false, value: null };
  }
  try {
    return { configured: true, value: JSON.parse(source) as unknown };
  } catch {
    return { configured: true, value: null };
  }
}

/**
 * Resolve the registry on every call.  This intentionally avoids a module
 * cache: secret rotation and removal must invalidate a transaction in every
 * protocol phase.
 *
 * A missing/invalid external registry leaves the historical browser client
 * available so the existing AoiTalk flow keeps working.  It never enables an
 * external S2S client; callers should use `external: true` when looking one
 * up.
 */
export function getAuthHandoffClients(
  raw = process.env[AUTH_HANDOFF_CLIENTS_ENV],
): AuthHandoffClient[] {
  const parsed = readRawConfig(raw);
  if (!parsed.configured) return [LEGACY_CLIENT];
  if (!Array.isArray(parsed.value) || parsed.value.length > MAX_CLIENTS) {
    return [LEGACY_CLIENT];
  }
  const clients = parsed.value.map(validateClient);
  if (clients.some((client) => client === null)) return [LEGACY_CLIENT];
  const external = clients as ConfiguredClient[];

  const ids = new Set<string>([LEGACY_AUTH_HANDOFF_CLIENT_ID]);
  const callbackPairs = new Set<string>([
    `${LEGACY_CLIENT.origin}\u0000${LEGACY_CLIENT.callbackPath}`,
  ]);
  const audiences = new Set<string>([LEGACY_CLIENT.audience]);
  for (const client of external) {
    const callbackPair = `${client.origin}\u0000${client.callbackPath}`;
    if (
      ids.has(client.clientId) ||
      callbackPairs.has(callbackPair) ||
      audiences.has(client.audience)
    ) {
      return [LEGACY_CLIENT];
    }
    ids.add(client.clientId);
    callbackPairs.add(callbackPair);
    audiences.add(client.audience);
  }
  return [LEGACY_CLIENT, ...external];
}

export function getAuthHandoffClient(
  clientId: string,
  raw = process.env[AUTH_HANDOFF_CLIENTS_ENV],
  options: { external?: boolean } = {},
): AuthHandoffClient | null {
  const client = getAuthHandoffClients(raw).find(
    (candidate) => candidate.clientId === clientId,
  );
  if (!client || (options.external && isLegacyAuthHandoffClient(client))) return null;
  return client;
}

export function getExternalAuthHandoffClients(
  raw = process.env[AUTH_HANDOFF_CLIENTS_ENV],
): AuthHandoffClient[] {
  return getAuthHandoffClients(raw).filter((client) => !isLegacyAuthHandoffClient(client));
}

export function getAuthHandoffIssuer(
  raw = process.env[AUTH_HANDOFF_ISSUER_ENV],
): string | null {
  const value = raw === undefined || raw.trim() === "" ? DEFAULT_AUTH_HANDOFF_ISSUER : raw;
  return isCanonicalHttpsOrigin(value) ? value : null;
}

/** The transaction binding is invalid as soon as any registry field changes. */
export function authHandoffRegistrationHash(client: AuthHandoffClient): string {
  const canonical = [
    client.clientId,
    client.origin,
    client.callbackPath,
    client.audience,
    client.secretSha256 ?? "",
  ].join("\u0000");
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

export function equalAuthHandoffDigest(
  left: string,
  right: string | null | undefined,
): boolean {
  if (!HASH_RE.test(left) || !right || !HASH_RE.test(right)) return false;
  const leftBytes = Buffer.from(left, "ascii");
  const rightBytes = Buffer.from(right, "ascii");
  return timingSafeEqual(leftBytes, rightBytes);
}

export function hashAuthHandoffSecret(secret: string): string {
  return createHash("sha256").update(secret, "utf8").digest("hex");
}

export type AuthHandoffBasicCredentials = {
  clientId: string;
  secret: string;
};

/** Parse Basic without trusting a host, forwarded header, or browser cookie. */
export function parseAuthHandoffBasic(
  authorization: string | null,
): AuthHandoffBasicCredentials | null {
  if (!authorization || authorization.includes(",") || authorization.trim() !== authorization) {
    return null;
  }
  const match = /^Basic ([A-Za-z0-9+/]+={0,2})$/u.exec(authorization);
  if (!match) return null;
  let decoded: string;
  try {
    const encoded = match[1];
    const bytes = Buffer.from(encoded, "base64");
    if (!bytes.length || bytes.toString("base64") !== encoded) return null;
    decoded = bytes.toString("utf8");
    if (Buffer.from(decoded, "utf8").compare(bytes) !== 0) return null;
  } catch {
    return null;
  }
  const separator = decoded.indexOf(":");
  if (separator <= 0) return null;
  const clientId = decoded.slice(0, separator);
  const secret = decoded.slice(separator + 1);
  if (!CLIENT_ID_RE.test(clientId) || !secret || secret.length > 512) return null;
  return { clientId, secret };
}

export function authenticateAuthHandoffClient(
  authorization: string | null,
): AuthHandoffClient | null {
  const credentials = parseAuthHandoffBasic(authorization);
  if (!credentials || credentials.clientId === LEGACY_AUTH_HANDOFF_CLIENT_ID) return null;
  const client = getAuthHandoffClient(credentials.clientId, undefined, { external: true });
  if (!client) return null;
  const suppliedDigest = hashAuthHandoffSecret(credentials.secret);
  return equalAuthHandoffDigest(suppliedDigest, client.secretSha256) ? client : null;
}

export function authHandoffCallbackUri(client: AuthHandoffClient): string {
  return `${client.origin}${client.callbackPath}`;
}
