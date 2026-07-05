import { NextRequest, NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeAiSuggestions } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  ensureDocsWorkspace,
  normalizeSuggestionStatus,
  serializeSuggestion,
} from "@/lib/server/knowledge-docs-utils";

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const [row] = await db
    .update(knowledgeAiSuggestions)
    .set({
      status: normalizeSuggestionStatus(body.status),
      updatedAt: new Date(),
    })
    .where(
      and(
        eq(knowledgeAiSuggestions.id, id),
        eq(knowledgeAiSuggestions.workspaceId, workspace.id),
      ),
    )
    .returning();

  if (!row) {
    return NextResponse.json({ detail: "AI提案が見つかりません" }, { status: 404 });
  }

  return NextResponse.json({ suggestion: serializeSuggestion(row) });
}
