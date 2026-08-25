import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import {
  listDocsState,
  serializeAttachment,
  serializeEdge,
  serializeField,
  serializeFieldValue,
  serializeImportItem,
  serializeImportJob,
  serializeNode,
  serializeNodePlacement,
  serializeNodeSupertag,
  serializeSupertag,
  serializeSupertagField,
  serializeSuggestion,
  serializeView,
  serializeWorkspace,
  getDocsNodeAccess,
} from "@/lib/server/knowledge-docs-utils";
import { jsonWithConditional } from "@/lib/server/http-cache";

type DocsListState = NonNullable<Awaited<ReturnType<typeof listDocsState>>>;
type DocsStateFilter = (
  state: DocsListState,
  visibleNodeIds: ReadonlySet<string>,
  visibleWorkspaceIds: ReadonlySet<string>,
) => DocsListState;

/**
 * A missing/throwing ACL relation filter must never fall back to the raw
 * state.  Keep the workspace shell for API compatibility, but clear every
 * row collection that could carry node-derived or project metadata.
 */
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
    // Older route-test mocks/deploys may not expose the optional batch helper;
    // retain the per-node compatibility path in that case.
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
    // A rolling deploy/mock may not expose the relation filter.  Continue with
    // the fail-closed filter below rather than serializing unfiltered state.
  }
  return (state) => emptyStateAfterAclFailure(state);
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

  const batchAccess = await resolveBatchAccess();
  const nodeAccessMap = batchAccess
    ? await batchAccess(state.nodes.map((node) => node.id), user)
    : new Map(
        (await Promise.all(
          state.nodes.map((node) => getDocsNodeAccess(node.id, user)),
        ))
          .filter((access): access is NonNullable<typeof access> => Boolean(access))
          .map((access) => [access.node.id, access]),
      );
  const visibleNodeIds = new Set(nodeAccessMap.keys());
  const visibleWorkspaceIds = new Set<string>();
  for (const access of nodeAccessMap.values()) {
    if (access.workspace?.id) visibleWorkspaceIds.add(access.workspace.id);
  }
  if (state.workspace.ownerUserId === user.id) visibleWorkspaceIds.add(state.workspace.id);
  const stateFilter = await resolveStateFilter();
  let visibleState: DocsListState;
  try {
    visibleState = stateFilter(state, visibleNodeIds, visibleWorkspaceIds);
  } catch {
    visibleState = emptyStateAfterAclFailure(state);
  }
  const serializedSupertags = visibleState.supertags.map(serializeSupertag);
  const library = serializeWorkspace(state.workspace);
  const payload = {
    library,
    docs_library_id: state.workspace.id,
    // A node that races out of ACL visibility is omitted entirely.  Falling
    // back to `read` for a null ACL while serializing the decrypted body would
    // leak private content to a caller whose share was just revoked.
    nodes: visibleState.nodes
      .filter((node) => nodeAccessMap.has(node.id))
      .map((node) => ({
        ...serializeNode(node),
        permission: nodeAccessMap.get(node.id)?.permission ?? "read",
      })),
    has_children_ids: visibleState.hasChildrenIds,
    loaded_children_parent_ids: visibleState.loadedChildrenParentIds,
    supertags: serializedSupertags,
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
  // 既存workspaceのseed処理は既定supertagのupdated_atだけを毎回更新する。
  // 実データが同一でもETagが変わらないよう、検証子からその揮発値だけを除外する。
  const stableSupertags = serializedSupertags.map((supertag) =>
    Object.fromEntries(
      Object.entries(supertag).filter(([key]) => key !== "updated_at"),
    ),
  );

  return jsonWithConditional(request, payload, {
    etagSource: { ...payload, supertags: stableSupertags },
  });
}
