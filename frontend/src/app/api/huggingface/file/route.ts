import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveTokenForUser } from "@/lib/hf/account";
import {
  buildAuthHeaders,
  buildFileUrl,
  type RepoType,
} from "@/lib/hf/client";
import { getMediaType } from "@/lib/hf/api-utils";

const MAX_TEXT_BYTES = 1024 * 1024;
const PRIVATE_HEADERS = { "Cache-Control": "private, no-store" };

/**
 * HFファイルをサーバー側でフェッチしてストリーミング返却する。
 * - トークンをクライアントに露出させない
 * - Range ヘッダを通して動画・音声シークに対応
 * - text モードならテキストとして返す
 */
export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401, headers: PRIVATE_HEADERS });
  }

  const sp = request.nextUrl.searchParams;
  const accountId = sp.get("accountId");
  const repoId = sp.get("repoId");
  const repoType = (sp.get("repoType") || "model") as RepoType;
  const path = sp.get("path");
  const revision = sp.get("revision") || "main";
  const mode = sp.get("mode"); // "text" | null

  if (!repoId || !path) {
    return NextResponse.json(
      { detail: "repoId, path は必須" },
      { status: 400, headers: PRIVATE_HEADERS },
    );
  }
  if (repoType !== "model" && repoType !== "dataset") {
    return NextResponse.json({ detail: "repoType 不正" }, { status: 400, headers: PRIVATE_HEADERS });
  }

  let resolved: Awaited<ReturnType<typeof resolveTokenForUser>> = null;
  try {
    resolved = accountId
      ? await resolveTokenForUser(String(user.id), accountId)
      : null;
  } catch {
    return NextResponse.json(
      { detail: "HFアカウントを解決できませんでした" },
      { status: 503, headers: PRIVATE_HEADERS },
    );
  }
  if (accountId && !resolved) {
    return NextResponse.json(
      { detail: "HFアカウントへのアクセス権がありません" },
      { status: 403, headers: PRIVATE_HEADERS },
    );
  }
  const token = resolved?.token;

  const url = buildFileUrl(repoId, path, repoType, revision);
  const headers: Record<string, string> = { ...buildAuthHeaders(token) };
  const range = request.headers.get("range");
  if (range) headers.range = range;

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      headers,
      redirect: "follow",
      signal: request.signal,
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `HFフェッチ失敗: ${String(err)}` },
      { status: 502, headers: PRIVATE_HEADERS },
    );
  }

  if (mode === "text") {
    // テキストプレビュー用
    if (!upstream.ok) {
      const t = await upstream.text().catch(() => "");
      return NextResponse.json(
        { detail: `HF ${upstream.status}: ${t.slice(0, 500)}` },
        { status: upstream.status, headers: PRIVATE_HEADERS },
      );
    }
    // Read only a bounded prefix.  Calling arrayBuffer() first would allow a
    // malicious/large text response to consume unbounded server memory before
    // the 1 MB preview limit is applied.
    const contentLengthHeader = upstream.headers.get("content-length");
    const parsedLength = contentLengthHeader === null ? NaN : Number(contentLengthHeader);
    const declaredLength = Number.isFinite(parsedLength) && parsedLength >= 0 ? parsedLength : null;
    if (
      declaredLength !== null &&
      declaredLength > MAX_TEXT_BYTES &&
      !upstream.body
    ) {
      return NextResponse.json(
        { detail: "テキストプレビューが大きすぎます" },
        { status: 413, headers: PRIVATE_HEADERS },
      );
    }
    const chunks: Uint8Array[] = [];
    let total = 0;
    let truncated = declaredLength !== null && declaredLength > MAX_TEXT_BYTES;
    if (upstream.body) {
      const reader = upstream.body.getReader();
      try {
        while (!truncated || total < MAX_TEXT_BYTES) {
          const { done, value } = await reader.read();
          if (done) break;
          const remaining = MAX_TEXT_BYTES - total;
          if (value.byteLength > remaining) {
            if (remaining > 0) chunks.push(value.slice(0, remaining));
            total = MAX_TEXT_BYTES;
            truncated = true;
            break;
          }
          chunks.push(value);
          total += value.byteLength;
          // A response without Content-Length may be exactly the limit; read
          // one more chunk to distinguish exact from truncated content.
          if (total >= MAX_TEXT_BYTES) {
            if (declaredLength !== null && declaredLength > MAX_TEXT_BYTES) {
              truncated = true;
              break;
            }
            // Even an absent or lying Content-Length must not make us retain
            // an oversized response.  Probe one more chunk before deciding
            // whether the prefix is complete.
            const next = await reader.read();
            if (!next.done) truncated = true;
            break;
          }
        }
      } finally {
        if (truncated) await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    }
    const buf = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      buf.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const text = new TextDecoder("utf-8", { fatal: false }).decode(buf);
    return NextResponse.json({
      success: true,
      text,
      truncated,
      // Report bytes actually read rather than trusting a missing or
      // malicious Content-Length.  This also keeps a null/empty response from
      // claiming bytes that were never delivered.
      size: total,
    }, { headers: PRIVATE_HEADERS });
  }

  // バイナリ / メディアストリーム
  const upstreamCT =
    upstream.headers.get("content-type") || inferContentType(path);
  const respHeaders: Record<string, string> = {
    "content-type": upstreamCT,
    // User-owned integrations must not be stored by a shared browser/proxy
    // cache.  Even public repository responses are scoped by the session.
    ...PRIVATE_HEADERS,
  };
  const cl = upstream.headers.get("content-length");
  const contentEncoding = upstream.headers.get("content-encoding");
  if (cl && !contentEncoding) respHeaders["content-length"] = cl;
  const ar = upstream.headers.get("accept-ranges");
  if (ar && !contentEncoding) respHeaders["accept-ranges"] = ar;
  const cr = upstream.headers.get("content-range");
  if (cr && !contentEncoding) respHeaders["content-range"] = cr;

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}

function inferContentType(path: string): string {
  const t = getMediaType(path);
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  if (t === "image") {
    if (ext === "svg") return "image/svg+xml";
    if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
    return `image/${ext}`;
  }
  if (t === "video") return `video/${ext === "mov" ? "quicktime" : ext}`;
  if (t === "audio") return `audio/${ext}`;
  if (t === "text") return "text/plain; charset=utf-8";
  return "application/octet-stream";
}
