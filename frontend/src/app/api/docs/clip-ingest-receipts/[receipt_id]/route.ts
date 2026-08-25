import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

/**
 * Receipt detail is proxied through the same-origin Docs API so the browser
 * never needs to know the FastAPI origin.  FastAPI owns the receipt ACL and
 * source-text redaction policy.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ receipt_id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const { receipt_id: receiptId } = await params;
  return proxyRequestToPythonApi(request, {
    path: ["docs", "clip-ingest-receipts", receiptId],
    user,
  });
}
