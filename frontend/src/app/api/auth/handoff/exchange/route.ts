import { NextRequest } from "next/server";
import { authenticateAuthHandoffClient } from "@/lib/server/auth-handoff-clients";
import {
  clientAuthBadRequest,
  clientAuthJson,
  clientAuthMethodNotAllowed,
  clientAuthUnauthorizedResponse,
  InvalidClientAuthBody,
  parseClientAuthExchangeBody,
  readBoundedJson,
} from "@/lib/server/auth-handoff-client-http";
import {
  consumeAuthHandoffRateLimit,
  exchangeClientHandoff,
} from "@/lib/server/auth-handoff-client-store";

export async function POST(request: NextRequest) {
  const client = authenticateAuthHandoffClient(request.headers.get("authorization"));
  let rateLimit;
  try {
    rateLimit = await consumeAuthHandoffRateLimit({
      endpoint: "exchange",
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
    body = parseClientAuthExchangeBody(await readBoundedJson(request));
  } catch (error) {
    if (error instanceof InvalidClientAuthBody) return clientAuthBadRequest();
    return clientAuthBadRequest();
  }

  try {
    const assertion = await exchangeClientHandoff({ client, body });
    return assertion ? clientAuthJson(assertion) : clientAuthBadRequest();
  } catch {
    return clientAuthBadRequest();
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
