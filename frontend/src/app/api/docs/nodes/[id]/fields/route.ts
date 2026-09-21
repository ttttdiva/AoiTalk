import { NextRequest, NextResponse } from "next/server";
import { and, eq, inArray } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFieldValues, knowledgeFields, knowledgeNodes, projects } from "@/db/schema";
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
import {
  lockAndAssertGenericDocsMutationAllowed,
  ManagedDocsAccessError,
  ManagedDocsMutationError,
} from "@/lib/server/managed-docs-policy";

/** A field definition from another Docs Library must never participate in a
 * node update.  In particular, do not let its id reach the delete query: a
 * malformed cross-library value could otherwise be removed by node_id alone.
 */
class ForeignDocsFieldError extends Error {
  readonly status = 409;

  constructor() {
    super("別のDocs Libraryのフィールドはこのnodeに設定できません");
  }
}

class CanonicalDocsFieldError extends Error {
  readonly status = 409;

  constructor() {
    super("案件情報の正本nodeのProject/Page Role fieldは専用Project APIで管理されます");
  }
}

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
  const systemKey = String(access.node.systemKey ?? "").trim();
  if (systemKey === "project_information_root" || systemKey.startsWith("project_information:")) {
    return NextResponse.json(
      { detail: "案件情報の正本nodeのProject/Page Role fieldは専用Project APIで管理されます" },
      { status: 409 },
    );
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

  let result: {
    rows: Array<typeof knowledgeFieldValues.$inferSelect>;
    fields: Array<typeof knowledgeFields.$inferSelect>;
    proxiedFieldIds: Set<string>;
  };
  try {
    result = await db.transaction(async (tx) => {
      if (typeof tx.execute === "function" || access.node.projectId || systemKey) {
        const pointerSelect = tx.select({ id: projects.id });
        const pointerFrom = pointerSelect.from(projects);
        const pointerWhere = pointerFrom.where(eq(projects.knowledgeNodeId, access.node.id));
        const pointerLimited = typeof pointerWhere.limit === "function"
          ? pointerWhere.limit(2)
          : pointerWhere;
        const pointerRows = typeof pointerLimited.for === "function"
          ? await pointerLimited.for("update")
          : await pointerLimited;
        if (pointerRows.length > 0) {
          throw new CanonicalDocsFieldError();
        }
      }
      await lockAndAssertGenericDocsMutationAllowed(access.node, tx, user);
      // The reverse-pointer check above is only a preflight. Recheck the
      // canonical identity after the managed-policy lock, because a Project
      // repair may have assigned this node while the first query was running.
      const postPolicyPointers = await tx
        .select({ id: projects.id })
        .from(projects)
        .where(eq(projects.knowledgeNodeId, access.node.id));
      const [postPolicyNode] = await tx
        .select({ systemKey: knowledgeNodes.systemKey })
        .from(knowledgeNodes)
        .where(eq(knowledgeNodes.id, access.node.id));
      const postPolicySystemKey = String(postPolicyNode?.systemKey ?? "").trim();
      if (
        postPolicyPointers.length > 0
        || postPolicySystemKey === "project_information_root"
        || postPolicySystemKey.startsWith("project_information:")
      ) {
        throw new CanonicalDocsFieldError();
      }

      const fields = await tx
        .select()
        .from(knowledgeFields)
        .where(
          and(
            eq(knowledgeFields.docsLibraryId, access.workspace.id),
            inArray(knowledgeFields.id, fieldIds),
          ),
      );
      const fieldsById = new Map(fields.map((field) => [field.id, field]));
      const requestedFieldIds = Array.from(new Set(fieldIds));
      const foreignFieldIds = requestedFieldIds.filter((fieldId) => !fieldsById.has(fieldId));
      if (foreignFieldIds.length > 0) {
        // Reject before applying task proxies or deleting existing values.
        // This keeps malformed foreign field_values intact and makes the
        // caller fix its library boundary instead of silently dropping data.
        throw new ForeignDocsFieldError();
      }
      const proxiedFieldIds = await applyDocsTaskFieldProxies({
        user,
        nodeId: access.node.id,
        fieldsById,
        requestedValues,
        transaction: tx,
      });
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
      const rows = values.length === 0
        ? []
        : await tx.insert(knowledgeFieldValues).values(values).returning();
      return { rows, fields, proxiedFieldIds };
    });
  } catch (error) {
    if (
      error instanceof ManagedDocsMutationError
      || error instanceof ManagedDocsAccessError
      || error instanceof ForeignDocsFieldError
      || error instanceof CanonicalDocsFieldError
    ) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    console.error("Docs field values update failed", { nodeId: id, error });
    return NextResponse.json(
      {
        detail: "タスク連携フィールドの更新に失敗しました",
        code: "docs_field_update_failed",
      },
      { status: 502 },
    );
  }

  const { rows, fields, proxiedFieldIds } = result;

  const taskFieldValues = proxiedFieldIds.size > 0
    ? await listDocsTaskSyntheticFieldValues({
      nodeIds: [access.node.id],
      fields,
      user,
    })
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
