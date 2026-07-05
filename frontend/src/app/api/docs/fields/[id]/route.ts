import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { normalizeDocsFieldType } from "@/lib/docs-model";
import {
  cleanString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  requireDocsField,
  serializeField,
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
  const workspace = await ensureDocsWorkspace(user);
  const current = await requireDocsField(id, workspace.id);
  if (!current) {
    return NextResponse.json({ detail: "fieldが見つかりません" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const updates: Partial<typeof knowledgeFields.$inferInsert> = {
    updatedAt: new Date(),
  };

  if ("name" in body) updates.name = cleanString(body.name, current.name, 120);
  if ("field_type" in body) updates.fieldType = normalizeDocsFieldType(body.field_type);
  if ("required" in body) updates.required = !!body.required;
  if ("options_json" in body) updates.optionsJson = normalizeJsonObject(body.options_json);
  if ("default_value_json" in body) updates.defaultValueJson = body.default_value_json ?? null;
  if ("sort_order" in body) {
    const sortOrder = Number(body.sort_order);
    if (!Number.isFinite(sortOrder)) {
      return NextResponse.json({ detail: "sort_orderが不正です" }, { status: 400 });
    }
    updates.sortOrder = sortOrder;
  }

  const [row] = await db
    .update(knowledgeFields)
    .set(updates)
    .where(eq(knowledgeFields.id, current.id))
    .returning();

  return NextResponse.json({ field: serializeField(row) });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const workspace = await ensureDocsWorkspace(user);
  const current = await requireDocsField(id, workspace.id);
  if (!current) {
    return NextResponse.json({ detail: "fieldが見つかりません" }, { status: 404 });
  }

  await db.delete(knowledgeFields).where(eq(knowledgeFields.id, current.id));
  return NextResponse.json({ ok: true });
}
