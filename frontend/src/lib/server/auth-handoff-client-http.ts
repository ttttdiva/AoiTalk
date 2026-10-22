import { NextResponse, type NextRequest } from "next/server";
import { getAuthHandoffIssuer } from "@/lib/server/auth-handoff-clients";

export const CLIENT_AUTH_HANDOFF_PATH =
  "/at/api/auth/handoff/client-authorize";
export const CLIENT_AUTH_HANDOFF_QUERY_REQUEST = "request";
export const CLIENT_AUTH_HANDOFF_QUERY_STATE = "state";
export const CLIENT_AUTH_HANDOFF_MAX_BODY_BYTES = 8 * 1024;
export const CLIENT_AUTH_HANDOFF_TTL_SECONDS = 60;
export const CLIENT_AUTH_HANDOFF_ASSERTION_TTL_SECONDS = 60;

export const CLIENT_AUTH_HANDOFF_NO_STORE_HEADERS = {
  "cache-control": "no-store, no-cache, must-revalidate, proxy-revalidate",
  pragma: "no-cache",
  expires: "0",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
};

export type ClientAuthRequestBody = {
  redirect_uri: string;
  audience: string;
  state: string;
  code_challenge: string;
  code_challenge_method: "S256";
};

export type ClientAuthExchangeBody = {
  code: string;
  state: string;
  redirect_uri: string;
  audience: string;
  code_verifier: string;
};

export class InvalidClientAuthBody extends Error {
  constructor() {
    super("Invalid auth handoff request");
    this.name = "InvalidClientAuthBody";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactlyKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  if (Object.keys(value).length !== keys.length) return false;
  const expected = new Set(keys);
  return Object.keys(value).every((key) => expected.has(key));
}

function isPkceValue(value: unknown, minLength = 43, maxLength = 128): value is string {
  return (
    typeof value === "string" &&
    value.length >= minLength &&
    value.length <= maxLength &&
    /^[A-Za-z0-9._~-]+$/u.test(value)
  );
}

function isOpaque43(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43}$/u.test(value);
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

/**
 * A tiny JSON scanner used only to reject duplicate object names.  JSON.parse
 * alone silently takes the last duplicate key, which would make validation
 * depend on which intermediary parsed the body first.
 */
function parseJsonWithoutDuplicateKeys(source: string): unknown {
  let index = 0;

  function skipWhitespace() {
    while (index < source.length && /\s/u.test(source[index] ?? "")) index += 1;
  }

  function scanString(): void {
    if (source[index] !== '"') throw new InvalidClientAuthBody();
    index += 1;
    while (index < source.length) {
      const char = source[index];
      if (char === '"') {
        index += 1;
        return;
      }
      if (char === "\\") {
        index += 1;
        const escaped = source[index];
        if (escaped === "u") {
          const hex = source.slice(index + 1, index + 5);
          if (!/^[0-9A-Fa-f]{4}$/u.test(hex)) throw new InvalidClientAuthBody();
          index += 5;
        } else if (escaped && '"\\/bfnrt'.includes(escaped)) {
          index += 1;
        } else {
          throw new InvalidClientAuthBody();
        }
        continue;
      }
      if (!char || char < " " || char === "\u007f") {
        throw new InvalidClientAuthBody();
      }
      index += 1;
    }
    throw new InvalidClientAuthBody();
  }

  function scanValue(): void {
    skipWhitespace();
    const char = source[index];
    if (char === '"') {
      scanString();
      return;
    }
    if (char === "{") {
      index += 1;
      skipWhitespace();
      const keys = new Set<string>();
      if (source[index] === "}") {
        index += 1;
        return;
      }
      while (index < source.length) {
        skipWhitespace();
        const keyStart = index;
        scanString();
        const key = JSON.parse(source.slice(keyStart, index)) as unknown;
        if (typeof key !== "string" || keys.has(key)) {
          throw new InvalidClientAuthBody();
        }
        keys.add(key);
        skipWhitespace();
        if (source[index] !== ":") throw new InvalidClientAuthBody();
        index += 1;
        scanValue();
        skipWhitespace();
        if (source[index] === "}") {
          index += 1;
          return;
        }
        if (source[index] !== ",") throw new InvalidClientAuthBody();
        index += 1;
      }
      throw new InvalidClientAuthBody();
    }
    if (char === "[") {
      index += 1;
      skipWhitespace();
      if (source[index] === "]") {
        index += 1;
        return;
      }
      while (index < source.length) {
        scanValue();
        skipWhitespace();
        if (source[index] === "]") {
          index += 1;
          return;
        }
        if (source[index] !== ",") throw new InvalidClientAuthBody();
        index += 1;
      }
      throw new InvalidClientAuthBody();
    }
    const literalStart = index;
    while (
      index < source.length &&
      !/[\s,\]}]/u.test(source[index] ?? "")
    ) {
      index += 1;
    }
    const literal = source.slice(literalStart, index);
    if (!/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)$/u.test(literal)) {
      throw new InvalidClientAuthBody();
    }
  }

  skipWhitespace();
  scanValue();
  skipWhitespace();
  if (index !== source.length) throw new InvalidClientAuthBody();
  try {
    return JSON.parse(source) as unknown;
  } catch {
    throw new InvalidClientAuthBody();
  }
}

function contentTypeIsJson(request: Request): boolean {
  const value = request.headers.get("content-type");
  if (!value) return false;
  const [mediaType, ...parameters] = value.split(";");
  if (mediaType.trim().toLowerCase() !== "application/json") return false;
  return parameters.every((parameter) => {
    const trimmed = parameter.trim();
    return trimmed === "" || /^charset=utf-8$/iu.test(trimmed);
  });
}

export async function readBoundedJson(request: NextRequest): Promise<unknown> {
  if (!contentTypeIsJson(request)) throw new InvalidClientAuthBody();
  const declared = request.headers.get("content-length");
  if (declared !== null) {
    if (!/^\d+$/u.test(declared) || Number(declared) > CLIENT_AUTH_HANDOFF_MAX_BODY_BYTES) {
      throw new InvalidClientAuthBody();
    }
  }
  if (!request.body) throw new InvalidClientAuthBody();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > CLIENT_AUTH_HANDOFF_MAX_BODY_BYTES) {
        await reader.cancel("auth handoff request body too large");
        throw new InvalidClientAuthBody();
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let source: string;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new InvalidClientAuthBody();
  }
  return parseJsonWithoutDuplicateKeys(source);
}

export function parseClientAuthRequestBody(value: unknown): ClientAuthRequestBody {
  if (
    !isRecord(value) ||
    !hasExactlyKeys(value, [
      "redirect_uri",
      "audience",
      "state",
      "code_challenge",
      "code_challenge_method",
    ]) ||
    !isBoundedString(value.redirect_uri, 2048) ||
    !isBoundedString(value.audience, 255) ||
    !isOpaque43(value.state) ||
    !isOpaque43(value.code_challenge) ||
    value.code_challenge_method !== "S256"
  ) {
    throw new InvalidClientAuthBody();
  }
  return {
    redirect_uri: value.redirect_uri,
    audience: value.audience,
    state: value.state,
    code_challenge: value.code_challenge,
    code_challenge_method: "S256",
  };
}

export function parseClientAuthExchangeBody(value: unknown): ClientAuthExchangeBody {
  if (
    !isRecord(value) ||
    !hasExactlyKeys(value, [
      "code",
      "state",
      "redirect_uri",
      "audience",
      "code_verifier",
    ]) ||
    !isOpaque43(value.code) ||
    !isOpaque43(value.state) ||
    !isBoundedString(value.redirect_uri, 2048) ||
    !isBoundedString(value.audience, 255) ||
    !isPkceValue(value.code_verifier)
  ) {
    throw new InvalidClientAuthBody();
  }
  return {
    code: value.code,
    state: value.state,
    redirect_uri: value.redirect_uri,
    audience: value.audience,
    code_verifier: value.code_verifier,
  };
}

export function noStoreEmptyResponse(
  status: number,
  extraHeaders?: Record<string, string>,
): NextResponse {
  return new NextResponse(null, {
    status,
    headers: { ...CLIENT_AUTH_HANDOFF_NO_STORE_HEADERS, ...extraHeaders },
  });
}

export function clientAuthUnauthorizedResponse(): NextResponse {
  // Deliberately omit WWW-Authenticate and a body.  The endpoint should not
  // become a browser login or a credential discovery oracle.
  return noStoreEmptyResponse(401);
}

export function clientAuthMethodNotAllowed(allow: string): NextResponse {
  return noStoreEmptyResponse(405, { allow });
}

export function clientAuthBadRequest(status = 400): NextResponse {
  return noStoreEmptyResponse(status);
}

export function clientAuthJson<T>(value: T, status = 200): NextResponse {
  return NextResponse.json(value, {
    status,
    headers: CLIENT_AUTH_HANDOFF_NO_STORE_HEADERS,
  });
}

export function clientAuthAuthorizationRedirect(
  requestHandle: string,
  state: string,
): string | null {
  const issuer = getAuthHandoffIssuer();
  if (!issuer) return null;
  const url = new URL(CLIENT_AUTH_HANDOFF_PATH, issuer);
  url.searchParams.set(CLIENT_AUTH_HANDOFF_QUERY_REQUEST, requestHandle);
  url.searchParams.set(CLIENT_AUTH_HANDOFF_QUERY_STATE, state);
  return url.toString();
}

export function clientAuthCallbackRedirect(
  redirectUri: string,
  code: string,
  state: string,
): string {
  const url = new URL(redirectUri);
  url.search = `?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
  return url.toString();
}

export function readExactClientAuthorizeQuery(
  request: NextRequest,
): { requestHandle: string; state: string } | null {
  const entries = Array.from(request.nextUrl.searchParams.entries());
  if (
    entries.length !== 2 ||
    entries[0]?.[0] !== CLIENT_AUTH_HANDOFF_QUERY_REQUEST ||
    entries[1]?.[0] !== CLIENT_AUTH_HANDOFF_QUERY_STATE
  ) {
    return null;
  }
  const requestHandle = entries[0]?.[1] ?? "";
  const state = entries[1]?.[1] ?? "";
  return isOpaque43(requestHandle) && isOpaque43(state)
    ? { requestHandle, state }
    : null;
}
