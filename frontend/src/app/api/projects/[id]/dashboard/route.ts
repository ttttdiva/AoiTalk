import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import { getDashboardData } from "@/lib/server/dashboard-data";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }
  const { id: projectId } = await params;
  const access = await getAccessibleProject(projectId, user.id);
  if (!access) {
    return NextResponse.json({ detail: "アクセス権がありません" }, { status: 403 });
  }
  return NextResponse.json(await getDashboardData({ type: "project", id: projectId }));
}
