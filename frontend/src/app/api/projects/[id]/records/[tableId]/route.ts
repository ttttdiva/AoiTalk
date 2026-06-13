import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { recordRows, recordTables } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getAccessibleProject, getWritableProject } from "@/lib/server/project-access";
import {
  cleanString,
  decryptRecordRow,
  getTableFields,
  requireRecordTable,
} from "@/lib/server/record-table-utils";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId } = await params;
  const access = await getAccessibleProject(id, user.id);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or inaccessible" }, { status: 404 });
  }

  const table = await requireRecordTable(id, tableId);
  if (!table) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }

  const [fields, rows] = await Promise.all([
    getTableFields(tableId),
    db
      .select()
      .from(recordRows)
      .where(and(eq(recordRows.tableId, tableId), isNull(recordRows.deletedAt)))
      .orderBy(recordRows.createdAt),
  ]);

  return NextResponse.json({ table, fields, rows: rows.map(decryptRecordRow) });
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }

  const table = await requireRecordTable(id, tableId);
  if (!table) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const updates: Partial<typeof recordTables.$inferInsert> = {
    updatedAt: new Date(),
  };
  if (body.name !== undefined) updates.name = cleanString(body.name, table.name);
  if (body.description !== undefined) {
    updates.description = cleanString(body.description, "") || null;
  }
  if (body.memory_policy !== undefined) {
    updates.memoryPolicy = cleanString(body.memory_policy, table.memoryPolicy ?? "manual");
  }
  if (body.default_sensitivity !== undefined) {
    updates.defaultSensitivity = cleanString(
      body.default_sensitivity,
      table.defaultSensitivity ?? "normal",
    );
  }

  const [updated] = await db
    .update(recordTables)
    .set(updates)
    .where(eq(recordTables.id, tableId))
    .returning();

  return NextResponse.json({ table: updated });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }

  const table = await requireRecordTable(id, tableId);
  if (!table) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }

  await db
    .update(recordTables)
    .set({ deletedAt: new Date(), updatedAt: new Date() })
    .where(eq(recordTables.id, tableId));

  return NextResponse.json({ success: true });
}
