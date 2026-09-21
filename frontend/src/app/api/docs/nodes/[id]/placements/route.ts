import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, max } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeNodePlacements, knowledgeNodes, projects } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  getKnowledgeDisplayDescendantIds,
  requireDocsNode,
  serializeNodePlacement,
} from "@/lib/server/knowledge-docs-utils";
import {
  assertGenericDocsMutationAllowed,
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsAccessError,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

class PlacementInvariantError extends Error {
  readonly status = 409;
}

type PlacementTransaction = Pick<typeof db, "select">;

/**
 * Lock both endpoint ancestor chains in one lexical order.  Placement writes
 * otherwise lock source then parent, so two reverse placements can each hold
 * one endpoint and wait forever on the other.
 */
async function lockPlacementNodeClosures(
  tx: PlacementTransaction,
  docsLibraryId: string,
  rootIds: Array<string | null | undefined>,
) {
  const closureIds = new Set<string>();
  for (const rootId of rootIds) {
    let currentId = rootId ?? null;
    const visited = new Set<string>();
    for (let depth = 0; currentId; depth += 1) {
      if (depth >= 512 || visited.has(currentId)) {
        throw new PlacementInvariantError("Docs nodeの親階層が循環しています");
      }
      visited.add(currentId);
      closureIds.add(currentId);
      const [row] = await tx
        .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, currentId),
            eq(knowledgeNodes.docsLibraryId, docsLibraryId),
          ),
        )
        .limit(1);
      if (!row) {
        throw new PlacementInvariantError("Docs nodeの親階層を確認できません");
      }
      currentId = row.parentId;
    }
  }
  const orderedIds = [...closureIds].sort();
  if (orderedIds.length === 0) return;
  await tx
    .select({ id: knowledgeNodes.id })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, docsLibraryId),
        inArray(knowledgeNodes.id, orderedIds),
      ),
    )
    .orderBy(asc(knowledgeNodes.id))
    .for("update");
}

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
  const sourceSystemKey = String(access.node.systemKey ?? "").trim();
  if (sourceSystemKey === "project_information_root" || sourceSystemKey.startsWith("project_information:")) {
    return NextResponse.json({ detail: "案件情報の正本nodeには通常の参照配置を実行できません" }, { status: 409 });
  }
  const sourcePointers = await db
    .select({ id: projects.id })
    .from(projects)
    .where(eq(projects.knowledgeNodeId, access.node.id))
    .limit(2);
  if (sourcePointers.length > 0) {
    return NextResponse.json({ detail: "Projectが参照するDocs nodeには通常の参照配置を実行できません" }, { status: 409 });
  }

  const body = await request.json().catch(() => ({}));
  const parentNodeId = cleanOptionalString(body.parent_node_id, 80);
  if (!parentNodeId) {
    return NextResponse.json({ detail: "parent_node_idは必須です" }, { status: 400 });
  }
  const parentAccess = await requireDocsNode(parentNodeId, user, "write");
  if (!parentAccess || parentAccess.workspace.id !== access.workspace.id) {
    return NextResponse.json({ detail: "配置先nodeへの書き込み権限がありません" }, { status: 403 });
  }
  if (parentNodeId === access.node.id) {
    return NextResponse.json({ detail: "自分自身へ参照配置できません" }, { status: 400 });
  }
  const descendantIds = await getKnowledgeDisplayDescendantIds(db, access.workspace.id, access.node.id);
  if (descendantIds.includes(parentNodeId)) {
    return NextResponse.json({ detail: "子孫nodeへ参照配置すると表示階層が循環します" }, { status: 400 });
  }

  const [parent] = await db
    .select()
    .from(knowledgeNodes)
    .where(and(eq(knowledgeNodes.id, parentNodeId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
    .limit(1);
  if (!parent) {
    return NextResponse.json({ detail: "配置先nodeが見つかりません" }, { status: 404 });
  }
  const managedParentRejection = await rejectManagedMutation(parent);
  if (managedParentRejection) return managedParentRejection;
  if (parent.archivedAt) {
    return NextResponse.json({ detail: "アーカイブ済みnodeには参照配置できません" }, { status: 409 });
  }
  if (String(parent.systemKey ?? "").trim() === "project_information_root" || String(parent.systemKey ?? "").trim().startsWith("project_information:")) {
    return NextResponse.json({ detail: "案件情報hub直下には参照配置できません" }, { status: 409 });
  }

  const placement = await db.transaction(async (tx) => {
    // Discover reverse Project pointers before taking any node lock. Include
    // those Projects in the deterministic Project lock set so a concurrent
    // canonical repair cannot acquire Project after this transaction has
    // already locked the source closure.
    const pointerSnapshot = await tx
      .select({ id: projects.id })
      .from(projects)
      .where(eq(projects.knowledgeNodeId, access.node.id))
      .limit(2);
    // Project-bound placement mutations follow the same Project->node lock
    // order as task writes and canonical repairs. Lock both endpoint
    // Projects in lexical order before touching either node; Personal nodes
    // simply contribute no project row.
    const endpointProjectIds = Array.from(
      new Set(
        [
          access.node.projectId,
          parent.projectId,
          ...pointerSnapshot.map((pointer) => pointer.id),
        ]
          .filter((projectId): projectId is string => Boolean(projectId))
          .map(String),
      ),
    ).sort();
    for (const projectId of endpointProjectIds) {
      await tx
        .select({ id: projects.id })
        .from(projects)
        .where(eq(projects.id, projectId))
        .limit(1)
          .for("update");
    }
    await lockPlacementNodeClosures(tx, access.workspace.id, [access.node.id, parentNodeId]);
    const sourcePointers = await tx
      .select({ id: projects.id })
      .from(projects)
      .where(eq(projects.knowledgeNodeId, access.node.id))
      .limit(2)
      .for("update");
    if (sourcePointers.length > 0) {
      throw new PlacementInvariantError("Projectが参照するDocs nodeには通常の参照配置を実行できません");
    }
    const [lockedSource] = await tx
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        projectId: knowledgeNodes.projectId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
        archivedAt: knowledgeNodes.archivedAt,
      })
      .from(knowledgeNodes)
      .where(and(eq(knowledgeNodes.id, access.node.id), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
      .limit(1)
      .for("update");
    const lockedSourceKey = String(lockedSource?.systemKey ?? "").trim();
    if (!lockedSource || lockedSource.archivedAt) {
      throw new PlacementInvariantError("source nodeが同時に変更されたため参照配置を中止しました");
    }
    await lockAndAssertGenericDocsMutationAllowed(lockedSource, tx, user);
    if (lockedSourceKey === "project_information_root" || lockedSourceKey.startsWith("project_information:")) {
      throw new PlacementInvariantError("案件情報の正本nodeには通常の参照配置を実行できません");
    }
    const [lockedParent] = await tx
      .select({
        id: knowledgeNodes.id,
        docsLibraryId: knowledgeNodes.docsLibraryId,
        parentId: knowledgeNodes.parentId,
        projectId: knowledgeNodes.projectId,
        systemKey: knowledgeNodes.systemKey,
        displayProps: knowledgeNodes.displayProps,
        archivedAt: knowledgeNodes.archivedAt,
      })
      .from(knowledgeNodes)
      .where(and(eq(knowledgeNodes.id, parentNodeId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
      .limit(1)
      .for("update");
    if (!lockedParent) throw new Error("配置先nodeが見つかりません");
    await lockAndAssertGenericDocsMutationAllowed(lockedParent, tx, user);
    if (lockedParent.archivedAt) throw new Error("アーカイブ済みnodeには参照配置できません");
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodePlacements.sortOrder) })
      .from(knowledgeNodePlacements)
      .where(eq(knowledgeNodePlacements.parentNodeId, parentNodeId));
    const [created] = await tx
      .insert(knowledgeNodePlacements)
      .values({
        nodeId: access.node.id,
        parentNodeId,
        sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
        collapsed: !!body.collapsed,
        createdBy: user.id,
      })
      .onConflictDoUpdate({
        target: [knowledgeNodePlacements.nodeId, knowledgeNodePlacements.parentNodeId],
        set: {
          sortOrder: typeof body.sort_order === "number" ? body.sort_order : (maxRow?.maxSort ?? 0) + 1,
          collapsed: !!body.collapsed,
        },
      })
      .returning();
    return created;
  }).catch((error) => {
    if (error instanceof PlacementInvariantError) return error;
    if (error instanceof ManagedDocsMutationError) return error;
    if (error instanceof ManagedDocsAccessError) return error;
    if (error instanceof Error && error.message.includes("アーカイブ済み")) {
      return null;
    }
    throw error;
  });
  if (placement instanceof PlacementInvariantError) {
    return NextResponse.json({ detail: placement.message }, { status: placement.status });
  }
  if (placement instanceof ManagedDocsMutationError) {
    return NextResponse.json({ detail: placement.message }, { status: placement.status });
  }
  if (placement instanceof ManagedDocsAccessError) {
    return NextResponse.json({ detail: placement.message }, { status: placement.status });
  }
  if (!placement) {
    return NextResponse.json({ detail: "アーカイブ済みnodeには参照配置できません" }, { status: 409 });
  }

  return NextResponse.json({ placement: serializeNodePlacement(placement) }, { status: 201 });
}

export async function DELETE(
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
  const sourceSystemKey = String(access.node.systemKey ?? "").trim();
  if (sourceSystemKey === "project_information_root" || sourceSystemKey.startsWith("project_information:")) {
    return NextResponse.json({ detail: "案件情報の正本nodeには通常の参照配置解除を実行できません" }, { status: 409 });
  }
  const sourcePointers = await db
    .select({ id: projects.id })
    .from(projects)
    .where(eq(projects.knowledgeNodeId, access.node.id))
    .limit(2);
  if (sourcePointers.length > 0) {
    return NextResponse.json({ detail: "Projectが参照するDocs nodeには通常の参照配置解除を実行できません" }, { status: 409 });
  }
  const parentNodeId = cleanOptionalString(request.nextUrl.searchParams.get("parent_node_id"), 80);
  if (!parentNodeId) {
    return NextResponse.json({ detail: "parent_node_idは必須です" }, { status: 400 });
  }
  const parentAccess = await requireDocsNode(parentNodeId, user, "write");
  if (!parentAccess || parentAccess.workspace.id !== access.workspace.id) {
    return NextResponse.json({ detail: "配置先nodeへの書き込み権限がありません" }, { status: 403 });
  }
  const managedParentRejection = await rejectManagedMutation(parentAccess.node);
  if (managedParentRejection) return managedParentRejection;
  try {
    await db.transaction(async (tx) => {
      const pointerSnapshot = await tx
        .select({ id: projects.id })
        .from(projects)
        .where(eq(projects.knowledgeNodeId, access.node.id))
        .limit(2);
      const endpointProjectIds = Array.from(
        new Set(
          [
            access.node.projectId,
            parentAccess.node.projectId,
            ...pointerSnapshot.map((pointer) => pointer.id),
          ]
            .filter((projectId): projectId is string => Boolean(projectId))
            .map(String),
        ),
      ).sort();
      for (const projectId of endpointProjectIds) {
        await tx
          .select({ id: projects.id })
          .from(projects)
          .where(eq(projects.id, projectId))
          .limit(1)
          .for("update");
      }
      await lockPlacementNodeClosures(tx, access.workspace.id, [access.node.id, parentNodeId]);
      const sourcePointers = await tx
        .select({ id: projects.id })
        .from(projects)
        .where(eq(projects.knowledgeNodeId, access.node.id))
        .limit(2)
        .for("update");
      if (sourcePointers.length > 0) {
        throw new PlacementInvariantError("Projectが参照するDocs nodeには通常の参照配置解除を実行できません");
      }
      const [lockedSource] = await tx
        .select({
          id: knowledgeNodes.id,
          docsLibraryId: knowledgeNodes.docsLibraryId,
          parentId: knowledgeNodes.parentId,
          projectId: knowledgeNodes.projectId,
          systemKey: knowledgeNodes.systemKey,
          displayProps: knowledgeNodes.displayProps,
          archivedAt: knowledgeNodes.archivedAt,
        })
        .from(knowledgeNodes)
        .where(and(eq(knowledgeNodes.id, access.node.id), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
        .limit(1)
        .for("update");
      const lockedSourceKey = String(lockedSource?.systemKey ?? "").trim();
      if (!lockedSource || lockedSource.archivedAt || lockedSourceKey === "project_information_root" || lockedSourceKey.startsWith("project_information:")) {
        throw new PlacementInvariantError("案件情報の正本nodeには通常の参照配置解除を実行できません");
      }
      await lockAndAssertGenericDocsMutationAllowed(lockedSource, tx, user);
      const [lockedParent] = await tx
        .select({
          id: knowledgeNodes.id,
          docsLibraryId: knowledgeNodes.docsLibraryId,
          parentId: knowledgeNodes.parentId,
          projectId: knowledgeNodes.projectId,
          systemKey: knowledgeNodes.systemKey,
          displayProps: knowledgeNodes.displayProps,
          archivedAt: knowledgeNodes.archivedAt,
        })
        .from(knowledgeNodes)
        .where(and(eq(knowledgeNodes.id, parentNodeId), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
        .limit(1)
        .for("update");
      if (!lockedParent) throw new Error("配置先nodeが見つかりません");
      await lockAndAssertGenericDocsMutationAllowed(lockedParent, tx, user);
      await tx
        .delete(knowledgeNodePlacements)
        .where(
          and(
            eq(knowledgeNodePlacements.nodeId, access.node.id),
            eq(knowledgeNodePlacements.parentNodeId, parentNodeId),
          ),
        );
    });
  } catch (error) {
    if (error instanceof PlacementInvariantError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (error instanceof ManagedDocsAccessError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }
  return NextResponse.json({ ok: true });
}
