import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, inArray, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeFields,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  getUserProjects,
  getWorkspaceViews,
  requireDocsNode,
  serializeField,
  serializeFieldValue,
  serializeNode,
  serializeNodeSupertag,
  serializeSupertag,
  serializeView,
} from "@/lib/server/knowledge-docs-utils";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";

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

  const rootPageId = access.node.rootPageId ?? access.node.id;
  const nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.workspaceId, access.workspace.id),
        isNull(knowledgeNodes.archivedAt),
        or(eq(knowledgeNodes.id, rootPageId), eq(knowledgeNodes.rootPageId, rootPageId)),
      ),
    )
    .orderBy(asc(knowledgeNodes.sortOrder));

  const nodeIds = nodes.map((node) => node.id);
  const [supertags, fields, nodeSupertags, storedFieldValues, views, projects] = await Promise.all([
    db
      .select()
      .from(knowledgeSupertags)
      .where(eq(knowledgeSupertags.workspaceId, access.workspace.id))
      .orderBy(asc(knowledgeSupertags.name)),
    db
      .select()
      .from(knowledgeFields)
      .where(eq(knowledgeFields.workspaceId, access.workspace.id))
      .orderBy(asc(knowledgeFields.sortOrder), asc(knowledgeFields.name)),
    nodeIds.length
      ? db
          .select()
          .from(knowledgeNodeSupertags)
          .where(inArray(knowledgeNodeSupertags.nodeId, nodeIds))
      : Promise.resolve([]),
    nodeIds.length
      ? db
          .select()
          .from(knowledgeFieldValues)
          .where(inArray(knowledgeFieldValues.nodeId, nodeIds))
      : Promise.resolve([]),
    getWorkspaceViews(access.workspace.id),
    getUserProjects(user.id),
  ]);
  const taskFieldValues = nodeIds.length
    ? await listDocsTaskSyntheticFieldValues({ nodeIds, fields })
    : [];
  const fieldValues = [...storedFieldValues, ...taskFieldValues];

  return NextResponse.json({
    focus_node_id: access.node.id,
    root_page_id: rootPageId,
    nodes: nodes.map(serializeNode),
    supertags: supertags.map(serializeSupertag),
    fields: fields.map(serializeField),
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: fieldValues.map(serializeFieldValue),
    views: views.map(serializeView),
    projects,
  });
}
