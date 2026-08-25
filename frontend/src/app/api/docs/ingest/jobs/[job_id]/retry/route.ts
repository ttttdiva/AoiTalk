import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type RouteContext = {
  params: Promise<{ job_id: string }>;
};

export async function POST(request: NextRequest, context: RouteContext) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const { job_id: jobId } = await context.params;
  if (!UUID_RE.test(jobId)) {
    return NextResponse.json({ detail: "Invalid job_id" }, { status: 400 });
  }

  return proxyRequestToPythonApi(request, {
    path: ["docs", "ingest", "jobs", jobId, "retry"],
    user,
    body: "{}",
    contentLength: 2,
    forwardHeaders: ["idempotency-key"],
  });
}
