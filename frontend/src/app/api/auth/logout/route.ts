import { NextRequest, NextResponse } from "next/server";
import { clearSessionCookie, getSession } from "@/lib/auth";
import { recordWebUILoginLog } from "@/lib/server/login-log";

export async function POST(request: NextRequest) {
  const user = await getSession();
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const response = NextResponse.json({ authenticated: false });
  if (user) {
    await recordWebUILoginLog({
      username: user.username,
      action: "logout",
      request,
      success: true,
    });
  }
  clearSessionCookie(response, protocol === "https");
  return response;
}
