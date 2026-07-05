import { NextRequest, NextResponse } from "next/server";
import { eq, max } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFields, knowledgeSupertagFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsFieldType } from "@/lib/docs-model";
import {
  cleanString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  requireDocsSupertag,
  serializeField,
} from "@/lib/server/knowledge-docs-utils";

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const supertagId = cleanString(body.supertag_id, "", 80);
  const name = cleanString(body.name, "", 120);
  if (!supertagId || !name) {
    return NextResponse.json({ detail: "supertag_idとnameは必須です" }, { status: 400 });
  }

  const supertag = await requireDocsSupertag(supertagId, workspace.id);
  if (!supertag) {
    return NextResponse.json({ detail: "supertagが見つかりません" }, { status: 404 });
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeFields.sortOrder) })
    .from(knowledgeFields)
    .where(eq(knowledgeFields.supertagId, supertag.id));

  const [row] = await db.transaction(async (tx) => {
    const [created] = await tx
      .insert(knowledgeFields)
      .values({
        workspaceId: workspace.id,
        supertagId: supertag.id,
        name,
        fieldType: normalizeDocsFieldType(body.field_type),
        required: !!body.required,
        optionsJson: normalizeJsonObject(body.options_json),
        defaultValueJson: body.default_value_json ?? null,
        sortOrder: (maxRow?.maxSort ?? 0) + 1,
      })
      .returning();
    await tx
      .insert(knowledgeSupertagFields)
      .values({
        supertagId: supertag.id,
        fieldId: created.id,
        sortOrder: created.sortOrder ?? 0,
        required: !!created.required,
        showInTemplate: true,
        optional: false,
      })
      .onConflictDoNothing();
    return [created];
  });

  return NextResponse.json({ field: serializeField(row) }, { status: 201 });
}
