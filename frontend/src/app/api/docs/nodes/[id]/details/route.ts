import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, or } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeAttachments,
  knowledgeFields,
  knowledgeNodes,
  knowledgeNodeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";
import {
  getKnowledgeNodeChildMetadata,
  getUserProjects,
  requireDocsNode,
  serializeFieldValue,
  serializeAttachment,
  serializeNode,
  serializeNodeWithoutBody,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const access = await requireDocsNode(id, user, "read");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const projects = await getUserProjects(user.id);
  const accessibleProjectIds = projects.map((project) => project.id);
  const [nodeSupertags, storedFieldValues, attachments, taskFields, childMetadata] = await Promise.all([
    db.select().from(knowledgeNodeSupertags).where(eq(knowledgeNodeSupertags.nodeId, id)),
    db.select().from(knowledgeFieldValues).where(eq(knowledgeFieldValues.nodeId, id)),
    db.select().from(knowledgeAttachments).where(eq(knowledgeAttachments.nodeId, id)),
    db
      .select({ id: knowledgeFields.id, systemKey: knowledgeFields.systemKey })
      .from(knowledgeFields)
      .where(and(
        eq(knowledgeFields.workspaceId, access.workspace.id),
        inArray(knowledgeFields.systemKey, ["task_status", "task_due", "task_start", "task_priority", "task_project"]),
      )),
    getKnowledgeNodeChildMetadata(access.workspace.id, [id], accessibleProjectIds),
  ]);
  const taskFieldValues = await listDocsTaskSyntheticFieldValues({ nodeIds: [id], fields: taskFields });
  const fieldValues = [...storedFieldValues, ...taskFieldValues];
  const serializedNode = serializeNode(access.node);
  const hasDetails = Boolean(
    typeof serializedNode.body_json.verbatim_content === "string"
    || serializedNode.body_json.bookmark
    || fieldValues.length > 0
    || attachments.length > 0,
  );
  const targetIds = Array.from(new Set(
    storedFieldValues.map((value) => value.targetNodeId).filter((value): value is string => Boolean(value)),
  ));
  const referencedNodes = targetIds.length
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(and(
          eq(knowledgeNodes.workspaceId, access.workspace.id),
          isNull(knowledgeNodes.archivedAt),
          accessibleProjectIds.length > 0
            ? or(isNull(knowledgeNodes.projectId), inArray(knowledgeNodes.projectId, accessibleProjectIds))
            : isNull(knowledgeNodes.projectId),
          inArray(knowledgeNodes.id, targetIds),
        ))
    : [];

  return NextResponse.json({
    nodes: [
      serializedNode,
      ...referencedNodes.map(serializeNodeWithoutBody),
    ],
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: fieldValues.map(serializeFieldValue),
    attachments: attachments.map(serializeAttachment),
    has_children_ids: childMetadata.hasChildrenIds,
    has_details_ids: hasDetails ? [id] : [],
    details_loaded_ids: [id],
  });
}
