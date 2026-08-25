import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getProjectSettingsProject,
} from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectRouteContext = { params: Promise<{ id: string }> };

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
 * Project Knowledge is a FastAPI-owned mutation.  This route intentionally
 * does only browser-session authentication and the Next-side project ACL
 * preflight before handing the original request stream to the Python proxy.
 * It must not reimplement the KnowledgeNode relation service in Drizzle.
 */
export async function GET(
  request: NextRequest,
  { params }: ProjectRouteContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "knowledge"],
    user,
  });
}

export async function POST(
  request: NextRequest,
  { params }: ProjectRouteContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id } = await params;
  const access = await getProjectSettingsProject(id, user);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "knowledge"],
    user,
  });
}
