import { NextRequest, NextResponse } from "next/server";
import {
  NO_STORE_HEADERS,
  proxyRequestToPythonApi,
  type InternalPythonUser,
} from "@/lib/server/python-api-proxy";

// These endpoints carry only SDP/JSON metadata. Keep the caps intentionally
// small so chunked requests cannot make the Next.js process buffer unbounded
// data before the Python proxy sees them.
export const MAX_LIVE_VOICE_SDP_BODY_BYTES = 256 * 1024;
export const MAX_LIVE_VOICE_EVENT_BODY_BYTES = 128 * 1024;
export const MAX_LIVE_VOICE_SESSION_BODY_BYTES = 32 * 1024;
export const MAX_LIVE_VOICE_END_BODY_BYTES = 0;

function tooLargeResponse(maxBytes: number): NextResponse {
  return NextResponse.json(
    { detail: `Live Voiceリクエストは${maxBytes}バイト以下にしてください` },
    { status: 413, headers: NO_STORE_HEADERS },
  );
}

/**
 * Read a request body through its stream with a hard byte bound. Checking only
 * Content-Length is insufficient because clients can use transfer-encoding:
 * chunked (or omit the header entirely), so every chunk is counted as read.
 */
export async function readLiveVoiceBodyBounded(
  request: NextRequest,
  maxBytes: number,
): Promise<Uint8Array | NextResponse> {
  const contentLength = request.headers.get("content-length");
  const declaredLength = contentLength === null ? null : Number(contentLength);
  if (
    declaredLength !== null &&
    Number.isFinite(declaredLength) &&
    declaredLength >= 0 &&
    declaredLength > maxBytes
  ) {
    return tooLargeResponse(maxBytes);
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
      if (total > maxBytes) {
        try {
          await reader.cancel();
        } catch {
          // The request is already being rejected; cancellation is best effort.
        }
        return tooLargeResponse(maxBytes);
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

/**
 * Authenticated live-voice proxy that forwards a body already constrained by
 * readLiveVoiceBodyBounded. The generic proxy accepts this BodyInit directly,
 * avoiding its legacy unbounded request.text() branch.
 */
export async function proxyBoundedLiveVoiceRequest(
  request: NextRequest,
  options: {
    path: string[];
    user: InternalPythonUser;
    maxBodyBytes: number;
  },
): Promise<NextResponse> {
  const body = await readLiveVoiceBodyBounded(request, options.maxBodyBytes);
  if (body instanceof NextResponse) return body;
  try {
    return await proxyRequestToPythonApi(request, {
      path: options.path,
      user: options.user,
      // Uint8Array is a valid undici/Fetch body at runtime. Cast only for the
      // DOM BodyInit type, which lags the Node Fetch implementation's type.
      body: body as unknown as BodyInit,
      contentLength: body.byteLength,
    });
  } catch (error) {
    // `buildInternalPythonHeaders` runs before the generic proxy's network
    // try/catch. Convert a missing startup key (or another synchronous proxy
    // configuration failure) into a safe structured response instead of a
    // Next.js 500 HTML error page. Never echo the exception text: it may
    // include deployment paths or header values.
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    const configured =
      message.includes("internal_api_key") || message.includes("internal api key");
    return NextResponse.json(
      {
        detail: configured
          ? "Live Voiceサーバーの設定が未完了です。管理者に INTERNAL_API_KEY を確認してもらってください。"
          : "Live Voiceプロキシを利用できません。管理者に設定を確認してもらってください。",
        code: configured ? "internal_api_not_configured" : "proxy_not_configured",
      },
      { status: 503, headers: NO_STORE_HEADERS },
    );
  }
}
