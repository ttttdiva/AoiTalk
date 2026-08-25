import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
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
  DOCS_NODE_TITLE_MAX,
  docsNodeTitlesMatch,
  insertDocsNode,
} from "@/lib/server/docs-node-writer";
import { jsonWithConditional } from "@/lib/server/http-cache";

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
    ["format", "block_type", "checked"]
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
    return Boolean(title)
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

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeNodes.sortOrder) })
    .from(knowledgeNodes)
    .where(and(
      eq(knowledgeNodes.parentId, access.node.id),
      eq(knowledgeNodes.docsLibraryId, access.workspace.id),
    ));
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
          docsLibraryId: access.workspace.id,
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
