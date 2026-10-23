import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { proxyExplorerDownload } from "@/lib/server/explorer-download-proxy";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import { isSameOriginMutation } from "@/lib/server/same-origin-mutation";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

// The FastAPI rig asset endpoint validates every file against this same
// per-file ceiling. Buffering within the ceiling prevents Next 16 from
// aborting the upstream stream when the route returns a response body tied to
// the incoming request signal.
const RIG_ASSET_MAX_BUFFERED_RESPONSE_BYTES = 128 * 1024 * 1024;

function durationMilliseconds(start: number, end: number): string {
  const elapsed = end - start;
  return Number.isFinite(elapsed) ? Math.max(0, elapsed).toFixed(3) : "0.000";
}

async function proxyToPython(
  request: NextRequest,
  params: { path: string[] },
) {
  const normalizedPath = params.path[0] === "api" ? params.path.slice(1) : params.path;
  const isPersonalOwnerApi = new Set([
    "personal-ledger",
    "personal-drafts",
    "personal-finance",
    "personal-career",
    "personal-reminders",
    "personal-agents",
  ]).has(normalizedPath[0]);
  const measureIntent =
    request.method === "POST" &&
    normalizedPath.length === 2 &&
    normalizedPath[0] === "chat" && normalizedPath[1] === "intent-candidates";
  const started = measureIntent ? performance.now() : 0;
  const user = await getSession();
  const authenticated = measureIntent ? performance.now() : 0;
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  // 個人データの書き込みは同一オリジン JSON 操作に限る（SameSite=Lax に加える CSRF 多層防御）。
  if (
    isPersonalOwnerApi &&
    request.method !== "GET" &&
    request.method !== "HEAD" &&
    (
      !isSameOriginMutation(request) ||
      request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase() !== "application/json" ||
      request.headers.get("sec-fetch-site") === "cross-site"
    )
  ) {
    return NextResponse.json({ detail: "同一オリジン JSON 操作が必要です" }, { status: 403 });
  }
  if (
    normalizedPath.length === 2 &&
    normalizedPath[0] === "explorer" &&
    normalizedPath[1] === "download"
  ) {
    return proxyExplorerDownload(request, user);
  }

  // Operations mutations and local task creation use an explicit idempotency
  // key. Forward only that allowlisted header for those routes; arbitrary headers
  // must remain on the Next.js side of the internal API boundary.
  const isLocalTaskCreate = request.method === "POST" &&
    normalizedPath.length === 2 && normalizedPath[0] === "local-agent" &&
    normalizedPath[1] === "tasks";
  const isAppActionRun = request.method === "POST" &&
    normalizedPath.length === 5 && normalizedPath[0] === "apps" &&
    normalizedPath[2] === "actions" && normalizedPath[4] === "runs";
  const isMediaAssetUpload = request.method === "POST" && normalizedPath.length === 4 && normalizedPath[0] === "operations" && normalizedPath[1] === "media" && normalizedPath[2] === "assets" && normalizedPath[3] === "upload";
  const isMediaAssetSources = request.method === "GET" && normalizedPath.length === 3 && normalizedPath[0] === "operations" && normalizedPath[1] === "media" && normalizedPath[2] === "asset-sources";
  const isMediaAssetPreview = request.method === "GET" && normalizedPath.length === 5 && normalizedPath[0] === "operations" && normalizedPath[1] === "media" && normalizedPath[2] === "assets" && normalizedPath[4] === "preview";
  const isProductDeliverable = request.method === "GET" && normalizedPath.length === 6 && normalizedPath[0] === "operations" && normalizedPath[1] === "media" && normalizedPath[2] === "product-actions" && normalizedPath[4] === "deliverable" && normalizedPath[5] === "content";
  const isAppArchiveDownload = request.method === "GET" && normalizedPath.length === 4 && normalizedPath[0] === "apps" && normalizedPath[2] === "files" && normalizedPath[3] === "archive";
  const isAppRunFileDownload = request.method === "GET" && normalizedPath.length === 7 && normalizedPath[0] === "apps" && normalizedPath[2] === "runs" && normalizedPath[4] === "files" && normalizedPath[6] === "download";
  const assetMime = request.headers.get("content-type") ?? "";
  if (isMediaAssetUpload && !["image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4", "video/webm", "video/quicktime"].includes(assetMime)) return NextResponse.json({ detail: "対応する画像・動画のbinary streamを指定してください" }, { status: 415 });
  const forwardHeaders =
    normalizedPath.length === 2 && normalizedPath[0] === "free-team" && normalizedPath[1] === "openai-confirmation" ? ["cookie"]
      // 個人APIは本人のWebセッションをバックエンドで再検証する（内部転送マーカーだけでは認めない）。
      : isPersonalOwnerApi ? ["cookie"]
      : isMediaAssetUpload ? ["idempotency-key", "x-asset-sha256"]
      : isMediaAssetPreview || isProductDeliverable ? ["if-match"]
      : isAppArchiveDownload || isAppRunFileDownload ? ["x-aoitalk-download-preflight"]
      : normalizedPath[0] === "operations" || isLocalTaskCreate || isAppActionRun
      ? (["idempotency-key"] as const) : undefined;
  const isRigAssetRequest =
    request.method === "GET" &&
    normalizedPath.length === 7 &&
    normalizedPath[0] === "characters" &&
    normalizedPath[1] === "manage" &&
    normalizedPath[3] === "rig" &&
    normalizedPath[4] === "assets";
  const isMediaActionPackage =
    request.method === "GET" && normalizedPath.length === 5 &&
    normalizedPath[0] === "operations" && normalizedPath[1] === "actions" &&
    normalizedPath[3] === "package" && normalizedPath[4] === "content";
  const response = await proxyRequestToPythonApi(request, {
    path: params.path,
    user: { ...user, authoritySource: "web_session" },
    forwardHeaders,
    ...(isMediaAssetSources ? { forwardResponseHeaders: ["x-media-asset-sources-partial", "x-media-asset-sources-warning"] } : {}),
    ...(isProductDeliverable ? { forwardResponseHeaders: ["x-artifact-sha256", "x-manifest-sha256"] } : {}),
    bufferResponse: !isMediaAssetPreview && !isProductDeliverable && (normalizedPath[0] === "operations" || isRigAssetRequest),
    ...(isMediaAssetUpload ? { maxRequestBytes: (assetMime.startsWith("image/") ? 25 : 512) * 1024 * 1024 } : {}),
    ...(isMediaAssetPreview || isProductDeliverable || isAppArchiveDownload ? { downstreamResponseLifetime: true } : {}),
    // Packages contain at most 25 MiB of assets plus 1 MiB of manifest
    // and bounded ZIP metadata. Only this exact GET route needs 27 MiB.
    // Ordinary artifact downloads retain their existing 26 MiB ceiling.
    maxBufferedResponseBytes: isRigAssetRequest
      ? RIG_ASSET_MAX_BUFFERED_RESPONSE_BYTES
      : isMediaActionPackage
        ? 27 * 1024 * 1024
        : normalizedPath[0] === "operations"
        ? normalizedPath[1] === "artifacts" && normalizedPath[3] === "content"
          ? 26 * 1024 * 1024
          : 8 * 1024 * 1024
        : undefined,
  });
  if (measureIntent) {
    // Add fixed numeric metrics without replacing backend timing/security
    // headers or touching the response stream, status, auth, or cancellation.
    response.headers.append(
      "Server-Timing",
      `chat_intent_web_auth;dur=${durationMilliseconds(started, authenticated)}, ` +
      `chat_intent_web_total;dur=${durationMilliseconds(started, performance.now())}`,
    );
  }
  return response;
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

