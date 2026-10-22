import { NextRequest } from "next/server";
import { getSessionSnapshot } from "@/lib/auth";
import { authorizeHandoff } from "@/lib/server/auth-handoff-store";
import {
  HANDOFF_AUTHORIZE_PATH,
  destinationCallbackLocation,
  handoffForbidden,
  handoffRedirect,
  handoffUnsupportedMethod,
  isValidHandoffValue,
  readSingleQueryValue,
  verifyHandoffGateway,
} from "@/lib/server/browser-handoff";

export async function GET(request: NextRequest) {
  if (
    !verifyHandoffGateway(
      request,
      "source",
      "GET",
      HANDOFF_AUTHORIZE_PATH,
    )
  ) {
    return handoffForbidden();
  }

  const state = readSingleQueryValue(request, "state");
  if (!state || !isValidHandoffValue(state)) return handoffForbidden();

  try {
    // getSessionSnapshot verifies the signed JWT, active user, session version,
    // and DB reset state.  No source-side login or consent page is reachable.
    const snapshot = await getSessionSnapshot();
    if (!snapshot || snapshot.user.isPasswordResetRequired) {
      return handoffForbidden();
    }

    const result = await authorizeHandoff({
      state,
      sourceUserId: String(snapshot.user.id),
      sourceSessionVersion: snapshot.sessionVersion,
      sourceExpiresAt: snapshot.expiresAt,
    });
    if (!result || !isValidHandoffValue(result.code)) return handoffForbidden();

    return handoffRedirect(destinationCallbackLocation(result.code, state));
  } catch {
    return handoffForbidden();
  }
}

export async function POST(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", HANDOFF_AUTHORIZE_PATH);
}

export async function PUT(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", HANDOFF_AUTHORIZE_PATH);
}

export async function PATCH(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", HANDOFF_AUTHORIZE_PATH);
}

export async function DELETE(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", HANDOFF_AUTHORIZE_PATH);
}

export async function HEAD(request: NextRequest) {
  return handoffUnsupportedMethod(request, "source", HANDOFF_AUTHORIZE_PATH);
}

