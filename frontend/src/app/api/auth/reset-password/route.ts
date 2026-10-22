import { NextRequest, NextResponse } from "next/server";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import {
  authResponse,
  denyGatewayAuthFallback,
} from "@/lib/server/browser-handoff";

const MAX_RESET_TOKEN_LENGTH = 8192;
const MAX_PASSWORD_LENGTH = 1024;

async function readResetPasswordInput(
  request: NextRequest,
): Promise<{ token: string; password: string } | null> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const { token, password } = body as Record<string, unknown>;
  if (
    typeof token !== "string" ||
    !token ||
    token.length > MAX_RESET_TOKEN_LENGTH ||
    typeof password !== "string" ||
    password.length < 6 ||
    password.length > MAX_PASSWORD_LENGTH
  ) {
    return null;
  }
  return { token, password };
}

export async function POST(request: NextRequest) {
  const gatewayDenied = denyGatewayAuthFallback(request);
  if (gatewayDenied) return gatewayDenied;

  const input = await readResetPasswordInput(request);
  if (!input) {
    return authResponse(NextResponse.json({ detail: "入力が不正です" }, { status: 400 }));
  }
  const response = await proxyRequestToPythonApi(request, {
    path: ["auth", "reset-password"],
    user: {},
    body: JSON.stringify(input),
  });
  return authResponse(response);
}

export async function GET(request: NextRequest) {
  const gatewayDenied = denyGatewayAuthFallback(request);
  if (gatewayDenied) return gatewayDenied;
  return authResponse(
    new NextResponse(null, { status: 405, headers: { allow: "POST" } }),
  );
}
