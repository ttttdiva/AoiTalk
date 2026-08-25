import { NextRequest, NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeSupertags } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  cleanString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  serializeSupertag,
} from "@/lib/server/knowledge-docs-utils";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const name = cleanString(body.name, "", 120);
  if (!name) {
    return NextResponse.json({ detail: "nameは必須です" }, { status: 400 });
  }
  const parentSupertagId = cleanOptionalString(body.parent_supertag_id, 80);
  if (parentSupertagId) {
    const [parent] = await db
      .select({ id: knowledgeSupertags.id })
      .from(knowledgeSupertags)
      .where(
        and(
          eq(knowledgeSupertags.id, parentSupertagId),
          eq(knowledgeSupertags.docsLibraryId, workspace.id),
        ),
      )
      .limit(1);
    if (!parent) {
      return NextResponse.json({ detail: "親supertagが見つかりません" }, { status: 404 });
    }
  }

  const [row] = await db
    .insert(knowledgeSupertags)
    .values({
      docsLibraryId: workspace.id,
      parentSupertagId,
      name,
      baseType: cleanString(body.base_type, "note", 40),
      description: cleanOptionalString(body.description, 2000),
      icon: cleanOptionalString(body.icon, 64),
      color: cleanOptionalString(body.color, 32),
      templateJson: normalizeJsonObject(body.template_json),
      configJson: normalizeJsonObject(body.config_json),
      titleTemplate: cleanOptionalString(body.title_template, 2000),
      aiInstructions: cleanOptionalString(body.ai_instructions, 5000),
    })
    .returning();

  return NextResponse.json({ supertag: serializeSupertag(row) }, { status: 201 });
}
