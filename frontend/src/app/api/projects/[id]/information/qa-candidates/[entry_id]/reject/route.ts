import { NextRequest } from "next/server";
import { mutateQaCandidate } from "../../_lib";

type Params = { params: Promise<{ id: string; entry_id: string }> };

export async function POST(request: NextRequest, { params }: Params) {
  const { id: projectId, entry_id: entryId } = await params;
  return mutateQaCandidate(request, projectId, entryId, "reject");
}
