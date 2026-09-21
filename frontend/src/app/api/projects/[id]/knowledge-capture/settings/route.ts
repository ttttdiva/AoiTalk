import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getProjectSettingsProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectKnowledgeCaptureSettingsContext = {
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

async function proxySettings(
  request: NextRequest,
  { params }: ProjectKnowledgeCaptureSettingsContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id } = await params;
  const access = await getProjectSettingsProject(id, user);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["projects", id, "knowledge-capture", "settings"],
    user,
  });
}

export async function GET(
  request: NextRequest,
  context: ProjectKnowledgeCaptureSettingsContext,
) {
  return proxySettings(request, context);
}

export async function PATCH(
  request: NextRequest,
  context: ProjectKnowledgeCaptureSettingsContext,
) {
  return proxySettings(request, context);
}
