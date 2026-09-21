import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  projects,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { plainDocsTitle } from "@/lib/docs-title";
import {
  getKnowledgeNodeChildMetadata,
  getDocsNodeAccess,
  requireDocsNode,
  serializeNode,
  serializeNodeSupertag,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import {
  DocsNodeInvariantError,
  DOCS_NODE_TITLE_MAX,
  docsNodeTitlesMatch,
  insertDocsNode,
} from "@/lib/server/docs-node-writer";
import { resolveProjectInformationNode } from "@/lib/server/project-information-hierarchy";
import { jsonWithConditional } from "@/lib/server/http-cache";
import {
  assertGenericDocsMutationAllowed,
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsAccessError,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

type IncomingTreeNode = {
  title?: unknown;
  children?: unknown;
  body_json?: unknown;
};

type NormalizedTreeNode = {
  title: string;
  children: NormalizedTreeNode[];
};

function normalizeTreeNode(value: unknown): NormalizedTreeNode {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("tree nodes must be objects");
  }
  const record = value as IncomingTreeNode;
  const title = typeof record.title === "string"
    ? record.title.slice(0, DOCS_NODE_TITLE_MAX)
    : "";
  if (!title.trim()) throw new Error("tree node title is required");
  const children = Array.isArray(record.children)
    ? record.children.map((child) => normalizeTreeNode(child))
    : [];
  return { title, children };
}

function validateTreeTitles(parentTitle: string, nodes: NormalizedTreeNode[]) {
  for (const node of nodes) {
    if (docsNodeTitlesMatch(parentTitle, node.title)) {
      throw new Error("親と同名の子nodeは作成できません");
    }
    validateTreeTitles(node.title, node.children);
  }
}

function serializeNavigationNode(node: typeof knowledgeNodes.$inferSelect) {
  const serialized = serializeNode(node);
  const body = serialized.body_json;
  const outlineBody = Object.fromEntries(
    ["format", "block_type", "checked", "blank"]
      .filter((key) => key in body)
      .map((key) => [key, body[key]]),
  ) as Record<string, unknown>;
  // Typed multiline blocks are normal editable content. Navigation payloads
  // may carry that content so a focus/tree response never regresses to a
  // readonly/empty representation. Legacy verbatim_* keys are omitted.
  if (
    body.format === "doc_block"
    && (body.block_type === "markdown" || body.block_type === "code")
    && typeof body.content === "string"
  ) {
    outlineBody.content = body.content.replace(/\r\n?/g, "\n");
    if (typeof body.label === "string") outlineBody.label = body.label;
    if (body.clip_ingest && typeof body.clip_ingest === "object" && !Array.isArray(body.clip_ingest)) {
      outlineBody.clip_ingest = body.clip_ingest;
    }
  }
  return {
    ...serialized,
    body_json: outlineBody,
    body_text: "",
  };
}

function isEmailOriginNode(node: typeof knowledgeNodes.$inferSelect) {
  const body = node.bodyJson;
  return node.systemKey?.startsWith("project_mail:") === true
    || Boolean(body && typeof body === "object" && !Array.isArray(body) && (body as Record<string, unknown>).format === "email");
}

function visibleNavigationNodes(
  nodes: Array<typeof knowledgeNodes.$inferSelect>,
) {
  let emailContext = false;
  return nodes.filter((node) => {
    emailContext = emailContext || isEmailOriginNode(node);
    const title = plainDocsTitle(node.title);
    const explicitBlank = node.isExplicitBlank === true;
    const identityKey = String(node.systemKey ?? "").trim();
    const identity = identityKey === "project_information_root"
      || identityKey.startsWith("project_information:");
    return (Boolean(title) || explicitBlank || identity)
      && !(node.title === "（空行）" && emailContext);
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await requireDocsNode(id, user, "read");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }
  const rootPageId = access.node.rootPageId ?? access.node.id;
  const ancestors: Array<typeof knowledgeNodes.$inferSelect> = [];
  const seenAncestorIds = new Set([access.node.id]);
  let parentId = access.node.parentId;
  while (parentId && ancestors.length < 100) {
    const [parent] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, parentId),
          eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (!parent || seenAncestorIds.has(parent.id)) break;
    const parentAccess = await requireDocsNode(parent.id, user, "read");
    if (!parentAccess) break;
    ancestors.unshift(parent);
    seenAncestorIds.add(parent.id);
    parentId = parent.parentId;
  }

  // navigation用treeはfocusとbreadcrumb祖先だけを返す。
  // 子一覧・Field値・重い本文detailは専用APIから必要なnodeだけを遅延取得する。
  const nodes = visibleNavigationNodes([...ancestors, access.node]);
  // Blank rows are structural legacy data, not addressable KnowledgeNodes.
  // A direct focus request for one must not leave the client with a phantom
  // focus id that can never render or lazy-load descendants.
  if (!nodes.some((node) => node.id === access.node.id)) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }
  const visibleRootPageId = nodes.some((node) => node.id === rootPageId)
    ? rootPageId
    : nodes[0]?.id ?? access.node.id;
  const nodeIds = nodes.map((node) => node.id);
  const nodeAccessRows = await Promise.all(nodes.map((node) => getDocsNodeAccess(node.id, user)));
  const permissionByNodeId = new Map(
    nodeAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => [item.node.id, item.permission]),
  );
  const [nodeSupertags, childMetadata] = await Promise.all([
    db
      .select({
        relation: knowledgeNodeSupertags,
        supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
      })
      .from(knowledgeNodeSupertags)
      .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
      .where(
        and(
          inArray(knowledgeNodeSupertags.nodeId, nodeIds),
          eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
        ),
      )
      .then((rows) => rows
        .filter((row) => row.supertagWorkspaceId === access.workspace.id)
        .map((row) => row.relation)),
    getKnowledgeNodeChildMetadata(
      access.workspace.id,
      nodeIds,
      null,
      false,
      user,
    ),
  ]);

  return jsonWithConditional(request, {
    focus_node_id: access.node.id,
    root_page_id: visibleRootPageId,
    nodes: nodes.map((node) => ({
      ...serializeNavigationNode(node),
      permission: permissionByNodeId.get(node.id),
    })),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    has_children_ids: childMetadata.hasChildrenIds,
    loaded_children_parent_ids: [],
  });
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
  try {
    // Tree creation is a generic Docs mutation.  Check the target and every
    // ancestor before parsing or creating any requested descendants so the
    // system-managed AoiTalk Guide cannot be extended through this bulk API.
    await assertGenericDocsMutationAllowed(access.node);
  } catch (error) {
    if (error instanceof ManagedDocsMutationError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    if (error instanceof ManagedDocsAccessError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }
  if (access.node.archivedAt) {
    return NextResponse.json({ detail: "アーカイブ済みnodeの下には作成できません" }, { status: 409 });
  }
  if (String(access.node.systemKey ?? "").trim() === "project_information_root") {
    return NextResponse.json({ detail: "案件情報hub直下にはProject経由でのみ作成できます" }, { status: 409 });
  }
  const pointerRows = await db
    .select()
    .from(projects)
    .where(eq(projects.knowledgeNodeId, access.node.id))
    .limit(2);
  const systemKey = String(access.node.systemKey ?? "").trim();
  let canonicalProjectPointer = false;
  if (pointerRows.length === 1) {
    // A matching pointer/key alone is not enough: malformed legacy rows can
    // carry both while living in a foreign library, under the wrong hub, or
    // without the canonical project_info tag.  Reuse the strict lifecycle
    // resolver so tree creation cannot bypass the Project-information
    // hierarchy invariants enforced by the other Docs write routes.
    const resolution = await resolveProjectInformationNode({
      project: pointerRows[0],
      includeInactive: false,
    });
    canonicalProjectPointer = resolution.status === "active"
      && resolution.node?.id === access.node.id
      && systemKey === `project_information:${pointerRows[0].id}`
      && access.node.projectId === pointerRows[0].id;
  }
  if (pointerRows.length > 0 && !canonicalProjectPointer) {
    return NextResponse.json(
      { detail: "Projectが参照するDocs nodeには通常のtree作成を実行できません" },
      { status: 409 },
    );
  }
  if (systemKey.startsWith("project_information:") && !canonicalProjectPointer) {
    return NextResponse.json(
      { detail: "stale案件情報の正本nodeには通常のtree作成を実行できません" },
      { status: 409 },
    );
  }

  const body = await request.json().catch(() => ({}));
  const rawNodes = Array.isArray(body.nodes) ? body.nodes : [body.node];
  let tree;
  try {
    tree = rawNodes.map((node: unknown) => normalizeTreeNode(node));
  } catch (error) {
    return NextResponse.json({ detail: error instanceof Error ? error.message : "invalid tree" }, { status: 400 });
  }
  try {
    validateTreeTitles(access.node.title, tree);
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "親と同名の子nodeは作成できません" },
      { status: 409 },
    );
  }

  const effectiveProjectId = access.node.projectId;
  let created: Array<typeof knowledgeNodes.$inferSelect>;
  try {
    created = await db.transaction(async (tx) => {
      let lockedProject: { id: string; knowledgeNodeId: string | null; isCompleted: boolean; deletedAt: Date | null } | null = null;
      if (effectiveProjectId) {
        const [projectRow] = await tx
          .select({
            id: projects.id,
            knowledgeNodeId: projects.knowledgeNodeId,
            isCompleted: projects.isCompleted,
            deletedAt: projects.deletedAt,
          })
          .from(projects)
          .where(eq(projects.id, effectiveProjectId))
          .for("update")
          .limit(1);
        if (!projectRow || projectRow.deletedAt || projectRow.isCompleted) {
          throw new DocsNodeInvariantError(
            "完了/削除済みProjectのDocsには新しいnodeを作成できません",
          );
        }
        lockedProject = projectRow;
      }
      const [lockedParent] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, id),
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          ),
        )
        .limit(1);
      if (!lockedParent || lockedParent.archivedAt) {
        throw new DocsNodeInvariantError("親nodeが同時変更されたため作成できません");
      }
      // Re-read the target/ancestor policy after taking the same transaction
      // lock used for insertion.  A concurrent Guide repair or reparenting
      // must not turn the preflight decision into a write bypass.
      await lockAndAssertGenericDocsMutationAllowed(lockedParent, tx, user);
      const [freshLockedParent] = await tx
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.id, id),
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
          ),
        )
        .limit(1)
        .for("update");
      if (!freshLockedParent || freshLockedParent.archivedAt) {
        throw new DocsNodeInvariantError("親nodeが同時変更されたため作成できません");
      }
      // The policy helper locked the complete closure in lexical order; use
      // the fresh target row for identity/body checks rather than the
      // unlocked preflight snapshot.
      const lockedParentRow = freshLockedParent;
      if (lockedParentRow.projectId !== effectiveProjectId) {
        throw new DocsNodeInvariantError("親nodeのProject identityが同時変更されたため作成できません");
      }
      if (lockedProject?.knowledgeNodeId === lockedParentRow.id && !canonicalProjectPointer) {
        throw new DocsNodeInvariantError("Projectが参照するDocs nodeには通常のtree作成を実行できません");
      }
      if (canonicalProjectPointer && lockedProject?.knowledgeNodeId !== lockedParentRow.id) {
        throw new DocsNodeInvariantError("Project canonical identityが同時変更されたため作成できません");
      }
      if (canonicalProjectPointer && lockedProject) {
        const pointerRowsInTx = await tx
          .select({ id: projects.id })
          .from(projects)
          .where(eq(projects.knowledgeNodeId, lockedParentRow.id))
          .for("update");
        if (pointerRowsInTx.length !== 1 || pointerRowsInTx[0]?.id !== lockedProject.id) {
          throw new DocsNodeInvariantError(
            "Project canonical pointerが同時変更されたため作成できません",
          );
        }
      }
      try {
        validateTreeTitles(lockedParentRow.title, tree);
      } catch (error) {
        throw new DocsNodeInvariantError(
          error instanceof Error ? error.message : "親と同名の子nodeは作成できません",
        );
      }
      const [maxRow] = await tx
        .select({ maxSort: max(knowledgeNodes.sortOrder) })
        .from(knowledgeNodes)
        .where(and(
          eq(knowledgeNodes.parentId, lockedParentRow.id),
          eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        ));
      const rootPageId = lockedParentRow.rootPageId ?? lockedParentRow.id;
      const rows: Array<typeof knowledgeNodes.$inferSelect> = [];
      const createChildren = async (
        parentId: string,
        nodes: NormalizedTreeNode[],
        baseSort: number,
      ) => {
        for (const [index, item] of nodes.entries()) {
          const node = await insertDocsNode(tx, {
            docsLibraryId: access.workspace.id,
            parentId,
            rootPageId,
            projectId: lockedParentRow.projectId,
            title: item.title,
            bodyJson: { format: "structured_tree_node" },
            nodeType: "node",
            sortOrder: baseSort + index,
            createdBy: user.id,
            updatedBy: user.id,
          });
          rows.push(node);
          await upsertKnowledgeSearchIndex(tx, node, node.title);
          await syncKnowledgeNodeReferenceEdges(tx, node, user.id);
          if (item.children.length > 0) await createChildren(node.id, item.children, 0);
        }
      };
      await createChildren(access.node.id, tree, (maxRow?.maxSort ?? 0) + 1);
      return rows;
    });
  } catch (error) {
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

  return NextResponse.json({ nodes: created.map(serializeNode) }, { status: 201 });
}
