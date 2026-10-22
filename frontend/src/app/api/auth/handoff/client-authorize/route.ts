import { NextRequest } from "next/server";
import { getSessionSnapshot } from "@/lib/auth";
import {
  CLIENT_AUTH_HANDOFF_PATH,
  clientAuthBadRequest,
  clientAuthCallbackRedirect,
  CLIENT_AUTH_HANDOFF_NO_STORE_HEADERS,
  readExactClientAuthorizeQuery,
} from "@/lib/server/auth-handoff-client-http";
import {
  authorizeClientHandoff,
  consumeAuthHandoffRateLimit,
} from "@/lib/server/auth-handoff-client-store";
import { getAuthHandoffIssuer } from "@/lib/server/auth-handoff-clients";
import {
  handoffUnsupportedMethod,
  verifyHandoffGateway,
} from "@/lib/server/browser-handoff";
import { NextResponse } from "next/server";

function redirectToRecipient(redirectUri: string, code: string, state: string): NextResponse {
  return new NextResponse(null, {
    status: 303,
    headers: {
      ...CLIENT_AUTH_HANDOFF_NO_STORE_HEADERS,
      location: clientAuthCallbackRedirect(redirectUri, code, state),
    },
  });
}

export async function GET(request: NextRequest) {
  if (!getAuthHandoffIssuer()) return clientAuthBadRequest(403);
  if (!verifyHandoffGateway(request, "source", "GET", CLIENT_AUTH_HANDOFF_PATH)) {
    return clientAuthBadRequest(403);
  }
  const query = readExactClientAuthorizeQuery(request);
  if (!query) return clientAuthBadRequest();

  try {
    let snapshot;
    try {
      snapshot = await getSessionSnapshot();
    } catch {
      return clientAuthBadRequest(503);
    }
    const snapshotIsValid = Boolean(
      snapshot &&
      snapshot.user &&
      snapshot.user.isActive &&
      !snapshot.user.isPasswordResetRequired &&
      Number.isSafeInteger(snapshot.sessionVersion) &&
      snapshot.sessionVersion >= 1 &&
      Number.isSafeInteger(snapshot.expiresAt) &&
      snapshot.expiresAt > 0 &&
      typeof snapshot.user.id === "string" &&
      snapshot.user.id.length > 0
    );
    const sourceUserId =
      snapshotIsValid && snapshot ? String(snapshot.user.id) : undefined;
    let rateLimit;
    try {
      rateLimit = await consumeAuthHandoffRateLimit({
        endpoint: "authorize",
        ...(sourceUserId ? { sourceUserId } : {}),
      });
    } catch {
      return clientAuthBadRequest(503);
    }
    if (!rateLimit.allowed) {
      const response = clientAuthBadRequest(429);
      response.headers.set("retry-after", String(rateLimit.retryAfter));
      return response;
    }
    if (!snapshotIsValid || !snapshot || !sourceUserId) {
      return clientAuthBadRequest(403);
    }
    const result = await authorizeClientHandoff({
      requestHandle: query.requestHandle,
      recipientState: query.state,
      sourceUserId,
      sourceSessionVersion: snapshot.sessionVersion,
      sourceExpiresAt: snapshot.expiresAt,
    });
    if (!result) return clientAuthBadRequest(403);
    return redirectToRecipient(result.redirectUri, result.code, result.state);
  } catch {
    return clientAuthBadRequest(403);
  }
}

export function POST(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}

export function PUT(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}

export function PATCH(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}

export function DELETE(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}

export function HEAD(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}

export function OPTIONS(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", CLIENT_AUTH_HANDOFF_PATH);
}
