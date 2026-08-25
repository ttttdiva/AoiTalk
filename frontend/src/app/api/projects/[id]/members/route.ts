import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projectMembers, projects, users } from "@/db/schema";
import { eq, and } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getManageableProject } from "@/lib/server/project-access";
import {
  PROJECT_PERMISSIONS,
  getDefaultProjectPermissions,
  hasEffectiveProjectPermission,
} from "@/lib/server/project-permissions";

class ProjectPermissionError extends Error {
  constructor() {
    super("権限がありません");
    this.name = "ProjectPermissionError";
  }
}

async function assertLockedProjectManager(
  tx: Parameters<Parameters<typeof db.transaction>[0]>[0],
  projectId: string,
  userId: string,
) {
  const [project] = await tx
    .select()
    .from(projects)
    .where(eq(projects.id, projectId))
    .limit(1)
    .for("update");
  if (!project || project.deletedAt) {
    throw new Error("Project not found");
  }

  const [principal] = await tx
    .select({ role: users.role })
    .from(users)
    .where(eq(users.id, userId))
    .limit(1);
  const [membership] = await tx
    .select({ permissions: projectMembers.permissions })
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.projectId, projectId),
        eq(projectMembers.userId, userId),
      ),
    )
    .limit(1);
  if (!hasEffectiveProjectPermission({
    userId,
    userRole: principal?.role,
    projectOwnerId: project.ownerId,
    memberPermissions: membership?.permissions,
    permission: "manage_members",
  })) {
    throw new ProjectPermissionError();
  }
  return project;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getManageableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const rows = await db
    .select({
      id: projectMembers.id,
      projectId: projectMembers.projectId,
      userId: projectMembers.userId,
      role: projectMembers.role,
      joinedAt: projectMembers.joinedAt,
      permissions: projectMembers.permissions,
      username: users.username,
      displayName: users.displayName,
    })
    .from(projectMembers)
    .innerJoin(users, eq(projectMembers.userId, users.id))
    .where(eq(projectMembers.projectId, id));

  const result = rows.map((r) => ({
    id: r.id,
    project_id: r.projectId,
    user_id: r.userId,
    role: r.role,
    permissions: r.permissions,
    joined_at: r.joinedAt,
    username: r.username,
    display_name: r.displayName,
  }));

  return NextResponse.json(result);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getManageableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  const body = await request.json();
  const { member_id } = body;

  if (!member_id) {
    return NextResponse.json(
      { detail: "member_idは必須です" },
      { status: 400 }
    );
  }

  try {
    const removed = await db.transaction(async (tx) => {
      const project = await assertLockedProjectManager(tx, id, user.id);
      const [member] = await tx
        .select()
        .from(projectMembers)
        .where(
          and(
            eq(projectMembers.id, member_id),
            eq(projectMembers.projectId, id),
          ),
        )
        .limit(1);

      if (!member) return { found: false, removed: false };
      if (member.userId === project.ownerId) {
        throw new Error("Project owner cannot be removed");
      }

      const deleted = await tx
        .delete(projectMembers)
        .where(
          and(
            eq(projectMembers.id, member_id),
            eq(projectMembers.projectId, id),
          ),
        )
        .returning({ id: projectMembers.id });
      return { found: true, removed: deleted.length > 0 };
    });

    if (!removed.found || !removed.removed) {
      return NextResponse.json(
        { detail: "メンバーが見つかりません" },
        { status: 404 },
      );
    }
    return NextResponse.json({ success: true });
  } catch (error) {
    if (error instanceof ProjectPermissionError) {
      return NextResponse.json({ detail: error.message }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project owner cannot be removed") {
      return NextResponse.json({ detail: "プロジェクトオーナーは削除できません" }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project not found") {
      return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
    }
    throw error;
  }
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getManageableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  const body = await request.json();
  const { member_id, role, permissions } = body;

  if (!member_id || !role) {
    return NextResponse.json(
      { detail: "member_idとroleは必須です" },
      { status: 400 }
    );
  }

  const validRoles = ["admin", "member", "viewer"];
  if (!validRoles.includes(role)) {
    return NextResponse.json(
      { detail: `ロールは ${validRoles.join(", ")} のいずれかです` },
      { status: 400 }
    );
  }
  let nextPermissions = getDefaultProjectPermissions(role);
  if (permissions !== undefined) {
    const isPlainObject =
      permissions !== null &&
      typeof permissions === "object" &&
      !Array.isArray(permissions);
    if (!isPlainObject) {
      return NextResponse.json(
        { detail: "permissionsはオブジェクトで指定してください" },
        { status: 400 },
      );
    }
    const entries = Object.entries(permissions as Record<string, unknown>);
    const unknownPermission = entries.find(
      ([key]) => !(PROJECT_PERMISSIONS as readonly string[]).includes(key),
    );
    const invalidValue = entries.find(([, value]) => typeof value !== "boolean");
    if (unknownPermission || invalidValue) {
      return NextResponse.json(
        { detail: "permissionsのキーまたは値が不正です" },
        { status: 400 },
      );
    }
    // 空objectはdeny-allとして有効な明示ACLにする。
    nextPermissions = permissions as Record<string, boolean>;
  }

  try {
    const updated = await db.transaction(async (tx) => {
      const project = await assertLockedProjectManager(tx, id, user.id);
      const [principal] = await tx
        .select({ role: users.role })
        .from(users)
        .where(eq(users.id, user.id))
        .limit(1);
      const [targetMember] = await tx
        .select({ userId: projectMembers.userId })
        .from(projectMembers)
        .where(
          and(
            eq(projectMembers.id, member_id),
            eq(projectMembers.projectId, id),
          ),
        )
        .limit(1);
      if (!targetMember) return null;
      if (targetMember.userId === project.ownerId) {
        throw new Error("Project owner cannot be changed");
      }
      const isOwnerOrAdmin =
        principal?.role === "admin" || project.ownerId === user.id;
      if (role === "admin" && !isOwnerOrAdmin) {
        throw new Error("Admin role requires project owner or global admin");
      }
      if (permissions !== undefined && !isOwnerOrAdmin) {
        throw new Error("Permission changes require project owner or global admin");
      }

      const [row] = await tx
        .update(projectMembers)
        .set({ role, permissions: nextPermissions })
        .where(
          and(
            eq(projectMembers.id, member_id),
            eq(projectMembers.projectId, id),
          ),
        )
        .returning();
      return row ?? null;
    });

    if (!updated) {
      return NextResponse.json(
        { detail: "メンバーが見つかりません" },
        { status: 404 },
      );
    }

    return NextResponse.json({
      id: updated.id,
      project_id: updated.projectId,
      user_id: updated.userId,
      role: updated.role,
      permissions: updated.permissions,
    });
  } catch (error) {
    if (error instanceof ProjectPermissionError) {
      return NextResponse.json({ detail: error.message }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project owner cannot be changed") {
      return NextResponse.json({ detail: "プロジェクトオーナーは変更できません" }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Admin role requires project owner or global admin") {
      return NextResponse.json({ detail: "adminロールの付与はオーナーまたは全体管理者だけが行えます" }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Permission changes require project owner or global admin") {
      return NextResponse.json({ detail: "権限の変更はオーナーまたは全体管理者だけが行えます" }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Project not found") {
      return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
    }
    throw error;
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getManageableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }
  const body = await request.json();
  const { username, user_id, role } = body;

  if (!username && !user_id) {
    return NextResponse.json(
      { detail: "usernameまたはuser_idは必須です" },
      { status: 400 }
    );
  }

  const requestedRole = typeof role === "string" ? role : "member";
  if (!["admin", "member", "viewer"].includes(requestedRole)) {
    return NextResponse.json(
      { detail: "ownerロールはプロジェクト作成時以外に付与できません" },
      { status: 403 },
    );
  }
  try {
    const result = await db.transaction(async (tx) => {
      const project = await assertLockedProjectManager(tx, id, user.id);
      const [principal] = await tx
        .select({ role: users.role })
        .from(users)
        .where(eq(users.id, user.id))
        .limit(1);
      if (
        requestedRole === "admin" &&
        principal?.role !== "admin" &&
        project.ownerId !== user.id
      ) {
        throw new Error("Admin role requires project owner or global admin");
      }

      // ユーザーを検索（user_id優先）
      const [targetUser] = user_id
        ? await tx
            .select()
            .from(users)
            .where(eq(users.id, user_id))
            .limit(1)
        : await tx
            .select()
            .from(users)
            .where(eq(users.username, username))
            .limit(1);
      if (!targetUser) throw new Error("User not found");

      const [existing] = await tx
        .select()
        .from(projectMembers)
        .where(
          and(
            eq(projectMembers.projectId, id),
            eq(projectMembers.userId, targetUser.id),
          ),
        )
        .limit(1);
      if (existing) throw new Error("Already a member");

      const [member] = await tx
        .insert(projectMembers)
        .values({
          projectId: id,
          userId: targetUser.id,
          role: requestedRole,
          permissions: getDefaultProjectPermissions(requestedRole),
          joinedAt: new Date(),
          invitedBy: user.id,
        })
        .returning();
      return { member, targetUser };
    });

    return NextResponse.json({
      id: result.member.id,
      project_id: result.member.projectId,
      user_id: result.member.userId,
      role: result.member.role,
      permissions: result.member.permissions,
      joined_at: result.member.joinedAt,
      username: result.targetUser.username,
      display_name: result.targetUser.displayName,
    });
  } catch (error) {
    if (error instanceof ProjectPermissionError) {
      return NextResponse.json({ detail: error.message }, { status: 403 });
    }
    if (error instanceof Error && error.message === "Admin role requires project owner or global admin") {
      return NextResponse.json({ detail: "adminロールの付与はオーナーまたは全体管理者だけが行えます" }, { status: 403 });
    }
    if (error instanceof Error && error.message === "User not found") {
      return NextResponse.json({ detail: "ユーザーが見つかりません" }, { status: 404 });
    }
    if (error instanceof Error && error.message === "Already a member") {
      return NextResponse.json({ detail: "既にメンバーです" }, { status: 409 });
    }
    if (error instanceof Error && error.message === "Project not found") {
      return NextResponse.json({ detail: "プロジェクトが見つかりません" }, { status: 404 });
    }
    throw error;
  }
}
