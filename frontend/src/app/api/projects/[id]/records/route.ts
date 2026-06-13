import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull, max } from "drizzle-orm";
import { db } from "@/db";
import { recordFields, recordTables, recordViews } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getAccessibleProject, getWritableProject } from "@/lib/server/project-access";
import { cleanString, countRowsByTable } from "@/lib/server/record-table-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or inaccessible" }, { status: 404 });
  }

  const tables = await db
    .select()
    .from(recordTables)
    .where(and(eq(recordTables.projectId, id), isNull(recordTables.deletedAt)))
    .orderBy(recordTables.sortOrder, recordTables.createdAt);
  const rowCounts = await countRowsByTable(id);

  return NextResponse.json({
    tables: tables.map((table) => ({
      ...table,
      row_count: rowCounts.get(table.id) ?? 0,
    })),
  });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const name = cleanString(body.name, "New table");
  const description = cleanString(body.description, "");
  const [maxRow] = await db
    .select({ maxSort: max(recordTables.sortOrder) })
    .from(recordTables)
    .where(eq(recordTables.projectId, id));
  const sortOrder = (maxRow?.maxSort ?? 0) + 1;

  const result = await db.transaction(async (tx) => {
    const [table] = await tx
      .insert(recordTables)
      .values({
        projectId: id,
        name,
        description: description || null,
        sortOrder,
        createdBy: user.id,
        tableMetadata: {},
      })
      .returning();

    const [titleField] = await tx
      .insert(recordFields)
      .values({
        tableId: table.id,
        key: "title",
        label: "Title",
        fieldType: "text",
        sortOrder: 0,
        isTitle: true,
        options: {},
      })
      .returning();

    const [view] = await tx
      .insert(recordViews)
      .values({
        tableId: table.id,
        name: "Grid",
        viewType: "grid",
        config: {},
        sortOrder: 0,
        createdBy: user.id,
      })
      .returning();

    return { table, fields: [titleField], views: [view] };
  });

  return NextResponse.json(result, { status: 201 });
}
