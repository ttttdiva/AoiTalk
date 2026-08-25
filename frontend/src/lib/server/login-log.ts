import { NextRequest } from "next/server";
import { randomUUID } from "node:crypto";
import { db } from "@/db";
import { webuiLoginLogs } from "@/db/schema";
import { resolveLoginClientIp } from "@/lib/server/login-throttle";
import type { LoginThrottleTransaction } from "@/lib/server/login-throttle";

type LoginLogAction = "login" | "logout";

type LoginLogOptions = {
  username: string | null | undefined;
  action: LoginLogAction;
  request: NextRequest;
  success?: boolean;
  failureReason?: string | null;
  sessionDurationSeconds?: number | null;
  executor?: LoginLogExecutor;
};

type LoginLogExecutor = typeof db | LoginThrottleTransaction;

export function isEnterpriseProfile(): boolean {
  return (
    process.env.AOITALK_PROFILE?.trim().toLowerCase() === "enterprise" ||
    process.env.AIVTUBER_ENV?.trim().toLowerCase() === "enterprise"
  );
}

export async function recordWebUILoginLog({
  username,
  action,
  request,
  success = true,
  failureReason = null,
  sessionDurationSeconds = null,
  executor = db,
}: LoginLogOptions): Promise<boolean> {
  try {
    const values = {
      id: randomUUID(),
      username: username?.trim() || "(unknown)",
      action,
      ipAddress: resolveLoginClientIp(request),
      userAgent: request.headers.get("user-agent") || null,
      success,
      failureReason,
      sessionDurationSeconds,
      createdAt: new Date(),
      loginMetadata: {},
    };
    if ("rollback" in executor) {
      // A failed statement aborts a PostgreSQL transaction until its savepoint
      // is rolled back. Keep audit availability observable as false without
      // poisoning the enclosing login transaction.
      await executor.transaction(async (savepoint) => {
        await savepoint.insert(webuiLoginLogs).values(values);
      });
    } else {
      await executor.insert(webuiLoginLogs).values(values);
    }
    return true;
  } catch (error) {
    console.error("Failed to record WebUI login log", error);
    return false;
  }
}
