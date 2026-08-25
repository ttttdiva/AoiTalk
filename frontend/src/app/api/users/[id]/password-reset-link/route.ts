import { NextRequest, NextResponse } from "next/server";
import { createPasswordResetToken, getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type RouteContext = {
  params: Promise<{ id: string }>;
};

function getRequestOrigin(request: Request) {
  const requestUrl = new URL(request.url);
  const forwardedProto = request.headers.get("x-forwarded-proto");
  const forwardedHost = request.headers.get("x-forwarded-host");
  const host = forwardedHost ?? request.headers.get("host") ?? requestUrl.host;
  const protocol = forwardedProto ?? requestUrl.protocol.replace(":", "");

  return `${protocol}://${host}`;
}

export async function POST(request: NextRequest, context: RouteContext) {
  const admin = await getSession();
  if (!admin) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  if (admin.role !== "admin") {
    return NextResponse.json({ detail: "管理者権限が必要です" }, { status: 403 });
  }

  const { id } = await context.params;
  const response = await proxyRequestToPythonApi(request, {
    path: ["users", encodeURIComponent(id), "password-reset-link"],
    user: admin,
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    return NextResponse.json(body, {
      status: response.status,
      headers: { "cache-control": "no-store, no-cache, must-revalidate" },
    });
  }

  const sessionVersion =
    body && typeof body === "object" && !Array.isArray(body) &&
    typeof body.session_version === "number"
      ? body.session_version
      : null;
  if (!sessionVersion || sessionVersion < 1) {
    return NextResponse.json(
      { detail: "再設定バージョンが不正です" },
      { status: 502 },
    );
  }

  const token = await createPasswordResetToken(id, sessionVersion);
  const baseUrl = getRequestOrigin(request);
  return NextResponse.json(
    {
      reset_url: `${baseUrl}/reset-password?token=${encodeURIComponent(token)}`,
      expires_in_hours: 24,
      session_version: sessionVersion,
    },
    { headers: { "cache-control": "no-store, no-cache, must-revalidate" } },
  );
}
