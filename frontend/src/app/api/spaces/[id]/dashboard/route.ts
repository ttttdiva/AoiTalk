import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getDashboardData } from "@/lib/server/dashboard-data";
import { getReadableSpace } from "@/lib/server/space-access";
import { getParticipatingProjectIds } from "@/lib/server/project-access";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "Authentication required" },
      { status: 401 },
    );
  }

  const { id: spaceId } = await params;
  const space = await getReadableSpace(spaceId, user);
  if (!space) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  // Dashboard is an operational aggregate. Global-admin visibility of every
  // project is retained for direct/admin project APIs, but an admin who is
  // not an owner/member must not see this space's task/time rollups.
  const projectIds = await getParticipatingProjectIds(user.id, { spaceId });

  const data = await getDashboardData({
    type: "space",
    id: spaceId,
    projectIds,
  });
  return NextResponse.json(data);
}
