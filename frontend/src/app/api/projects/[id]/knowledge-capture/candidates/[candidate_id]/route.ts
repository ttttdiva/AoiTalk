import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getProjectSettingsProject,
  getWritableProject,
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

  const response = await proxyRequestToPythonApi(request, {
    path: ["knowledge-capture", "candidates", candidateId],
    user,
  });
  if (!response.ok) return response;
  const body: unknown = await response.clone().json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) return response;
  const [writable, manageable] = await Promise.all([
    getWritableProject(id, user),
    getProjectSettingsProject(id, user),
  ]);
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  return NextResponse.json({
    ...body,
    can_write: Boolean(writable),
    can_publish: Boolean(manageable),
  }, { status: response.status, headers });
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
