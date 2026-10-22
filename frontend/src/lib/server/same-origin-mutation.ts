import type { NextRequest } from "next/server";

/**
 * 状態を変更する要求が、このアプリ自身のオリジンから来たかを確認する。
 * Origin を優先し、無い場合に限り Referer を使う。どちらも無ければ拒否する。
 * セッション Cookie の SameSite=Lax に加える多層防御（CSRF 対策）。
 */
export function isSameOriginMutation(request: NextRequest): boolean {
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  if (!host) return false;
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const protocol = forwardedProto || request.nextUrl.protocol.replace(":", "");
  const expectedOrigin = `${protocol}://${host}`;
  const origin = request.headers.get("origin")?.trim();
  if (origin) return origin === expectedOrigin;
  const referer = request.headers.get("referer")?.trim();
  if (!referer) return false;
  try {
    return new URL(referer).origin === expectedOrigin;
  } catch {
    return false;
  }
}
