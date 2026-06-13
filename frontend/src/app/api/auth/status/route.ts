import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";

export async function GET() {
  const user = await getSession();
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
      password_reset_required: user.isPasswordResetRequired,
      user_settings: user.userSettings ?? {},
    },
  });
}
