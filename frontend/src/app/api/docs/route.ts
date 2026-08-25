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
import { isExplicitBlankParagraph } from "@/lib/docs-block-model";
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
  getDocsNodeAccess,
  requireDocsNode,
  syncKnowledgeNodeReferenceEdges,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import {
  DOCS_NODE_TITLE_MAX,
  docsNodeTitlesMatch,
  insertDocsNode,
  normalizeDocsNodeBodyJson,
  updateDocsNode,
} from "@/lib/server/docs-node-writer";
import {
  ensureProjectInformationHierarchyNode,
  getPersonalDocsLibrary,
  isDefaultInboxProject,
} from "@/lib/server/project-information-hierarchy";
import {
  assertGenericDocsMutationAllowed,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

type DocsListState = NonNullable<Awaited<ReturnType<typeof listDocsState>>>;
type DocsStateFilter = (
  state: DocsListState,
  visibleNodeIds: ReadonlySet<string>,
  visibleWorkspaceIds: ReadonlySet<string>,
) => DocsListState;

/** Never serialize raw state when the ACL relation filter is unavailable. */
function emptyStateAfterAclFailure(state: DocsListState): DocsListState {
  return {
    ...state,
    nodes: [],
    hasChildrenIds: [],
    childCountByParent: {},
    loadedChildrenParentIds: [],
    supertags: [],
    nodeSupertags: [],
    supertagFields: [],
    placements: [],
    fields: [],
    fieldValues: [],
    views: [],
    suggestions: [],
    importJobs: [],
    importItems: [],
    attachments: [],
    edges: [],
    projects: [],
  };
}

async function resolveBatchAccess() {
  try {
    const docsUtilsModule = await import("@/lib/server/knowledge-docs-utils");
    return docsUtilsModule.getDocsNodeAccessMap;
  } catch {
    return undefined;
  }
}

async function resolveStateFilter(): Promise<DocsStateFilter> {
  try {
    const docsUtilsModule = await import("@/lib/server/knowledge-docs-utils");
    if (typeof docsUtilsModule.filterDocsStateToVisibleNodes === "function") {
      return docsUtilsModule.filterDocsStateToVisibleNodes as DocsStateFilter;
    }
  } catch {
    // Keep the fail-closed fallback below for rolling deploys and old mocks.
  }
  return (state) => emptyStateAfterAclFailure(state);
}

async function serializeDocsState(
  state: NonNullable<Awaited<ReturnType<typeof listDocsState>>>,
  user: Awaited<ReturnType<typeof getSession>>,
) {
  const batchAccess = await resolveBatchAccess();
  const accessMap = user
    ? batchAccess
      ? await batchAccess(state.nodes.map((node) => node.id), user)
      : new Map(
          (await Promise.all(
            state.nodes.map((node) => getDocsNodeAccess(node.id, user)),
          ))
            .filter((access): access is NonNullable<typeof access> => Boolean(access))
            .map((access) => [access.node.id, access]),
        )
    : new Map();
  const visibleNodeIds = new Set(accessMap.keys());
  const visibleWorkspaceIds = new Set<string>();
  for (const access of accessMap.values()) {
    if (access.workspace?.id) visibleWorkspaceIds.add(access.workspace.id);
  }
  if (user && state.workspace.ownerUserId === user.id) {
    visibleWorkspaceIds.add(state.workspace.id);
  }
  const stateFilter = await resolveStateFilter();
  let visibleState: DocsListState;
  try {
    visibleState = stateFilter(state, visibleNodeIds, visibleWorkspaceIds);
  } catch {
    visibleState = emptyStateAfterAclFailure(state);
  }
  const library = serializeWorkspace(state.workspace);
  return {
    library,
    docs_library_id: state.workspace.id,
    // ACL races fail closed: do not serialize a body for a node whose access
    // disappeared after listDocsState assembled its candidate set.
    nodes: visibleState.nodes
      .filter((node) => accessMap.has(node.id))
      .map((node) => ({
        ...serializeNode(node),
        permission: accessMap.get(node.id)?.permission,
      })),
    has_children_ids: visibleState.hasChildrenIds,
    loaded_children_parent_ids: visibleState.loadedChildrenParentIds,
    supertags: visibleState.supertags.map(serializeSupertag),
    node_supertags: visibleState.nodeSupertags.map(serializeNodeSupertag),
    supertag_fields: visibleState.supertagFields.map(serializeSupertagField),
    placements: visibleState.placements.map(serializeNodePlacement),
    fields: visibleState.fields.map(serializeField),
    field_values: visibleState.fieldValues.map(serializeFieldValue),
    views: visibleState.views.map(serializeView),
    ai_suggestions: visibleState.suggestions.map(serializeSuggestion),
    import_jobs: visibleState.importJobs.map(serializeImportJob),
    import_items: visibleState.importItems.map(serializeImportItem),
    attachments: visibleState.attachments.map(serializeAttachment),
    edges: visibleState.edges.map(serializeEdge),
    projects: visibleState.projects,
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
    supertagId: searchParams.get("supertag_id"),
    includeArchived: searchParams.get("include_archived") === "1",
  });
  if (!state) {
    return NextResponse.json({ detail: "権限がありません" }, { status: 403 });
  }

  return NextResponse.json(await serializeDocsState(state, user));
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const projectId = cleanOptionalString(body.project_id, 80);
  const bodyText = typeof body.body_text === "string"
    ? body.body_text.slice(0, 200000)
    : "";
  const nodeType = normalizeDocsNodeType(body.node_type);
  let bodyJson: Record<string, unknown>;
  try {
    // Keep rolling-deploy/legacy test doubles that predate the shared writer
    // normalizer fail-closed without making them crash at module load time.
    bodyJson = typeof normalizeDocsNodeBodyJson === "function"
      ? normalizeDocsNodeBodyJson(body.body_json)
      : normalizeJsonObject(body.body_json);
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "body_jsonが不正です" },
      { status: 400 },
    );
  }
  const title =
    typeof body.title === "string"
      ? body.title.slice(0, DOCS_NODE_TITLE_MAX)
      : deriveKnowledgeBlockTitle(bodyText);
  // Validate before ensure* calls: malformed blank requests must not create a
  // personal/project Docs workspace as a side effect.
  if (!title.trim() && !isExplicitBlankParagraph(title, bodyJson, nodeType)) {
    return NextResponse.json(
      { detail: "空行はDocs nodeとして保存できません" },
      { status: 400 },
    );
  }
  let workspace: Awaited<ReturnType<typeof ensureDocsWorkspace>> | null = null;
  let projectAccess: Awaited<ReturnType<typeof ensureProjectWritable>> = null;
  let projectNode: typeof knowledgeNodes.$inferSelect | null = null;
  const requestedId = cleanOptionalString(body.id, 80);
  let parentId = cleanOptionalString(body.parent_id, 80);
  const requestedSortOrder =
    typeof body.sort_order === "number" && Number.isFinite(body.sort_order)
      ? body.sort_order
      : null;
  if (projectId) {
    projectAccess = await ensureProjectWritable(projectId, user);
    if (!projectAccess) {
      return NextResponse.json({ detail: "Projectへの書き込み権限がありません" }, { status: 403 });
    }
    if (isDefaultInboxProject(projectAccess.project)) {
      return NextResponse.json(
        { detail: "Inboxは案件情報Docsの保存先ではありません" },
        { status: 409 },
      );
    }
    projectNode = await ensureProjectInformationHierarchyNode({
      userId: user.id,
      project: projectAccess.project,
    });
    workspace = await getPersonalDocsLibrary(projectAccess.project.ownerId);
    if (!workspace) {
      return NextResponse.json(
        { detail: "Project owner Personal Docs Library could not be resolved" },
        { status: 409 },
      );
    }
    if (!parentId) parentId = projectNode.id;
  } else {
    workspace = await ensureDocsWorkspace(user);
  }
  let parent: typeof knowledgeNodes.$inferSelect | null = null;
  if (parentId) {
    const parentAccess = await requireDocsNode(parentId, user, "write");
    if (!parentAccess) {
      return NextResponse.json({ detail: "親nodeへの書き込み権限がありません" }, { status: 403 });
    }
    if (!projectId) {
      workspace = parentAccess.workspace;
    } else if (parentAccess.workspace.id !== workspace.id) {
      return NextResponse.json({ detail: "Project Docsと別workspaceの親は指定できません" }, { status: 400 });
    }
    const [parentRow] = await db
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.id, parentId),
          eq(knowledgeNodes.docsLibraryId, workspace.id),
        ),
      )
      .limit(1);
    if (!parentRow) {
      return NextResponse.json({ detail: "親nodeが見つかりません" }, { status: 404 });
    }
    parent = parentRow;
    try {
      await assertGenericDocsMutationAllowed(parentRow);
    } catch (error) {
      if (error instanceof ManagedDocsMutationError) {
        return NextResponse.json({ detail: error.message }, { status: error.status });
      }
      throw error;
    }
  }

  if (projectId && parent) {
    // Explicit project writes are constrained to the canonical Project
    // subtree.  In particular, a caller must not attach Project A content to
    // a null-project Home node (or to Project B) merely because both nodes
    // are in the same Personal Library.  `rootPageId` is checked in addition
    // to `project_id` so malformed/stale ordinary nodes cannot become a
    // cross-project bridge.
    if (
      parent.projectId !== projectId ||
      !projectNode ||
      (parent.id !== projectNode.id && parent.rootPageId !== projectNode.rootPageId)
    ) {
      return NextResponse.json(
        { detail: "指定された親nodeはProject Docsの正規サブツリーではありません" },
        { status: 400 },
      );
    }
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

  if (parent && docsNodeTitlesMatch(parent.title, title)) {
    return NextResponse.json(
      { detail: "親と同名の子nodeは作成できません" },
      { status: 409 },
    );
  }

  const rawRequestedSupertagIds: string[] = Array.isArray(body.supertag_ids)
    ? body.supertag_ids.filter(
        (id: unknown): id is string => typeof id === "string",
      )
    : [];
  const requestedSupertagIds = Array.from(
    new Set<string>(rawRequestedSupertagIds),
  );
  const requestedFieldValues = Array.isArray(body.field_values)
    ? body.field_values
    : [];

  const result = await db.transaction(async (tx) => {
    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, workspace.id),
          parentId ? eq(knowledgeNodes.parentId, parentId) : isNull(knowledgeNodes.parentId),
        ),
      );
    const sortOrder = requestedSortOrder ?? (maxRow?.maxSort ?? 0) + 1;

    const node = await insertDocsNode(tx, {
      id: requestedId ?? undefined,
      docsLibraryId: workspace.id,
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
            eq(knowledgeSupertags.docsLibraryId, workspace.id),
            inArray(knowledgeSupertags.id, requestedSupertagIds),
          ),
        );
      if (validTags.length !== requestedSupertagIds.length) {
        throw new Error("指定されたSupertagが見つかりません");
      }
      await tx.insert(knowledgeNodeSupertags).values(
        validTags.map((tag) => ({
          nodeId: finalNode.id,
          supertagId: tag.id,
          createdBy: user.id,
        })),
      );
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
                  eq(knowledgeFields.docsLibraryId, workspace.id),
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

  if (result.title.trim()) {
    try {
      await reconcileDocsTaskBinding({
        user,
        docsLibraryId: workspace.id,
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
  }

  return NextResponse.json({ node: serializeNode(result) }, { status: 201 });
}
