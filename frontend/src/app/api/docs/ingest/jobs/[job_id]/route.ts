import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type RouteContext = {
  params: Promise<{ job_id: string }>;
};

async function proxy(
  request: NextRequest,
  jobId: string,
  suffix: "retry" | null = null,
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }
  if (!UUID_RE.test(jobId)) {
    return NextResponse.json({ detail: "Invalid job_id" }, { status: 400 });
  }

  const options = {
    path: ["docs", "ingest", "jobs", jobId, ...(suffix ? [suffix] : [])],
    user,
  };
  if (request.method === "POST") {
    return proxyRequestToPythonApi(request, {
      ...options,
      // Retry has no request payload.  Send a bounded body instead of
      // forwarding the browser's empty request stream, which can be aborted
      // by Next before the upstream fetch starts.
      body: "{}",
      contentLength: 2,
      // A caller may supply a stable key when retrying after an uncertain
      // response.  Only the helper's strict allowlist can reach Python.
      forwardHeaders: ["idempotency-key"],
    });
  }
  return proxyRequestToPythonApi(request, options);
}

export async function GET(request: NextRequest, context: RouteContext) {
  const { job_id: jobId } = await context.params;
  return proxy(request, jobId);
}

export async function POST(request: NextRequest, context: RouteContext) {
  const { job_id: jobId } = await context.params;
  return proxy(request, jobId, "retry");
}
