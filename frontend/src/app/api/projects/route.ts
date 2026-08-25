import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { projects, projectMembers } from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { eq, isNull } from "drizzle-orm";
import { getSession } from "@/lib/auth";
import { ensureUserInboxSetup } from "@/lib/server/inbox-project";
import { canWriteSpace } from "@/lib/server/space-access";
import {
  getDefaultProjectPermissions,
  hasEffectiveProjectPermission,
  hasProjectPermission,
} from "@/lib/server/project-permissions";
import { isForeignDefaultInboxProject } from "@/lib/server/project-list-visibility";
import {
  ensureProjectInformationHierarchyNode,
  getProjectInformationHierarchyNode,
  getPersonalDocsLibrary,
} from "@/lib/server/project-information-hierarchy";

function serializeLibrary(library: typeof docsLibraries.$inferSelect | null | undefined) {
  if (!library) return null;
  return {
    id: library.id,
    library_id: library.id,
    docs_library_id: library.id,
    name: library.name,
    description: library.description,
    owner_user_id: library.ownerUserId,
    library_type: library.libraryType ?? "personal",
    settings: library.settingsJson ?? {},
    created_at: library.createdAt instanceof Date ? library.createdAt.toISOString() : library.createdAt,
    updated_at: library.updatedAt instanceof Date ? library.updatedAt.toISOString() : library.updatedAt,
  };
}

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
    knowledgeNodeId: "knowledge_node_id",
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
  canWrite?: boolean,
  isParticipating?: boolean,
  membership?: { role: string | null; permissions: unknown } | null,
  canManageSettings?: boolean,
): Record<string, unknown> {
  const result = toSnake(row);
  const metadata = parseProjectMetadata(result.metadata);
  const aliases = normalizeAliases(metadata.aliases);
  const color = typeof metadata.color === "string" ? metadata.color : null;
  result.metadata = metadata;
  result.aliases = aliases;
  result.color = color;
  if (canWrite !== undefined) result.can_write = canWrite;
  if (isParticipating !== undefined) result.is_participating = isParticipating;
  if (membership !== undefined) {
    result.membership = membership
      ? { role: membership.role, permissions: membership.permissions ?? null }
      : null;
  }
  if (canManageSettings !== undefined) {
    result.can_manage_settings = canManageSettings;
  }
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
    .select({
      projectId: projectMembers.projectId,
      role: projectMembers.role,
      permissions: projectMembers.permissions,
    })
    .from(projectMembers)
    .where(eq(projectMembers.userId, user.id));

  const membershipByProjectId = new Map(
    memberships.map((membership) => [membership.projectId, membership]),
  );
  const rows = await db
    .select()
    .from(projects)
    .where(isNull(projects.deletedAt));

  const visibleRows = rows
    .filter((row) => !isForeignDefaultInboxProject(row, user.id))
    .filter(
      (row) =>
        user.role === "admin" ||
        row.ownerId === user.id ||
        hasProjectPermission(
          membershipByProjectId.get(row.id)?.permissions,
          "read",
        ),
    );
  const result = (await Promise.all(visibleRows.map(async (row) => {
      // `projects.knowledge_node_id` is a denormalized reverse pointer.  Do
      // not expose an ordinary/stale node as a link: only the canonical
      // project-information child under the owner's Personal hub qualifies.
      let canonicalNodeId: string | null = null;
      try {
        const hierarchy = await getProjectInformationHierarchyNode({ project: row });
        canonicalNodeId = hierarchy.node?.id ?? null;
      } catch (error) {
        // A malformed/stale hierarchy must not make the entire Projects list
        // fail.  Omit the reverse pointer and let the Project-information
        // repair boundary handle it with the appropriate write ACL.
        console.warn("Project Docs canonical pointer validation failed:", error);
      }
      const membership = membershipByProjectId.get(row.id) ?? null;
      const isParticipating =
        row.ownerId === user.id ||
        hasProjectPermission(membership?.permissions, "read");
      const canManageSettings = hasEffectiveProjectPermission({
        userId: user.id,
        userRole: user.role,
        projectOwnerId: row.ownerId,
        memberPermissions: membership?.permissions,
        permission: "manage_settings",
      });
      return serializeProject(
        {
          ...(row as unknown as Record<string, unknown>),
          knowledgeNodeId: canonicalNodeId,
        },
        user.role === "admin" ||
          row.ownerId === user.id ||
          hasProjectPermission(membership?.permissions, "write"),
        isParticipating,
        membership,
        canManageSettings,
      );
    }))).filter(Boolean);
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

  const normalizedRequestedSlug =
    typeof slug === "string" ? toSlug(slug.trim()) : "";
  if (normalizedRequestedSlug.startsWith("inbox-project-")) {
    return NextResponse.json(
      { detail: "Inbox予約slugは通常のProject作成には使用できません" },
      { status: 400 },
    );
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
      storageQuotaMb: 1000,
      storageUsedMb: 0,
      projectMetadata: mergedMetadata,
    })
    .returning();

  await db.insert(projectMembers).values({
    projectId: project.id,
    userId: user.id,
    role: "owner",
    permissions: getDefaultProjectPermissions("owner"),
  });

  // Project information is a real child of the owner's Personal Docs
  // Library's 案件情報 hub.  Bootstrap it during project creation so
  // `projects.knowledge_node_id` is authoritative from the first response;
  // the hierarchy helper is idempotent under concurrent retries.
  const informationNode = await ensureProjectInformationHierarchyNode({
    userId: user.id,
    project,
  });
  const library = await getPersonalDocsLibrary(user.id);

  return NextResponse.json({
    success: true,
    project: {
      ...serializeProject(
        { ...(project as unknown as Record<string, unknown>), knowledgeNodeId: informationNode.id },
        true,
        true,
        null,
        true,
      ),
      knowledge_node_id: informationNode.id,
      docs_library_id: library?.id ?? informationNode.docsLibraryId,
      library: serializeLibrary(library),
    },
  });
}
