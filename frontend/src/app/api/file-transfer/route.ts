import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyFileTransfer } from "@/lib/server/file-transfer-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const runtime = "nodejs";

async function handleTransfer(request: NextRequest): Promise<NextResponse> {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "認証が必要です" },
      { status: 401, headers: { "cache-control": "no-store" } },
    );
  }
  const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    return NextResponse.json(
      { detail: "Content-Type must be application/json" },
      { status: 415, headers: { "cache-control": "no-store" } },
    );
  }
  return proxyFileTransfer(request, {
    ...user,
    authoritySource: "web_session",
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  return handleTransfer(request);
}
