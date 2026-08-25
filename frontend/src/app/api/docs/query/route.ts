import { NextRequest, NextResponse } from "next/server";
import { and, asc, desc, eq, inArray, isNull, or, sql } from "drizzle-orm";
import { db } from "@/db";
import {
  knowledgeFields,
  knowledgeFieldValues,
  knowledgeNodes,
  knowledgeNodeSupertags,
  knowledgeNodeShares,
  knowledgeSupertags,
  tasks,
} from "@/db/schema";
import { docsLibraries } from "@/lib/server/docs-library-schema";
import { getSession } from "@/lib/auth";
import {
  getParticipatingProjectIds,
} from "@/lib/server/task-route-utils";
import {
  ensureDocsWorkspace,
  getDocsLibraryIdsForReadableProjects,
  getDocsNodeAccess,
  decryptNodeBodyText,
  normalizeJsonObject,
  serializeFieldValue,
  serializeNode,
  serializeNodeSupertag,
} from "@/lib/server/knowledge-docs-utils";
import { listDocsTaskSyntheticFieldValues } from "@/lib/server/docs-task-binding";
import { resolveDocsTagFilters } from "@/lib/docs-query-utils";
import { docsRelativeDateRange } from "@/lib/docs-relative-date";

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

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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

function isUuid(value: string) {
  return UUID_RE.test(value);
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

/**
 * Resolve a system/name key only when it is unambiguous across the searched
 * libraries. Personal libraries intentionally carry the same built-in keys;
 * silently choosing whichever row happened to be returned first would make
 * an all-project query filter the wrong library.
 */
function uniqueKeyMap<T>(
  rows: T[],
  keyOf: (row: T) => string | null | undefined,
  valueOf: (row: T) => string,
) {
  const values = new Map<string, string>();
  const ambiguous = new Set<string>();
  for (const row of rows) {
    const rawKey = keyOf(row);
    if (!rawKey) continue;
    const key = rawKey.toLowerCase();
    const value = valueOf(row);
    if (ambiguous.has(key)) continue;
    const existing = values.get(key);
    if (existing && existing !== value) {
      values.delete(key);
      ambiguous.add(key);
      continue;
    }
    values.set(key, value);
  }
  return values;
}

function tagExistsCondition(tagIds: string[]) {
  if (tagIds.length === 0) return sql`false`;
  return sql`exists (
    select 1 from knowledge_node_supertags docs_nst
    inner join knowledge_supertags docs_tags on docs_tags.id = docs_nst.supertag_id
    where docs_nst.node_id = ${knowledgeNodes.id}
      and docs_tags.docs_library_id = ${knowledgeNodes.docsLibraryId}
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
      inner join knowledge_fields docs_fields on docs_fields.id = docs_fv.field_id
      where docs_fv.node_id = ${knowledgeNodes.id}
        and docs_fields.docs_library_id = ${knowledgeNodes.docsLibraryId}
        and docs_fv.field_id = ${fieldId}::uuid
    )`;
  }
  const comparable = fieldComparableValueSql();
  const expected = String(filter.value ?? "");
  const dateRange = docsRelativeDateRange(filter.value);
  if (dateRange === false) return sql`false`;
  if (dateRange) {
    return sql`exists (
      select 1 from knowledge_field_values docs_fv
      inner join knowledge_fields docs_fields on docs_fields.id = docs_fv.field_id
      where docs_fv.node_id = ${knowledgeNodes.id}
        and docs_fields.docs_library_id = ${knowledgeNodes.docsLibraryId}
        and docs_fv.field_id = ${fieldId}::uuid
        and left(${comparable}, 10) >= ${dateRange.start}
        and left(${comparable}, 10) < ${dateRange.end}
    )`;
  }
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
      and exists (
        select 1 from knowledge_fields docs_fields
        where docs_fields.id = docs_fv.field_id
          and docs_fields.docs_library_id = ${knowledgeNodes.docsLibraryId}
      )
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
    const dateRange = docsRelativeDateRange(filter.value);
    if (dateRange === false) return false;
    if (dateRange) {
      const actualDate = actual.slice(0, 10);
      if (!actualDate || actualDate < dateRange.start || actualDate >= dateRange.end) return false;
      continue;
    }
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
  docsLibraryId: string,
) {
  return {
    id: `task:${task.id}`,
    docs_library_id: docsLibraryId,
    parent_id: null,
    root_page_id: null,
    project_id: task.projectId,
    system_key: null,
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
  const rawQueryJson = normalizeJsonObject(body.query_json ?? body.query);
  // Project selection is no longer a generic Docs query scope. Project IDs
  // remain node/task identity fields and ACL inputs, but a query cannot switch
  // to another library or narrow candidates by `project_id`.
  const queryJson = { ...rawQueryJson };
  delete queryJson.project_id;
  const workspace = await ensureDocsWorkspace(user);
  if (!workspace) {
    return NextResponse.json({ detail: "Docs workspaceへのアクセス権がありません" }, { status: 403 });
  }
  // Search candidates from every workspace the actor could possibly see.
  // ACL resolution below is authoritative; keeping the SQL predicate broad
  // is necessary for a shared personal subtree whose node.project_id points
  // at a project the recipient cannot otherwise list.
  const personalWorkspace = await ensureDocsWorkspace(user);
  const sharedRows = await db
    .select({ docsLibraryId: knowledgeNodes.docsLibraryId })
    .from(knowledgeNodeShares)
    .innerJoin(knowledgeNodes, eq(knowledgeNodeShares.nodeId, knowledgeNodes.id))
    .innerJoin(docsLibraries, eq(knowledgeNodes.docsLibraryId, docsLibraries.id))
    .where(
      and(
        eq(knowledgeNodeShares.userId, user.id),
        eq(docsLibraries.libraryType, "personal"),
      ),
    );
  const workspaceIds = await getDocsLibraryIdsForReadableProjects(user.id, [
    workspace.id,
    personalWorkspace?.id,
    ...sharedRows.map((row) => row.docsLibraryId),
  ].filter((value): value is string => Boolean(value)));

  const clauses = clausesFromQuery(queryJson);
  const allTags = await db
    .select()
    .from(knowledgeSupertags)
    .where(inArray(knowledgeSupertags.docsLibraryId, workspaceIds));
  const tagIdBySystemKey = uniqueKeyMap(
    allTags,
    (tag) => tag.systemKey,
    (tag) => tag.id,
  );
  const tagIdByName = uniqueKeyMap(
    allTags,
    (tag) => tag.name,
    (tag) => tag.id,
  );
  const textClause = clauses.find((clause) => typeof clause.text === "string" || typeof clause.q === "string");
  const q =
    typeof body.q === "string"
      ? body.q.trim()
      : typeof textClause?.text === "string"
        ? textClause.text.trim()
        : typeof textClause?.q === "string"
          ? textClause.q.trim()
          : "";
  const { tagFilters, unresolvedTagConditions } = resolveDocsTagFilters({
    explicitSupertagIds: cleanStringArray(body.supertag_ids),
    clauses,
    tagIdSet: new Set(allTags.map((tag) => tag.id)),
    tagIdBySystemKey,
    tagIdByName,
  });
  if (unresolvedTagConditions.length > 0) {
    return NextResponse.json({
      nodes: [],
      node_supertags: [],
      field_values: [],
      next_cursor: null,
      unresolved_tag_conditions: unresolvedTagConditions,
    });
  }
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
  const includeVirtualTasks = body.include_virtual_tasks === true || queryJson.include_virtual_tasks === true;

  const allFields = await db
    .select()
    .from(knowledgeFields)
    .where(inArray(knowledgeFields.docsLibraryId, workspaceIds));
  const fieldsById = new Map(allFields.map((field) => [field.id, field]));
  const fieldsBySystemKey = uniqueKeyMap(
    allFields,
    (field) => field.systemKey,
    (field) => field.id,
  );
  const fieldsByName = uniqueKeyMap(
    allFields,
    (field) => field.name,
    (field) => field.id,
  );
  const resolvedFieldFilters: FieldFilter[] = [];
  for (const filter of fieldFilters) {
    if (typeof filter.field_id !== "string") {
      resolvedFieldFilters.push(filter);
      continue;
    }
    const rawFieldId = filter.field_id;
    const resolved = isUuid(rawFieldId)
      ? rawFieldId
      : fieldsBySystemKey.get(rawFieldId.toLowerCase()) ?? fieldsByName.get(rawFieldId.toLowerCase()) ?? null;
    if (!resolved) {
      return NextResponse.json({ detail: `Unknown docs field: ${rawFieldId}` }, { status: 400 });
    }
    if (!fieldsById.has(resolved)) {
      return NextResponse.json({ detail: `Unknown docs field: ${rawFieldId}` }, { status: 400 });
    }
    resolvedFieldFilters.push({ ...filter, field_id: resolved });
  }
  const taskSystemFields = allFields.filter((field) => field.systemKey && (TASK_SYSTEM_KEYS as readonly string[]).includes(field.systemKey));
  const taskFieldSystemKeyById = new Map(
    taskSystemFields
      .filter((field) => !!field.systemKey)
      .map((field) => [field.id, field.systemKey as string]),
  );
  const taskFieldIdsBySystemKey = uniqueKeyMap(
    taskSystemFields,
    (field) => field.systemKey,
    (field) => field.id,
  );
  const taskFieldsBySystemKey = new Map(
    Array.from(taskFieldIdsBySystemKey.entries())
      .map(([systemKey, fieldId]) => [systemKey, fieldsById.get(fieldId)] as const)
      .filter((entry): entry is readonly [string, typeof knowledgeFields.$inferSelect] => Boolean(entry[1])),
  );
  const taskFieldFilters = resolvedFieldFilters.filter(
    (filter) =>
      typeof filter.field_id === "string" &&
      taskFieldSystemKeyById.has(filter.field_id),
  );
  const docsFieldFilters = resolvedFieldFilters.filter(
    (filter) =>
      typeof filter.field_id !== "string" ||
      !taskFieldSystemKeyById.has(filter.field_id),
  );
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
  const storedTaskFieldConditions = taskFieldFilters.map(fieldFilterCondition).filter(Boolean);
  const taskCandidateCondition = includesTaskTag && storedTaskFieldConditions.length > 0
    ? or(
        and(...storedTaskFieldConditions),
        sql`exists (
          select 1 from tasks linked_task
          where linked_task.knowledge_node_id = ${knowledgeNodes.id}
            and linked_task.deleted_at is null
            and linked_task.archived_at is null
        )`,
      )
    : undefined;
  const notLegacyEmailBlank = sql<boolean>`NOT (
    ${knowledgeNodes.title} = '（空行）'
    AND EXISTS (
      WITH RECURSIVE email_ancestors AS (
        SELECT id, parent_id, system_key, docs_library_id,
               ARRAY[id]::uuid[] AS visited_path, 0 AS depth
        FROM knowledge_nodes
        WHERE id = ${knowledgeNodes.id}
          AND docs_library_id = ${knowledgeNodes.docsLibraryId}
        UNION ALL
        SELECT parent.id, parent.parent_id, parent.system_key,
               parent.docs_library_id,
               child.visited_path || ARRAY[parent.id]::uuid[], child.depth + 1
        FROM knowledge_nodes AS parent
        INNER JOIN email_ancestors AS child ON parent.id = child.parent_id
        WHERE parent.docs_library_id = ${knowledgeNodes.docsLibraryId}
          AND child.depth < 512
          AND NOT parent.id = ANY(child.visited_path)
      )
      SELECT 1 FROM email_ancestors WHERE system_key LIKE 'project_mail:%'
    )
  )`;

  const conditions = [
    inArray(knowledgeNodes.docsLibraryId, workspaceIds),
    isNull(knowledgeNodes.archivedAt),
    // 空行は KnowledgeNode ではない。旧legacy rowも検索結果へ戻さない。
    sql`regexp_replace(trim(${knowledgeNodes.title}), '[[:space:]]+', '', 'g') <> ''`,
    notLegacyEmailBlank,
    // Do not pre-filter project_id for an all-project query.  A personal
    // node may be explicitly shared while carrying an inaccessible project
    // reference; getDocsNodeAccess below filters that candidate safely.
    q
      ? sql`exists (
          select 1 from knowledge_search_index docs_ksi
          where docs_ksi.node_id = ${knowledgeNodes.id}
            and docs_ksi.docs_library_id in (${uuidListSql(workspaceIds)})
            and (docs_ksi.title_text ilike ${`%${q}%`} or docs_ksi.body_text_plain ilike ${`%${q}%`})
        )`
      : undefined,
    ...tagConditions,
    ...fieldConditions,
    taskCandidateCondition,
  ].filter(Boolean);

  let nodes = await db
    .select()
    .from(knowledgeNodes)
    .where(and(...conditions))
    .orderBy(desc(knowledgeNodes.updatedAt), asc(knowledgeNodes.sortOrder))
    .limit(Math.min(500, Math.max(limit + 1, (limit + 1) * 4)))
    .offset(cursor);

  // SQL workspace/project predicates are not sufficient for inherited
  // personal subtree shares. Re-check every candidate through the same ACL
  // resolver used by node/detail APIs before returning search results.
  const visibleCandidates = await Promise.all(
    nodes.map((node) => getDocsNodeAccess(node.id, user)),
  );
  nodes = visibleCandidates
    .map((access) => access?.node)
    .filter((node): node is NonNullable<typeof node> => Boolean(node));

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
    const linkedNodeIds = new Set(
      linkedTasks
        .map((task) => task.knowledgeNodeId)
        .filter((nodeId): nodeId is string => typeof nodeId === "string"),
    );
    nodes = nodes.filter((node) => !linkedNodeIds.has(node.id) || matchingNodeIds.has(node.id));
  }

  const readableTaskProjectIds = includesTaskTag
    ? await getParticipatingProjectIds(user.id)
    : [];
  const virtualTasks =
    includeVirtualTasks && includesTaskTag && readableTaskProjectIds.length > 0
      ? (
          await db
            .select()
            .from(tasks)
            .where(
              and(
                inArray(tasks.projectId, readableTaskProjectIds),
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
  const nodeWorkspaceById = new Map(
    nodes.map((node) => [node.id, node.docsLibraryId]),
  );
  const [nodeSupertagRows, fieldValueRows] = candidateIds.length
    ? await Promise.all([
        db
          .select({
            relation: knowledgeNodeSupertags,
            supertagWorkspaceId: knowledgeSupertags.docsLibraryId,
          })
          .from(knowledgeNodeSupertags)
          .innerJoin(knowledgeSupertags, eq(knowledgeNodeSupertags.supertagId, knowledgeSupertags.id))
          .where(inArray(knowledgeNodeSupertags.nodeId, candidateIds))
          .then((rows) => rows
            // A malformed relation can point at a tag from another library.
            // Keep only definitions belonging to the source node's library.
            .filter((row) => row.supertagWorkspaceId === nodeWorkspaceById.get(row.relation.nodeId))
            .map((row) => row.relation)),
        db
          .select({
            value: knowledgeFieldValues,
            fieldWorkspaceId: knowledgeFields.docsLibraryId,
          })
          .from(knowledgeFieldValues)
          .innerJoin(knowledgeFields, eq(knowledgeFieldValues.fieldId, knowledgeFields.id))
          .where(inArray(knowledgeFieldValues.nodeId, candidateIds))
          .then((rows) => rows
            // Field values are metadata in the source node's library.  Do not
            // let a foreign definition/value survive an ID-only lookup.
            .filter((row) => row.fieldWorkspaceId === nodeWorkspaceById.get(row.value.nodeId))
            .map((row) => row.value)),
      ])
    : [[], []];

  const targetIds = Array.from(new Set(
    fieldValueRows
      .map((value) => value.targetNodeId)
      .filter((value): value is string => Boolean(value)),
  ));
  const targetAccessRows = await Promise.all(
    targetIds.map((targetId) => getDocsNodeAccess(targetId, user)),
  );
  const targetAccessById = new Map(
    targetAccessRows
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => [item.node.id, item]),
  );
  // A target_node_id is itself sensitive metadata.  It must be readable by
  // the actor and live in the same library as the source value's node.
  const storedFieldValues = fieldValueRows.filter((value) => {
    if (!value.targetNodeId) return true;
    const targetAccess = targetAccessById.get(value.targetNodeId);
    return Boolean(
      targetAccess
      && targetAccess.workspace.id === nodeWorkspaceById.get(value.nodeId),
    );
  });
  const nodeSupertags = nodeSupertagRows;

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
        user,
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
