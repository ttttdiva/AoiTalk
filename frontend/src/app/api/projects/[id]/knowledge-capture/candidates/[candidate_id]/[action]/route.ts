import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getProjectSettingsProject,
} from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectKnowledgeCaptureCandidateActionContext = {
  params: Promise<{ id: string; candidate_id: string; action: string }>;
};

const READ_ACTIONS = new Set(["dismiss"]);
const MANAGE_ACTIONS = new Set(["publish", "adopt-publication-guard"]);

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

function invalidAction() {
  return NextResponse.json({ detail: "無効な操作です" }, { status: 404 });
}

export async function POST(
  request: NextRequest,
  { params }: ProjectKnowledgeCaptureCandidateActionContext,
) {
  const user = await getSession();
  if (!user) return unauthorized();

  const {
    id,
    candidate_id: candidateId,
    action,
  } = await params;
  if (!READ_ACTIONS.has(action) && !MANAGE_ACTIONS.has(action)) {
    return invalidAction();
  }

  const access = READ_ACTIONS.has(action)
    ? await getAccessibleProject(id, user.id)
    : await getProjectSettingsProject(id, user);
  if (access === undefined) return projectNotFound();
  if (access === null) return forbidden();

  return proxyRequestToPythonApi(request, {
    path: [
      "knowledge-capture",
      "candidates",
      candidateId,
      action,
    ],
    user,
  });
}
