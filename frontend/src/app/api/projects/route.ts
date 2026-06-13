import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projects, projectMembers } from "@/db/schema";
import { eq, inArray } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { ensureUserInboxSetup } from "@/lib/server/inbox-project";
import { canWriteSpace } from "@/lib/server/space-access";

function toSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

async function resolveUniqueProjectSlug(
  name: string,
  requestedSlug: unknown,
): Promise<string> {
  const normalizedRequestedSlug =
    typeof requestedSlug === "string" ? toSlug(requestedSlug.trim()) : "";
  const baseSlug = normalizedRequestedSlug || toSlug(name) || "project";
  let candidate = baseSlug;
  let suffix = 1;

  while (true) {
    const [existing] = await db
      .select({ id: projects.id })
      .from(projects)
      .where(eq(projects.slug, candidate))
      .limit(1);

    if (!existing) {
      return candidate;
    }

    candidate = `${baseSlug}-${suffix}`;
    suffix += 1;
  }
}

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
  return [
    ...new Set(
      value
        .filter((item): item is string => typeof item === "string")
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean),
    ),
  ];
}

function serializeProject(
  row: Record<string, unknown>,
): Record<string, unknown> {
  const result = toSnake(row);
  const metadata = parseProjectMetadata(result.metadata);
  const aliases = normalizeAliases(metadata.aliases);
  const color = typeof metadata.color === "string" ? metadata.color : null;
  result.metadata = metadata;
  result.aliases = aliases;
  result.color = color;
  return result;
}

export async function GET() {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  try {
    await ensureUserInboxSetup(user.id);
  } catch (err) {
    console.warn("Inbox 初期化失敗:", err);
  }

  const memberships = await db
    .select({ projectId: projectMembers.projectId })
    .from(projectMembers)
    .where(eq(projectMembers.userId, user.id));

  if (memberships.length === 0) {
    return NextResponse.json({ projects: [], total: 0 });
  }

  const projectIds = memberships.map((m) => m.projectId);
  const rows = await db
    .select()
    .from(projects)
    .where(inArray(projects.id, projectIds));

  const result = rows.map((r) =>
    serializeProject(r as unknown as Record<string, unknown>),
  );
  return NextResponse.json({ projects: result, total: result.length });
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json();
  const {
    name,
    description,
    slug,
    estimated_hours,
    space_id,
    aliases,
    color,
    metadata,
  } = body;

  if (!name) {
    return NextResponse.json({ detail: "nameは必須です" }, { status: 400 });
  }

  const finalSlug = await resolveUniqueProjectSlug(name, slug);

  if (space_id) {
    const access = await canWriteSpace(space_id, user);
    if (!access.space) {
      return NextResponse.json(
        { detail: "スペースが見つかりません" },
        { status: 404 },
      );
    }
    if (!access.allowed) {
      return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
    }
  }

  const mergedMetadata = {
    ...parseProjectMetadata(metadata),
    aliases: normalizeAliases(aliases),
    color: typeof color === "string" && color.trim() ? color.trim() : null,
  };

  const [project] = await db
    .insert(projects)
    .values({
      name,
      description: description || null,
      slug: finalSlug,
      ownerId: user.id,
      estimatedHours: estimated_hours != null ? Number(estimated_hours) : null,
      spaceId: space_id || null,
      projectMetadata: mergedMetadata,
    })
    .returning();

  await db.insert(projectMembers).values({
    projectId: project.id,
    userId: user.id,
    role: "admin",
  });

  return NextResponse.json({
    success: true,
    project: serializeProject(project as unknown as Record<string, unknown>),
  });
}
