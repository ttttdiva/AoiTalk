import { NextRequest } from "next/server";
import { mutateQaCandidate } from "../../_lib";

type Params = { params: Promise<{ id: string; entry_id: string }> };

// Explicit archive verb; the underlying operation is always a soft-delete.
export async function POST(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId, "delete");
}
