import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projectMembers, projects, users } from "@/db/schema";
import { and, eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import {
  mergeManagementConfigIntoMetadata,
  normalizeProjectManagementConfig,
} from "@/lib/server/project-workspace-management";
import { hasProjectPermission } from "@/lib/server/project-permissions";

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
  const body = await request.json();

  // Metadata also carries the bounded upload idempotency ledger.  Lock and
  // re-read the project inside the same transaction before merging so a
  // concurrent upload commit cannot be overwritten by a stale PATCH snapshot.
  const result = await db.transaction(async (tx) => {
    const [project] = await tx
      .select()
      .from(projects)
      .where(and(eq(projects.id, id), isNull(projects.deletedAt)))
      .limit(1)
      .for("update");
    if (!project) return { status: "not-found" as const };

    const [principal] = await tx
      .select({ role: users.role })
      .from(users)
      .where(eq(users.id, user.id))
      .limit(1);
    const [membership] = await tx
      .select({ permissions: projectMembers.permissions })
      .from(projectMembers)
      .where(
        and(
          eq(projectMembers.projectId, id),
          eq(projectMembers.userId, user.id),
        ),
      )
      .limit(1);
    const canWrite =
      principal?.role === "admin" ||
      project.ownerId === user.id ||
      hasProjectPermission(membership?.permissions, "write");
    if (!canWrite) return { status: "forbidden" as const };

    // `project` is the row locked above, so this metadata is the latest
    // committed value (including any upload idempotency records).
    const latestConfig = normalizeProjectManagementConfig(
      project.projectMetadata,
    );
    const snakeRules =
      body && typeof body.task_rules === "object" && body.task_rules
        ? body.task_rules
        : {};
    const camelRules =
      body && typeof body.taskRules === "object" && body.taskRules
        ? body.taskRules
        : {};
    const metadata = mergeManagementConfigIntoMetadata(
      project.projectMetadata,
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
            snakeRules.auto_create_followup ??
            camelRules.autoCreateFollowup ??
            latestConfig.taskRules.autoCreateFollowup,
          autoCreateDueTask:
            snakeRules.auto_create_due_task ??
            camelRules.autoCreateDueTask ??
            latestConfig.taskRules.autoCreateDueTask,
          requireConfirmationForWbsChange:
            snakeRules.require_confirmation_for_wbs_change ??
            camelRules.requireConfirmationForWbsChange ??
            latestConfig.taskRules.requireConfirmationForWbsChange,
        },
      },
    );

    const [updated] = await tx
      .update(projects)
      .set({ projectMetadata: metadata, updatedAt: new Date() })
      .where(eq(projects.id, id))
      .returning({ projectMetadata: projects.projectMetadata });
    return {
      status: "ok" as const,
      config: normalizeProjectManagementConfig(updated.projectMetadata),
    };
  });

  if (result.status === "not-found") {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (result.status === "forbidden") {
    return NextResponse.json(
      { detail: "プロジェクトが見つからないか権限がありません" },
      { status: 403 },
    );
  }

  return NextResponse.json({
    config: result.config,
  });
}
