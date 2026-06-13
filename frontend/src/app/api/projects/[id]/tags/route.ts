import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { tags, projects } from "@/db/schema";
import { eq, and } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import {
  getAccessibleProject,
  getWritableProject,
} from "@/lib/server/project-access";

async function resolveSpaceId(projectId: string): Promise<string | null> {
  const [p] = await db
    .select({ spaceId: projects.spaceId })
    .from(projects)
    .where(eq(projects.id, projectId))
    .limit(1);
  return p?.spaceId ?? null;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const spaceId = await resolveSpaceId(id);
  if (!spaceId) {
    return NextResponse.json([]);
  }

  const rows = await db.select().from(tags).where(eq(tags.spaceId, spaceId));

  const result = rows.map((t) => ({
    id: t.id,
    space_id: t.spaceId,
    name: t.name,
    color: t.color,
    created_by: t.createdBy,
    created_at: t.createdAt,
  }));

  return NextResponse.json(result);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  const body = await request.json();

  if (!body.name) {
    return NextResponse.json({ detail: "nameは必須です" }, { status: 400 });
  }

  const spaceId = await resolveSpaceId(id);
  if (!spaceId) {
    return NextResponse.json(
      { detail: "プロジェクトがスペースに所属していません" },
      { status: 400 },
    );
  }

  const [existing] = await db
    .select()
    .from(tags)
    .where(and(eq(tags.spaceId, spaceId), eq(tags.name, body.name)))
    .limit(1);
  if (existing) {
    return NextResponse.json({
      id: existing.id,
      space_id: existing.spaceId,
      name: existing.name,
      color: existing.color,
      created_by: existing.createdBy,
      created_at: existing.createdAt,
    });
  }

  const [tag] = await db
    .insert(tags)
    .values({
      spaceId,
      name: body.name,
      color: body.color || null,
      createdBy: user.id,
    })
    .returning();

  return NextResponse.json({
    id: tag.id,
    space_id: tag.spaceId,
    name: tag.name,
    color: tag.color,
    created_by: tag.createdBy,
    created_at: tag.createdAt,
  });
}
