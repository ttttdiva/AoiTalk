import { isIP } from "node:net";
import { sql } from "drizzle-orm";
import type { NextRequest } from "next/server";
import { db } from "@/db";

const LOGIN_WINDOW_MS = 10 * 60 * 1000;
const LOGIN_FAILURE_THRESHOLD = 5;
const LOGIN_MAX_BACKOFF_SECONDS = 60;

type AttemptRow = {
  success: boolean | null;
  failure_reason: string | null;
  created_at: Date | string | null;
};

export type LoginThrottleResult<T> =
  | { throttled: true; retryAfter: number }
  | { throttled: false; value: T };

export type LoginThrottleTransaction = Parameters<
  Parameters<typeof db.transaction>[0]
>[0];

function trustsForwardedLoginIp(): boolean {
  return process.env.AOITALK_LOGIN_TRUST_PROXY?.trim() === "true";
}

export function resolveLoginClientIp(request: NextRequest): string {
  // NextRequest does not expose the socket peer. Forwarded headers therefore
  // remain untrusted unless the deployment explicitly guarantees that this
  // listener is reachable only through its trusted reverse proxy boundary.
  if (!trustsForwardedLoginIp()) return "unknown";

  const forwarded = request.headers.get("x-forwarded-for")?.trim() || "";
  if (forwarded && !forwarded.includes(",") && isIP(forwarded)) {
    return forwarded;
  }
  const realIp = request.headers.get("x-real-ip")?.trim() || "";
  if (realIp && !realIp.includes(",") && isIP(realIp)) return realIp;
  return "unknown";
}

function retryAfterForAttempts(rows: AttemptRow[], now: Date): number | null {
  let failures = 0;
  let latestFailureAt: Date | null = null;
  for (const row of rows) {
    if (row.success === true) break;
    if (row.failure_reason === "rate_limited") continue;
    const createdAt = row.created_at ? new Date(row.created_at) : null;
    if (!createdAt || Number.isNaN(createdAt.getTime())) continue;
    latestFailureAt ??= createdAt;
    failures += 1;
  }
  if (failures < LOGIN_FAILURE_THRESHOLD || !latestFailureAt) return null;
  const delay = Math.min(
    LOGIN_MAX_BACKOFF_SECONDS,
    2 ** (failures - LOGIN_FAILURE_THRESHOLD),
  );
  const elapsed = Math.max(0, (now.getTime() - latestFailureAt.getTime()) / 1000);
  const remaining = delay - elapsed;
  return remaining > 0 ? Math.max(1, Math.ceil(remaining)) : null;
}

function resultRows<T>(result: unknown): T[] {
  if (Array.isArray(result)) return result as T[];
  if (
    result &&
    typeof result === "object" &&
    Array.isArray((result as { rows?: unknown }).rows)
  ) {
    return (result as { rows: T[] }).rows;
  }
  return [];
}

export async function withLoginThrottle<T>(
  request: NextRequest,
  username: string,
  operation: (tx: LoginThrottleTransaction) => Promise<T>,
): Promise<LoginThrottleResult<T>> {
  const normalizedUsername = username.trim().toLowerCase();
  const clientIp = resolveLoginClientIp(request);
  const lockKey = `aoitalk-login:${clientIp}:${normalizedUsername}`;
  let operationStarted = false;

  try {
    return await db.transaction(async (tx) => {
      const lockResult = await tx.execute(sql`
        SELECT pg_try_advisory_xact_lock(hashtextextended(${lockKey}, 0)) AS acquired
      `);
      const acquired = resultRows<{ acquired: boolean }>(lockResult)[0]?.acquired;
      if (acquired !== true) return { throttled: true, retryAfter: 1 };

      // drizzle `sql` rejects Date params (postgres.js treats them as strings).
      const since = new Date(Date.now() - LOGIN_WINDOW_MS).toISOString();
      const attemptsResult = await tx.execute(sql`
        SELECT success, failure_reason, created_at
        FROM webui_login_logs
        WHERE action = 'login'
          AND lower(trim(username)) = ${normalizedUsername}
          AND ip_address = ${clientIp}
          AND created_at >= ${since}
          AND (success IS TRUE OR failure_reason IS NULL OR failure_reason <> 'rate_limited')
        ORDER BY created_at DESC
        LIMIT 32
      `);
      const retryAfter = retryAfterForAttempts(
        resultRows<AttemptRow>(attemptsResult),
        new Date(),
      );
      if (retryAfter !== null) return { throttled: true, retryAfter };

      operationStarted = true;
      return { throttled: false, value: await operation(tx) };
    });
  } catch (error) {
    if (operationStarted) throw error;
    console.error("Login throttle guard unavailable; failing open", error);
    // When the guard itself is unavailable there is no live transaction to
    // share. Run the operation in a fresh transaction so it still uses one
    // pool connection for lookup/update/audit.
    return {
      throttled: false,
      value: await db.transaction((tx) => operation(tx)),
    };
  }
}
