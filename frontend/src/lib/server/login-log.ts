import { NextRequest } from "next/server";
import { randomUUID } from "node:crypto";
import { db } from "@/db";
import { webuiLoginLogs } from "@/db/schema";

type LoginLogAction = "login" | "logout";

type LoginLogOptions = {
  username: string | null | undefined;
  action: LoginLogAction;
  request: NextRequest;
  success?: boolean;
  failureReason?: string | null;
  sessionDurationSeconds?: number | null;
};

function getClientIp(request: NextRequest): string | null {
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) {
    const [first] = forwardedFor.split(",");
    const ip = first?.trim();
    if (ip) return ip;
  }

  const realIp = request.headers.get("x-real-ip")?.trim();
  return realIp || null;
}

export async function recordWebUILoginLog({
  username,
  action,
  request,
  success = true,
  failureReason = null,
  sessionDurationSeconds = null,
}: LoginLogOptions): Promise<void> {
  try {
    await db.insert(webuiLoginLogs).values({
      id: randomUUID(),
      username: username?.trim() || "(unknown)",
      action,
      ipAddress: getClientIp(request),
      userAgent: request.headers.get("user-agent") || null,
      success,
      failureReason,
      sessionDurationSeconds,
      createdAt: new Date(),
      loginMetadata: {},
    });
  } catch (error) {
    console.error("Failed to record WebUI login log", error);
  }
}
