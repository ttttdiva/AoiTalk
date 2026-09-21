import { NextRequest } from "next/server";
import { mutateQaCandidate } from "../../_lib";

type Params = { params: Promise<{ id: string; entry_id: string }> };

// Compatibility alias for clients that use Docs-candidate terminology.
export async function POST(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId, "accept");
}
