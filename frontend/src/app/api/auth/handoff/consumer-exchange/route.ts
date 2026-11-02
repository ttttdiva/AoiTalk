import { NextRequest } from "next/server";
import { authenticateAuthHandoffClient } from "@/lib/server/auth-handoff-clients";
import {
  clientAuthBadRequest,
  clientAuthJson,
  clientAuthMethodNotAllowed,
  clientAuthUnauthorizedResponse,
  parseConsumerExchangeBody,
  readBoundedJson,
} from "@/lib/server/auth-handoff-client-http";
import {
  consumeAuthHandoffRateLimit,
  exchangeClientHandoff,
} from "@/lib/server/auth-handoff-client-store";

export async function POST(request: NextRequest) {
  const client = authenticateAuthHandoffClient(request.headers.get("authorization"));
  try {
    const limit = await consumeAuthHandoffRateLimit({ endpoint: "exchange", clientId: client?.clientId });
    if (!limit.allowed) return clientAuthBadRequest(429);
  } catch { return clientAuthBadRequest(503); }
  if (!client) return clientAuthUnauthorizedResponse();
  let input;
  try { input = parseConsumerExchangeBody(await readBoundedJson(request)); }
  catch { return clientAuthBadRequest(); }
  try {
    const result = await exchangeClientHandoff({ client, ...input });
    return result?.ticket && result?.renewalProof && result?.sourceExpiresAt
      ? clientAuthJson({
          ticket: result.ticket,
          expires_at: result.exp,
          renewal_proof: result.renewalProof,
          source_expires_at: result.sourceExpiresAt,
          identity: {
            iss: result.iss,
            sub: result.sub,
            aud: result.aud,
            iat: result.iat,
            exp: result.exp,
          },
        })
      : clientAuthBadRequest();
  } catch { return clientAuthBadRequest(503); }
}

export function GET() { return clientAuthMethodNotAllowed("POST"); }
export function PUT() { return clientAuthMethodNotAllowed("POST"); }
export function PATCH() { return clientAuthMethodNotAllowed("POST"); }
export function DELETE() { return clientAuthMethodNotAllowed("POST"); }
export function HEAD() { return clientAuthMethodNotAllowed("POST"); }
export function OPTIONS() { return clientAuthMethodNotAllowed("POST"); }
