import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getDashboardData } from "@/lib/server/dashboard-data";
import { getReadableProjectIdsForSpace } from "@/lib/server/space-access";

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
  const projectIds = await getReadableProjectIdsForSpace(spaceId, user);
  if (projectIds === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const data = await getDashboardData({
    type: "space",
    id: spaceId,
    projectIds,
  });
  return NextResponse.json(data);
}
