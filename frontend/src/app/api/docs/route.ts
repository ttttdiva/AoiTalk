import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFieldValues,
  knowledgeFields,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsNodeType } from "@/lib/docs-model";
import { reconcileDocsTaskBinding } from "@/lib/server/docs-task-binding";
import {
  appendKnowledgeRevision,
  cleanOptionalString,
  deriveKnowledgeBlockTitle,
  ensureDocsWorkspace,
  ensureProjectWritable,
  listDocsState,
  normalizeFieldValueInput,
  normalizeJsonObject,
  serializeAttachment,
  serializeEdge,
  serializeField,
  serializeFieldValue,
  serializeImportItem,
  serializeImportJob,
  serializeNode,
  serializeNodePlacement,
  serializeNodeSupertag,
  serializeSupertagField,
  serializeSuggestion,
  serializeSupertag,
  serializeView,
  serializeWorkspace,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { DOCS_NODE_TITLE_MAX, insertDocsNode, updateDocsNode } from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  isDefaultInboxProject,
} from "@/lib/server/project-information-hierarchy";

function serializeDocsState(state: NonNullable<Awaited<ReturnType<typeof listDocsState>>>) {
  return {
    workspace: serializeWorkspace(state.workspace),
    nodes: state.nodes.map(serializeNode),
    has_children_ids: state.hasChildrenIds,
    loaded_children_parent_ids: state.loadedChildrenParentIds,
    supertags: state.supertags.map(serializeSupertag),
    node_supertags: state.nodeSupertags.map(serializeNodeSupertag),
    supertag_fields: state.supertagFields.map(serializeSupertagField),
    placements: state.placements.map(serializeNodePlacement),
    fields: state.fields.map(serializeField),
    field_values: state.fieldValues.map(serializeFieldValue),
    views: state.views.map(serializeView),
    ai_suggestions: state.suggestions.map(serializeSuggestion),
    import_jobs: state.importJobs.map(serializeImportJob),
    import_items: state.importItems.map(serializeImportItem),
    attachments: state.attachments.map(serializeAttachment),
    edges: state.edges.map(serializeEdge),
    projects: state.projects,
  };
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const searchParams = request.nextUrl.searchParams;
  const state = await listDocsState(user, {
    search: searchParams.get("q"),
    projectId: searchParams.get("project_id"),
    supertagId: searchParams.get("supertag_id"),
    includeArchived: searchParams.get("include_archived") === "1",
  });
  if (!state) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  return NextResponse.json(serializeDocsState(state));
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const bodyText = typeof body.body_text === "string"
    ? body.body_text.slice(0, 200000)
    : "";
  const bodyJson = normalizeJsonObject(body.body_json);
  const nodeType = normalizeDocsNodeType(body.node_type);
  const title =
    typeof body.title === "string"
      ? body.title.slice(0, DOCS_NODE_TITLE_MAX)
      : deriveKnowledgeBlockTitle(bodyText);
  const requestedId = cleanOptionalString(body.id, 80);
  let parentId = cleanOptionalString(body.parent_id, 80);
  const projectId = cleanOptionalString(body.project_id, 80);
  const requestedSortOrder =
    typeof body.sort_order === "number" && Number.isFinite(body.sort_order)
      ? body.sort_order
      : null;

  if (projectId) {
    const access = await ensureProjectWritable(projectId, user);
    if (!access) {
      return NextResponse.json({ detail: "Projectへの書き込み権限がありません" }, { status: 403 });
    }
    if (isDefaultInboxProject(access.project)) {
      return NextResponse.json(
        { detail: "Inboxは案件情報Docsの保存先ではありません" },
        { status: 409 },
      );
    }
    if (!parentId) {
      const projectNode = await ensureProjectInformationHierarchyNode({
        workspaceId: workspace.id,
        userId: user.id,
        project: access.project,
      });
      parentId = projectNode.id;
    }
  }

  let parent: typeof knowledgeNodes.$inferSelect | null = null;
  if (parentId) {
    const [parentRow] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, parentId),
          eq(knowledgeNodes.workspaceId, workspace.id),
        ),
      )
      .limit(1);
    if (!parentRow) {
      return NextResponse.json({ detail: "親nodeが見つかりません" }, { status: 404 });
    }
    parent = parentRow;
  }

  if (parent?.projectId) {
    const access = await ensureProjectWritable(parent.projectId, user);
    if (!access) {
      return NextResponse.json({ detail: "親nodeのProject書き込み権限がありません" }, { status: 403 });
    }
    if (isDefaultInboxProject(access.project)) {
      return NextResponse.json(
        { detail: "InboxはDocsの案件保存先ではありません" },
        { status: 409 },
      );
    }
    if (projectId && projectId !== parent.projectId) {
      return NextResponse.json(
        { detail: "親nodeと異なるProjectには関連付けられません" },
        { status: 400 },
      );
    }
  }

  const requestedSupertagIds = Array.isArray(body.supertag_ids)
    ? body.supertag_ids.filter((id: unknown): id is string => typeof id === "string")
    : [];
  const requestedFieldValues = Array.isArray(body.field_values)
    ? body.field_values
    : [];

  const result = await db.transaction(async (tx) => {
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.workspaceId, workspace.id),
          parentId ? eq(knowledgeNodes.parentId, parentId) : isNull(knowledgeNodes.parentId),
        ),
      );
    const sortOrder = requestedSortOrder ?? (maxRow?.maxSort ?? 0) + 1;

    const node = await insertDocsNode(tx, {
      id: requestedId ?? undefined,
      workspaceId: workspace.id,
      parentId,
      rootPageId: parent?.rootPageId ?? parent?.id ?? null,
      projectId: projectId ?? parent?.projectId ?? null,
      title,
      description: cleanOptionalString(body.description, 200000) ?? "",
      bodyJson,
      nodeType,
      displayProps: normalizeJsonObject(body.display_props),
      queryJson: nodeType === "search" ? normalizeJsonObject(body.query_json) : null,
      viewJson: normalizeJsonObject(body.view_json),
      dayDate: cleanOptionalString(body.day_date, 40) ?? null,
      sortOrder,
      createdBy: user.id,
      updatedBy: user.id,
    });

    const finalRootPageId = !node.parentId ? node.id : node.rootPageId;
    const finalNode =
      finalRootPageId !== node.rootPageId
        ? await updateDocsNode(tx, node.id, { rootPageId: finalRootPageId, updatedBy: user.id, updatedAt: new Date() })
        : node;

    await upsertKnowledgeSearchIndex(tx, finalNode, finalNode.title);
    await appendKnowledgeRevision(tx, finalNode, user.id, "nodeを作成");

    if (requestedSupertagIds.length > 0) {
      const validTags = await tx
        .select({ id: knowledgeSupertags.id })
        .from(knowledgeSupertags)
        .where(
          and(
            eq(knowledgeSupertags.workspaceId, workspace.id),
            inArray(knowledgeSupertags.id, requestedSupertagIds),
          ),
        );
      if (validTags.length > 0) {
        await tx.insert(knowledgeNodeSupertags).values(
          validTags.map((tag) => ({
            nodeId: finalNode.id,
            supertagId: tag.id,
            createdBy: user.id,
          })),
        );
      }
    }

    if (requestedFieldValues.length > 0) {
      const fieldIds = requestedFieldValues
        .map((item: unknown) =>
          item && typeof item === "object"
            ? (item as Record<string, unknown>).field_id
            : null,
        )
        .filter((id: unknown): id is string => typeof id === "string");
      const fields =
        fieldIds.length > 0
          ? await tx
              .select()
              .from(knowledgeFields)
              .where(
                and(
                  eq(knowledgeFields.workspaceId, workspace.id),
                  inArray(knowledgeFields.id, fieldIds),
                ),
              )
          : [];
      const fieldsById = new Map(fields.map((field) => [field.id, field]));
      const values = requestedFieldValues.flatMap((item: unknown) => {
        if (!item || typeof item !== "object") return [];
        const record = item as Record<string, unknown>;
        const fieldId = typeof record.field_id === "string" ? record.field_id : "";
        const field = fieldsById.get(fieldId);
        if (!field) return [];
        return [
          {
            ...normalizeFieldValueInput(field, record.value),
            nodeId: finalNode.id,
            updatedBy: user.id,
          },
        ];
      });
      if (values.length > 0) {
        await tx.insert(knowledgeFieldValues).values(values);
      }
    }

    await syncKnowledgeNodeReferenceEdges(tx, finalNode, user.id);

    return finalNode;
  });

  try {
    await reconcileDocsTaskBinding({
      user,
      workspaceId: workspace.id,
      node: result,
      previousSupertagIds: [],
      nextSupertagIds: requestedSupertagIds,
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: "Docs nodeは作成されましたが、タスク連携に失敗しました",
        error: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }

  return NextResponse.json({ node: serializeNode(result) }, { status: 201 });
}
