import { NextRequest, NextResponse } from "next/server";
import { and, asc, desc, eq, inArray, isNull, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeSupertags,
  tasks,
} from "@/db/schema";
import { getSession } from "@/lib/auth";
import { getReadableProjectIds } from "@/lib/server/task-route-utils";
import {
  ensureDocsWorkspace,
  ensureProjectReadable,
  decryptNodeBodyText,
  normalizeJsonObject,
  serializeFieldValue,
  serializeNode,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";

type FieldFilter = {
  field_id?: unknown;
  op?: unknown;
  value?: unknown;
};

const TASK_SYSTEM_KEYS = [
  "task_status",
  "task_due",
  "task_start",
  "task_priority",
  "task_project",
] as const;

function cleanStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function clausesFromQuery(value: unknown): Record<string, unknown>[] {
  const query = normalizeJsonObject(value);
  const clauses = Array.isArray(query.and) ? query.and : [];
  return clauses.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
}

function queryLimit(body: Record<string, unknown>, query: Record<string, unknown>) {
  return Math.min(Math.max(Number(body.limit ?? query.limit) || 200, 1), 500);
}

function queryCursor(body: Record<string, unknown>) {
  const raw = Number(body.cursor ?? 0);
  return Number.isFinite(raw) ? Math.max(0, Math.floor(raw)) : 0;
}

function collectDescendantTagIds(tags: Array<typeof knowledgeSupertags.$inferSelect>, tagId: string) {
  const childrenByParent = new Map<string, string[]>();
  for (const tag of tags) {
    if (!tag.parentSupertagId) continue;
    const next = childrenByParent.get(tag.parentSupertagId) ?? [];
    next.push(tag.id);
    childrenByParent.set(tag.parentSupertagId, next);
  }
  const ids = new Set([tagId]);
  const queue = [...(childrenByParent.get(tagId) ?? [])];
  while (queue.length > 0) {
    const id = queue.shift();
    if (!id || ids.has(id)) continue;
    ids.add(id);
    queue.push(...(childrenByParent.get(id) ?? []));
  }
  return ids;
}

function uuidListSql(ids: string[]) {
  return sql.join(ids.map((id) => sql`${id}::uuid`), sql`, `);
}

function tagExistsCondition(tagIds: string[]) {
  if (tagIds.length === 0) return sql`false`;
  return sql`exists (
    select 1 from knowledge_node_supertags docs_nst
    where docs_nst.node_id = ${knowledgeNodes.id}
      and docs_nst.supertag_id in (${uuidListSql(tagIds)})
  )`;
}

function fieldComparableValueSql() {
  return sql`coalesce(
    docs_fv.value_text,
    docs_fv.target_node_id::text,
    docs_fv.value_number::text,
    docs_fv.value_datetime::text,
    docs_fv.value_json::text,
    ''
  )`;
}

function fieldFilterCondition(filter: FieldFilter) {
  if (typeof filter.field_id !== "string") return undefined;
  const fieldId = filter.field_id;
  const op = typeof filter.op === "string" ? filter.op : "=";
  if (op === "not_set" || op === "is_not_set") {
    return sql`not exists (
      select 1 from knowledge_field_values docs_fv
      where docs_fv.node_id = ${knowledgeNodes.id}
        and docs_fv.field_id = ${fieldId}::uuid
    )`;
  }
  const comparable = fieldComparableValueSql();
  const expected = String(filter.value ?? "");
  const valueCondition =
    op === "is_set"
      ? sql`true`
      : op === "contains"
        ? sql`${comparable} ilike ${`%${expected}%`}`
        : op === "!="
          ? sql`${comparable} <> ${expected}`
        : [">", "<", ">=", "<="].includes(op)
            ? Number.isFinite(Number(expected))
              ? sql`docs_fv.value_number ${sql.raw(op)} ${Number(expected)}`
              : sql`false`
            : sql`${comparable} = ${expected}`;
  return sql`exists (
    select 1 from knowledge_field_values docs_fv
    where docs_fv.node_id = ${knowledgeNodes.id}
      and docs_fv.field_id = ${fieldId}::uuid
      and ${valueCondition}
  )`;
}

function taskComparableValue(
  task: typeof tasks.$inferSelect,
  systemKey: string,
): string {
  if (systemKey === "task_status") return task.status ?? "";
  if (systemKey === "task_due") return task.endAt ? String(task.endAt) : "";
  if (systemKey === "task_start") return task.startAt ? String(task.startAt) : "";
  if (systemKey === "task_priority") return task.priority ?? "";
  if (systemKey === "task_project") return task.projectId ?? "";
  return "";
}

function taskMatchesSystemFieldFilters(
  task: typeof tasks.$inferSelect,
  filters: FieldFilter[],
  fieldSystemKeyById: Map<string, string>,
): boolean {
  for (const filter of filters) {
    if (typeof filter.field_id !== "string") return false;
    const systemKey = fieldSystemKeyById.get(filter.field_id);
    if (!systemKey) return false;
    const op = typeof filter.op === "string" ? filter.op : "=";
    const actual = taskComparableValue(task, systemKey);
    const expected = String(filter.value ?? "");
    if (op === "is_set") {
      if (!actual) return false;
      continue;
    }
    if (op === "not_set" || op === "is_not_set") {
      if (actual) return false;
      continue;
    }
    if (op === "contains") {
      if (!actual.toLowerCase().includes(expected.toLowerCase())) return false;
      continue;
    }
    if (op === "!=") {
      if (actual === expected) return false;
      continue;
    }
    if ([">", "<", ">=", "<="].includes(op)) {
      const actualNumber = Date.parse(actual) || Number(actual);
      const expectedNumber = Date.parse(expected) || Number(expected);
      if (!Number.isFinite(actualNumber) || !Number.isFinite(expectedNumber)) return false;
      if (op === ">" && !(actualNumber > expectedNumber)) return false;
      if (op === "<" && !(actualNumber < expectedNumber)) return false;
      if (op === ">=" && !(actualNumber >= expectedNumber)) return false;
      if (op === "<=" && !(actualNumber <= expectedNumber)) return false;
      continue;
    }
    if (actual !== expected) return false;
  }
  return true;
}

function serializeVirtualTaskNode(
  task: typeof tasks.$inferSelect,
  workspaceId: string,
) {
  return {
    id: `task:${task.id}`,
    workspace_id: workspaceId,
    parent_id: null,
    root_page_id: null,
    project_id: task.projectId,
    title: task.title,
    description: task.description ?? "",
    body_json: {
      virtual_kind: "task",
      task_id: task.id,
      knowledge_node_id: task.knowledgeNodeId,
    },
    body_text: "",
    node_type: "node",
    display_props: { show_checkbox: true, virtual_kind: "task" },
    query_json: null,
    view_json: {},
    day_date: null,
    sort_order: task.sortOrder ?? 0,
    created_by: task.createdBy,
    updated_by: task.createdBy,
    created_at: task.createdAt ? new Date(task.createdAt).toISOString() : null,
    updated_at: task.updatedAt ? new Date(task.updatedAt).toISOString() : null,
    archived_at: null,
  };
}

function serializeVirtualTaskFieldValues(
  task: typeof tasks.$inferSelect,
  taskFieldsBySystemKey: Map<string, typeof knowledgeFields.$inferSelect>,
) {
  const values: Array<ReturnType<typeof serializeFieldValue>> = [];
  const push = (systemKey: string, value: string | Date | null | undefined) => {
    const field = taskFieldsBySystemKey.get(systemKey);
    if (!field || value === null || value === undefined || value === "") return;
    values.push({
      node_id: `task:${task.id}`,
      field_id: field.id,
      value_json: null,
      value_text:
        systemKey === "task_due" || systemKey === "task_start"
          ? null
          : String(value),
      value_number: null,
      value_datetime:
        systemKey === "task_due" || systemKey === "task_start"
          ? new Date(value).toISOString()
          : null,
      target_node_id: null,
      updated_at: task.updatedAt ? new Date(task.updatedAt).toISOString() : null,
      updated_by: task.createdBy,
    });
  };
  push("task_status", task.status);
  push("task_due", task.endAt);
  push("task_start", task.startAt);
  push("task_priority", task.priority);
  push("task_project", task.projectId);
  return values;
}

export async function POST(request: NextRequest) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }

  const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
  const queryJson = normalizeJsonObject(body.query_json ?? body.query);
  const projectId =
    typeof body.project_id === "string"
      ? body.project_id
      : typeof queryJson.project_id === "string"
        ? queryJson.project_id
        : null;
  const workspace = await ensureDocsWorkspace(user);
  if (projectId) {
    const project = await ensureProjectReadable(projectId, user);
    if (!project) {
      return NextResponse.json({ detail: "Projectへの読み取り権限がありません" }, { status: 403 });
    }
  }

  const clauses = clausesFromQuery(queryJson);
  const textClause = clauses.find((clause) => typeof clause.text === "string" || typeof clause.q === "string");
  const q =
    typeof body.q === "string"
      ? body.q.trim()
      : typeof textClause?.text === "string"
        ? textClause.text.trim()
        : typeof textClause?.q === "string"
          ? textClause.q.trim()
          : "";
  const tagFilters = [
    ...cleanStringArray(body.supertag_ids).map((tagId) => ({ tagId, includeDescendants: true })),
    ...clauses
      .map((clause) => ({
        tagId: clause.tag,
        includeDescendants: clause.include_descendants !== false,
      }))
      .filter((item): item is { tagId: string; includeDescendants: boolean } => typeof item.tagId === "string"),
  ];
  const supertagIds = Array.from(new Set(tagFilters.map((item) => item.tagId)));
  const mode = body.supertag_mode === "or" ? "or" : "and";
  const astFieldFilters: FieldFilter[] = clauses
    .filter((clause) => typeof (clause.field_id ?? clause.field) === "string")
    .map((clause) => ({
      field_id: clause.field_id ?? clause.field,
      op: clause.op ?? clause.operator,
      value: clause.value,
    }));
  const bodyFieldFilters: FieldFilter[] = Array.isArray(body.field_filters)
    ? body.field_filters.filter((item: unknown): item is FieldFilter =>
        Boolean(item && typeof item === "object"),
      )
    : [];
  const fieldFilters = [...bodyFieldFilters, ...astFieldFilters];
  const limit = queryLimit(body, queryJson);
  const cursor = queryCursor(body);
  const sort = typeof body.sort === "string" ? body.sort : typeof queryJson.sort === "string" ? queryJson.sort : "updated_desc";
  const sortFieldId = typeof queryJson.sort_field_id === "string" ? queryJson.sort_field_id : null;

  const taskSystemFields = await db
    .select()
    .from(knowledgeFields)
    .where(
      and(
        eq(knowledgeFields.workspaceId, workspace.id),
        inArray(knowledgeFields.systemKey, [...TASK_SYSTEM_KEYS]),
      ),
    );
  const taskFieldSystemKeyById = new Map(
    taskSystemFields
      .filter((field) => !!field.systemKey)
      .map((field) => [field.id, field.systemKey as string]),
  );
  const taskFieldsBySystemKey = new Map(
    taskSystemFields
      .filter((field) => !!field.systemKey)
      .map((field) => [field.systemKey as string, field]),
  );
  const taskFieldFilters = fieldFilters.filter(
    (filter) =>
      typeof filter.field_id === "string" &&
      taskFieldSystemKeyById.has(filter.field_id),
  );
  const docsFieldFilters = fieldFilters.filter(
    (filter) =>
      typeof filter.field_id !== "string" ||
      !taskFieldSystemKeyById.has(filter.field_id),
  );

  const allTags = supertagIds.length > 0
    ? await db
        .select()
        .from(knowledgeSupertags)
        .where(eq(knowledgeSupertags.workspaceId, workspace.id))
    : [];
  const acceptedTagSets = supertagIds.map((tagId) => {
    const filter = tagFilters.find((item) => item.tagId === tagId);
    return Array.from(filter?.includeDescendants === false ? new Set([tagId]) : collectDescendantTagIds(allTags, tagId));
  });
  const tagConditions = acceptedTagSets.length === 0
    ? []
    : mode === "or"
      ? [tagExistsCondition(Array.from(new Set(acceptedTagSets.flat())))]
      : acceptedTagSets.map(tagExistsCondition);
  const fieldConditions = docsFieldFilters.map(fieldFilterCondition).filter(Boolean);
  const taskTagIds = new Set(
    allTags.filter((tag) => tag.systemKey === "task").map((tag) => tag.id),
  );
  const includesTaskTag = acceptedTagSets.some((set) =>
    set.some((tagId) => taskTagIds.has(tagId)),
  );

  const conditions = [
    eq(knowledgeNodes.workspaceId, workspace.id),
    isNull(knowledgeNodes.archivedAt),
    projectId ? eq(knowledgeNodes.projectId, projectId) : undefined,
    q
      ? sql`exists (
          select 1 from knowledge_search_index docs_ksi
          where docs_ksi.node_id = ${knowledgeNodes.id}
            and docs_ksi.workspace_id = ${workspace.id}::uuid
            and (docs_ksi.title_text ilike ${`%${q}%`} or docs_ksi.body_text_plain ilike ${`%${q}%`})
        )`
      : undefined,
    ...tagConditions,
    ...fieldConditions,
  ].filter(Boolean);

  let nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...conditions))
    .orderBy(desc(knowledgeNodes.updatedAt), asc(knowledgeNodes.sortOrder))
    .limit(Math.min(500, limit + 1))
    .offset(cursor);

  if (includesTaskTag && taskFieldFilters.length > 0 && nodes.length > 0) {
    const nodeIds = nodes.map((node) => node.id);
    const linkedTasks = await db
      .select()
      .from(tasks)
      .where(
        and(
          inArray(tasks.knowledgeNodeId, nodeIds),
          isNull(tasks.deletedAt),
          isNull(tasks.archivedAt),
        ),
      );
    const matchingNodeIds = new Set(
      linkedTasks
        .filter((task) =>
          taskMatchesSystemFieldFilters(
            task,
            taskFieldFilters,
            taskFieldSystemKeyById,
          ),
        )
        .map((task) => task.knowledgeNodeId)
        .filter((nodeId): nodeId is string => typeof nodeId === "string"),
    );
    nodes = nodes.filter((node) => matchingNodeIds.has(node.id));
  }

  const readableTaskProjectIds =
    includesTaskTag && !projectId ? await getReadableProjectIds(user.id) : [];
  const virtualTasks =
    includesTaskTag && (projectId || readableTaskProjectIds.length > 0)
      ? (
          await db
            .select()
            .from(tasks)
            .where(
              and(
                projectId
                  ? eq(tasks.projectId, projectId)
                  : inArray(tasks.projectId, readableTaskProjectIds),
                isNull(tasks.deletedAt),
                isNull(tasks.archivedAt),
                q
                  ? sql`(${tasks.title} ilike ${`%${q}%`} or ${tasks.description} ilike ${`%${q}%`})`
                  : undefined,
              ),
            )
            .orderBy(desc(tasks.updatedAt), asc(tasks.sortOrder))
            .limit(Math.min(500, limit + 1))
            .offset(cursor)
        )
          .filter((task) => !task.knowledgeNodeId)
          .filter((task) =>
            taskFieldFilters.length === 0
              ? true
              : taskMatchesSystemFieldFilters(
                  task,
                  taskFieldFilters,
                  taskFieldSystemKeyById,
                ),
          )
          .slice(0, limit + 1)
      : [];

  const hasMoreNodes = nodes.length > limit;
  const hasMoreVirtualTasks = virtualTasks.length > limit;
  nodes = nodes.slice(0, limit);
  const pagedVirtualTasks = virtualTasks.slice(0, limit);

  const candidateIds = nodes.map((node) => node.id);
  const [nodeSupertags, storedFieldValues] = candidateIds.length
    ? await Promise.all([
        db
          .select()
          .from(knowledgeNodeSupertags)
          .where(inArray(knowledgeNodeSupertags.nodeId, candidateIds)),
        db
          .select()
          .from(knowledgeFieldValues)
          .where(inArray(knowledgeFieldValues.nodeId, candidateIds)),
      ])
    : [[], []];

  const sortFieldValueByNode = new Map(
    sortFieldId
      ? storedFieldValues
          .filter((value) => value.fieldId === sortFieldId)
          .map((value) => [
            value.nodeId,
            value.valueText
              ?? value.targetNodeId
              ?? (value.valueNumber === null || value.valueNumber === undefined ? "" : String(value.valueNumber))
              ?? (value.valueDatetime ? String(value.valueDatetime) : "")
              ?? JSON.stringify(value.valueJson ?? ""),
          ])
      : [],
  );
  nodes = [...nodes].sort((a, b) => {
    if (sortFieldId) {
      const aValue = sortFieldValueByNode.get(a.id) ?? "";
      const bValue = sortFieldValueByNode.get(b.id) ?? "";
      const compared = aValue.localeCompare(bValue);
      if (compared !== 0) return sort === "title_desc" || sort === "updated_desc" ? -compared : compared;
    }
    if (sort === "title_asc" || sort === "title_desc") {
      const value = (a.title || decryptNodeBodyText(a.bodyText) || "").localeCompare(
        b.title || decryptNodeBodyText(b.bodyText) || "",
      );
      return sort === "title_asc" ? value : -value;
    }
    if (sort === "updated_asc" || sort === "updated_desc") {
      const aTime = a.updatedAt ? new Date(a.updatedAt).getTime() : 0;
      const bTime = b.updatedAt ? new Date(b.updatedAt).getTime() : 0;
      return sort === "updated_asc" ? aTime - bTime : bTime - aTime;
    }
    if ((a.rootPageId ?? a.id) !== (b.rootPageId ?? b.id)) {
      return (a.rootPageId ?? a.id).localeCompare(b.rootPageId ?? b.id);
    }
    return (a.sortOrder ?? 0) - (b.sortOrder ?? 0);
  }).slice(0, limit);

  const filteredNodeIds = new Set(nodes.map((node) => node.id));
  const taskFieldValues = filteredNodeIds.size > 0
    ? await listDocsTaskSyntheticFieldValues({
        nodeIds: Array.from(filteredNodeIds),
        fields: taskSystemFields,
      })
    : [];
  return NextResponse.json({
    nodes: [
      ...nodes.map(serializeNode),
      ...pagedVirtualTasks.map((task) => serializeVirtualTaskNode(task, workspace.id)),
    ],
    node_supertags: nodeSupertags
      .filter((entry) => filteredNodeIds.has(entry.nodeId))
      .map(serializeNodeSupertag),
    field_values: [
      ...storedFieldValues
        .filter((value) => filteredNodeIds.has(value.nodeId))
        .map(serializeFieldValue),
      ...taskFieldValues.map(serializeFieldValue),
      ...pagedVirtualTasks.flatMap((task) =>
        serializeVirtualTaskFieldValues(task, taskFieldsBySystemKey),
      ),
    ],
    next_cursor: hasMoreNodes || hasMoreVirtualTasks ? String(cursor + limit) : null,
  });
}
