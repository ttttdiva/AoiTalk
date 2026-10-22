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
import { authHandoffTransactions, users } from "@/db/schema";

/** The two browser origins are deliberately constants, never caller input. */
export const HANDOFF_SOURCE_ORIGIN = "https://az-vnetspace.com";
export const HANDOFF_DESTINATION_ORIGIN = "https://nk-h01.f5.si";
export const HANDOFF_AUTHORIZE_PATH = "/at/api/auth/handoff/authorize";
export const HANDOFF_CALLBACK_PATH = "/at/api/auth/handoff/callback";
export const HANDOFF_TRANSACTION_TTL_SECONDS = 60;
export const HANDOFF_MAX_SOURCE_TTL_SECONDS = 60 * 60 * 24 * 7;

const SECRET_BYTES = 32;
const SECRET_LENGTH = 43;
const DEFAULT_RETURN_PATH = "/at/";
const SECRET_RE = /^[A-Za-z0-9_-]{43}$/u;
const HASH_RE = /^[a-f0-9]{64}$/u;

type HandoffTime = Date | number;

export type CreateHandoffTransactionInput = {
  returnPath?: string;
  now?: HandoffTime;
};

export type CreateHandoffTransactionResult = {
  transactionId: string;
  state: string;
  /** Value set as the destination browser's HttpOnly binding cookie. */
  binding: string;
  returnPath: string;
  expiresAt: number;
};

export type AuthorizeHandoffInput = {
  state: string;
  sourceUserId: string;
  sourceSessionVersion: number;
  sourceExpiresAt: number;
  now?: HandoffTime;
};

export type AuthorizeHandoffResult = {
  transactionId: string;
  state: string;
  code: string;
  returnPath: string;
  sourceUserId: string;
  sessionVersion: number;
  sourceExpiresAt: number;
};

export type ConsumeHandoffInput = {
  state: string;
  binding: string;
  code: string;
  /** Existing destination identity must match before the one-time consume. */
  existingDestinationUserId: string | null;
  now?: HandoffTime;
};

export type ConsumeHandoffResult = {
  transactionId: string;
  userId: string;
  sessionVersion: number;
  sourceExpiresAt: number;
  returnPath: string;
};

export type CleanupExpiredHandoffsInput = {
  now?: HandoffTime;
  limit?: number;
};

function nowDate(value?: HandoffTime): Date {
  if (value === undefined) return new Date();
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error("Invalid handoff time");
  return date;
}

function unixSeconds(value: Date): number {
  return Math.floor(value.getTime() / 1000);
}

function toDate(value: Date | string | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function randomSecret(): string {
  const secret = randomBytes(SECRET_BYTES).toString("base64url");
  if (secret.length !== SECRET_LENGTH) {
    throw new Error("Unexpected handoff secret length");
  }
  return secret;
}

function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function sha256Base64Url(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("base64url");
}

function equalDigest(left: string, right: string | null | undefined): boolean {
  if (!right || !HASH_RE.test(left) || !HASH_RE.test(right)) return false;
  return timingSafeEqual(Buffer.from(left, "ascii"), Buffer.from(right, "ascii"));
}

function assertSecret(value: string, name: string): void {
  if (!SECRET_RE.test(value)) throw new Error(`Invalid handoff ${name}`);
}

function validateReturnPath(value: string | undefined): string {
  const returnPath = value ?? DEFAULT_RETURN_PATH;
  if (
    returnPath.length > 2048 ||
    !returnPath.startsWith("/") ||
    returnPath.startsWith("//") ||
    /[\u0000-\u001f\u007f]/u.test(returnPath)
  ) {
    throw new Error("Invalid handoff return path");
  }
  return returnPath;
}

function rowExpirySeconds(value: Date): number {
  return unixSeconds(value);
}

/**
 * Create a transaction before the browser is redirected to the canonical
 * origin.  The code is generated at authorization time so only its digest is
 * persisted; state and browser binding are returned exactly once to the route.
 */
export async function createHandoffTransaction(
  input: CreateHandoffTransactionInput = {},
): Promise<CreateHandoffTransactionResult> {
  const now = nowDate(input.now);
  const nowSeconds = unixSeconds(now);
  const returnPath = validateReturnPath(input.returnPath);
  const transactionExpiresAt = new Date(
    (nowSeconds + HANDOFF_TRANSACTION_TTL_SECONDS) * 1000,
  );
  // Keep the store self-cleaning without a process-wide timer.  The bounded
  // delete only targets already-expired or consumed rows, so a live handoff
  // cannot be removed by an opportunistic start request.  A database failure
  // intentionally aborts creation; the route turns that into its generic
  // asset-free failure response.
  await cleanupExpiredHandoffs({ limit: 100 });
  const state = randomSecret();
  const binding = randomSecret();
  const pkceVerifier = randomSecret();
  const pkceChallenge = sha256Base64Url(pkceVerifier);
  const [row] = await db
    .insert(authHandoffTransactions)
    .values({
      stateHash: sha256Hex(state),
      bindingHash: sha256Hex(binding),
      pkceChallenge,
      pkceVerifier,
      sourceOrigin: HANDOFF_SOURCE_ORIGIN,
      destinationOrigin: HANDOFF_DESTINATION_ORIGIN,
      callbackPath: HANDOFF_CALLBACK_PATH,
      returnPath,
      createdAt: now,
      expiresAt: transactionExpiresAt,
    })
    .returning({
      id: authHandoffTransactions.id,
      expiresAt: authHandoffTransactions.expiresAt,
    });
  if (!row) throw new Error("Failed to create auth handoff transaction");

  const expiresAt = rowExpirySeconds(row.expiresAt);
  return {
    transactionId: String(row.id),
    state,
    binding,
    returnPath,
    expiresAt,
  };
}

/**
 * Authorize exactly once on the canonical origin, after the route has
 * verified its current session.  The row and user are locked so logout and
 * concurrent authorization cannot race this version check.
 */
export async function authorizeHandoff(
  input: AuthorizeHandoffInput,
): Promise<AuthorizeHandoffResult | null> {
  assertSecret(input.state, "state");
  if (
    !input.sourceUserId ||
    !Number.isInteger(input.sourceSessionVersion) ||
    input.sourceSessionVersion < 1 ||
    !Number.isInteger(input.sourceExpiresAt) ||
    input.sourceExpiresAt <= 0
  ) {
    return null;
  }
  const now = nowDate(input.now);

  return await db.transaction(async (tx) => {
    const [row] = await tx
      .select()
      .from(authHandoffTransactions)
      .where(eq(authHandoffTransactions.stateHash, sha256Hex(input.state)))
      .for("update")
      .limit(1);
    if (!row) return null;
    if (row.sourceOrigin !== HANDOFF_SOURCE_ORIGIN) return null;
    if (row.destinationOrigin !== HANDOFF_DESTINATION_ORIGIN) return null;
    if (row.callbackPath !== HANDOFF_CALLBACK_PATH) return null;
    if (row.authorizedAt || row.consumedAt) return null;
    const rowExpiresAt = toDate(row.expiresAt);
    if (!rowExpiresAt || rowExpiresAt.getTime() <= now.getTime()) return null;
    if (row.sourceUserId && String(row.sourceUserId) !== input.sourceUserId) {
      return null;
    }
    if (
      row.sourceSessionVersion !== null &&
      row.sourceSessionVersion !== input.sourceSessionVersion
    ) {
      return null;
    }

    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.id, input.sourceUserId))
      .for("update")
      .limit(1);
    if (!user || !user.isActive || user.isPasswordResetRequired) return null;
    const userVersion = Number(user.sessionVersion ?? 1);
    if (userVersion !== input.sourceSessionVersion) return null;

    // A production request may have waited on either lock.  Re-read wall
    // clock after both locks so a transaction cannot be authorized after its
    // 60-second lifetime merely because the first timestamp was captured
    // before lock acquisition.
    const effectiveNow = input.now === undefined ? new Date() : now;
    const effectiveNowSeconds = unixSeconds(effectiveNow);
    if (rowExpiresAt.getTime() <= effectiveNow.getTime()) return null;
    const effectiveSourceExpiresAt = Math.min(
      Math.trunc(input.sourceExpiresAt),
      effectiveNowSeconds + HANDOFF_MAX_SOURCE_TTL_SECONDS,
    );
    if (effectiveSourceExpiresAt <= effectiveNowSeconds) return null;

    const sourceExpiry = Math.min(
      effectiveSourceExpiresAt,
      row.sourceExpiresAt
        ? rowExpirySeconds(toDate(row.sourceExpiresAt)!)
        : effectiveSourceExpiresAt,
    );
    if (sourceExpiry <= effectiveNowSeconds) return null;
    const code = randomSecret();
    const [updated] = await tx
      .update(authHandoffTransactions)
      .set({
        sourceUserId: input.sourceUserId,
        sourceSessionVersion: input.sourceSessionVersion,
        sourceExpiresAt: new Date(sourceExpiry * 1000),
        codeHash: sha256Hex(code),
        authorizedAt: sql`current_timestamp`,
      })
      .where(
        and(
          eq(authHandoffTransactions.id, row.id),
          isNull(authHandoffTransactions.authorizedAt),
          isNull(authHandoffTransactions.consumedAt),
        ),
      )
      .returning({ id: authHandoffTransactions.id });
    if (!updated) return null;
    return {
      transactionId: String(row.id),
      state: input.state,
      code,
      returnPath: row.returnPath,
      sourceUserId: input.sourceUserId,
      sessionVersion: input.sourceSessionVersion,
      sourceExpiresAt: sourceExpiry,
    };
  });
}

/**
 * Verify all callback material before marking a transaction consumed.  The
 * handoff row and source user are locked in one transaction, making replay,
 * concurrent exchange, and logout/version changes linearizable.
 */
export async function consumeHandoff(
  input: ConsumeHandoffInput,
): Promise<ConsumeHandoffResult | null> {
  assertSecret(input.state, "state");
  assertSecret(input.binding, "binding");
  assertSecret(input.code, "code");
  const now = nowDate(input.now);
  const nowSeconds = unixSeconds(now);

  return await db.transaction(async (tx) => {
    const [row] = await tx
      .select()
      .from(authHandoffTransactions)
      .where(eq(authHandoffTransactions.stateHash, sha256Hex(input.state)))
      .for("update")
      .limit(1);
    if (!row) return null;
    if (row.sourceOrigin !== HANDOFF_SOURCE_ORIGIN) return null;
    if (row.destinationOrigin !== HANDOFF_DESTINATION_ORIGIN) return null;
    if (row.callbackPath !== HANDOFF_CALLBACK_PATH) return null;
    if (row.consumedAt || !row.authorizedAt || !row.codeHash) return null;
    const rowExpiresAt = toDate(row.expiresAt);
    if (!rowExpiresAt || rowExpiresAt.getTime() <= now.getTime()) return null;
    if (!equalDigest(sha256Hex(input.binding), row.bindingHash)) return null;
    if (!equalDigest(sha256Hex(input.code), row.codeHash)) return null;
    if (sha256Base64Url(row.pkceVerifier) !== row.pkceChallenge) return null;
    const sourceUserId = row.sourceUserId ? String(row.sourceUserId) : null;
    if (!sourceUserId || !row.sourceSessionVersion || !row.sourceExpiresAt) {
      return null;
    }
    if (
      input.existingDestinationUserId !== null &&
      input.existingDestinationUserId !== sourceUserId
    ) {
      return null;
    }
    const sourceExpiresAtDate = toDate(row.sourceExpiresAt);
    if (!sourceExpiresAtDate) return null;
    const sourceExpiresAt = rowExpirySeconds(sourceExpiresAtDate);
    if (sourceExpiresAt <= nowSeconds) return null;

    const [user] = await tx
      .select()
      .from(users)
      .where(eq(users.id, sourceUserId))
      .for("update")
      .limit(1);
    if (!user || !user.isActive || user.isPasswordResetRequired) return null;
    const sessionVersion = Number(user.sessionVersion ?? 1);
    if (sessionVersion !== row.sourceSessionVersion) return null;

    const effectiveNow = input.now === undefined ? new Date() : now;
    const effectiveNowSeconds = unixSeconds(effectiveNow);
    if (rowExpiresAt.getTime() <= effectiveNow.getTime()) return null;
    if (sourceExpiresAt <= effectiveNowSeconds) return null;

    const [updated] = await tx
      .update(authHandoffTransactions)
      .set({ consumedAt: sql`current_timestamp` })
      .where(
        and(
          eq(authHandoffTransactions.id, row.id),
          isNull(authHandoffTransactions.consumedAt),
          gt(authHandoffTransactions.expiresAt, sql`current_timestamp`),
        ),
      )
      .returning({ id: authHandoffTransactions.id });
    if (!updated) return null;
    return {
      transactionId: String(row.id),
      userId: sourceUserId,
      sessionVersion,
      sourceExpiresAt,
      returnPath: row.returnPath,
    };
  });
}

/** Delete a bounded batch of expired/consumed rows; safe for opportunistic cleanup. */
export async function cleanupExpiredHandoffs(
  input: CleanupExpiredHandoffsInput = {},
): Promise<number> {
  const now = nowDate(input.now);
  const configuredLimit = input.limit ?? 100;
  if (!Number.isFinite(configuredLimit)) throw new Error("Invalid cleanup limit");
  const limit = Math.min(1000, Math.max(1, Math.trunc(configuredLimit)));
  const cutoff = input.now === undefined ? sql`current_timestamp` : now;
  const ids = await db
    .select({ id: authHandoffTransactions.id })
    .from(authHandoffTransactions)
    .where(
      or(
        lt(authHandoffTransactions.expiresAt, cutoff),
        and(
          isNotNull(authHandoffTransactions.consumedAt),
          lt(authHandoffTransactions.consumedAt, cutoff),
        ),
      ),
    )
    .limit(limit);
  if (ids.length === 0) return 0;
  const deleted = await db
    .delete(authHandoffTransactions)
    .where(inArray(authHandoffTransactions.id, ids.map(({ id }) => id)))
    .returning({ id: authHandoffTransactions.id });
  return deleted.length;
}

