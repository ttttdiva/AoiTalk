import { NextRequest, NextResponse } from "next/server";

// Drizzle 直読みで一覧/詳細を返す Next.js Route Handler 向けの、条件付き GET 共通ヘルパー。
// - 弱い ETag（W/"..."）をレスポンス本文（または指定した etagSource）から算出する。
// - `Cache-Control: private, no-cache` を付与し、ブラウザに毎回再検証させる。
// - `If-None-Match` 一致時は 304（空ボディ）を返し、低帯域環境の帯域を節約する。
//
// 適用対象は「ユーザーごとに分離済み」の GET レスポンスのみ（getSession 済みハンドラ）。
// private 指定により共有キャッシュには保存されず、ブラウザ内キャッシュに限定される。

const PRIVATE_NO_CACHE = "private, no-cache";

// FNV-1a 32bit ハッシュ（依存追加なし）。本文の同一性判定用途には十分。
function fnv1a(input: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

export function computeWeakEtag(body: string): string {
  // 長さとハッシュを組み合わせて衝突耐性を上げる。
  return `W/"${body.length.toString(16)}-${fnv1a(body)}"`;
}

function normalizeEtag(tag: string): string {
  return tag
    .trim()
    .replace(/^W\//i, "")
    .replace(/^"|"$/g, "");
}

export function ifNoneMatchSatisfied(
  header: string | null,
  etag: string,
): boolean {
  if (!header) return false;
  if (header.trim() === "*") return true;
  const target = normalizeEtag(etag);
  return header
    .split(",")
    .some((candidate) => normalizeEtag(candidate) === target);
}

/**
 * JSON レスポンスに弱い ETag と private,no-cache を付与し、If-None-Match 一致で 304 を返す。
 *
 * @param options.etagSource ETag 算出に使う値（省略時は本文全体）。
 *   server_time 等の毎回変わる揮発値を ETag から除外したい場合に、それを含まない値を渡す。
 * @param options.extraHeaders 追加の応答ヘッダー。
 */
export function jsonWithConditional(
  request: NextRequest,
  data: unknown,
  options?: {
    etagSource?: unknown;
    extraHeaders?: Record<string, string>;
  },
): NextResponse {
  const body = JSON.stringify(data);
  const etagBody =
    options?.etagSource === undefined
      ? body
      : JSON.stringify(options.etagSource);
  const etag = computeWeakEtag(etagBody);
  const headers: Record<string, string> = {
    "cache-control": PRIVATE_NO_CACHE,
    etag,
    ...(options?.extraHeaders ?? {}),
  };

  if (ifNoneMatchSatisfied(request.headers.get("if-none-match"), etag)) {
    return new NextResponse(null, { status: 304, headers });
  }

  return new NextResponse(body, {
    status: 200,
    headers: { "content-type": "application/json", ...headers },
  });
}
