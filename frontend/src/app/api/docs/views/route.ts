import { NextRequest, NextResponse } from "next/server";
import { and, eq, max } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeSavedViews, knowledgeSupertags } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  cleanOptionalString,
  ensureDocsWorkspace,
  normalizeJsonObject,
  serializeView,
} from "@/lib/server/knowledge-docs-utils";

const VIEW_LAYOUTS = new Set(["list", "table", "board", "calendar", "cards"]);

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));
  const workspace = await ensureDocsWorkspace(user);
  const supertagId = cleanOptionalString(body.supertag_id, 80);
  const name = cleanOptionalString(body.name, 200);
  const requestedLayout = cleanOptionalString(body.layout, 40) ?? "list";
  const layout = VIEW_LAYOUTS.has(requestedLayout) ? requestedLayout : "list";

  if (!supertagId) {
    return NextResponse.json({ detail: "supertag_idが必要です" }, { status: 400 });
  }
  if (!name) {
    return NextResponse.json({ detail: "nameが必要です" }, { status: 400 });
  }

  const [tag] = await db
    .select()
    .from(knowledgeSupertags)
    .where(
      and(
        eq(knowledgeSupertags.id, supertagId),
        eq(knowledgeSupertags.workspaceId, workspace.id),
      ),
    )
    .limit(1);
  if (!tag) {
    return NextResponse.json({ detail: "Supertagが見つかりません" }, { status: 404 });
  }

  const [maxRow] = await db
    .select({ maxSort: max(knowledgeSavedViews.sortOrder) })
    .from(knowledgeSavedViews)
    .where(
      and(
        eq(knowledgeSavedViews.workspaceId, workspace.id),
        eq(knowledgeSavedViews.supertagId, supertagId),
      ),
    );
  const sortOrder = (maxRow?.maxSort ?? 0) + 1;

  const [view] = await db
    .insert(knowledgeSavedViews)
    .values({
      workspaceId: workspace.id,
      supertagId,
      name,
      layout,
      configJson: normalizeJsonObject(body.config_json),
      sortOrder,
      createdBy: user.id,
    })
    .returning();

  return NextResponse.json({ view: serializeView(view) });
}
