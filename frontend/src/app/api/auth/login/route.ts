import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import {
  attachSessionCookie,
  createSessionToken,
  verifyPassword,
} from "@/lib/auth";
import { recordWebUILoginLog } from "@/lib/server/login-log";

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { username, password } = body;

  if (!username || !password) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "missing_credentials",
    });
    return NextResponse.json(
      { detail: "Username and password are required" },
      { status: 400 },
    );
  }

  const [user] = await db
    .select()
    .from(users)
    .where(eq(users.username, username))
    .limit(1);

  if (!user || !user.passwordHash) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "invalid_credentials",
    });
    return NextResponse.json(
      { detail: "Authentication failed" },
      { status: 401 },
    );
  }

  const valid = await verifyPassword(password, user.passwordHash);
  if (!valid) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "invalid_credentials",
    });
    return NextResponse.json(
      { detail: "Authentication failed" },
      { status: 401 },
    );
  }

  if (!user.isActive) {
    await recordWebUILoginLog({
      username,
      action: "login",
      request,
      success: false,
      failureReason: "account_disabled",
    });
    return NextResponse.json(
      { detail: "Account is inactive" },
      { status: 403 },
    );
  }

  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const token = await createSessionToken(user.id);

  await db
    .update(users)
    .set({ lastLogin: new Date() })
    .where(eq(users.id, user.id));

  await recordWebUILoginLog({
    username: user.username,
    action: "login",
    request,
    success: true,
  });

  const response = NextResponse.json({
    authenticated: true,
    user: {
      username: user.username,
      role: user.role,
      display_name: user.displayName,
      password_reset_required: user.isPasswordResetRequired,
    },
  });
  attachSessionCookie(response, token, protocol === "https");
  return response;
}
