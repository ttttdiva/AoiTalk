import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { recordRows } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import {
  asRecord,
  cleanString,
  decryptRecordRow,
  decryptRecordRowValues,
  encryptRecordRowStorage,
  getTableFields,
  materializeRow,
  requireRecordTable,
} from "@/lib/server/record-table-utils";

async function requireRow(projectId: string, tableId: string, rowId: string) {
  const [row] = await db
    .select()
    .from(recordRows)
    .where(
      and(
        eq(recordRows.id, rowId),
        eq(recordRows.projectId, projectId),
        eq(recordRows.tableId, tableId),
        isNull(recordRows.deletedAt),
      ),
    )
    .limit(1);
  return row ?? null;
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string; rowId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId, rowId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }
  if (!(await requireRecordTable(id, tableId))) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }
  const row = await requireRow(id, tableId, rowId);
  if (!row) {
    return NextResponse.json({ detail: "Record row not found" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const currentValues = decryptRecordRowValues(row.values);
  const values =
    body.values === undefined
      ? currentValues
      : { ...currentValues, ...asRecord(body.values) };
  const fields = await getTableFields(tableId);
  const materialized = materializeRow(values, fields);
  const encryptedStorage = encryptRecordRowStorage(values, materialized);

  const [updated] = await db
    .update(recordRows)
    .set({
      values: encryptedStorage.values,
      title: encryptedStorage.title,
      dueAt: materialized.dueAt,
      searchText: encryptedStorage.searchText,
      sensitivity:
        body.sensitivity === undefined
          ? row.sensitivity
          : cleanString(body.sensitivity, row.sensitivity ?? "normal"),
      updatedAt: new Date(),
    })
    .where(eq(recordRows.id, rowId))
    .returning();

  return NextResponse.json({ row: decryptRecordRow(updated) });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string; rowId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId, rowId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }
  if (!(await requireRecordTable(id, tableId))) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }
  if (!(await requireRow(id, tableId, rowId))) {
    return NextResponse.json({ detail: "Record row not found" }, { status: 404 });
  }

  await db
    .update(recordRows)
    .set({ deletedAt: new Date(), updatedAt: new Date() })
    .where(eq(recordRows.id, rowId));

  return NextResponse.json({ success: true });
}
