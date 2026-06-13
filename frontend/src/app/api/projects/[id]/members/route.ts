import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projectMembers, users } from "@/db/schema";
import { eq, and } from "drizzle-orm";
import { getSession } from "@/lib/auth";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;

  const rows = await db
    .select({
      id: projectMembers.id,
      projectId: projectMembers.projectId,
      userId: projectMembers.userId,
      role: projectMembers.role,
      joinedAt: projectMembers.joinedAt,
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
  const body = await request.json();
  const { member_id } = body;

  if (!member_id) {
    return NextResponse.json(
      { detail: "member_idは必須です" },
      { status: 400 }
    );
  }

  // メンバーの存在確認
  const [member] = await db
    .select()
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.id, member_id),
        eq(projectMembers.projectId, id)
      )
    )
    .limit(1);

  if (!member) {
    return NextResponse.json(
      { detail: "メンバーが見つかりません" },
      { status: 404 }
    );
  }

  await db
    .delete(projectMembers)
    .where(eq(projectMembers.id, member_id));

  return NextResponse.json({ success: true });
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
  const body = await request.json();
  const { member_id, role } = body;

  if (!member_id || !role) {
    return NextResponse.json(
      { detail: "member_idとroleは必須です" },
      { status: 400 }
    );
  }

  const validRoles = ["owner", "admin", "member", "viewer"];
  if (!validRoles.includes(role)) {
    return NextResponse.json(
      { detail: `ロールは ${validRoles.join(", ")} のいずれかです` },
      { status: 400 }
    );
  }

  const [updated] = await db
    .update(projectMembers)
    .set({ role })
    .where(
      and(
        eq(projectMembers.id, member_id),
        eq(projectMembers.projectId, id)
      )
    )
    .returning();

  if (!updated) {
    return NextResponse.json(
      { detail: "メンバーが見つかりません" },
      { status: 404 }
    );
  }

  return NextResponse.json({
    id: updated.id,
    project_id: updated.projectId,
    user_id: updated.userId,
    role: updated.role,
  });
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
  const body = await request.json();
  const { username, user_id, role } = body;

  if (!username && !user_id) {
    return NextResponse.json(
      { detail: "usernameまたはuser_idは必須です" },
      { status: 400 }
    );
  }

  // ユーザーを検索（user_id優先）
  const [targetUser] = user_id
    ? await db
        .select()
        .from(users)
        .where(eq(users.id, user_id))
        .limit(1)
    : await db
        .select()
        .from(users)
        .where(eq(users.username, username))
        .limit(1);

  if (!targetUser) {
    return NextResponse.json(
      { detail: "ユーザーが見つかりません" },
      { status: 404 }
    );
  }

  // 既にメンバーかチェック
  const [existing] = await db
    .select()
    .from(projectMembers)
    .where(
      and(
        eq(projectMembers.projectId, id),
        eq(projectMembers.userId, targetUser.id)
      )
    )
    .limit(1);

  if (existing) {
    return NextResponse.json(
      { detail: "既にメンバーです" },
      { status: 409 }
    );
  }

  const [member] = await db
    .insert(projectMembers)
    .values({
      projectId: id,
      userId: targetUser.id,
      role: role || "member",
      joinedAt: new Date(),
      invitedBy: user.id,
    })
    .returning();

  return NextResponse.json({
    id: member.id,
    project_id: member.projectId,
    user_id: member.userId,
    role: member.role,
    joined_at: member.joinedAt,
    username: targetUser.username,
    display_name: targetUser.displayName,
  });
}
