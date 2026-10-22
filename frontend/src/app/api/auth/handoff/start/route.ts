import { NextRequest } from "next/server";
import { getSessionSnapshot } from "@/lib/auth";
import { countCookieOccurrences } from "@/lib/cookie-guard";
import { SESSION_COOKIE_NAME } from "@/lib/session-cookie";
import {
  createHandoffTransaction,
} from "@/lib/server/auth-handoff-store";
import {
  HANDOFF_START_PATH,
  handoffForbidden,
  handoffRedirect,
  handoffUnsupportedMethod,
  readSingleQueryValue,
  safeHandoffReturnPath,
  setBindingCookie,
  sourceAuthorizeLocation,
  verifyHandoffGateway,
} from "@/lib/server/browser-handoff";

export async function GET(request: NextRequest) {
  if (
    !verifyHandoffGateway(
      request,
      "destination",
      "GET",
      HANDOFF_START_PATH,
    )
  ) {
    return handoffForbidden();
  }

  const returnPath = safeHandoffReturnPath(readSingleQueryValue(request, "next"));
  try {
    const existing = await getSessionSnapshot();
    if (existing) {
      const response = handoffRedirect(returnPath);
      return response;
    }
    if (
      countCookieOccurrences(
        request.headers.get("cookie"),
        SESSION_COOKIE_NAME,
      ) !== 0
    ) {
      return handoffForbidden();
    }

    // Keep transaction creation server-side.  The store generates state,
    // binding, PKCE material, and the short-lived code; none of those values
    // except state travel in this first redirect.
    const created = await createHandoffTransaction({ returnPath });
    const { state, binding } = created;

    const response = handoffRedirect(sourceAuthorizeLocation(state));
    if (!setBindingCookie(response, state, binding)) {
      return handoffForbidden();
    }
    return response;
  } catch {
    // Authentication handoff failures must remain asset-free and must not
    // reveal database, JWT, verifier, state, or code details.
    return handoffForbidden();
  }
}

export async function POST(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_START_PATH);
}

export async function PUT(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_START_PATH);
}

export async function PATCH(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_START_PATH);
}

export async function DELETE(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_START_PATH);
}

export async function HEAD(request: NextRequest) {
  return handoffUnsupportedMethod(request, "destination", HANDOFF_START_PATH);
}

