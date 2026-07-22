import type { DocsBlockSnapshot } from "@/lib/docs-block-model";
import { plainDocsTitle } from "@/lib/docs-title";
import type {
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsState,
  DocsSupertag,
  DocsAiSuggestion,
} from "./types";
import {
  buildChildrenByParent,
  fieldValueToDraft,
} from "./docs-utils";

// 子コンポーネントへ読み取り専用状態を反映したAPIクライアントを渡す。
export type DocsApiFetch = <T>(path: string, init?: RequestInit) => Promise<T>;

export type TodayResponse = {
  node: DocsNode;
  supertag?: DocsSupertag;
  node_supertags?: DocsState["node_supertags"];
};

export type DocsTaskBinding = {
  id: string;
  project_id: string | null;
  knowledge_node_id: string | null;
  title: string;
  status: string | null;
};

export type NodePatch = Partial<
  Pick<DocsNode, "title" | "aliases" | "description" | "body_text" | "body_json" | "display_props" | "query_json" | "view_json" | "parent_id" | "sort_order" | "node_type" | "project_id">
>;

export type SearchView = "table" | "board" | "calendar" | "cards" | "list";
export type SearchSort = "" | "updated_desc" | "updated_asc" | "title_asc" | "title_desc";
export type SearchFieldFilterOp = "=" | "!=" | "contains" | "is_set" | "not_set" | ">" | "<" | ">=" | "<=";
export type SearchFieldFilterDraft = {
  fieldId: string;
  op: SearchFieldFilterOp;
  value: string;
};

export type DocsQueryResponse = Pick<DocsState, "nodes" | "node_supertags" | "field_values"> & {
  next_cursor?: string | null;
};

export type SidebarContextMenuState = {
  x: number;
  y: number;
  nodeId: string;
};

export const SEARCH_SORT_OPTIONS: Array<{ value: SearchSort; label: string }> = [
  { value: "", label: "Default sort" },
  { value: "updated_desc", label: "Updated desc" },
  { value: "updated_asc", label: "Updated asc" },
  { value: "title_asc", label: "Title A-Z" },
  { value: "title_desc", label: "Title Z-A" },
];

export const SEARCH_FIELD_FILTER_OPS: Array<{ value: SearchFieldFilterOp; label: string; needsValue: boolean }> = [
  { value: "contains", label: "contains", needsValue: true },
  { value: "=", label: "=", needsValue: true },
  { value: "!=", label: "!=", needsValue: true },
  { value: "is_set", label: "is set", needsValue: false },
  { value: "not_set", label: "not set", needsValue: false },
  { value: ">", label: ">", needsValue: true },
  { value: "<", label: "<", needsValue: true },
  { value: ">=", label: ">=", needsValue: true },
  { value: "<=", label: "<=", needsValue: true },
];

export type LoadOptions = {
  focusToday?: boolean;
  date?: string;
  nodeId?: string;
};

// 固定コマンドに加え、Supertag config_json.tools で宣言される任意のツールコマンドも受け付ける。
export type DocsAiCommand =
  | "continue"
  | "extract_tasks"
  | "rewrite"
  | "fill_fields"
  | "generate_minutes"
  | (string & {});

export type DocsAiCommandResult = {
  suggestion?: DocsAiSuggestion;
  result?: {
    mode?: string;
    lines?: string[];
    replacement?: string;
    fields?: Array<{ name?: string; value?: string }>;
    summary?: string;
  };
};

export type DocsAiPreview = {
  node: DocsNode;
  command: DocsAiCommand;
  suggestionId?: string;
  result: NonNullable<DocsAiCommandResult["result"]>;
};

// Supertag config_json.tools の1要素。
export type DocsSupertagTool = { command: string; label: string };

export type DocsCommandMode =
  | { kind: "root" }
  | { kind: "tag" }
  | { kind: "move"; leaveReference: boolean }
  | { kind: "view" }
  | { kind: "field"; fieldId: string };

export const SUPERTAGS_OVERVIEW_ID = "__supertags_overview__";

export const COLLAPSED_KEY = "aoitalk.docs.outline.collapsed";
export const SIDEBAR_COLLAPSED_KEY = "aoitalk.docs.sidebar.collapsed";
export const DOCS_SIDEBAR_SLOT_ID = "docs-sidebar-slot";

export function getDocsSidebarSlotSnapshot() {
  if (typeof document === "undefined") return null;
  return document.getElementById(DOCS_SIDEBAR_SLOT_ID);
}

export function subscribeDocsSidebarSlot(callback: () => void) {
  if (typeof document === "undefined") return () => {};
  const observer = new MutationObserver(callback);
  observer.observe(document.body, { childList: true, subtree: true });
  return () => observer.disconnect();
}

export function readCollapsed(key = COLLAPSED_KEY): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : []);
  } catch {
    return new Set();
  }
}

export function writeCollapsed(value: Set<string>, key = COLLAPSED_KEY) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(key, JSON.stringify(Array.from(value)));
}

export function nodeText(node: DocsNode) {
  return plainDocsTitle(node.title || node.body_text) || "Untitled";
}

export function renderNodeTitleTemplate(
  node: DocsNode,
  tags: DocsSupertag[],
  fields: DocsField[],
  values: DocsFieldValue[],
) {
  const tag = tags.find((item) => item.title_template);
  const template = tag?.title_template?.trim();
  if (!template) return nodeText(node);
  const valueByFieldId = new Map(values.map((value) => [value.field_id, fieldValueToDraft(value)]));
  const fieldByName = new Map(fields.map((field) => [field.name.toLowerCase(), field]));
  return template.replace(/\{([^}]+)\}/g, (_match, rawName: string) => {
    const name = String(rawName).trim();
    if (name === "title") return nodeText(node);
    const field = fieldByName.get(name.toLowerCase());
    return field ? valueByFieldId.get(field.id) ?? "" : "";
  }).replace(/\s+/g, " ").trim() || nodeText(node);
}

export function mergeById<T extends { id: string }>(current: T[], next: T) {
  return current.some((item) => item.id === next.id)
    ? current.map((item) => (item.id === next.id ? next : item))
    : [...current, next];
}

export function valueByNodeField(values: DocsFieldValue[]) {
  return new Map(values.map((value) => [`${value.node_id}:${value.field_id}`, value]));
}

export function fieldsForNode(
  node: DocsNode,
  nodeTags: Map<string, DocsSupertag[]>,
  fieldsByTag: Map<string, DocsField[]>,
) {
  const byId = new Map<string, DocsField>();
  for (const tag of nodeTags.get(node.id) ?? []) {
    for (const field of fieldsByTag.get(tag.id) ?? []) byId.set(field.id, field);
  }
  return Array.from(byId.values()).sort((a, b) => a.sort_order - b.sort_order);
}

export function outlineRows(
  parentId: string,
  childrenByParent: Map<string | null, DocsNode[]>,
  collapsed: Set<string>,
  depth = 0,
  path = new Set<string>(),
) {
  const rows: Array<{ node: DocsNode; depth: number }> = [];
  for (const child of childrenByParent.get(parentId) ?? []) {
    if (child.archived_at) continue;
    if (path.has(child.id)) continue;
    rows.push({ node: child, depth });
    if (!collapsed.has(child.id)) {
      rows.push(...outlineRows(child.id, childrenByParent, collapsed, depth + 1, new Set([...path, child.id])));
    }
  }
  return rows;
}

export function buildOutlineChildren(nodes: DocsNode[], placements: DocsState["placements"]) {
  const merged = [...nodes];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  for (const placement of placements) {
    const node = byId.get(placement.node_id);
    if (!node || node.archived_at) continue;
    merged.push({
      ...node,
      parent_id: placement.parent_node_id,
      sort_order: placement.sort_order,
      display_props: {
        ...node.display_props,
        placement_id: placement.id,
        placement_reference: true,
        collapsed: placement.collapsed,
      },
    });
  }
  return buildChildrenByParent(merged.filter((node) => !node.archived_at));
}

export function searchTagIds(node: DocsNode) {
  const query = node.query_json;
  const clauses = Array.isArray(query?.and) ? query.and : [];
  return clauses
    .map((clause) => (clause && typeof clause === "object" && "tag" in clause ? clause.tag : null))
    .filter((value): value is string => typeof value === "string");
}

export function searchQueryClauses(query: unknown) {
  const record = readConfigRecord(query);
  return Array.isArray(record.and)
    ? record.and.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
}

export function replaceSearchClause(
  query: unknown,
  predicate: (clause: Record<string, unknown>) => boolean,
  nextClause: Record<string, unknown> | null,
) {
  const record = { ...readConfigRecord(query) };
  const clauses = searchQueryClauses(record).filter((clause) => !predicate(clause));
  if (nextClause) clauses.push(nextClause);
  if (clauses.length > 0) {
    record.and = clauses;
  } else {
    delete record.and;
  }
  return record;
}

export function isSearchTextClause(clause: Record<string, unknown>) {
  return typeof clause.text === "string" || typeof clause.q === "string";
}

export function isSearchFieldClause(clause: Record<string, unknown>) {
  return typeof (clause.field_id ?? clause.field) === "string";
}

export function searchTextFilter(node: DocsNode) {
  const clause = searchQueryClauses(node.query_json).find(isSearchTextClause);
  return typeof clause?.text === "string" ? clause.text : typeof clause?.q === "string" ? clause.q : "";
}

export function normalizeSearchFieldOp(value: unknown): SearchFieldFilterOp {
  return SEARCH_FIELD_FILTER_OPS.some((item) => item.value === value) ? value as SearchFieldFilterOp : "contains";
}

export function searchFieldFilter(node: DocsNode): SearchFieldFilterDraft {
  const clause = searchQueryClauses(node.query_json).find(isSearchFieldClause);
  return {
    fieldId: typeof (clause?.field_id ?? clause?.field) === "string" ? String(clause?.field_id ?? clause?.field) : "",
    op: normalizeSearchFieldOp(clause?.op ?? clause?.operator),
    value: clause?.value === undefined || clause?.value === null ? "" : String(clause.value),
  };
}

export function searchProjectScope(node: DocsNode) {
  const projectId = node.query_json?.project_id;
  return typeof projectId === "string" ? projectId : "";
}

export function searchGroupBy(node: DocsNode) {
  const groupBy = node.query_json?.group_by;
  if (typeof groupBy === "string") return groupBy;
  if (groupBy && typeof groupBy === "object" && !Array.isArray(groupBy)) {
    const fieldId = (groupBy as Record<string, unknown>).field_id ?? (groupBy as Record<string, unknown>).field;
    return typeof fieldId === "string" ? fieldId : "";
  }
  return "";
}

export function withSearchTextFilter(query: unknown, text: string) {
  const trimmed = text.trim();
  return replaceSearchClause(query, isSearchTextClause, trimmed ? { text: trimmed } : null);
}

export function withSearchFieldFilter(query: unknown, filter: SearchFieldFilterDraft) {
  const fieldId = filter.fieldId.trim();
  const op = normalizeSearchFieldOp(filter.op);
  const needsValue = SEARCH_FIELD_FILTER_OPS.find((item) => item.value === op)?.needsValue !== false;
  return replaceSearchClause(
    query,
    isSearchFieldClause,
    fieldId
      ? {
          field_id: fieldId,
          op,
          ...(needsValue ? { value: filter.value } : {}),
        }
      : null,
  );
}
export function withSearchProjectScope(query: unknown, projectId: string) {
  const record = { ...readConfigRecord(query) };
  if (projectId) {
    record.project_id = projectId;
  } else {
    delete record.project_id;
  }
  return record;
}

export function withSearchGroupBy(query: unknown, fieldId: string) {
  const record = { ...readConfigRecord(query) };
  const trimmed = fieldId.trim();
  if (trimmed) {
    record.group_by = trimmed;
  } else {
    delete record.group_by;
  }
  return record;
}

export function safeNodeDisplayProps(node: DocsNode) {
  const rest = { ...(node.display_props ?? {}) };
  delete rest.placement_id;
  delete rest.placement_reference;
  delete rest.collapsed;
  return rest;
}

export function snapshotDocsNode(node: DocsNode): DocsBlockSnapshot {
  return {
    id: node.id,
    parent_id: node.parent_id,
    title: node.title,
    description: node.description,
    body_text: node.body_text,
    body_json: node.body_json,
    node_type: node.node_type,
    sort_order: node.sort_order,
  };
}

export function patchFromSnapshot(snapshot: DocsBlockSnapshot): NodePatch {
  return {
    parent_id: snapshot.parent_id,
    title: snapshot.title,
    description: snapshot.description ?? "",
    // body_text は title の検索ミラー。履歴の古いミラー値で空タイトルを
    // 差し戻さないよう、復元時は正本から再構成する。
    body_text: snapshot.title,
    body_json: snapshot.body_json ?? {},
    node_type: snapshot.node_type as DocsNode["node_type"],
    sort_order: snapshot.sort_order,
  };
}

export function nodeDateDelta(node: DocsNode, deltaDays: number) {
  if (!node.day_date) return null;
  const date = new Date(`${node.day_date.slice(0, 10)}T00:00:00+09:00`);
  if (Number.isNaN(date.getTime())) return null;
  date.setDate(date.getDate() + deltaDays);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

export function titleTagNames(title: string) {
  return Array.from(title.matchAll(/(^|\s)#([\p{L}\p{N}_-]+)/gu)).map((match) => match[2]);
}

export function titleWithoutTagTokens(title: string) {
  return title.replace(/(^|\s)#[\p{L}\p{N}_-]+/gu, " ").replace(/\s+/g, " ").trim();
}

export function tagSetByNodeId(nodeSupertags: DocsState["node_supertags"]) {
  const map = new Map<string, Set<string>>();
  for (const relation of nodeSupertags) {
    const next = map.get(relation.node_id) ?? new Set<string>();
    next.add(relation.supertag_id);
    map.set(relation.node_id, next);
  }
  return map;
}

export function tagIdsFromRelatedConfig(tag: DocsSupertag) {
  const config = readConfigRecord(tag.config_json);
  const related = config.related_content ?? config.relatedContent;
  const query = related && typeof related === "object" && !Array.isArray(related)
    ? (related as Record<string, unknown>).query ?? related
    : null;
  const record = query && typeof query === "object" && !Array.isArray(query)
    ? query as Record<string, unknown>
    : null;
  const clauses = Array.isArray(record?.and) ? record.and : [];
  return clauses
    .map((clause) => (clause && typeof clause === "object" && "tag" in clause ? clause.tag : null))
    .filter((value): value is string => typeof value === "string");
}

export function searchView(node: DocsNode): SearchView {
  const view = node.view_json?.view;
  return view === "board" || view === "calendar" || view === "cards" || view === "list" || view === "table"
    ? view
    : "list";
}

export function searchSort(node: DocsNode): SearchSort {
  const sort = node.query_json?.sort;
  return sort === "updated_desc" || sort === "updated_asc" || sort === "title_asc" || sort === "title_desc"
    ? sort
    : "";
}

export function readConfigRecord(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
