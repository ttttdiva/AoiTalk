import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

/**
 * Stream completed Markdown/text meeting documents to the canonical Python
 * Docs route.  The browser session is translated to the internal auth
 * headers by the shared proxy; the multipart body is never routed through
 * ClipIngest or buffered in Next.js.
 */
export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }
  return proxyRequestToPythonApi(request, {
    path: ["docs", "meeting-import"],
    user,
  });
}
