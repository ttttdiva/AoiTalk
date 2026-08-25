import {
  hasMeaningfulBlockTitle,
  isExplicitBlankParagraph,
  type DocsBlockSnapshot,
} from "@/lib/docs-block-model";
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

export const DOCS_WORKSPACE_UNMOUNTED_MESSAGE = "Docs Workspaceは既に閉じられています";

export function isDocsWorkspaceUnmountedError(error: unknown): boolean {
  return error instanceof Error && error.message === DOCS_WORKSPACE_UNMOUNTED_MESSAGE;
}

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
/** @deprecated Sidebar expansion is session-only; removed on Docs mount. Kept for legacy cleanup. */
export const SIDEBAR_COLLAPSED_KEY = "aoitalk.docs.sidebar.collapsed";
// 子は遅延読込のため「collapsed に無い」だけでは展開を復元できない（未読込＝折りたたみ表示）。
// ユーザーが実際に開いたノードをここに記録し、再訪時に子を先読みして展開状態へ戻す。
export const EXPANDED_KEY = "aoitalk.docs.outline.expanded";
// 復元で先読みする親ノードの上限。Docs は「基本は格納」で上位ノードを軽く保つ設計なので、
// 起動時に走る自動先読みは小さく抑える。まとめて開きたい時は Ctrl+→ の2回押しを使う。
export const EXPANDED_RESTORE_LIMIT = 15;
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
  if (isExplicitBlankParagraph(node.title, node.body_json, node.node_type)) return "";
  return plainDocsTitle(node.title || node.body_text) || "Untitled";
}

/**
 * Explicit blank paragraphs are persisted outline entries and remain visible
 * as empty rows. Existing legacy rows can still be present in old workspaces,
 * so every outline/list projection applies the same contract instead of
 * rendering arbitrary blank nodes as "Untitled".
 */
export function isDocsNodeTitleVisible(
  node: Pick<DocsNode, "title"> & Partial<Pick<DocsNode, "body_json" | "node_type">>,
) {
  // The serialized Docs contract always carries a string title. Treat an
  // absent field from older/partial client snapshots as visible for
  // backwards compatibility; only an explicit blank title is a legacy row.
  if (typeof node.title !== "string") return true;
  if (hasMeaningfulBlockTitle(node.title)) return true;
  return isExplicitBlankParagraph(node.title, node.body_json, node.node_type);
}

/**
 * Legacy mail importers used a literal ``（空行）`` label for an empty line.
 * It is data, not a general-purpose blank marker: hide it only when the node
 * is reachable from an email-origin document.  Ordinary user nodes with the
 * same title remain visible.
 */
export function isLegacyEmailEmptyLineNode(
  node: Pick<DocsNode, "id" | "title" | "parent_id" | "system_key" | "body_json">,
  nodesById: Map<string, Pick<DocsNode, "id" | "parent_id" | "system_key" | "body_json">>,
) {
  if (node.title !== "（空行）") return false;
  const isEmailDocument = (candidate: Pick<DocsNode, "system_key" | "body_json">) =>
    candidate.body_json?.format === "email"
    || candidate.system_key?.startsWith("project_mail:") === true;
  if (isEmailDocument(node)) return true;
  const seen = new Set<string>();
  let parentId = node.parent_id;
  while (parentId && !seen.has(parentId)) {
    seen.add(parentId);
    const parent = nodesById.get(parentId);
    if (!parent) break;
    if (isEmailDocument(parent)) return true;
    parentId = parent.parent_id;
  }
  return false;
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

const LEGACY_EMAIL_FIELD_KEYS = new Map([
  ["件名", "email_subject"],
  ["メール日時", "email_date"],
  ["From", "email_from"],
  ["To", "email_to"],
  ["CC", "email_cc"],
  ["BCC", "email_bcc"],
  ["Message-ID", "email_message_id"],
  ["In-Reply-To", "email_in_reply_to"],
  ["References", "email_references"],
  ["元ファイル名", "email_source_filename"],
  ["元ファイルのプロジェクト内パス", "email_source_path"],
  ["本文", "email_body"],
]);

function legacyEmailOutlineChunks(value: string, maxChunks: number) {
  const chunks: string[] = [];
  let truncated = false;
  for (const line of value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n")) {
    const characters = Array.from(line);
    const lineChunks = characters.length === 0
      ? ["（空行）"]
      : Array.from(
        { length: Math.ceil(characters.length / 450) },
        (_, index) => characters.slice(index * 450, (index + 1) * 450).join(""),
      );
    for (const chunk of lineChunks) {
      if (chunks.length >= maxChunks) {
        truncated = true;
        break;
      }
      chunks.push(chunk);
    }
    if (truncated) break;
  }
  if (truncated) chunks[chunks.length - 1] = "（続きはノードのフィールドに全文保存されています）";
  return chunks.length > 0 ? chunks : ["（空）"];
}

export function isLegacyEmailOutlineCandidate(
  documentNode: DocsNode,
  documentTags: DocsSupertag[],
  candidate: DocsNode,
) {
  return documentNode.body_json?.format === "email"
    && documentNode.system_key?.startsWith("project_mail:") === true
    && documentTags.some((tag) => tag.system_key === "email")
    && candidate.parent_id === documentNode.id
    && LEGACY_EMAIL_FIELD_KEYS.has(candidate.title)
    && !candidate.system_key
    && candidate.display_props?.placement_reference !== true;
}

export function suppressLegacyEmailOutlineRows(
  rows: Array<{ node: DocsNode; depth: number }>,
  documentNode: DocsNode,
  documentTags: DocsSupertag[],
  documentFields: DocsField[],
  documentFieldValues: DocsFieldValue[],
  childrenByParent: Map<string | null, DocsNode[]>,
  nodeHasChildren: (nodeId: string) => boolean,
) {
  if (!documentTags.some((tag) => tag.system_key === "email")) return rows;

  let hiddenRootDepth: number | null = null;
  return rows.filter((row) => {
    if (hiddenRootDepth !== null) {
      if (row.depth > hiddenRootDepth) return false;
      hiddenRootDepth = null;
    }
    const fieldSystemKey = LEGACY_EMAIL_FIELD_KEYS.get(row.node.title);
    const field = fieldSystemKey
      ? documentFields.find((candidate) => candidate.system_key === fieldSystemKey)
      : undefined;
    const fieldValue = field
      ? documentFieldValues.find((candidate) => candidate.field_id === field.id)
      : undefined;
    const expectedChunks = field
      ? legacyEmailOutlineChunks(fieldValueToDraft(fieldValue), fieldSystemKey === "email_body" ? 32 : 4)
      : [];
    const actualChildren = (childrenByParent.get(row.node.id) ?? [])
      .filter((child) => !child.archived_at);
    const childrenExactlyMatch =
      expectedChunks.length === actualChildren.length
      && actualChildren.every((child, index) =>
        !child.system_key
        && child.display_props?.placement_reference !== true
        && !nodeHasChildren(child.id)
        && child.title === expectedChunks[index]);
    const isLegacyMirror =
      row.depth === 0
      && isLegacyEmailOutlineCandidate(documentNode, documentTags, row.node)
      && field !== undefined
      && actualChildren.length > 0
      && childrenExactlyMatch;
    if (!isLegacyMirror) return true;
    hiddenRootDepth = row.depth;
    return false;
  });
}

export function outlineRows(
  parentId: string,
  childrenByParent: Map<string | null, DocsNode[]>,
  collapsed: Set<string>,
  depth = 0,
  path = new Set<string>(),
  isVisible: (node: DocsNode) => boolean = isDocsNodeTitleVisible,
) {
  const rows: Array<{ node: DocsNode; depth: number }> = [];
  for (const child of childrenByParent.get(parentId) ?? []) {
    if (child.archived_at) continue;
    if (path.has(child.id)) continue;
    if (!isVisible(child)) {
      // A legacy blank node must not hide meaningful descendants. Keep the
      // hierarchy readable by walking through the invisible row at the same
      // depth without exposing it as a KnowledgeNode row.
      rows.push(...outlineRows(child.id, childrenByParent, collapsed, depth, new Set([...path, child.id]), isVisible));
      continue;
    }
    rows.push({ node: child, depth });
    if (!collapsed.has(child.id)) {
      rows.push(...outlineRows(child.id, childrenByParent, collapsed, depth + 1, new Set([...path, child.id]), isVisible));
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
  const record = normalizeSearchQuery(query);
  return Array.isArray(record.and)
    ? record.and.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
}

export function replaceSearchClause(
  query: unknown,
  predicate: (clause: Record<string, unknown>) => boolean,
  nextClause: Record<string, unknown> | null,
) {
  const record = normalizeSearchQuery(query);
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

export function searchGroupBy(node: DocsNode) {
  const groupBy = normalizeSearchQuery(node.query_json).group_by;
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
export function withSearchGroupBy(query: unknown, fieldId: string) {
  const record = normalizeSearchQuery(query);
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

/** Return a sidebar/outline list while hoisting legacy invisible blank rows. */
export function hoistedVisibleChildren(
  childrenByParent: Map<string | null, DocsNode[]>,
  parentId: string | null,
  isVisible: (node: DocsNode) => boolean,
  path = new Set<string>(),
) {
  const result: DocsNode[] = [];
  for (const child of childrenByParent.get(parentId) ?? []) {
    if (path.has(child.id) || child.archived_at) continue;
    if (isVisible(child)) {
      result.push(child);
      continue;
    }
    // Only title-invisible rows are hoisted.  A meaningful row hidden by an
    // explicit sidebar policy must continue to hide its subtree as before.
    if (!isDocsNodeTitleVisible(child)) {
      result.push(...hoistedVisibleChildren(
        childrenByParent,
        child.id,
        isVisible,
        new Set([...path, child.id]),
      ));
    }
  }
  return result;
}

/**
 * Search nodes are library-wide.  Older saved views may still carry the
 * retired `project_id` query key; remove it at every search boundary so that
 * those views cannot silently reintroduce a project-only scope.
 */
export function normalizeSearchQuery(value: unknown) {
  const record = { ...readConfigRecord(value) };
  delete record.project_id;
  return record;
}
