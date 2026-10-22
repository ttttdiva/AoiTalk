import { NextRequest } from "next/server";
import {
  attachSessionCookie,
  createSessionToken,
  getSessionSnapshot,
  SESSION_MAX_AGE_SECONDS,
} from "@/lib/auth";
import { countCookieOccurrences } from "@/lib/cookie-guard";
import { SESSION_COOKIE_NAME } from "@/lib/session-cookie";
import { consumeHandoff } from "@/lib/server/auth-handoff-store";
import {
  HANDOFF_CALLBACK_PATH,
  clearBindingCookie,
  handoffForbidden,
  handoffRedirect,
  handoffUnsupportedMethod,
  isValidHandoffValue,
  readSingleCookie,
  safeHandoffReturnPath,
  verifyHandoffGateway,
  handoffBindingCookieName,
} from "@/lib/server/browser-handoff";

function callbackQuery(request: NextRequest):
  | { code: string; state: string }
  | null {
  const entries = Array.from(request.nextUrl.searchParams.entries());
  if (
    entries.length !== 2 ||
    entries[0]?.[0] !== "code" ||
    entries[1]?.[0] !== "state"
  ) {
    return null;
  }
  const code = entries[0][1];
  const state = entries[1][1];
  if (!isValidHandoffValue(code) || !isValidHandoffValue(state)) return null;
  return { code, state };
}

export async function GET(request: NextRequest) {
  if (
    !verifyHandoffGateway(
      request,
      "destination",
      "GET",
      HANDOFF_CALLBACK_PATH,
    )
  ) {
    return handoffForbidden();
  }

  const query = callbackQuery(request);
  if (!query) return handoffForbidden();
  const cookieName = handoffBindingCookieName(query.state);
  if (!cookieName) return handoffForbidden();
  const binding = readSingleCookie(request, cookieName);
  if (!binding || !isValidHandoffValue(binding)) return handoffForbidden();

  try {
    // A different user already signed in on DDNS must never be silently
    // replaced.  The store performs the authoritative row/user re-check under
    // its atomic consume transaction; this check provides the explicit
    // existing-browser guard before issuing the new cookie.
    const existing = await getSessionSnapshot();
    if (
      !existing &&
      countCookieOccurrences(
        request.headers.get("cookie"),
        SESSION_COOKIE_NAME,
      ) !== 0
    ) {
      return handoffForbidden();
    }
    const consumed = await consumeHandoff({
      code: query.code,
      state: query.state,
      binding,
      existingDestinationUserId: existing ? String(existing.user.id) : null,
    });
    if (!consumed) {
      return handoffForbidden();
    }

    const userId = consumed.userId;
    const sessionVersion = consumed.sessionVersion;
    const expiresAt = consumed.sourceExpiresAt;
    if (!userId || !Number.isSafeInteger(sessionVersion) || sessionVersion < 1) {
      return handoffForbidden();
    }
    if (existing && String(existing.user.id) !== userId) {
      return handoffForbidden();
    }

    const now = Math.floor(Date.now() / 1000);
    const cappedExpiry = Math.min(expiresAt, now + SESSION_MAX_AGE_SECONDS);
    if (!Number.isSafeInteger(cappedExpiry) || cappedExpiry <= now) {
      return handoffForbidden();
    }
    const token = await createSessionToken(
      userId,
      false,
      sessionVersion,
      { expiresAt: cappedExpiry },
    );
    const returnPath = safeHandoffReturnPath(
      consumed.returnPath,
    );
    const response = handoffRedirect(returnPath);
    // session-cookie.ts appends the legacy-root expiry as a raw header.  Set
    // the binding deletion first so the later cookies.set() reserialization
    // cannot discard that legacy expiry from the final response.
    clearBindingCookie(response, query.state);
    attachSessionCookie(response, token, true, {
      maxAge: Math.max(1, cappedExpiry - now),
    });
    return response;
  } catch {
    return handoffForbidden();
  }
}

export async function POST(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_CALLBACK_PATH);
}

export async function PUT(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_CALLBACK_PATH);
}

export async function PATCH(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_CALLBACK_PATH);
}

export async function DELETE(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_CALLBACK_PATH);
}

export async function HEAD(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_CALLBACK_PATH);
}

