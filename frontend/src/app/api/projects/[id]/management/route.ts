import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projects } from "@/db/schema";
import { eq } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";
import {
  mergeManagementConfigIntoMetadata,
  normalizeProjectManagementConfig,
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
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }

  return NextResponse.json({
    config: normalizeProjectManagementConfig(result.project.projectMetadata),
  });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const result = await getWritableProject(id, user);
  if (!result) {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 404 },
    );
  }

  const body = await request.json();
  const metadata = mergeManagementConfigIntoMetadata(
    result.project.projectMetadata,
    {
      workspaceRoot: null,
      wbsFile: body.wbs_file ?? body.wbsFile,
      issueFile: body.issue_file ?? body.issueFile,
      riskFile: body.risk_file ?? body.riskFile,
      requestFiles: Array.isArray(body.request_files)
        ? body.request_files
        : Array.isArray(body.requestFiles)
          ? body.requestFiles
          : undefined,
      taskRules: {
        autoCreateFollowup:
          body.task_rules?.auto_create_followup ??
          body.taskRules?.autoCreateFollowup ??
          true,
        autoCreateDueTask:
          body.task_rules?.auto_create_due_task ??
          body.taskRules?.autoCreateDueTask ??
          false,
        requireConfirmationForWbsChange:
          body.task_rules?.require_confirmation_for_wbs_change ??
          body.taskRules?.requireConfirmationForWbsChange ??
          true,
      },
    },
  );

  const [updated] = await db
    .update(projects)
    .set({ projectMetadata: metadata, updatedAt: new Date() })
    .where(eq(projects.id, id))
    .returning();

  return NextResponse.json({
    config: normalizeProjectManagementConfig(updated.projectMetadata),
  });
}
