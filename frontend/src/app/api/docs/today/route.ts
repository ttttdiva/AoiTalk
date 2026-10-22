import { NextRequest, NextResponse } from "next/server";
import { and, asc, eq, isNull, max, sql } from "drizzle-orm";
import { docsTodayDate, docsIsoWeekNumber } from "@/lib/docs-daily-calendar";
import { db } from "@/db";
import {
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  appendKnowledgeRevision,
  ensureDocsWorkspace,
  serializeNode,
  serializeNodeSupertag,
  serializeSupertag,
  upsertKnowledgeSearchIndex,
} from "@/lib/server/knowledge-docs-utils";
import { insertDocsNode, updateDocsNode } from "@/lib/server/docs-node-writer";

function dateFromIsoInTokyo(value: string) {
  return new Date(`${value}T00:00:00+09:00`);
}

function requestedIsoDate(request: NextRequest) {
  const requested = request.nextUrl.searchParams.get("date");
  if (requested && /^\d{4}-\d{2}-\d{2}$/.test(requested)) {
    const date = dateFromIsoInTokyo(requested);
    if (!Number.isNaN(date.getTime()) && docsTodayDate(date) === requested)
      return requested;
  }
  return docsTodayDate();
}

type DocsTransaction = Parameters<Parameters<typeof db.transaction>[0]>[0];

async function ensureSystemNode(
  tx: DocsTransaction,
  docsLibraryId: string,
  title: string,
  parentId: string | null,
  sortOrder: number,
  userId: string,
) {
  const [existing] = await tx
    .select()
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.docsLibraryId, docsLibraryId),
        eq(knowledgeNodes.title, title),
        parentId
          ? eq(knowledgeNodes.parentId, parentId)
          : isNull(knowledgeNodes.parentId),
        isNull(knowledgeNodes.archivedAt),
      ),
    )
    .orderBy(asc(knowledgeNodes.createdAt))
    .limit(1);
  if (existing) return existing;
  const created = await insertDocsNode(tx, {
    docsLibraryId,
    parentId,
    rootPageId: parentId,
    title,
    description: "",
    bodyJson: { inline: [{ type: "text", text: title }] },
    nodeType: "system",
    displayProps: {},
    viewJson: {},
    sortOrder,
    createdBy: userId,
    updatedBy: userId,
  });
  return created;
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const workspace = await ensureDocsWorkspace(user);
  const today = requestedIsoDate(request);
  const targetDate = dateFromIsoInTokyo(today);
  const title = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "short",
    day: "numeric",
    weekday: "short",
  }).format(targetDate);

  return db.transaction(async (tx) => {
    // Both Python Daily capture and Web Today lock this row before looking up
    // any ancestors/Day tag. One transaction makes same-day creation atomic.
    await tx.execute(
      sql`SELECT id FROM docs_libraries WHERE id = ${workspace.id} FOR NO KEY UPDATE`,
    );

    const dayTag = await (async () => {
      const [existing] = await tx
        .select()
        .from(knowledgeSupertags)
        .where(
          and(
            eq(knowledgeSupertags.docsLibraryId, workspace.id),
            eq(knowledgeSupertags.name, "Day"),
          ),
        )
        .limit(1);
      if (existing) return existing;
      const [created] = await tx
        .insert(knowledgeSupertags)
        .values({
          docsLibraryId: workspace.id,
          name: "Day",
          baseType: "note",
          description: "Daily note",
          icon: "calendar-days",
          color: "#38bdf8",
          templateJson: { format: "outline_template", nodes: [] },
          pinnedFieldIds: [],
          configJson: { pinned: true },
        })
        .returning();
      return created;
    })();

    const dailyRoot = await ensureSystemNode(
      tx,
      workspace.id,
      "Daily notes",
      null,
      10,
      user.id,
    );
    const year = today.slice(0, 4);
    const yearRoot = await ensureSystemNode(
      tx,
      workspace.id,
      year,
      dailyRoot.id,
      Number(year),
      user.id,
    );
    const weekRoot = await ensureSystemNode(
      tx,
      workspace.id,
      `Week ${String(docsIsoWeekNumber(today)).padStart(2, "0")}`,
      yearRoot.id,
      docsIsoWeekNumber(today),
      user.id,
    );

    const [existingDay] = await tx
      .select()
      .from(knowledgeNodes)
      .where(
        and(
          eq(knowledgeNodes.docsLibraryId, workspace.id),
          eq(knowledgeNodes.dayDate, today),
          isNull(knowledgeNodes.archivedAt),
        ),
      )
      .limit(1);
    if (existingDay) {
      const normalizedDay =
        existingDay.parentId !== weekRoot.id ||
        existingDay.rootPageId !== dailyRoot.id
          ? ((await updateDocsNode(tx, existingDay.id, {
              parentId: weekRoot.id,
              rootPageId: dailyRoot.id,
              updatedBy: user.id,
              updatedAt: new Date(),
            })) ?? existingDay)
          : existingDay;
      const tags = await tx
        .select({
          relation: knowledgeNodeSupertags,
          supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
        })
        .from(knowledgeNodeSupertags)
        .innerJoin(
          knowledgeSupertags,
          eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id),
        )
        .where(
          and(
            eq(knowledgeNodeSupertags.nodeId, normalizedDay.id),
            eq(knowledgeSupertags.docsLibraryId, workspace.id),
          ),
        )
        .then((rows) =>
          rows
            .filter((row) => row.supertagWorkspaceId === workspace.id)
            .map((row) => row.relation),
        );
      return NextResponse.json({
        node: serializeNode(normalizedDay),
        supertag: serializeSupertag(dayTag),
        node_supertags: tags.map(serializeNodeSupertag),
      });
    }

    const [maxRow] = await tx
      .select({ maxSort: max(knowledgeNodes.sortOrder) })
      .from(knowledgeNodes)
      .where(eq(knowledgeNodes.parentId, weekRoot.id));
    const node = await insertDocsNode(tx, {
      docsLibraryId: workspace.id,
      parentId: weekRoot.id,
      rootPageId: dailyRoot.id,
      title,
      description: "",
      bodyJson: { inline: [{ type: "text", text: title }] },
      nodeType: "day",
      displayProps: {},
      viewJson: { view: "outline" },
      dayDate: today,
      sortOrder: (maxRow?.maxSort ?? 0) + 1,
      createdBy: user.id,
      updatedBy: user.id,
    });
    await tx
      .insert(knowledgeNodeSupertags)
      .values({
        nodeId: node.id,
        supertagId: dayTag.id,
        createdBy: user.id,
      })
      .onConflictDoNothing();
    await upsertKnowledgeSearchIndex(tx, node, title);
    await appendKnowledgeRevision(tx, node, user.id, "今日のDayノードを作成");

    return NextResponse.json(
      {
        node: serializeNode(node),
        supertag: serializeSupertag(dayTag),
        node_supertags: [
          {
            node_id: node.id,
            supertag_id: dayTag.id,
            created_at: null,
            created_by: user.id,
          },
        ],
      },
      { status: 201 },
    );
  });
}
