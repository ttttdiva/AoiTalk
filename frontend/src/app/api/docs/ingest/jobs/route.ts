import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

const MAX_DOCS_INGEST_JOB_BODY_BYTES = 1024 * 1024;

function tooLargeBodyResponse(): NextResponse {
  return NextResponse.json(
    { detail: "取り込みジョブのリクエストは1MiB以下にしてください" },
    { status: 413 },
  );
}

/**
 * Buffer the small JSON job payload through its stream, enforcing a hard cap
 * for clients that omit Content-Length or use chunked transfer encoding.
 */
async function readBoundedBody(
  request: NextRequest,
): Promise<Uint8Array | NextResponse> {
  const contentLength = request.headers.get("content-length");
  const declaredLength = contentLength === null ? null : Number(contentLength);
  if (
    declaredLength !== null &&
    Number.isFinite(declaredLength) &&
    declaredLength >= 0 &&
    declaredLength > MAX_DOCS_INGEST_JOB_BODY_BYTES
  ) {
    return tooLargeBodyResponse();
  }

  const body = request.body;
  if (!body) return new Uint8Array();

  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value || value.byteLength === 0) continue;
      total += value.byteLength;
      if (total > MAX_DOCS_INGEST_JOB_BODY_BYTES) {
        try {
          await reader.cancel();
        } catch {
          // The request is already being rejected; cancellation is best effort.
        }
        return tooLargeBodyResponse();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

async function proxy(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const options = {
    path: ["docs", "ingest", "jobs"],
    user,
  };
  if (request.method === "POST") {
    const body = await readBoundedBody(request);
    if (body instanceof NextResponse) return body;
    return proxyRequestToPythonApi(request, {
      ...options,
      // The proxy helper forwards only this explicit, safety-reviewed header.
      // This route reads the bounded JSON body before proxying so Next does not
      // abort the browser's request stream while the upstream fetch starts.
      body: body as unknown as BodyInit,
      contentLength: body.byteLength,
      forwardHeaders: ["idempotency-key"],
    });
  }
  return proxyRequestToPythonApi(request, options);
}

export async function GET(request: NextRequest) {
  return proxy(request);
}

export async function POST(request: NextRequest) {
  return proxy(request);
}
