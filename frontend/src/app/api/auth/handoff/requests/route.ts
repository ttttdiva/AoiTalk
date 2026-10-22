import { NextRequest } from "next/server";
import {
  authHandoffCallbackUri,
  authenticateAuthHandoffClient,
  getAuthHandoffIssuer,
} from "@/lib/server/auth-handoff-clients";
import {
  clientAuthAuthorizationRedirect,
  clientAuthBadRequest,
  clientAuthJson,
  clientAuthMethodNotAllowed,
  clientAuthUnauthorizedResponse,
  CLIENT_AUTH_HANDOFF_TTL_SECONDS,
  InvalidClientAuthBody,
  parseClientAuthRequestBody,
  readBoundedJson,
} from "@/lib/server/auth-handoff-client-http";
import {
  consumeAuthHandoffRateLimit,
  createClientHandoffRequest,
} from "@/lib/server/auth-handoff-client-store";

export async function POST(request: NextRequest) {
  const client = authenticateAuthHandoffClient(request.headers.get("authorization"));
  let rateLimit;
  try {
    rateLimit = await consumeAuthHandoffRateLimit({
      endpoint: "requests",
      clientId: client?.clientId,
    });
  } catch {
    return clientAuthBadRequest(503);
  }
  if (!rateLimit.allowed) {
    const response = clientAuthBadRequest(429);
    response.headers.set("retry-after", String(rateLimit.retryAfter));
    return response;
  }
  if (!client) return clientAuthUnauthorizedResponse();

  let body;
  try {
    body = parseClientAuthRequestBody(await readBoundedJson(request));
  } catch (error) {
    if (error instanceof InvalidClientAuthBody) return clientAuthBadRequest();
    return clientAuthBadRequest();
  }
  if (body.redirect_uri !== authHandoffCallbackUri(client) || body.audience !== client.audience) {
    return clientAuthBadRequest();
  }
  if (!getAuthHandoffIssuer()) return clientAuthBadRequest(503);

  try {
    const created = await createClientHandoffRequest({ client, body });
    const authorizationUrl = clientAuthAuthorizationRedirect(
      created.requestHandle,
      created.state,
    );
    if (!authorizationUrl) return clientAuthBadRequest(503);
    return clientAuthJson({
      authorization_url: authorizationUrl,
      expires_in: CLIENT_AUTH_HANDOFF_TTL_SECONDS,
    });
  } catch {
    return clientAuthBadRequest(503);
  }
}

export function GET() {
  return clientAuthMethodNotAllowed("POST");
}

export function PUT() {
  return clientAuthMethodNotAllowed("POST");
}

export function PATCH() {
  return clientAuthMethodNotAllowed("POST");
}

export function DELETE() {
  return clientAuthMethodNotAllowed("POST");
}

export function HEAD() {
  return clientAuthMethodNotAllowed("POST");
}

export function OPTIONS() {
  return clientAuthMethodNotAllowed("POST");
}
