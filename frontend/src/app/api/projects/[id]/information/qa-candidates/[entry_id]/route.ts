import { NextRequest } from "next/server";
import { mutateQaCandidate } from "../_lib";

type Params = { params: Promise<{ id: string; entry_id: string }> };

/**
 * Generic mutation endpoint for integrations that prefer an action payload:
 * ``{ action: "accept" | "reject" | "delete", expected_version: n }``.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId);
}

// PATCH is accepted as an integration-friendly alias for action payloads;
// the lifecycle semantics remain identical to POST and still require the
// optimistic expected_version token.
export async function PATCH(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId);
}

/** DELETE remains a soft-delete and still requires the optimistic version. */
export async function DELETE(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId, "delete");
}
