import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import {
  and,
  eq,
  gt,
  inArray,
  isNotNull,
  isNull,
  lt,
  or,
  sql,
} from "drizzle-orm";
import { db } from "@/db";
import { authHandoffClientTransactions, authHandoffRateLimits, users } from "@/db/schema";
import {
  authHandoffCallbackUri,
  authHandoffRegistrationHash,
  equalAuthHandoffDigest,
  getAuthHandoffClient,
  getAuthHandoffIssuer,
  isLegacyAuthHandoffClient,
  type AuthHandoffClient,
} from "@/lib/server/auth-handoff-clients";
import {
  CLIENT_AUTH_HANDOFF_ASSERTION_TTL_SECONDS,
  CLIENT_AUTH_HANDOFF_TTL_SECONDS,
  type ClientAuthExchangeBody,
  type ClientAuthRequestBody,
} from "@/lib/server/auth-handoff-client-http";

export const AUTH_HANDOFF_RATE_LIMIT_WINDOW_SECONDS = 60;
export const AUTH_HANDOFF_REQUEST_RATE_LIMIT = 30;
export const AUTH_HANDOFF_EXCHANGE_RATE_LIMIT = 60;
export const AUTH_HANDOFF_AUTHORIZE_RATE_LIMIT = 60;
export const AUTH_HANDOFF_ANONYMOUS_RATE_LIMIT = 20;
export const AUTH_HANDOFF_MAX_SOURCE_TTL_SECONDS = 60 * 60 * 24 * 7;

const SECRET_BYTES = 32;
const OPAQUE_LENGTH = 43;
const HASH_RE = /^[a-f0-9]{64}$/u;
const OPAQUE_RE = /^[A-Za-z0-9_-]{43}$/u;

type HandoffTime = Date | number;

export type CreateClientHandoffInput = {
  client: AuthHandoffClient;
  body: ClientAuthRequestBody;
  now?: HandoffTime;
};

export type CreateClientHandoffResult = {
  requestHandle: string;
  state: string;
  expiresAt: number;
  client: AuthHandoffClient;
};

export type AuthorizeClientHandoffInput = {
  requestHandle: string;
  recipientState: string;
  sourceUserId: string;
  sourceSessionVersion: number;
  sourceExpiresAt: number;
  now?: HandoffTime;
};

export type AuthorizeClientHandoffResult = {
  code: string;
  state: string;
  redirectUri: string;
  audience: string;
  sourceExpiresAt: number;
};

export type ExchangeClientHandoffInput = {
  client: AuthHandoffClient;
  body: ClientAuthExchangeBody;
  now?: HandoffTime;
};

export type AuthHandoffIdentityAssertion = {
  iss: string;
  sub: string;
  aud: string;
  iat: number;
  exp: number;
};

export type AuthHandoffRateLimitEndpoint = "requests" | "exchange" | "authorize";

export type AuthHandoffRateLimitResult = {
  allowed: boolean;
  attempts: number;
  limit: number;
  retryAfter: number;
};

function nowDate(value?: HandoffTime): Date {
  if (value === undefined) return new Date();
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error("Invalid auth handoff time");
  return date;
}

function unixSeconds(value: Date): number {
  return Math.floor(value.getTime() / 1000);
}

function toDate(value: Date | string | number | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function digest(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function challenge(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("base64url");
}

function randomOpaque(): string {
  const value = randomBytes(SECRET_BYTES).toString("base64url");
  if (value.length !== OPAQUE_LENGTH) throw new Error("Unexpected auth handoff secret length");
  return value;
}

function assertOpaque(value: string, label: string): void {
  if (!OPAQUE_RE.test(value)) throw new Error(`Invalid auth handoff ${label}`);
}

function equalChallenge(left: string, right: string | null | undefined): boolean {
  if (!right || left.length !== right.length) return false;
  const leftBytes = Buffer.from(left, "ascii");
  const rightBytes = Buffer.from(right, "ascii");
  return timingSafeEqual(leftBytes, rightBytes);
}

function verifyClientBinding(
  row: {
    clientId: string;
    registrationHash: string;
    sourceOrigin: string;
    destinationOrigin: string;
    callbackPath: string;
    audience: string;
  },
  issuer: string,
): AuthHandoffClient | null {
  const client = getAuthHandoffClient(row.clientId, undefined, { external: true });
  if (!client || row.destinationOrigin !== client.origin || row.callbackPath !== client.callbackPath || row.audience !== client.audience) {
    return null;
  }
  if (row.sourceOrigin !== issuer) return null;
  const currentHash = authHandoffRegistrationHash(client);
  return equalAuthHandoffDigest(currentHash, row.registrationHash) ? client : null;
}

function sourceExpiry(
  sourceExpiresAt: number,
  nowSeconds: number,
): number | null {
  if (!Number.isSafeInteger(sourceExpiresAt) || sourceExpiresAt <= nowSeconds) return null;
  const bounded = Math.min(
    sourceExpiresAt,
    nowSeconds + AUTH_HANDOFF_MAX_SOURCE_TTL_SECONDS,
  );
  return bounded > nowSeconds ? bounded : null;
}

/**
 * Create a recipient request before any browser navigation.  Only the digest
 * of the opaque request handle is persisted.
 */
export async function createClientHandoffRequest(
  input: CreateClientHandoffInput,
): Promise<CreateClientHandoffResult> {
  const issuer = getAuthHandoffIssuer();
  const currentClient = getAuthHandoffClient(input.client.clientId, undefined, {
    external: true,
  });
  if (
    !issuer ||
    !currentClient ||
    isLegacyAuthHandoffClient(input.client) ||
    !equalAuthHandoffDigest(
      authHandoffRegistrationHash(input.client),
      authHandoffRegistrationHash(currentClient),
    )
  ) {
    throw new Error("Auth handoff issuer/configuration unavailable");
  }
  const now = nowDate(input.now);
  const nowSeconds = unixSeconds(now);
  const requestHandle = randomOpaque();
  const expiresAt = new Date((nowSeconds + CLIENT_AUTH_HANDOFF_TTL_SECONDS) * 1000);
  const state = input.body.state;
  if (
    !OPAQUE_RE.test(state) ||
    input.body.code_challenge_method !== "S256" ||
    !OPAQUE_RE.test(input.body.code_challenge) ||
    input.body.redirect_uri !== authHandoffCallbackUri(currentClient) ||
    input.body.audience !== currentClient.audience
  ) {
    throw new Error("Invalid auth handoff client request");
  }
  const registrationHash = authHandoffRegistrationHash(currentClient);
  await cleanupExpiredClientHandoffs({ now: input.now });
  const [row] = await db
    .insert(authHandoffClientTransactions)
    .values({
      requestHash: digest(requestHandle),
      recipientStateHash: digest(state),
      clientId: currentClient.clientId,
      registrationHash,
      sourceOrigin: issuer,
      destinationOrigin: currentClient.origin,
      callbackPath: currentClient.callbackPath,
      audience: currentClient.audience,
      pkceChallenge: input.body.code_challenge,
      createdAt: now,
      expiresAt,
    })
    .returning({
      id: authHandoffClientTransactions.id,
      expiresAt: authHandoffClientTransactions.expiresAt,
    });
  if (!row) throw new Error("Failed to create auth handoff request");
  const storedExpiry = toDate(row.expiresAt);
  if (!storedExpiry) throw new Error("Invalid auth handoff request expiry");
  return {
    requestHandle,
    state,
    expiresAt: unixSeconds(storedExpiry),
    client: currentClient,
  };
}

/**
 * Authorize a browser request under the source gateway proof.  The row and
 * source user are locked together, then the wall clock is read again so an
 * expired request cannot be revived by lock wait time.
 */
export async function authorizeClientHandoff(
  input: AuthorizeClientHandoffInput,
): Promise<AuthorizeClientHandoffResult | null> {
  assertOpaque(input.requestHandle, "request handle");
  assertOpaque(input.recipientState, "recipient state");
  const issuer = getAuthHandoffIssuer();
  if (!issuer || !input.sourceUserId || !Number.isSafeInteger(input.sourceSessionVersion) || input.sourceSessionVersion < 1) {
    return null;
  }
  const now = nowDate(input.now);
  return await db.transaction(async (tx) => {
    const [row] = await tx
      .select()
      .from(authHandoffClientTransactions)
      .where(eq(authHandoffClientTransactions.requestHash, digest(input.requestHandle)))
      .for("update")
      .limit(1);
    if (!row || !equalAuthHandoffDigest(digest(input.recipientState), row.recipientStateHash)) return null;
    const client = verifyClientBinding(row, issuer);
    if (!client || row.authorizedAt || row.consumedAt || row.codeHash) return null;
    const rowExpiresAt = toDate(row.expiresAt);
    if (!rowExpiresAt || rowExpiresAt.getTime() <= now.getTime()) return null;
    if (!Number.isInteger(input.sourceExpiresAt) || input.sourceExpiresAt <= 0) return null;

    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.id, input.sourceUserId))
      .for("update")
      .limit(1);
    if (!user || !user.isActive || user.isPasswordResetRequired) return null;
    if (Number(user.sessionVersion ?? 1) !== input.sourceSessionVersion) return null;

    const effectiveNow = input.now === undefined ? new Date() : now;
    const effectiveNowSeconds = unixSeconds(effectiveNow);
    if (rowExpiresAt.getTime() <= effectiveNow.getTime()) return null;
    const boundedSourceExpiry = sourceExpiry(input.sourceExpiresAt, effectiveNowSeconds);
    if (boundedSourceExpiry === null) return null;

    const code = randomOpaque();
    const [updated] = await tx
      .update(authHandoffClientTransactions)
      .set({
        sourceUserId: input.sourceUserId,
        sourceSessionVersion: input.sourceSessionVersion,
        sourceExpiresAt: new Date(boundedSourceExpiry * 1000),
        codeHash: digest(code),
        authorizedAt: sql`current_timestamp`,
      })
      .where(
        and(
          eq(authHandoffClientTransactions.id, row.id),
          isNull(authHandoffClientTransactions.authorizedAt),
          isNull(authHandoffClientTransactions.consumedAt),
          isNull(authHandoffClientTransactions.codeHash),
        ),
      )
      .returning({ id: authHandoffClientTransactions.id });
    if (!updated) return null;
    return {
      code,
      state: input.recipientState,
      redirectUri: authHandoffCallbackUri(client),
      audience: client.audience,
      sourceExpiresAt: boundedSourceExpiry,
    };
  });
}

/**
 * Consume the code and issue a small identity assertion.  Every check occurs
 * before the single update, so a malformed callback or a stale PKCE proof does
 * not burn a code.
 */
export async function exchangeClientHandoff(
  input: ExchangeClientHandoffInput,
): Promise<AuthHandoffIdentityAssertion | null> {
  assertOpaque(input.body.code, "code");
  assertOpaque(input.body.state, "recipient state");
  const issuer = getAuthHandoffIssuer();
  if (!issuer || isLegacyAuthHandoffClient(input.client)) return null;
  const now = nowDate(input.now);
  return await db.transaction(async (tx) => {
    const [row] = await tx
      .select()
      .from(authHandoffClientTransactions)
      .where(eq(authHandoffClientTransactions.codeHash, digest(input.body.code)))
      .for("update")
      .limit(1);
    if (!row || !equalAuthHandoffDigest(digest(input.body.state), row.recipientStateHash)) return null;
    const current = verifyClientBinding(row, issuer);
    if (
      !current ||
      current.clientId !== input.client.clientId ||
      !equalAuthHandoffDigest(
        authHandoffRegistrationHash(current),
        authHandoffRegistrationHash(input.client),
      )
    ) {
      return null;
    }
    if (row.consumedAt || !row.authorizedAt || !row.codeHash) return null;
    if (input.body.redirect_uri !== authHandoffCallbackUri(current)) return null;
    if (input.body.audience !== current.audience) return null;
    if (!equalAuthHandoffDigest(digest(input.body.code), row.codeHash)) return null;
    if (!equalChallenge(challenge(input.body.code_verifier), row.pkceChallenge)) return null;

    const rowExpiresAt = toDate(row.expiresAt);
    const sourceExpiresAtDate = toDate(row.sourceExpiresAt);
    if (!rowExpiresAt || !sourceExpiresAtDate) return null;
    if (!row.sourceUserId) return null;
    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.id, row.sourceUserId))
      .for("update")
      .limit(1);
    if (!user || !user.isActive || user.isPasswordResetRequired) return null;
    if (!row.sourceSessionVersion || Number(user.sessionVersion ?? 1) !== row.sourceSessionVersion) return null;

    const effectiveNow = input.now === undefined ? new Date() : now;
    const iat = unixSeconds(effectiveNow);
    const sourceExpiresAt = Math.floor(sourceExpiresAtDate.getTime() / 1000);
    if (rowExpiresAt.getTime() <= effectiveNow.getTime() || sourceExpiresAt <= iat) return null;
    const exp = Math.min(sourceExpiresAt, iat + CLIENT_AUTH_HANDOFF_ASSERTION_TTL_SECONDS);
    if (exp <= iat) return null;

    const [updated] = await tx
      .update(authHandoffClientTransactions)
      .set({ consumedAt: sql`current_timestamp` })
      .where(
        and(
          eq(authHandoffClientTransactions.id, row.id),
          isNull(authHandoffClientTransactions.consumedAt),
          gt(authHandoffClientTransactions.expiresAt, sql`current_timestamp`),
        ),
      )
      .returning({ id: authHandoffClientTransactions.id });
    if (!updated) return null;
    return {
      iss: issuer,
      sub: String(row.sourceUserId),
      aud: current.audience,
      iat,
      exp,
    };
  });
}

function endpointLimit(
  endpoint: AuthHandoffRateLimitEndpoint,
  authenticated: boolean,
): number {
  if (!authenticated) return AUTH_HANDOFF_ANONYMOUS_RATE_LIMIT;
  if (endpoint === "requests") return AUTH_HANDOFF_REQUEST_RATE_LIMIT;
  if (endpoint === "exchange") return AUTH_HANDOFF_EXCHANGE_RATE_LIMIT;
  return AUTH_HANDOFF_AUTHORIZE_RATE_LIMIT;
}

const SOURCE_USER_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

/**
 * Persistent fixed-window limiter.  Anonymous failures always share one
 * bucket; client supplied IDs are never used before Basic authentication.
 * A database error is thrown so the caller can fail closed before creating or
 * consuming any handoff row.
 */
export async function consumeAuthHandoffRateLimit(input: {
  endpoint: AuthHandoffRateLimitEndpoint;
  clientId?: string;
  sourceUserId?: string;
  now?: HandoffTime;
}): Promise<AuthHandoffRateLimitResult> {
  if (
    input.endpoint !== "requests" &&
    input.endpoint !== "exchange" &&
    input.endpoint !== "authorize"
  ) {
    throw new Error("Invalid auth handoff rate limit endpoint");
  }
  if (input.endpoint === "authorize") {
    if (input.clientId !== undefined) {
      throw new Error("Auth handoff authorize rate limit cannot use client ID");
    }
    if (
      input.sourceUserId !== undefined &&
      !SOURCE_USER_ID_RE.test(input.sourceUserId)
    ) {
      throw new Error("Invalid auth handoff source user ID");
    }
  } else {
    if (input.sourceUserId !== undefined) {
      throw new Error("S2S auth handoff rate limit cannot use source user ID");
    }
    if (
      input.clientId !== undefined &&
      (typeof input.clientId !== "string" ||
        input.clientId.length === 0 ||
        input.clientId.length > 64)
    ) {
      throw new Error("Invalid auth handoff client ID");
    }
  }
  const now = nowDate(input.now);
  const windowSeconds = Math.floor(unixSeconds(now) / AUTH_HANDOFF_RATE_LIMIT_WINDOW_SECONDS) * AUTH_HANDOFF_RATE_LIMIT_WINDOW_SECONDS;
  const windowStartedAt = new Date(windowSeconds * 1000);
  const windowIso = windowStartedAt.toISOString();
  const authenticated =
    input.endpoint === "authorize"
      ? Boolean(input.sourceUserId)
      : Boolean(input.clientId);
  const subject =
    input.endpoint === "authorize"
      ? input.sourceUserId
        ? `source:${input.sourceUserId}`
        : "anonymous"
      : input.clientId
        ? `client:${input.clientId}`
        : "anonymous";
  const bucket = `auth-handoff:${input.endpoint}:${subject}`;
  if (bucket.length > 128) throw new Error("Invalid auth handoff rate limit bucket");
  const limit = endpointLimit(input.endpoint, authenticated);

  const [row] = await db
    .insert(authHandoffRateLimits)
    .values({ bucket, windowStartedAt, attempts: 1 })
    .onConflictDoUpdate({
      target: authHandoffRateLimits.bucket,
      set: {
        windowStartedAt: sql`case when ${authHandoffRateLimits.windowStartedAt} >= ${windowIso}::timestamptz then ${authHandoffRateLimits.windowStartedAt} else ${windowIso}::timestamptz end`,
        attempts: sql`case when ${authHandoffRateLimits.windowStartedAt} >= ${windowIso}::timestamptz then ${authHandoffRateLimits.attempts} + 1 else 1 end`,
      },
    })
    .returning({
      windowStartedAt: authHandoffRateLimits.windowStartedAt,
      attempts: authHandoffRateLimits.attempts,
    });
  if (!row) throw new Error("Failed to update auth handoff rate limit");
  const storedWindow = toDate(row.windowStartedAt);
  if (!storedWindow || !Number.isInteger(row.attempts) || row.attempts < 1) {
    throw new Error("Invalid auth handoff rate limit row");
  }
  const retryAfter = Math.max(
    1,
    Math.ceil((storedWindow.getTime() + AUTH_HANDOFF_RATE_LIMIT_WINDOW_SECONDS * 1000 - now.getTime()) / 1000),
  );
  return {
    allowed: row.attempts <= limit,
    attempts: row.attempts,
    limit,
    retryAfter,
  };
}

export function isAuthHandoffHash(value: unknown): value is string {
  return typeof value === "string" && HASH_RE.test(value);
}

/** Delete only expired or already consumed rows, never a live request. */
export async function cleanupExpiredClientHandoffs(input: {
  now?: HandoffTime;
  limit?: number;
} = {}): Promise<number> {
  const now = nowDate(input.now);
  const configuredLimit = input.limit ?? 100;
  if (!Number.isFinite(configuredLimit)) throw new Error("Invalid cleanup limit");
  const limit = Math.min(1000, Math.max(1, Math.trunc(configuredLimit)));
  const cutoff = input.now === undefined ? sql`current_timestamp` : now;
  const ids = await db
    .select({ id: authHandoffClientTransactions.id })
    .from(authHandoffClientTransactions)
    .where(
      or(
        lt(authHandoffClientTransactions.expiresAt, cutoff),
        and(
          isNotNull(authHandoffClientTransactions.consumedAt),
          lt(authHandoffClientTransactions.consumedAt, cutoff),
        ),
      ),
    )
    .limit(limit);
  if (ids.length === 0) return 0;
  const deleted = await db
    .delete(authHandoffClientTransactions)
    .where(inArray(authHandoffClientTransactions.id, ids.map(({ id }) => id)))
    .returning({ id: authHandoffClientTransactions.id });
  return deleted.length;
}
