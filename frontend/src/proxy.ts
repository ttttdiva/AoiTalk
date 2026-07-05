import { jwtVerify } from "jose";
import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = [
  "/login",
  "/reset-password",
  "/api/auth/login",
  "/api/auth/login-form",
  "/api/auth/reset-password",
  "/api/auth/status",
];
const COOKIE_NAME = "aoitalk_session";

function getRequiredJwtSecret(): Uint8Array {
  const secret = process.env.NEXTAUTH_SECRET?.trim();
  if (!secret) {
    throw new Error("NEXTAUTH_SECRET is required for AoiTalk session signing");
  }
  return new TextEncoder().encode(secret);
}

const SECRET = getRequiredJwtSecret();

function redirectToLogin(request: NextRequest) {
  const loginUrl = new URL("/login", request.url);
  const nextPath = `${request.nextUrl.pathname}${request.nextUrl.search}`;
  if (nextPath !== "/login") loginUrl.searchParams.set("next", nextPath);
  return NextResponse.redirect(loginUrl);
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // 公開パス、API、静的ファイルはスキップ
  if (
    PUBLIC_PATHS.some((p) => pathname.startsWith(p)) ||
    pathname.startsWith("/_next") ||
    pathname.startsWith("/api/") ||
    pathname.includes(".")
  ) {
    return NextResponse.next();
  }

  // セッションCookie確認。FastAPI の旧Cookieなど、Next.js JWTではない値は通さない。
  const session = request.cookies.get(COOKIE_NAME);
  if (!session) {
    return redirectToLogin(request);
  }

  try {
    await jwtVerify(session.value, SECRET);
  } catch {
    const response = redirectToLogin(request);
    response.cookies.set(COOKIE_NAME, "", {
      httpOnly: true,
      secure: request.nextUrl.protocol === "https:",
      sameSite: "lax",
      maxAge: 0,
      path: "/",
    });
    return response;
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
