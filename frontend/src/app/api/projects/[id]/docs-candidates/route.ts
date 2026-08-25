import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectDocsCandidatesRouteContext = {
  params: Promise<{ id: string }>;
};

function unauthorized() {
  return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
}

function projectNotFound() {
  return NextResponse.json(
    { detail: "プロジェクトが見つかりません" },
    { status: 404 },
  );
}

function forbidden() {
  return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
}

/**
 * The review queue is owned by FastAPI.  Next only authenticates the browser
 * session and performs the corresponding Project ACL preflight before
 * forwarding the request (including its query string/body) to Python.
 */
export async function GET(
  request: NextRequest,
  { params }: ProjectDocsCandidatesRouteContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "docs-candidates"],
    user,
  });
}
