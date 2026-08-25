import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

/**
 * Keep the Docs UI on its normal same-origin API surface while the receipt
 * data remains authoritative in FastAPI.  FastAPI performs the node/library
 * ACL check and deliberately returns 404 for inaccessible or archived nodes.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const { id } = await params;
  return proxyRequestToPythonApi(request, {
    path: ["docs", "nodes", id, "clip-ingest-receipts"],
    user,
  });
}
