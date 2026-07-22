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
} from "@/lib/server/knowledge-docs-utils";

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

  return NextResponse.json({
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
  });
}
