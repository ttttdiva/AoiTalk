import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, or } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeEdges,
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodePlacements,
  knowledgeNodes,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  requireDocsNode,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";

type ReferenceKind = "inline_ref" | "reference-edge" | "placement" | "field_ref";

function addReference(
  map: Map<string, { node: typeof knowledgeNodes.$inferSelect; kind: ReferenceKind; snippet: string }>,
  node: typeof knowledgeNodes.$inferSelect | undefined,
  kind: ReferenceKind,
  snippet: string,
  selectedNodeId: string,
) {
  if (!node || node.id === selectedNodeId || map.has(node.id)) return;
  map.set(node.id, { node, kind, snippet });
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

  const [edges, placements, fieldRefs] = await Promise.all([
    db
      .select()
      .from(knowledgeEdges)
      .where(
        and(
          inArray(knowledgeEdges.relationType, ["inline_ref", "references"]),
          or(
            eq(knowledgeEdges.sourceNodeId, access.node.id),
            eq(knowledgeEdges.targetNodeId, access.node.id),
          ),
        ),
      )
      .limit(500),
    db
      .select()
      .from(knowledgeNodePlacements)
      .where(eq(knowledgeNodePlacements.nodeId, access.node.id))
      .limit(500),
    db
      .select({ value: knowledgeFieldValues, field: knowledgeFields })
      .from(knowledgeFieldValues)
      .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
      .innerJoin(knowledgeNodes, eq(knowledgeFieldValues.nodeId, knowledgeNodes.id))
      .where(
        and(
          eq(knowledgeFieldValues.targetNodeId, access.node.id),
          // A malformed value must not turn a field from another library into
          // a reference to this node.  Source and target share the same docs
          // library boundary before ACL checks below are applied.
          eq(knowledgeFields.docsLibraryId, access.workspace.id),
          eq(knowledgeNodes.docsLibraryId, access.workspace.id),
        ),
      )
      .limit(500),
  ]);

  const referenceNodeIds = Array.from(new Set([
    ...edges.flatMap((edge) => [edge.sourceNodeId, edge.targetNodeId]),
    ...placements.map((placement) => placement.parentNodeId),
    ...fieldRefs.map((row) => row.value.nodeId),
  ].filter((nodeId) => nodeId !== access.node.id)));
  const visibleReferenceIds = (
    await Promise.all(referenceNodeIds.map((nodeId) => requireDocsNode(nodeId, user, "read")))
  )
    .filter((item): item is NonNullable<typeof item> => Boolean(item))
    .map((item) => item.node.id);
  const nodes = visibleReferenceIds.length
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(inArray(knowledgeNodes.id, visibleReferenceIds))
        .limit(1000)
    : [];
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const backlinks = new Map<
    string,
    { node: typeof knowledgeNodes.$inferSelect; kind: ReferenceKind; snippet: string }
  >();
  const outgoing = new Map<
    string,
    { node: typeof knowledgeNodes.$inferSelect; kind: ReferenceKind; snippet: string }
  >();
  const referencedIn = new Map<
    string,
    { node: typeof knowledgeNodes.$inferSelect; kind: ReferenceKind; snippet: string }
  >();
  const fieldReferences = new Map<
    string,
    { node: typeof knowledgeNodes.$inferSelect; kind: ReferenceKind; snippet: string; fieldName?: string }
  >();

  for (const edge of edges) {
    if (edge.targetNodeId === access.node.id) {
      addReference(
        backlinks,
        nodesById.get(edge.sourceNodeId),
        edge.relationType === "inline_ref" ? "inline_ref" : "reference-edge",
        "Inline reference",
        access.node.id,
      );
    }
    if (edge.sourceNodeId === access.node.id) {
      addReference(
        outgoing,
        nodesById.get(edge.targetNodeId),
        edge.relationType === "inline_ref" ? "inline_ref" : "reference-edge",
        "Inline reference",
        access.node.id,
      );
    }
  }

  for (const placement of placements) {
    addReference(
      referencedIn,
      nodesById.get(placement.parentNodeId),
      "placement",
      "Reference placement",
      access.node.id,
    );
  }

  for (const row of fieldRefs) {
    const sourceNode = nodesById.get(row.value.nodeId);
    if (!sourceNode || sourceNode.id === access.node.id) continue;
    fieldReferences.set(`${sourceNode.id}:${row.field.id}`, {
      node: sourceNode,
      kind: "field_ref",
      snippet: `${row.field.name}: ${access.node.title}`,
      fieldName: row.field.name,
    });
  }

  const serialize = (item: {
    node: typeof knowledgeNodes.$inferSelect;
    kind: ReferenceKind;
    snippet: string;
    fieldName?: string;
  }) => ({
    node: serializeNode(item.node),
    kind: item.kind,
    snippet: item.snippet,
    field_name: item.fieldName,
  });

  return NextResponse.json({
    backlinks: Array.from(backlinks.values()).map(serialize),
    referenced_in: Array.from(referencedIn.values()).map(serialize),
    field_refs: Array.from(fieldReferences.values()).map(serialize),
    outgoing: Array.from(outgoing.values()).map(serialize),
  });
}
