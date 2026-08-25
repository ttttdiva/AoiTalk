import { NextRequest, NextResponse } from "next/server";
import {
  clearFastAPISessionCookie,
  clearSessionCookie,
  getSession,
} from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export async function POST(request: NextRequest) {
  const protocol = request.headers.get("x-forwarded-proto") || "http";
  let user: Awaited<ReturnType<typeof getSession>>;
  try {
    user = await getSession({
      allowPasswordReset: true,
      throwOnDatabaseError: true,
    });
  } catch (error) {
    console.error("Failed to resolve WebUI session during logout", error);
    return NextResponse.json(
      {
        authenticated: true,
        local_session_cleared: false,
        global_revocation: false,
        detail: "ログアウト処理に失敗しました。再試行してください",
      },
      { status: 503 },
    );
  }
  let globalRevocation = false;
  let localSessionCleared = user === null;
  let status = 200;
  let detail: string | undefined;
  if (user) {
    const upstream = await proxyRequestToPythonApi(request, {
      path: ["auth", "logout"],
      user: { id: String(user.id), username: user.username },
    });
    const result = await upstream.json().catch(() => null);
    const resultRecord =
      result && typeof result === "object" && !Array.isArray(result)
        ? (result as Record<string, unknown>)
        : null;
    globalRevocation = resultRecord?.global_revocation === true;
    // A successful account-wide revocation is sufficient to clear the local
    // cookies even when the backend reports an audit warning as 503.  Keeping
    // the browser session would otherwise leave a stale token in place.
    localSessionCleared = globalRevocation;
    status = upstream.status;
    if (!globalRevocation) {
      detail =
        typeof resultRecord?.detail === "string"
          ? resultRecord.detail
          : "全セッションの失効に失敗しました。再試行してください";
      if (status < 400) status = 503;
    }
  }
  const response = NextResponse.json(
    {
      authenticated: !localSessionCleared,
      local_session_cleared: localSessionCleared,
      global_revocation_required: user !== null,
      global_revocation: globalRevocation,
      ...(detail ? { detail } : {}),
    },
    { status },
  );
  if (localSessionCleared) {
    clearSessionCookie(response, protocol === "https");
    clearFastAPISessionCookie(response, protocol === "https");
  }
  return response;
}
