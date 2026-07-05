import { NextRequest, NextResponse } from "next/server";
import { and, eq } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeSavedViews } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  serializeView,
} from "@/lib/server/knowledge-docs-utils";

const VIEW_LAYOUTS = new Set(["list", "table", "board", "calendar", "cards"]);

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
  const updates: Partial<typeof knowledgeSavedViews.$inferInsert> = {
    updatedAt: new Date(),
  };

  if ("name" in body) {
    const name = cleanOptionalString(body.name, 200);
    if (!name) return NextResponse.json({ detail: "nameが必要です" }, { status: 400 });
    updates.name = name;
  }
  if ("layout" in body) {
    const layout = cleanOptionalString(body.layout, 40) ?? "";
    if (!VIEW_LAYOUTS.has(layout)) return NextResponse.json({ detail: "layoutが不正です" }, { status: 400 });
    updates.layout = layout;
  }
  if ("config_json" in body) updates.configJson = normalizeJsonObject(body.config_json);
  if (typeof body.sort_order === "number" && Number.isFinite(body.sort_order)) updates.sortOrder = body.sort_order;

  const [view] = await db
    .update(knowledgeSavedViews)
    .set(updates)
    .where(
      and(
        eq(knowledgeSavedViews.id, id),
        eq(knowledgeSavedViews.workspaceId, workspace.id),
      ),
    )
    .returning();
  if (!view) {
    return NextResponse.json({ detail: "Saved viewが見つかりません" }, { status: 404 });
  }

  return NextResponse.json({ view: serializeView(view) });
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
  const [view] = await db
    .delete(knowledgeSavedViews)
    .where(
      and(
        eq(knowledgeSavedViews.id, id),
        eq(knowledgeSavedViews.workspaceId, workspace.id),
      ),
    )
    .returning();
  if (!view) {
    return NextResponse.json({ detail: "Saved viewが見つかりません" }, { status: 404 });
  }

  return NextResponse.json({ view: serializeView(view) });
}
