import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { users } from "@/db/schema";
import {
  attachSessionCookie,
  createSessionToken,
  verifyPassword,
} from "@/lib/auth";

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { username, password } = body;

  if (!username || !password) {
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
    return NextResponse.json(
      { detail: "Authentication failed" },
      { status: 401 },
    );
  }

  const valid = await verifyPassword(password, user.passwordHash);
  if (!valid) {
    return NextResponse.json(
      { detail: "Authentication failed" },
      { status: 401 },
    );
  }

  if (!user.isActive) {
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
