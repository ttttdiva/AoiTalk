import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

/**
 * Stage ClipIngest files without putting their bytes into the browser's job
 * history. The request is streamed as multipart through the Python proxy;
 * callers receive opaque staging IDs and later send those IDs to /ingest.
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
    path: ["docs", "ingest", "uploads"],
    user,
  });
}
