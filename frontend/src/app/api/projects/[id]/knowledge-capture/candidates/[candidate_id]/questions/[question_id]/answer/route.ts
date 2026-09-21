import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";

type ProjectKnowledgeCaptureAnswerContext = {
  params: Promise<{ id: string; candidate_id: string; question_id: string }>;
};

export async function POST(
  request: NextRequest,
  { params }: ProjectKnowledgeCaptureAnswerContext,
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const {
    id,
    candidate_id: candidateId,
    question_id: questionId,
  } = await params;
  const access = await getAccessibleProject(id, user.id);
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
    path: [
      "knowledge-capture",
      "candidates",
      candidateId,
      "questions",
      questionId,
      "answer",
    ],
    user,
  });
}
