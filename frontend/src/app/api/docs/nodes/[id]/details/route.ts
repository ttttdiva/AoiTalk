import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeAttachments,
  knowledgeFields,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";
import {
  getKnowledgeNodeChildMetadata,
  getDocsNodeAccess,
  serializeFieldValue,
  serializeAttachment,
  serializeNode,
  serializeNodeWithoutBody,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";
import { jsonWithConditional } from "@/lib/server/http-cache";

function visibleBodyJson(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const body = { ...(value as Record<string, unknown>) };
  // Legacy ClipIngest payloads are migration input, not user-visible content.
  // Never reintroduce the old readonly representation through the details API.
  delete body.verbatim_blocks;
  delete body.verbatim_content;
  return body;
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });

  const { id } = await params;
  const access = await getDocsNodeAccess(id, user);
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const [nodeSupertagRows, fieldValueRows, attachments, taskFields, childMetadata] = await Promise.all([
    db
      .select({
        relation: knowledgeNodeSupertags,
        supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
      })
      .from(knowledgeNodeSupertags)
      .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
      .where(
        and(
          eq(knowledgeNodeSupertags.nodeId, id),
          eq(knowledgeSupertags.docsLibraryId, access.workspace.id),
        ),
      )
      .then((rows) => rows
        .filter((row) => row.supertagWorkspaceId === access.workspace.id)
        .map((row) => row.relation)),
    db
      .select({
        value: knowledgeFieldValues,
        fieldWorkspaceId: knowledgeFields.docsLibraryId,
      })
      .from(knowledgeFieldValues)
      .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
      .where(
        and(
          eq(knowledgeFieldValues.nodeId, id),
          eq(knowledgeFields.docsLibraryId, access.workspace.id),
        ),
      )
      .then((rows) => rows
        .filter((row) => row.fieldWorkspaceId === access.workspace.id)
        .map((row) => row.value)),
    db.select().from(knowledgeAttachments).where(eq(knowledgeAttachments.nodeId, id)),
    db
      .select({ id: knowledgeFields.id, systemKey: knowledgeFields.systemKey })
      .from(knowledgeFields)
      .where(and(
        eq(knowledgeFields.docsLibraryId, access.workspace.id),
        inArray(knowledgeFields.systemKey, ["task_status", "task_due", "task_start", "task_priority", "task_project"]),
      )),
    getKnowledgeNodeChildMetadata(
      access.workspace.id,
      [id],
      null,
      false,
      user,
    ),
  ]);
  const taskFieldValues = await listDocsTaskSyntheticFieldValues({
    nodeIds: [id],
    fields: taskFields,
    user,
  });
  const storedFieldValues = fieldValueRows;
  const targetIds = Array.from(new Set(
    storedFieldValues
      .map((value) => value.targetNodeId)
      .filter((value): value is string => Boolean(value)),
  ));
  const targetAccessRows = await Promise.all(
    targetIds.map((targetId) => getDocsNodeAccess(targetId, user)),
  );
  const targetAccessById = new Map(
    targetAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .filter((item) => item.workspace.id === access.workspace.id)
      .map((item) => [item.node.id, item]),
  );
  // A field value may reference a target in another library or a node the
  // actor cannot read.  Drop that value entirely rather than returning a
  // foreign target UUID as metadata.
  const visibleStoredFieldValues = storedFieldValues.filter(
    (value) => !value.targetNodeId || targetAccessById.has(value.targetNodeId),
  );
  const fieldValues = [...visibleStoredFieldValues, ...taskFieldValues];
  const nodeSupertags = nodeSupertagRows;
  const serializedNode = {
    ...serializeNode(access.node),
    permission: access.permission,
  };
  const hasDetails = Boolean(
    serializedNode.body_json.bookmark
    || fieldValues.length > 0
    || attachments.length > 0,
  );
  const referencedAccess = Array.from(targetAccessById.values());
  const referencedIds = referencedAccess
    .map((item) => item.node.id);
  const referencedNodes = referencedIds.length
    ? await db
        .select()
        .from(knowledgeNodes)
        .where(
          and(
            eq(knowledgeNodes.docsLibraryId, access.workspace.id),
            isNull(knowledgeNodes.archivedAt),
            inArray(knowledgeNodes.id, referencedIds),
          ),
        )
    : [];

  return jsonWithConditional(request, {
    nodes: [
      { ...serializedNode, body_json: visibleBodyJson(serializedNode.body_json) },
      ...referencedNodes.map((node) => ({
        ...serializeNodeWithoutBody(node),
        permission: "read" as const,
      })),
    ],
    node_supertags: nodeSupertags.map(serializeNodeSupertag),
    field_values: fieldValues.map(serializeFieldValue),
    attachments: attachments.map(serializeAttachment),
    has_children_ids: childMetadata.hasChildrenIds,
    has_details_ids: hasDetails ? [id] : [],
    details_loaded_ids: [id],
  });
}
