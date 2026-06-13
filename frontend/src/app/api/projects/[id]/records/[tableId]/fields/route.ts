import { NextRequest, NextResponse } from "next/server";
import { eq, max } from "drizzle-orm";
import { db } from "@/db";
import { recordFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import {
  asRecord,
  cleanString,
  normalizeFieldType,
  requireRecordTable,
  uniqueFieldKey,
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
  const label = cleanString(body.label, "New field");
  const fieldType = normalizeFieldType(body.field_type ?? body.fieldType);
  const key = await uniqueFieldKey(tableId, cleanString(body.key, label));
  const [maxRow] = await db
    .select({ maxSort: max(recordFields.sortOrder) })
    .from(recordFields)
    .where(eq(recordFields.tableId, tableId));
  const sortOrder = (maxRow?.maxSort ?? 0) + 1;

  const [field] = await db
    .insert(recordFields)
    .values({
      tableId,
      key,
      label,
      fieldType,
      options: asRecord(body.options),
      required: body.required === true,
      sortOrder,
      sensitivity: cleanString(body.sensitivity, "normal"),
    })
    .returning();

  return NextResponse.json({ field }, { status: 201 });
}
