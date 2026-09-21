/**
 * Canonical Hydrus endpoint policy used by the Next.js credential store.
 *
 * Hydrus is a local/desktop integration, but the URL is still used by a
 * server-side HTTP client.  Keep loopback and arbitrary private-network
 * destinations as separate policy decisions: a trusted native Windows
 * Personal process may connect to its own loopback Hydrus Client, while LAN
 * destinations still require the explicit private-host opt-in.  Public
 * hostnames are resolved before use so a DNS answer cannot silently turn an
 * apparently public URL into an SSRF target.
 */

import dns from "node:dns/promises";
import net from "node:net";

export const DEFAULT_HYDRUS_API_URL = "http://127.0.0.1:45869";

export type HydrusEndpointRejectReason =
  | "invalid-url"
  | "unsupported-protocol"
  | "embedded-credentials"
  | "loopback-requires-native"
  | "private-host"
  | "private-resolution"
  | "dns-failure";

export type HydrusEndpointCheck =
  | {
      allowed: true;
      url: string;
      kind: "loopback" | "private" | "public";
    }
  | {
      allowed: false;
      reason: HydrusEndpointRejectReason;
    };

export interface HydrusPolicyOptions {
  /** Dependency-injection hooks keep policy tests deterministic. */
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform | string;
  lookup?: (
    hostname: string,
  ) => Promise<Array<{ address: string; family?: number }>>;
}

function truthy(value: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test(value?.trim() || "");
}

/** Match ``src.features.Features.profile_name`` fail-closed semantics. */
export function effectiveHydrusProfile(
  env: NodeJS.ProcessEnv = process.env,
): string {
  const profile = env.AOITALK_PROFILE?.trim().toLowerCase() || "";
  const legacyEnvironment = env.AIVTUBER_ENV?.trim().toLowerCase() || "";
  // Enterprise wins over a contradictory/stale Personal selector, just like
  // the Python feature profile resolver.
  if (profile === "enterprise" || legacyEnvironment === "enterprise") {
    return "enterprise";
  }
  return profile || legacyEnvironment || "personal";
}

/**
 * Whether this process is the trusted native Windows Personal integration.
 * The marker is injected by the native launcher; it is intentionally not a
 * browser-controlled value.  In particular, ``HYDRUS_ALLOW_PRIVATE_HOSTS``
 * never grants loopback access by itself.
 */
export function isNativeLocalPersonal(
  options: Pick<HydrusPolicyOptions, "env" | "platform"> = {},
): boolean {
  const env = options.env || process.env;
  const platform = options.platform || process.platform;
  return (
    platform === "win32" &&
    truthy(env.AOITALK_NATIVE_LOCAL) &&
    effectiveHydrusProfile(env) === "personal" &&
    !truthy(env.AOITALK_DOCKER)
  );
}

function normalizedHost(host: string): string {
  return host.trim().replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
}

/**
 * Convert an IPv4-mapped IPv6 literal to its IPv4 spelling.  WHATWG URL
 * canonicalization rewrites ``::ffff:127.0.0.1`` as ``::ffff:7f00:1`` before
 * this policy sees it, so checking only for a dotted suffix is insufficient.
 */
function mappedIpv4Address(host: string): string | null {
  const normalized = normalizedHost(host);
  if (net.isIP(normalized) !== 6) return null;
  const halves = normalized.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":").filter(Boolean) : [];
  const right = halves.length === 2 && halves[1]
    ? halves[1].split(":").filter(Boolean)
    : [];
  const dotted = right.at(-1);
  if (dotted?.includes(".")) {
    const octets = dotted.split(".").map(Number);
    if (
      octets.length !== 4 ||
      octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)
    ) {
      return null;
    }
    right.pop();
    right.push(
      ((octets[0] << 8) | octets[1]).toString(16),
      ((octets[2] << 8) | octets[3]).toString(16),
    );
  }
  const missing = 8 - left.length - right.length;
  if (missing < 0 || (halves.length === 1 && missing !== 0)) return null;
  const groups = [...left, ...Array.from({ length: missing }, () => "0"), ...right];
  if (
    groups.length !== 8 ||
    groups.slice(0, 5).some((group) => Number.parseInt(group, 16) !== 0)
  ) {
    return null;
  }
  if (Number.parseInt(groups[5], 16) !== 0xffff) return null;
  const high = Number.parseInt(groups[6], 16);
  const low = Number.parseInt(groups[7], 16);
  if (!Number.isInteger(high) || !Number.isInteger(low)) return null;
  return `${high >> 8}.${high & 0xff}.${low >> 8}.${low & 0xff}`;
}

/** Return true only for loopback destinations, not all private addresses. */
export function isLoopbackHost(host: string): boolean {
  const normalized = normalizedHost(host);
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost")
  ) {
    return true;
  }
  const family = net.isIP(normalized);
  if (family === 4) {
    const octets = normalized.split(".").map(Number);
    return octets.length === 4 && octets[0] === 127;
  }
  if (family === 6) {
    if (normalized === "::1") return true;
    const mapped = mappedIpv4Address(normalized);
    if (mapped) return isLoopbackHost(mapped);
  }
  return false;
}

/**
 * Return true for RFC1918, loopback, link-local, unspecified, reserved and
 * other non-public address forms.  Hostnames in ``.local`` are treated as
 * private because they are mDNS/local-network names.
 */
export function isPrivateHost(host: string): boolean {
  const normalized = normalizedHost(host);
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized.endsWith(".local")
  ) {
    return true;
  }
  const family = net.isIP(normalized);
  if (family === 4) {
    const octets = normalized.split(".").map(Number);
    if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part))) {
      return false;
    }
    const [a, b] = octets;
    return (
      a === 0 ||
      a === 10 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0) ||
      (a === 192 && b === 0 && octets[2] === 2) ||
      (a === 192 && b === 88 && octets[2] === 99) ||
      (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) ||
      (a === 198 && b === 51) ||
      (a === 203 && b === 0) ||
      a >= 224
    );
  }
  if (family === 6) {
    const mapped = mappedIpv4Address(normalized);
    if (mapped) return isPrivateHost(mapped);
    return (
      normalized === "::" ||
      normalized === "::1" ||
      normalized.startsWith("fc") ||
      normalized.startsWith("fd") ||
      normalized.startsWith("100:") ||
      normalized.startsWith("2001:2") ||
      normalized.startsWith("2001:10") ||
      normalized.startsWith("2001:20") ||
      normalized.startsWith("2001:db8") ||
      normalized.startsWith("3fff:") ||
      normalized.startsWith("fe8") ||
      normalized.startsWith("fe9") ||
      normalized.startsWith("fea") ||
      normalized.startsWith("feb") ||
      normalized.startsWith("ff")
    );
  }
  return false;
}

function allowPrivateHosts(env: NodeJS.ProcessEnv): boolean {
  return (
    truthy(env.HYDRUS_ALLOW_PRIVATE_HOSTS) ||
    truthy(env.AOITALK_HYDRUS_ALLOW_PRIVATE_URLS)
  );
}

function normalizeUrl(value: string):
  | { parsed: URL; normalized: string; hostname: string }
  | { reason: HydrusEndpointRejectReason } {
  const trimmed = value.trim();
  if ([...trimmed].some((character) => {
    const code = character.charCodeAt(0);
    return code < 0x20 || (code >= 0x7f && code <= 0x9f);
  })) {
    return { reason: "invalid-url" };
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { reason: "invalid-url" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { reason: "unsupported-protocol" };
  }
  if (parsed.username || parsed.password) {
    return { reason: "embedded-credentials" };
  }
  if (parsed.search || parsed.hash || trimmed.includes("?") || trimmed.includes("#")) {
    return { reason: "invalid-url" };
  }
  const hostname = normalizedHost(parsed.hostname);
  if (!hostname) return { reason: "invalid-url" };
  const literalFamily = net.isIP(hostname);
  if (literalFamily && isForbiddenLiteral(hostname)) {
    return { reason: "private-host" };
  }
  return {
    parsed,
    hostname,
    normalized: parsed.toString().replace(/\/$/, ""),
  };
}

function isForbiddenLiteral(host: string): boolean {
  const normalized = normalizedHost(host);
  if (!net.isIP(normalized)) return false;
  const mapped = mappedIpv4Address(normalized);
  if (mapped) return isForbiddenLiteral(mapped);
  // Unspecified and reserved literals do not identify a Hydrus service.  Do
  // not let the administrator private-host opt-in turn these into a broad
  // bind/all-address request.
  if (normalized === "0.0.0.0" || normalized === "::") return true;
  if (/^(?:255\.){3}255$/.test(normalized)) return true;
  if (normalized.startsWith("ff")) return true;
  if (net.isIP(normalized) === 4) {
    const octets = normalized.split(".").map(Number);
    const [a, b, c] = octets;
    if (a >= 224) return true;
    return (
      (a === 192 && b === 0 && c === 2) ||
      (a === 192 && b === 88 && c === 99) ||
      (a === 198 && b === 51) ||
      (a === 203 && b === 0)
    );
  }
  return (
    normalized.startsWith("100:") ||
    normalized.startsWith("2001:2") ||
    normalized.startsWith("2001:10") ||
    normalized.startsWith("2001:20") ||
    normalized.startsWith("2001:db8") ||
    normalized.startsWith("3fff:")
  );
}

function isLoopbackAddress(address: string): boolean {
  const normalized = normalizedHost(address);
  const family = net.isIP(normalized);
  if (family === 4) {
    const octets = normalized.split(".").map(Number);
    return octets.length === 4 && octets[0] === 127;
  }
  if (family === 6) {
    if (normalized === "::1") return true;
    const mapped = mappedIpv4Address(normalized);
    if (mapped) return isLoopbackAddress(mapped);
  }
  return false;
}

/**
 * Validate a Hydrus endpoint and perform the DNS private-address guard.
 *
 * A literal private LAN endpoint is allowed only when the administrator has
 * explicitly opted in.  A public hostname resolving to a private address is
 * rejected even with that opt-in: the opt-in is for intentionally named local
 * endpoints, not for bypassing the public-host DNS SSRF check.
 */
export async function inspectHydrusEndpoint(
  value: string,
  options: HydrusPolicyOptions = {},
): Promise<HydrusEndpointCheck> {
  if (typeof value !== "string" || !value.trim()) {
    return { allowed: false, reason: "invalid-url" };
  }
  const normalized = normalizeUrl(value);
  if ("reason" in normalized) return { allowed: false, reason: normalized.reason };

  const env = options.env || process.env;
  const loopback = isLoopbackHost(normalized.hostname);
  const privateHost = isPrivateHost(normalized.hostname);
  if (loopback) {
    if (!isNativeLocalPersonal(options)) {
      return { allowed: false, reason: "loopback-requires-native" };
    }
    // Literal 127/8 and ::1 are intrinsically loopback.  ``localhost`` and
    // ``*.localhost`` are DNS names, so require every answer to remain
    // loopback before allowing the native-local exception.  This prevents a
    // hosts-file/DNS rebinding from turning the trusted alias into an LAN or
    // public destination.
    if (!net.isIP(normalized.hostname)) {
      try {
        const lookup =
          options.lookup ||
          (async (hostname: string) =>
            await dns.lookup(hostname, { all: true, verbatim: true }));
        const addresses = await lookup(normalized.hostname);
        if (
          !addresses.length ||
          addresses.some((entry) => !isLoopbackAddress(String(entry.address)))
        ) {
          return { allowed: false, reason: "private-resolution" };
        }
      } catch {
        return { allowed: false, reason: "dns-failure" };
      }
    }
    return { allowed: true, url: normalized.normalized, kind: "loopback" };
  }
  if (privateHost && !allowPrivateHosts(env)) {
    return { allowed: false, reason: "private-host" };
  }

  // Resolve both private hostnames (all resolved addresses must remain
  // private) and public hostnames (none may be private).  This mirrors the
  // Python policy and protects against a DNS answer changing the trust class
  // of a configured hostname.
  try {
    const lookup =
      options.lookup ||
      (async (hostname: string) =>
        await dns.lookup(hostname, { all: true, verbatim: true }));
    const addresses = await lookup(normalized.hostname);
    if (!addresses.length) {
      return { allowed: false, reason: "private-resolution" };
    }
    if (
      privateHost
        ? addresses.some((entry) => !isPrivateHost(String(entry.address)))
        : addresses.some((entry) => isPrivateHost(String(entry.address)))
    ) {
      return { allowed: false, reason: "private-resolution" };
    }
  } catch {
    return { allowed: false, reason: "dns-failure" };
  }
  return {
    allowed: true,
    url: normalized.normalized,
    kind: privateHost ? "private" : "public",
  };
}

/** Return the normalized URL only when the canonical policy allows it. */
export async function validateHydrusApiUrl(
  value: string,
  options: HydrusPolicyOptions = {},
): Promise<string | null> {
  const result = await inspectHydrusEndpoint(value, options);
  return result.allowed ? result.url : null;
}
