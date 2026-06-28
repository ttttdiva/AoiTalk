import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projects, projectMembers } from "@/db/schema";
import { eq, and, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { getAccessibleProject } from "@/lib/server/project-access";
import { proxyRequestToPythonApi } from "@/lib/server/python-api-proxy";
import { canWriteSpace } from "@/lib/server/space-access";

function toSnake(row: Record<string, unknown>): Record<string, unknown> {
  const map: Record<string, string> = {
    id: "id",
    name: "name",
    description: "description",
    slug: "slug",
    ownerId: "owner_id",
    allowJoinRequests: "allow_join_requests",
    storageQuotaMb: "storage_quota_mb",
    storageUsedMb: "storage_used_mb",
    estimatedHours: "estimated_hours",
    isCompleted: "is_completed",
    spaceId: "space_id",
    createdAt: "created_at",
    updatedAt: "updated_at",
    deletedAt: "deleted_at",
    projectMetadata: "metadata",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    out[map[k] ?? k] = v;
  }
  return out;
}

function parseProjectMetadata(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return { ...(value as Record<string, unknown>) };
}

function normalizeAliases(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean))];
}

function serializeProject(row: Record<string, unknown>): Record<string, unknown> {
  const result = toSnake(row);
  const metadata = parseProjectMetadata(result.metadata);
  result.metadata = metadata;
  result.aliases = normalizeAliases(metadata.aliases);
  result.color = typeof metadata.color === "string" ? metadata.color : null;
  return result;
}

function isInboxProject(row: { ownerId: string; slug: string; projectMetadata: unknown }) {
  const metadata = parseProjectMetadata(row.projectMetadata);
  return (
    row.slug === `inbox-project-${row.ownerId}` ||
    metadata.isInboxDefault === true
  );
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

  const result = await getAccessibleProject(id, user.id);
  if (!result) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 }
    );
  }

  return NextResponse.json(
    serializeProject(result.project as unknown as Record<string, unknown>)
  );
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
  const {
    name,
    description,
    estimated_hours,
    space_id,
    is_completed,
    aliases,
    color,
    metadata,
  } = body;

  // プロジェクトのadminメンバーか確認
  const [membership] = await db
    .select()
    .from(projectMembers)
    .where(
      and(eq(projectMembers.projectId, id), eq(projectMembers.userId, user.id))
    )
    .limit(1);

  if (!membership || (membership.role !== "admin" && user.role !== "admin")) {
    return NextResponse.json(
      { detail: "権限がありません" },
      { status: 403 }
    );
  }

  const updates: Record<string, unknown> = { updatedAt: new Date() };
  if (name !== undefined) updates.name = name;
  if (description !== undefined) updates.description = description;
  if (estimated_hours !== undefined) updates.estimatedHours = estimated_hours != null ? Number(estimated_hours) : null;
  if (space_id !== undefined) {
    if (space_id) {
      const access = await canWriteSpace(space_id, user);
      if (!access.space) {
        return NextResponse.json(
          { detail: "スペースが見つかりません" },
          { status: 404 },
        );
      }
      if (!access.allowed) {
        return NextResponse.json(
          { detail: "権限がありません" },
          { status: 403 },
        );
      }
    }
    updates.spaceId = space_id || null;
  }
  if (is_completed !== undefined) updates.isCompleted = Boolean(is_completed);

  if (
    aliases !== undefined ||
    color !== undefined ||
    metadata !== undefined
  ) {
    const [current] = await db
      .select({ projectMetadata: projects.projectMetadata })
      .from(projects)
      .where(and(eq(projects.id, id), isNull(projects.deletedAt)))
      .limit(1);
    const mergedMetadata = {
      ...parseProjectMetadata(current?.projectMetadata),
      ...parseProjectMetadata(metadata),
    };
    if (aliases !== undefined) {
      mergedMetadata.aliases = normalizeAliases(aliases);
    }
    if (color !== undefined) {
      mergedMetadata.color =
        typeof color === "string" && color.trim() ? color.trim() : null;
    }
    updates.projectMetadata = mergedMetadata;
  }

  const [updated] = await db
    .update(projects)
    .set(updates)
    .where(and(eq(projects.id, id), isNull(projects.deletedAt)))
    .returning();

  if (!updated) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 },
    );
  }

  return NextResponse.json(
    serializeProject(updated as unknown as Record<string, unknown>)
  );
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

  // システム管理者またはプロジェクトオーナーのみ削除可
  const [project] = await db
    .select()
    .from(projects)
    .where(and(eq(projects.id, id), isNull(projects.deletedAt)))
    .limit(1);

  if (!project) {
    return NextResponse.json(
      { detail: "プロジェクトが見つかりません" },
      { status: 404 }
    );
  }

  if (isInboxProject(project)) {
    return NextResponse.json(
      { detail: "Inboxプロジェクトは削除できません" },
      { status: 400 }
    );
  }

  if (project.ownerId !== user.id && user.role !== "admin") {
    return NextResponse.json(
      { detail: "権限がありません" },
      { status: 403 }
    );
  }

  return proxyRequestToPythonApi(request, {
    path: ["projects", id],
    user,
  });
}
