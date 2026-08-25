import { NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";

import { db } from "@/db";
import { projectMembers, users } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getWritableProject(id, user);
  if (access === undefined) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }
  if (access === null) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const rows = await db
    .select({
      userId: projectMembers.userId,
      username: users.username,
      displayName: users.displayName,
    })
    .from(projectMembers)
    .innerJoin(users, eq(projectMembers.userId, users.id))
    .where(and(eq(projectMembers.projectId, id), eq(users.isActive, true)));
  const members = rows.map((row) => ({
    user_id: row.userId,
    username: row.username,
    display_name: row.displayName,
  }));

  return NextResponse.json({ members, total: members.length });
}
