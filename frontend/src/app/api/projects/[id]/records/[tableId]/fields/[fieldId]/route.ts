import { NextRequest, NextResponse } from "next/server";
import { and, eq, isNull } from "drizzle-orm";
import { db } from "@/db";
import { recordFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getWritableProject } from "@/lib/server/project-access";
import {
  asRecord,
  cleanString,
  normalizeFieldType,
  requireRecordTable,
} from "@/lib/server/record-table-utils";

async function requireField(tableId: string, fieldId: string) {
  const [field] = await db
    .select()
    .from(recordFields)
    .where(
      and(
        eq(recordFields.id, fieldId),
        eq(recordFields.tableId, tableId),
        isNull(recordFields.deletedAt),
      ),
    )
    .limit(1);
  return field ?? null;
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string; fieldId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId, fieldId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }
  if (!(await requireRecordTable(id, tableId))) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }
  const field = await requireField(tableId, fieldId);
  if (!field) {
    return NextResponse.json({ detail: "Record field not found" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const updates: Partial<typeof recordFields.$inferInsert> = {
    updatedAt: new Date(),
  };
  if (body.label !== undefined) updates.label = cleanString(body.label, field.label);
  if (body.field_type !== undefined || body.fieldType !== undefined) {
    updates.fieldType = normalizeFieldType(body.field_type ?? body.fieldType);
  }
  if (body.options !== undefined) updates.options = asRecord(body.options);
  if (body.required !== undefined) updates.required = body.required === true;
  if (body.sensitivity !== undefined) {
    updates.sensitivity = cleanString(body.sensitivity, field.sensitivity ?? "normal");
  }
  if (body.is_title !== undefined || body.isTitle !== undefined) {
    updates.isTitle = body.is_title === true || body.isTitle === true;
  }
  if (body.is_due !== undefined || body.isDue !== undefined) {
    updates.isDue = body.is_due === true || body.isDue === true;
  }

  const [updated] = await db
    .update(recordFields)
    .set(updates)
    .where(eq(recordFields.id, fieldId))
    .returning();

  return NextResponse.json({ field: updated });
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string; tableId: string; fieldId: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { id, tableId, fieldId } = await params;
  const access = await getWritableProject(id, user);
  if (!access) {
    return NextResponse.json({ detail: "Project not found or not writable" }, { status: 404 });
  }
  if (!(await requireRecordTable(id, tableId))) {
    return NextResponse.json({ detail: "Record table not found" }, { status: 404 });
  }
  const field = await requireField(tableId, fieldId);
  if (!field) {
    return NextResponse.json({ detail: "Record field not found" }, { status: 404 });
  }

  await db
    .update(recordFields)
    .set({ deletedAt: new Date(), updatedAt: new Date() })
    .where(eq(recordFields.id, fieldId));

  return NextResponse.json({ success: true });
}
