import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { knowledgeAiSuggestions } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  cleanString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  normalizeSuggestionStatus,
  requireDocsNode,
  serializeSuggestion,
} from "@/lib/server/knowledge-docs-utils";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const nodeId = cleanOptionalString(body.node_id, 80);
  if (nodeId) {
    const nodeAccess = await requireDocsNode(nodeId, user, "read");
    if (!nodeAccess || nodeAccess.workspace.id !== workspace.id) {
      return NextResponse.json({ detail: "nodeが見つかりません" }, { status: 404 });
    }
  }

  const suggestionType = cleanString(body.suggestion_type, "", 80);
  if (!suggestionType) {
    return NextResponse.json({ detail: "suggestion_typeは必須です" }, { status: 400 });
  }

  const confidence =
    body.confidence === null || body.confidence === undefined
      ? null
      : Number(body.confidence);
  const [row] = await db
    .insert(knowledgeAiSuggestions)
    .values({
      docsLibraryId: workspace.id,
      nodeId,
      suggestionType,
      payloadJson: normalizeJsonObject(body.payload_json),
      status: normalizeSuggestionStatus(body.status),
      confidence: Number.isFinite(confidence) ? confidence : null,
      createdBy: user.id,
    })
    .returning();

  return NextResponse.json({ suggestion: serializeSuggestion(row) }, { status: 201 });
}
