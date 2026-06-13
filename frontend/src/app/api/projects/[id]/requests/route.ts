import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import {
  normalizeProjectManagementConfig,
  readWbsRows,
  summarizeWorkspaceRequests,
} from "@/lib/server/project-workspace-management";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const result = await getAccessibleProject(id, user.id);
  if (!result) {
    return NextResponse.json({ detail: "プロジェクトが見つからないか権限がありません" }, { status: 404 });
  }

  const config = normalizeProjectManagementConfig(result.project.projectMetadata);
  const scan = readWbsRows(id, config);
  const requests = summarizeWorkspaceRequests(id, config, scan.rows);

  return NextResponse.json({
    requests,
    errors: scan.errors,
    summary: {
      total: requests.length,
      customer: requests.filter((item) => item.target === "customer").length,
      waiting: requests.filter((item) => item.status === "waiting").length,
      blocked: requests.filter((item) => item.status === "blocked").length,
    },
  });
}
