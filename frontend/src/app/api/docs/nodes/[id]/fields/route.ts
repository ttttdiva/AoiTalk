import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFieldValues, knowledgeFields } from "@/db/schema";
import { getSession } from "@/lib/auth";
import {
  applyDocsTaskFieldProxies,
  listDocsTaskSyntheticFieldValues,
} from "@/lib/server/docs-task-binding";
import {
  normalizeFieldValueInput,
  requireDocsNode,
  serializeFieldValue,
} from "@/lib/server/knowledge-docs-utils";

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const { id } = await params;
  const access = await requireDocsNode(id, user, "write");
  if (!access) {
    return NextResponse.json({ detail: "nodeが見つからないか権限がありません" }, { status: 404 });
  }

  const body = await request.json().catch(() => ({}));
  const requestedValues: unknown[] = Array.isArray(body.field_values)
    ? body.field_values
    : [];
  const fieldIds: string[] = requestedValues
    .map((item: unknown) =>
      item && typeof item === "object"
        ? (item as Record<string, unknown>).field_id
        : null,
    )
    .filter((item: unknown): item is string => typeof item === "string");

  if (fieldIds.length === 0) {
    return NextResponse.json({ field_values: [] });
  }

  const fields = await db
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.workspaceId, access.workspace.id),
        inArray(knowledgeFields.id, fieldIds),
      ),
    );
  const fieldsById = new Map(fields.map((field) => [field.id, field]));
  let proxiedFieldIds = new Set<string>();
  try {
    proxiedFieldIds = await applyDocsTaskFieldProxies({
      user,
      nodeId: access.node.id,
      fieldsById,
      requestedValues,
    });
  } catch (error) {
    return NextResponse.json(
      {
        detail: "タスク連携フィールドの更新に失敗しました",
        error: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }
  const values = requestedValues.flatMap((item: unknown) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const fieldId = typeof record.field_id === "string" ? record.field_id : "";
    if (proxiedFieldIds.has(fieldId)) return [];
    const field = fieldsById.get(fieldId);
    if (!field) return [];
    return [
      {
        ...normalizeFieldValueInput(field, record.value),
        nodeId: access.node.id,
        updatedBy: user.id,
        updatedAt: new Date(),
      },
    ];
  });

  const requestedFieldIds = Array.from(new Set(fieldIds));
  const rows = await db.transaction(async (tx) => {
    if (requestedFieldIds.length > 0) {
      await tx
        .delete(knowledgeFieldValues)
        .where(
          and(
            eq(knowledgeFieldValues.nodeId, access.node.id),
            inArray(knowledgeFieldValues.fieldId, requestedFieldIds),
          ),
        );
    }
    if (values.length === 0) return [];
    return await tx.insert(knowledgeFieldValues).values(values).returning();
  });

  const taskFieldValues = proxiedFieldIds.size > 0
    ? await listDocsTaskSyntheticFieldValues({ nodeIds: [access.node.id], fields })
    : [];

  return NextResponse.json({
    field_values: [
      ...rows.map(serializeFieldValue),
      ...taskFieldValues
        .filter((value) => proxiedFieldIds.has(value.fieldId))
        .map(serializeFieldValue),
    ],
  });
}
