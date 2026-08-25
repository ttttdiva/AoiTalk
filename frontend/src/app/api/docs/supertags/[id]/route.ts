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
  requireDocsSupertag,
  serializeSupertag,
} from "@/lib/server/knowledge-docs-utils";

async function wouldCreateSupertagCycle(
  docsLibraryId: string,
  currentId: string,
  proposedParentId: string,
) {
  const tags = await db
    .select({
      id: knowledgeSupertags.id,
      parentSupertagId: knowledgeSupertags.parentSupertagId,
    })
    .from(knowledgeSupertags)
    .where(eq(knowledgeSupertags.docsLibraryId, docsLibraryId));
  const parentById = new Map(tags.map((tag) => [tag.id, tag.parentSupertagId]));
  let cursor: string | null | undefined = proposedParentId;
  const seen = new Set<string>();
  while (cursor) {
    if (cursor === currentId) return true;
    if (seen.has(cursor)) return true;
    seen.add(cursor);
    cursor = parentById.get(cursor);
  }
  return false;
}

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
  const current = await requireDocsSupertag(id, workspace.id);
  if (!current) {
    return NextResponse.json({ detail: "supertagが見つかりません" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const updates: Partial<typeof knowledgeSupertags.$inferInsert> = {
    updatedAt: new Date(),
  };

  if ("name" in body) updates.name = cleanString(body.name, current.name, 120);
  if ("base_type" in body) {
    updates.baseType = cleanString(body.base_type, current.baseType ?? "note", 40);
  }
  if ("description" in body) updates.description = cleanOptionalString(body.description, 2000);
  if ("icon" in body) updates.icon = cleanOptionalString(body.icon, 64);
  if ("color" in body) updates.color = cleanOptionalString(body.color, 32);
  if ("template_json" in body) updates.templateJson = normalizeJsonObject(body.template_json);
  if ("config_json" in body) updates.configJson = normalizeJsonObject(body.config_json);
  if ("title_template" in body) {
    updates.titleTemplate = cleanOptionalString(body.title_template, 2000);
  }
  if ("ai_instructions" in body) {
    updates.aiInstructions = cleanOptionalString(body.ai_instructions, 5000);
  }
  if ("parent_supertag_id" in body) {
    const parentSupertagId = cleanOptionalString(body.parent_supertag_id, 80);
    if (parentSupertagId === current.id) {
      return NextResponse.json({ detail: "自分自身を親にはできません" }, { status: 400 });
    }
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
      if (await wouldCreateSupertagCycle(workspace.id, current.id, parentSupertagId)) {
        return NextResponse.json({ detail: "supertag継承が循環します" }, { status: 400 });
      }
    }
    updates.parentSupertagId = parentSupertagId;
  }

  const [row] = await db
    .update(knowledgeSupertags)
    .set(updates)
    .where(eq(knowledgeSupertags.id, current.id))
    .returning();

  return NextResponse.json({ supertag: serializeSupertag(row) });
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
  const current = await requireDocsSupertag(id, workspace.id);
  if (!current) {
    return NextResponse.json({ detail: "supertagが見つかりません" }, { status: 404 });
  }
  if (current.systemKey) {
    return NextResponse.json({ detail: "システムsupertagは削除できません" }, { status: 400 });
  }

  await db.delete(knowledgeSupertags).where(eq(knowledgeSupertags.id, current.id));
  return NextResponse.json({ ok: true });
}
