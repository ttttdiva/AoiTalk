import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { resolveToken, getFallbackToken } from "@/lib/hf/account";
import {
  buildAuthHeaders,
  buildFileUrl,
  type RepoType,
} from "@/lib/hf/client";
import { getMediaType } from "@/lib/hf/api-utils";

/**
 * HFファイルをサーバー側でフェッチしてストリーミング返却する。
 * - トークンをクライアントに露出させない
 * - Range ヘッダを通して動画・音声シークに対応
 * - text モードならテキストとして返す
 */
export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
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
      { status: 400 },
    );
  }
  if (repoType !== "model" && repoType !== "dataset") {
    return NextResponse.json({ detail: "repoType 不正" }, { status: 400 });
  }

  const resolved = accountId ? resolveToken(accountId) : null;
  const token = resolved?.token ?? getFallbackToken();

  const url = buildFileUrl(repoId, path, repoType, revision);
  const headers: Record<string, string> = { ...buildAuthHeaders(token) };
  const range = request.headers.get("range");
  if (range) headers.range = range;

  let upstream: Response;
  try {
    upstream = await fetch(url, { headers, redirect: "follow" });
  } catch (err) {
    return NextResponse.json(
      { detail: `HFフェッチ失敗: ${String(err)}` },
      { status: 502 },
    );
  }

  if (mode === "text") {
    // テキストプレビュー用
    if (!upstream.ok) {
      const t = await upstream.text().catch(() => "");
      return NextResponse.json(
        { detail: `HF ${upstream.status}: ${t.slice(0, 500)}` },
        { status: upstream.status },
      );
    }
    // サイズ上限 1MB でカット
    const MAX = 1024 * 1024;
    const buf = await upstream.arrayBuffer();
    const truncated = buf.byteLength > MAX;
    const slice = truncated ? buf.slice(0, MAX) : buf;
    const text = new TextDecoder("utf-8", { fatal: false }).decode(slice);
    return NextResponse.json({
      success: true,
      text,
      truncated,
      size: buf.byteLength,
    });
  }

  // バイナリ / メディアストリーム
  const upstreamCT =
    upstream.headers.get("content-type") || inferContentType(path);
  const respHeaders: Record<string, string> = {
    "content-type": upstreamCT,
    "cache-control": "private, max-age=300",
  };
  const cl = upstream.headers.get("content-length");
  if (cl) respHeaders["content-length"] = cl;
  const ar = upstream.headers.get("accept-ranges");
  if (ar) respHeaders["accept-ranges"] = ar;
  const cr = upstream.headers.get("content-range");
  if (cr) respHeaders["content-range"] = cr;

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
