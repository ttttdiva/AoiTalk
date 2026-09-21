import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyExplorerDownload } from "@/lib/server/explorer-download-proxy";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

async function proxyToPython(
  request: NextRequest,
  params: { path: string[] },
) {
  const normalizedPath = params.path[0] === "api" ? params.path.slice(1) : params.path;
  const user = await getSession();
  if (!user) {
    const isHydrusPath =
      normalizedPath[0] === "hydrus";
    if (isHydrusPath) {
      return NextResponse.json(
        {
          detail: {
            category: "hydrus",
            code: "authentication_required",
            message: "認証が必要です",
          },
        },
        { status: 401, headers: { "Cache-Control": "private, no-store" } },
      );
    }
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  if (
    normalizedPath.length === 2 &&
    normalizedPath[0] === "explorer" &&
    normalizedPath[1] === "download"
  ) {
    return proxyExplorerDownload(request, user);
  }

  // Operations mutations use an explicit idempotency key.  Forward only that
  // allowlisted header for the Operations namespace; arbitrary browser headers
  // must remain on the Next.js side of the internal API boundary.
  const forwardHeaders =
    normalizedPath[0] === "operations" ? (["idempotency-key"] as const) : undefined;
  return proxyRequestToPythonApi(request, {
    path: params.path,
    user: { ...user, authoritySource: "web_session" },
    forwardHeaders,
    bufferResponse: normalizedPath[0] === "operations",
    maxBufferedResponseBytes:
      normalizedPath[0] === "operations" ? 8 * 1024 * 1024 : undefined,
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}

export async function HEAD(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyToPython(request, await params);
}
