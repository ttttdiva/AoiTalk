import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, max, or } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  getUserProjects,
  getKnowledgeNodeChildMetadata,
  requireDocsNode,
  serializeNode,
  serializeNodeSupertag,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { DOCS_NODE_TITLE_MAX, insertDocsNode } from "@/lib/server/docs-node-writer";

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

function serializeNavigationNode(node: typeof knowledgeNodes.$inferSelect) {
  const serialized = serializeNode(node);
  const body = serialized.body_json;
  return {
    ...serialized,
    body_json: Object.fromEntries(
      ["format", "block_type", "checked"]
        .filter((key) => key in body)
        .map((key) => [key, body[key]]),
    ),
    body_text: "",
  };
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
  const access = await requireDocsNode(id, user, "read");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const projects = await getUserProjects(user.id);
  const accessibleProjectIds = projects.map((project) => project.id);
  const accessibleNodeCondition = accessibleProjectIds.length > 0
    ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
    : isNull(knowledgeNodes.projectId);
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
          eq(knowledgeNodes.workspaceId, access.workspace.id),
          isNull(knowledgeNodes.archivedAt),
          accessibleNodeCondition,
        ),
      )
      .limit(1);
    if (!parent || seenAncestorIds.has(parent.id)) break;
    ancestors.unshift(parent);
    seenAncestorIds.add(parent.id);
    parentId = parent.parentId;
  }

  // navigation用treeはfocusとbreadcrumb祖先だけを返す。
  // 子一覧・Field値・重い本文detailは専用APIから必要なnodeだけを遅延取得する。
  const nodes = [...ancestors, access.node];
  const nodeIds = nodes.map((node) => node.id);
  const [nodeSupertags, childMetadata] = await Promise.all([
    db.select().from(knowledgeNodeSupertags).where(inArray(knowledgeNodeSupertags.nodeId, nodeIds)),
    getKnowledgeNodeChildMetadata(access.workspace.id, nodeIds, accessibleProjectIds),
  ]);

  return NextResponse.json({
    focus_node_id: access.node.id,
    root_page_id: access.node.rootPageId ?? access.node.id,
    nodes: nodes.map(serializeNavigationNode),
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

  const body = await request.json().catch(() => ({}));
  const rawNodes = Array.isArray(body.nodes) ? body.nodes : [body.node];
  let tree;
  try {
    tree = rawNodes.map((node: unknown) => normalizeTreeNode(node));
  } catch (error) {
    return NextResponse.json({ detail: error instanceof Error ? error.message : "invalid tree" }, { status: 400 });
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeNodes.sortOrder) })
    .from(knowledgeNodes)
    .where(eq(knowledgeNodes.parentId, access.node.id));
  const rootPageId = access.node.rootPageId ?? access.node.id;
  const created = await db.transaction(async (tx) => {
    const rows: Array<typeof knowledgeNodes.$inferSelect> = [];
    const createChildren = async (
      parentId: string,
      nodes: NormalizedTreeNode[],
      baseSort: number,
    ) => {
      for (const [index, item] of nodes.entries()) {
        const node = await insertDocsNode(tx, {
          workspaceId: access.workspace.id,
          parentId,
          rootPageId,
          projectId: access.node.projectId,
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

  return NextResponse.json({ nodes: created.map(serializeNode) }, { status: 201 });
}
