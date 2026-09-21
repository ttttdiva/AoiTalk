import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, max, isNull } from "drizzle-orm";
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
import {
  DocsNodeInvariantError,
  updateDocsNode,
  updateDocsNodesByIds,
} from "@/lib/server/docs-node-writer";
import { getWritableProject } from "@/lib/server/project-access";
import { isDefaultInboxProject } from "@/lib/server/project-information-hierarchy";
import {
  assertGenericDocsMutationAllowed,
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsAccessError,
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

class DocsMoveInvariantError extends Error {
  readonly status = 409;
}

type ActiveProjectPointerLookup = {
  project: { id: string; isCompleted: boolean; deletedAt: Date | null } | null;
  failed: boolean;
};

async function getActiveProjectPointer(nodeId: string): Promise<ActiveProjectPointerLookup> {
  try {
    const projectsForNode = await db
      .select({ id: projects.id, isCompleted: projects.isCompleted, deletedAt: projects.deletedAt })
      .from(projects)
      .where(eq(projects.knowledgeNodeId, nodeId))
      .limit(2);
    if (projectsForNode.length > 1) {
      return { project: null, failed: true };
    }
    const project = projectsForNode[0];
    return {
      project: project ?? null,
      failed: false,
    };
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
  const identitySystemKey = String(access.node.systemKey ?? "").trim();
  if (identitySystemKey === "project_information_root" || identitySystemKey.startsWith("project_information:")) {
    return NextResponse.json(
      { detail: "案件情報の正本/stale nodeは専用クリーンアップ経路でのみ変更できます" },
      { status: 409 },
    );
  }
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
  let descendantIds = await getKnowledgeNodeDescendantIds(db, access.workspace.id, access.node.id);

  // The Personal 案件情報 hub is a metadata shell.  Moving it (or any
  // ancestor closure containing a Project pointer) would rewrite every
  // descendant root_page_id and silently invalidate canonical identities.
  if (String(access.node.systemKey ?? "").trim() === "project_information_root") {
    return NextResponse.json(
      { detail: "案件情報hubは通常のDocs moveでは移動できません" },
      { status: 409 },
    );
  }
  if (pointerLookup.project) {
    // A reverse Project pointer is authoritative even when the row's
    // system_key/project_id fields are malformed or the Project is retained.
    // Never let generic move rewrite such an identity into an unrecoverable
    // stale subtree; the owner cleanup route is the explicit lifecycle path.
    return NextResponse.json(
      { detail: "Projectが参照するDocs nodeは通常のDocs moveでは移動できません" },
      { status: 409 },
    );
  }
  if (descendantIds.length > 0) {
    try {
      const pointerRows = await db
        .select({ id: projects.id })
        .from(projects)
        .where(inArray(projects.knowledgeNodeId, [access.node.id, ...descendantIds]))
        .limit(1);
      if (pointerRows.length > 0) {
        return NextResponse.json(
          { detail: "Projectが参照するDocs nodeを含むため通常のDocs moveでは移動できません" },
          { status: 409 },
        );
      }
    } catch {
      return projectPointerLookupFailure();
    }
  }

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
    if (parentRow.archivedAt) {
      return NextResponse.json({ detail: "アーカイブ済みnodeの下には移動できません" }, { status: 409 });
    }
    if (String(parentRow.systemKey ?? "").trim() === "project_information_root") {
      return NextResponse.json({ detail: "案件情報hub直下への通常のDocs moveはできません" }, { status: 409 });
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

  const oldParentId = access.node.parentId;
  const leaveReference = body.leave_reference === true;

  let updated: typeof knowledgeNodes.$inferSelect;
  try {
    updated = await db.transaction(async (tx) => {
    if (typeof tx.select === "function") {
      // Refresh the subtree under the transaction.  The preflight list can
      // miss a child inserted concurrently; detect that drift before any
      // root/project rewrite so no canonical descendant is stranded.
      if (access.node.projectId) {
        const [lockedProject] = await tx
          .select({ isCompleted: projects.isCompleted, deletedAt: projects.deletedAt })
          .from(projects)
          .where(eq(projects.id, access.node.projectId))
          .for("update")
          .limit(1);
        if (!lockedProject || lockedProject.deletedAt || lockedProject.isCompleted) {
          throw new DocsMoveInvariantError(
            "完了/削除済みProjectのDocs nodeは通常のmoveで変更できません",
          );
        }
      }
      const lockedSnapshotDescendantIds = await getKnowledgeNodeDescendantIds(
        tx,
        access.workspace.id,
        access.node.id,
      );
      const closureIds = [access.node.id, ...lockedSnapshotDescendantIds];
      // Opposite concurrent moves (A under B and B under A) must not acquire
      // the source closure and destination ancestor chain in opposite orders.
      // Collect the destination path first, then lock the union in lexical
      // order before running either managed-policy walk.
      const destinationAncestorIds: string[] = [];
      if (parent?.id) {
        const seenDestination = new Set<string>();
        let destinationId: string | null = parent.id;
        for (let depth = 0; destinationId; depth += 1) {
          if (depth >= 512 || seenDestination.has(destinationId)) {
            throw new DocsMoveInvariantError("移動先Docsの親階層が循環しています");
          }
          seenDestination.add(destinationId);
          const [destination] = await tx
            .select({ id: knowledgeNodes.id, parentId: knowledgeNodes.parentId })
            .from(knowledgeNodes)
            .where(
              and(
                eq(knowledgeNodes.id, destinationId),
                eq(knowledgeNodes.docsLibraryId, access.workspace.id),
              ),
            )
            .limit(1);
          if (!destination) {
            throw new DocsMoveInvariantError("移動先Docsの親階層を確認できません");
          }
          destinationAncestorIds.push(destination.id);
          destinationId = destination.parentId;
        }
      }
      // Match Project-information repair's Project->node lock order.  Lock
      // reverse pointers before the source closure so a concurrent repair
      // cannot assign a canonical pointer to a row being moved.
      const pointers = await tx
        .select({ id: projects.id })
        .from(projects)
        .where(inArray(projects.knowledgeNodeId, closureIds))
        .for("update");
      if (pointers.length > 0) {
        throw new DocsMoveInvariantError(
          "Projectが参照するDocs nodeを含むため通常のDocs moveでは移動できません",
        );
      }
      const nodeLockIds = Array.from(new Set([...closureIds, ...destinationAncestorIds])).sort();
      await tx
        .select({ id: knowledgeNodes.id })
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
            inArray(knowledgeNodes.id, nodeLockIds),
          ),
        )
        .orderBy(asc(knowledgeNodes.id))
        .for("update");
      const stabilizedDescendantIds = await getKnowledgeNodeDescendantIds(
        tx,
        access.workspace.id,
        access.node.id,
      );
      const snapshotSet = new Set(lockedSnapshotDescendantIds);
      if (
        stabilizedDescendantIds.length !== snapshotSet.size
        || stabilizedDescendantIds.some((nodeId) => !snapshotSet.has(nodeId))
      ) {
        throw new DocsMoveInvariantError(
          "Docs subtreeが同時更新されたためmoveを中止しました",
        );
      }
      if (newParentId && stabilizedDescendantIds.includes(newParentId)) {
        throw new DocsMoveInvariantError("子孫nodeを親にすると階層が破綻します");
      }
      const identityRows = await tx
        .select({ id: knowledgeNodes.id, systemKey: knowledgeNodes.systemKey })
        .from(knowledgeNodes)
        .where(inArray(knowledgeNodes.id, closureIds));
      if (identityRows.some((row) =>
        row.id !== access.node.id
        && (String(row.systemKey ?? "").trim() === "project_information_root"
          || String(row.systemKey ?? "").trim().startsWith("project_information:"))
      )) {
        throw new DocsMoveInvariantError(
          "Project canonical/stale identityを含むDocs subtreeは通常のmoveで変更できません",
        );
      }
      // Re-read and lock the source before applying the managed policy. The
      // preflight decision is not a transaction boundary, so a concurrent
      // reparent could otherwise turn an ordinary source into a Guide
      // descendant just before this move updates it.
      const [lockedSource] = await tx
        .select()
        .from(knowledgeNodes)
        .where(and(eq(knowledgeNodes.id, access.node.id), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
        .limit(1)
        .for("update");
      if (!lockedSource || lockedSource.archivedAt) {
        throw new DocsMoveInvariantError("source nodeが同時に変更されたためmoveを中止しました");
      }
      await lockAndAssertGenericDocsMutationAllowed(lockedSource, tx, user);
      descendantIds = stabilizedDescendantIds;
      if (parent?.id) {
        const [lockedParent] = await tx
          .select()
          .from(knowledgeNodes)
          .where(and(eq(knowledgeNodes.id, parent.id), eq(knowledgeNodes.docsLibraryId, access.workspace.id)))
          .limit(1)
          .for("update");
        if (!lockedParent || lockedParent.archivedAt) {
          throw new DocsMoveInvariantError("アーカイブ済みnodeの下には移動できません");
        }
        if (String(lockedParent.systemKey ?? "").trim() === "project_information_root") {
          throw new DocsMoveInvariantError("案件情報hub直下への通常のDocs moveはできません");
        }
        if (lockedParent.projectId !== access.node.projectId) {
          throw new DocsMoveInvariantError("ProjectをまたぐDocs node移動はできません");
        }
        if (
          access.node.projectId
          && lockedParent.id !== access.node.id
          && lockedParent.rootPageId !== access.node.rootPageId
        ) {
          throw new DocsMoveInvariantError("Projectの正規サブツリー外へは移動できません");
        }
        // Apply the same transactional policy to the destination chain. A
        // concurrent reparent must not allow a generic move into the Guide.
        await lockAndAssertGenericDocsMutationAllowed(lockedParent, tx, user);
        // Use the locked structural values for the write below rather than
        // the preflight snapshot, which may have been repaired concurrently.
        parent = lockedParent;
      }
    }
    const [lockedMaxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          parent?.id
            ? eq(knowledgeNodes.parentId, parent.id)
            : isNull(knowledgeNodes.parentId),
        ),
      );
    const transactionSortOrder = typeof body.sort_order === "number"
      ? body.sort_order
      : (lockedMaxRow?.maxSort ?? 0) + 1;
    const row = await updateDocsNode(tx, access.node.id, {
        parentId: parent?.id ?? null,
        rootPageId: parent ? (parent.rootPageId ?? parent.id) : access.node.id,
        projectId: parent?.projectId ?? null,
        sortOrder: transactionSortOrder,
        updatedBy: user.id,
        updatedAt: new Date(),
      });
    if (!row) {
      throw new DocsMoveInvariantError("Docs nodeが同時に削除されたためmoveできません");
    }
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
  } catch (error) {
    if (error instanceof DocsMoveInvariantError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (typeof DocsNodeInvariantError === "function" && error instanceof DocsNodeInvariantError) {
      return NextResponse.json(
        { detail: error.message, code: error.code },
        { status: error.status },
      );
    }
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (error instanceof ManagedDocsAccessError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }

  return NextResponse.json({ node: serializeNode(updated) });
}
