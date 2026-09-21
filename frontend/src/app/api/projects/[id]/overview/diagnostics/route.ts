import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectOverviewDiagnosticsRouteContext = {
  params: Promise<{ id: string }>;
};

/**
 * Proxy the operator-only Overview diagnostics endpoint.  Authentication and
 * Project read ACL are checked at the Next boundary; FastAPI performs the
 * stricter manage_settings/owner/admin check and strips all secrets.
 */
export async function GET(
  request: NextRequest,
  { params }: ProjectOverviewDiagnosticsRouteContext,
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json(
      { detail: "認証が必要です" },
      { status: 401 },
    );
  }

  const { id } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json(
      { detail: "権限がありません" },
      { status: 403 },
    );
  }

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "overview", "diagnostics"],
    user,
  });
}
