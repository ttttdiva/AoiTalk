import { NextRequest, NextResponse } from "next/server";
import { db } from "@/db";
import { recordRows } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import {
  asRecord,
  cleanString,
  decryptRecordRow,
  encryptRecordRowStorage,
  getTableFields,
  materializeRow,
  requireRecordTable,
} from "@/lib/server/record-table-utils";

export async function POST(
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
  const values = asRecord(body.values);
  const fields = await getTableFields(tableId);
  const materialized = materializeRow(values, fields);
  const encryptedStorage = encryptRecordRowStorage(values, materialized);

  const [row] = await db
    .insert(recordRows)
    .values({
      tableId,
      projectId: id,
      createdBy: user.id,
      values: encryptedStorage.values,
      title: encryptedStorage.title,
      dueAt: materialized.dueAt,
      searchText: encryptedStorage.searchText,
      sensitivity: cleanString(body.sensitivity, table.defaultSensitivity ?? "normal"),
      rowMetadata: asRecord(body.metadata),
    })
    .returning();

  return NextResponse.json({ row: decryptRecordRow(row) }, { status: 201 });
}
