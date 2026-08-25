import { NextRequest } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return Response.json({ detail: "認証が必要です" }, { status: 401 });
  }
  return proxyRequestToPythonApi(request, {
    path: ["web-push", "vapid-public-key"],
    user,
  });
}
