import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

async function proxy(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  return proxyRequestToPythonApi(request, {
    path: ["web-push", "subscription"],
    user,
  });
}

export async function POST(request: NextRequest) {
  return proxy(request);
}

export async function DELETE(request: NextRequest) {
  return proxy(request);
}
