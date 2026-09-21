import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getProjectSettingsProject,
} from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectKnowledgeCaptureCandidateContext = {
  params: Promise<{ id: string; candidate_id: string }>;
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

export async function GET(
  request: NextRequest,
  { params }: ProjectKnowledgeCaptureCandidateContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id, candidate_id: candidateId } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["knowledge-capture", "candidates", candidateId],
    user,
  });
}

export async function PATCH(
  request: NextRequest,
  { params }: ProjectKnowledgeCaptureCandidateContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const { id, candidate_id: candidateId } = await params;
  const access = await getProjectSettingsProject(id, user);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: ["knowledge-capture", "candidates", candidateId, "draft"],
    user,
  });
}
