import { NextRequest, NextResponse } from "next/server";
import { and, eq, gte, inArray, isNull, lte, or, sql } from "drizzle-orm";
import { db } from "@/db";
import { knowledgeFields, knowledgeFieldValues, knowledgeNodes } from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  ensureDocsWorkspace,
  serializeField,
  serializeFieldValue,
  serializeNode,
} from "@/lib/server/knowledge-docs-utils";

function parseDateParam(value: string | null, fallback: Date) {
  if (!value) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? fallback : parsed;
}

export async function GET(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const now = new Date();
  const defaultStart = new Date(now);
  defaultStart.setDate(defaultStart.getDate() - 30);
  const defaultEnd = new Date(now);
  defaultEnd.setDate(defaultEnd.getDate() + 90);
  const start = parseDateParam(request.nextUrl.searchParams.get("start"), defaultStart);
  const end = parseDateParam(request.nextUrl.searchParams.get("end"), defaultEnd);
  const startDay = start.toISOString().slice(0, 10);
  const endDay = end.toISOString().slice(0, 10);
  const projectId = request.nextUrl.searchParams.get("project_id");
  const spaceId = projectId ? null : request.nextUrl.searchParams.get("space_id");
  const workspace = await ensureDocsWorkspace(user);
  const readableProjectIds = await getReadableProjectIds(user.id, {
    projectId,
    spaceId,
  });
  const projectScope = projectId || spaceId;
  const projectVisibilityCondition = projectScope
    ? readableProjectIds.length > 0
      ? inArray(knowledgeNodes.projectId, readableProjectIds)
      : sql`false`
    : or(
        isNull(knowledgeNodes.projectId),
        readableProjectIds.length > 0
          ? inArray(knowledgeNodes.projectId, readableProjectIds)
          : isNull(knowledgeNodes.projectId),
      );

  const rows = await db
    .select({
      node: knowledgeNodes,
      field: knowledgeFields,
      value: knowledgeFieldValues,
    })
    .from(knowledgeFieldValues)
    .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
    .innerJoin(knowledgeNodes, eq(knowledgeFieldValues.nodeId, knowledgeNodes.id))
    .where(
      and(
        eq(knowledgeNodes.workspaceId, workspace.id),
        isNull(knowledgeNodes.archivedAt),
        projectVisibilityCondition,
        eq(knowledgeFields.fieldType, "date"),
        gte(knowledgeFieldValues.valueDatetime, start),
        lte(knowledgeFieldValues.valueDatetime, end),
      ),
    )
    .limit(500);

  const dayRows = await db
    .select({ node: knowledgeNodes })
    .from(knowledgeNodes)
    .where(
      and(
        eq(knowledgeNodes.workspaceId, workspace.id),
        isNull(knowledgeNodes.archivedAt),
        projectVisibilityCondition,
        gte(knowledgeNodes.dayDate, startDay),
        lte(knowledgeNodes.dayDate, endDay),
      ),
    )
    .limit(500);

  return NextResponse.json({
    items: [
      ...dayRows.map((row) => ({
        id: `day:${row.node.id}`,
        node: serializeNode(row.node),
        field: {
          id: "day_date",
          name: "Day",
          field_type: "date",
          system_key: "day_date",
        },
        value: {
          node_id: row.node.id,
          field_id: "day_date",
          value_datetime: row.node.dayDate,
        },
        start: row.node.dayDate,
        end: row.node.dayDate,
        all_day: true,
      })),
      ...rows.map((row) => ({
        id: `${row.node.id}:${row.field.id}`,
        node: serializeNode(row.node),
        field: serializeField(row.field),
        value: serializeFieldValue(row.value),
        start: row.value.valueDatetime,
        end: row.value.valueDatetime,
        all_day: true,
      })),
    ],
  });
}
