import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { avatarUrl } from "@/lib/server/user-avatar";

function safeAuthSource(value: unknown): "local" | "active_directory" {
  return value === "active_directory" || value === "ad"
    ? "active_directory"
    : "local";
}

export async function GET() {
  const user = await getSession({ allowPasswordReset: true });
  if (!user) {
    return NextResponse.json({ authenticated: false });
  }
  const authSource = safeAuthSource(
    (user as typeof user & { authSource?: unknown }).authSource,
  );
  return NextResponse.json({
    authenticated: true,
    user: {
      id: user.id,
      username: user.username,
      role: user.role,
      display_name: user.displayName,
      avatar_url: avatarUrl(user.id, user.avatarPath),
      password_reset_required: user.isPasswordResetRequired,
      auth_source: authSource,
      user_settings: user.userSettings ?? {},
    },
  });
}
