import { NextRequest, NextResponse } from "next/server";
import { clearSessionCookie } from "@/lib/auth";

export async function POST(request: NextRequest) {
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  const response = NextResponse.json({ authenticated: false });
  clearSessionCookie(response, protocol === "https");
  return response;
}
