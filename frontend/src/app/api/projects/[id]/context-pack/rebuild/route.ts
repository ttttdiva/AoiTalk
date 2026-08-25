import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getProjectSettingsProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectContextPackRebuildRouteContext = {
  params: Promise<{ id: string }>;
};

export async function POST(
  request: NextRequest,
  { params }: ProjectContextPackRebuildRouteContext,
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getProjectSettingsProject(id, user);
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "context-pack", "rebuild"],
    user,
  });
}
