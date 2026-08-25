import { NextRequest, NextResponse } from "next/server";
import { and, eq, max, isNull } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodePlacements, knowledgeNodes, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  getKnowledgeDisplayDescendantIds,
  getKnowledgeNodeDescendantIds,
  requireDocsNode,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";
import { updateDocsNode, updateDocsNodesByIds } from "@/lib/server/docs-node-writer";
import { getWritableProject } from "@/lib/server/project-access";
import { isDefaultInboxProject } from "@/lib/server/project-information-hierarchy";
import {
  assertGenericDocsMutationAllowed,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

async function rejectManagedMutation(
  node: Parameters<typeof assertGenericDocsMutationAllowed>[0],
) {
  try {
    await assertGenericDocsMutationAllowed(node);
    return null;
  } catch (error) {
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }
}

type ActiveProjectPointerLookup = {
  project: { id: string; isCompleted: boolean } | null;
  failed: boolean;
};

async function getActiveProjectPointer(nodeId: string): Promise<ActiveProjectPointerLookup> {
  try {
    const [project] = await db
      .select({ id: projects.id, isCompleted: projects.isCompleted })
      .from(projects)
      .where(and(eq(projects.knowledgeNodeId, nodeId), isNull(projects.deletedAt)))
      .limit(1);
    return { project: project && !project.isCompleted ? project : null, failed: false };
  } catch {
    return { project: null, failed: true };
  }
}

function isCanonicalProjectRoot(
  node: typeof knowledgeNodes.$inferSelect,
  project: { id: string } | null,
) {
  return Boolean(
    project
    && node.projectId === project.id
    && node.systemKey === `project_information:${project.id}`
    && node.parentId
    && node.rootPageId,
  );
}

function projectPointerLookupFailure() {
  return NextResponse.json(
    { detail: "Project canonical identityを確認できないためDocs操作を中止しました" },
    { status: 503 },
  );
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
  const access = await requireDocsNode(id, user, "write");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const managedSourceRejection = await rejectManagedMutation(access.node);
  if (managedSourceRejection) return managedSourceRejection;

  const pointerLookup = await getActiveProjectPointer(access.node.id);
  if (pointerLookup.failed) return projectPointerLookupFailure();
  if (isCanonicalProjectRoot(access.node, pointerLookup.project)) {
    return NextResponse.json(
      { detail: "アクティブProjectのcanonical情報rootは通常のDocs moveでは移動できません" },
      { status: 409 },
    );
  }

  const body = await request.json().catch(() => ({}));
  const hasNewParent = Object.prototype.hasOwnProperty.call(body, "new_parent_id");
  const newParentId = cleanOptionalString(body.new_parent_id, 80);
  if (!hasNewParent) {
    return NextResponse.json({ detail: "new_parent_idは必須です" }, { status: 400 });
  }
  if (newParentId === access.node.id) {
    return NextResponse.json({ detail: "自分自身を移動先にできません" }, { status: 400 });
  }
  const displayDescendantIds = await getKnowledgeDisplayDescendantIds(db, access.workspace.id, access.node.id);
  if (newParentId && displayDescendantIds.includes(newParentId)) {
    return NextResponse.json({ detail: "子孫nodeへ移動すると階層が循環します" }, { status: 400 });
  }
  const descendantIds = await getKnowledgeNodeDescendantIds(db, access.workspace.id, access.node.id);

  let parent: typeof knowledgeNodes.$inferSelect | null = null;
  if (newParentId) {
    const [parentRow] = await db
      .select()
      .from(knowledgeNodes)
      .where(and(eq(knowledgeNodes.id, newParentId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
      .limit(1);
    if (!parentRow) {
      return NextResponse.json({ detail: "移動先nodeが見つかりません" }, { status: 404 });
    }
    parent = parentRow;
    const parentAccess = await requireDocsNode(parent.id, user, "write");
    if (!parentAccess) {
      return NextResponse.json({ detail: "移動先nodeへの書き込み権限がありません" }, { status: 403 });
    }
    const managedParentRejection = await rejectManagedMutation(parent);
    if (managedParentRejection) return managedParentRejection;
    if (parent.projectId) {
      const projectAccess = await getWritableProject(parent.projectId, user);
      if (!projectAccess) {
        return NextResponse.json(
          { detail: "移動先Projectへの書き込み権限がありません" },
          { status: 403 },
        );
      }
      if (isDefaultInboxProject(projectAccess.project)) {
        return NextResponse.json(
          { detail: "InboxはDocsの案件保存先ではありません" },
          { status: 409 },
        );
      }
    }
    // A move is an intra-project operation.  Never clear a source Project's
    // identity by moving it under a Home/personal parent, and never reparent
    // Project A content below Project B (including malformed stale roots).
    if (access.node.projectId !== parent.projectId) {
      return NextResponse.json(
        { detail: "ProjectをまたぐDocs node移動はできません" },
        { status: 400 },
      );
    }
    if (
      access.node.projectId &&
      access.node.rootPageId !== parent.rootPageId &&
      access.node.id !== parent.id
    ) {
      return NextResponse.json(
        { detail: "Projectの正規サブツリー外へは移動できません" },
        { status: 400 },
      );
    }
  } else if (access.node.projectId) {
    // Explicit null parent is valid only for ordinary Personal nodes.  A
    // Project node must remain below its canonical Project information root.
    return NextResponse.json(
      { detail: "Project Docs nodeをPersonal rootへ移動することはできません" },
      { status: 400 },
    );
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeNodes.sortOrder) })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        newParentId ? eq(knowledgeNodes.parentId, newParentId) : isNull(knowledgeNodes.parentId),
      ),
    );
  const oldParentId = access.node.parentId;
  const leaveReference = body.leave_reference === true;

  const updated = await db.transaction(async (tx) => {
    const row = await updateDocsNode(tx, access.node.id, {
        parentId: parent?.id ?? null,
        rootPageId: parent ? (parent.rootPageId ?? parent.id) : access.node.id,
        projectId: parent?.projectId ?? null,
        sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
    if (leaveReference && oldParentId) {
      await tx
        .insert(knowledgeNodePlacements)
        .values({
          nodeId: access.node.id,
          parentNodeId: oldParentId,
          sortOrder: access.node.sortOrder ?? 0,
          collapsed: false,
          createdBy: user.id,
        })
        .onConflictDoNothing();
    }
    if (descendantIds.length > 0) {
      await updateDocsNodesByIds(tx, descendantIds, {
          rootPageId: row.rootPageId,
          projectId: row.projectId,
          updatedBy: user.id,
          updatedAt: new Date(),
        });
    }
    await appendKnowledgeRevision(tx, row, user.id, leaveReference ? "nodeを参照を残して移動" : "nodeを移動");
    return row;
  });

  return NextResponse.json({ node: serializeNode(updated) });
}
