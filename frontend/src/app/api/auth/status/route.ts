import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { avatarUrl } from "@/lib/server/user-avatar";

export async function GET() {
  const user = await getSession({ allowPasswordReset: true });
  if (!user) {
    return NextResponse.json({ authenticated: false });
  }
  return NextResponse.json({
    authenticated: true,
    user: {
      id: user.id,
      username: user.username,
      role: user.role,
      display_name: user.displayName,
      avatar_url: avatarUrl(user.id, user.avatarPath),
      password_reset_required: user.isPasswordResetRequired,
      user_settings: user.userSettings ?? {},
    },
  });
}
