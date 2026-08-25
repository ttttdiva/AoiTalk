import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }
  return proxyRequestToPythonApi(request, {
    path: ["docs", "ingest"],
    user,
  });
}
