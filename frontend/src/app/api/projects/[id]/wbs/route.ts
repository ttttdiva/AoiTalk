import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import {
  normalizeProjectManagementConfig,
  readWbsRows,
  selectUpcomingWbsRows,
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
  const upcoming = selectUpcomingWbsRows(scan.rows, 20);
  const requests = summarizeWorkspaceRequests(id, config, scan.rows);

  return NextResponse.json({
    config,
    file_path: scan.filePath,
    rows: scan.rows,
    upcoming,
    requests,
    errors: scan.errors,
    summary: {
      total: scan.rows.length,
      open: scan.rows.filter((row) => row.status !== "closed").length,
      review: scan.rows.filter((row) => row.status === "review").length,
      overdue: scan.rows.filter((row) => {
        if (!row.plannedEnd || row.status === "closed") return false;
        return new Date(`${row.plannedEnd}T23:59:59`).getTime() < Date.now();
      }).length,
      request_count: requests.length,
    },
  });
}
