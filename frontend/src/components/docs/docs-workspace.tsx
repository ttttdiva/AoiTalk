"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import {
  Archive,
  AtSign,
  CalendarDays,
  CheckSquare,
  ChevronDown,
  ChevronRight,
  Columns2,
  ExternalLink,
  KanbanSquare,
  Hash,
  Link2,
  ListFilter,
  Plus,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Tags,
  Table2,
  Type,
  X,
  type LucideIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useProject } from "@/contexts/project-context";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { createDocsNodeWikilink } from "@/lib/docs-references";
import { midpointSortOrder, sortNodesByPosition } from "@/lib/docs-block-model";
import { cn } from "@/lib/utils";
import { FieldControl } from "./field-control";
import { OutlineDocumentEditor, type OutlineEditorRow } from "./outline/outline-editor";
import type { OutlineOperation } from "./outline/outline-doc";
import type {
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsProject,
  DocsReference,
  ReferencesState,
  DocsState,
  DocsSupertag,
  DocsSavedView,
  DocsAiSuggestion,
} from "./types";
import { EMPTY_REFERENCES, EMPTY_STATE } from "./types";
import {
  apiFetch,
  buildBreadcrumb,
  buildChildrenByParent,
  docsFieldType,
  fieldDraftToPayload,
  fieldOptions,
  fieldValueToDraft,
  projectsFromContext,
  tagColorStyle,
} from "./docs-utils";

type TodayResponse = {
  node: DocsNode;
  supertag?: DocsSupertag;
  node_supertags?: DocsState["node_supertags"];
};

type DocsTaskBinding = {
  id: string;
  project_id: string | null;
  knowledge_node_id: string | null;
  title: string;
  status: string | null;
};

type NodePatch = Partial<
  Pick<DocsNode, "title" | "description" | "display_props" | "query_json" | "view_json" | "parent_id" | "sort_order" | "node_type" | "project_id">
>;

type SearchView = "table" | "board" | "calendar" | "cards" | "list";
type SearchSort = "" | "updated_desc" | "updated_asc" | "title_asc" | "title_desc";
type SearchFieldFilterOp = "=" | "!=" | "contains" | "is_set" | "not_set" | ">" | "<" | ">=" | "<=";
type SearchFieldFilterDraft = {
  fieldId: string;
  op: SearchFieldFilterOp;
  value: string;
};

type DocsQueryResponse = Pick<DocsState, "nodes" | "node_supertags" | "field_values"> & {
  next_cursor?: string | null;
};

type SidebarContextMenuState = {
  x: number;
  y: number;
  nodeId: string;
};

const SEARCH_SORT_OPTIONS: Array<{ value: SearchSort; label: string }> = [
  { value: "", label: "Default sort" },
  { value: "updated_desc", label: "Updated desc" },
  { value: "updated_asc", label: "Updated asc" },
  { value: "title_asc", label: "Title A-Z" },
  { value: "title_desc", label: "Title Z-A" },
];

const SEARCH_FIELD_FILTER_OPS: Array<{ value: SearchFieldFilterOp; label: string; needsValue: boolean }> = [
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

type LoadOptions = {
  focusToday?: boolean;
  date?: string;
};

type DocsAiCommandResult = {
  suggestion?: DocsAiSuggestion;
  result?: {
    mode?: string;
    lines?: string[];
    replacement?: string;
    fields?: Array<{ name?: string; value?: string }>;
    summary?: string;
  };
};

type DocsAiPreview = {
  node: DocsNode;
  command: "continue" | "extract_tasks" | "rewrite";
  suggestionId?: string;
  result: NonNullable<DocsAiCommandResult["result"]>;
};

type AutocompleteKind = "tag" | "ref" | "slash";

type InlineAutocomplete = {
  kind: AutocompleteKind;
  nodeId: string;
  query: string;
  from: number;
  to: number;
};

type DocsCommandMode =
  | { kind: "root" }
  | { kind: "tag" }
  | { kind: "move"; leaveReference: boolean }
  | { kind: "view" }
  | { kind: "field"; fieldId: string };

const SUPERTAGS_OVERVIEW_ID = "__supertags_overview__";

const COLLAPSED_KEY = "aoitalk.docs.outline.collapsed";

function readCollapsed(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const parsed = JSON.parse(window.localStorage.getItem(COLLAPSED_KEY) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(value: Set<string>) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(Array.from(value)));
}

function nodeText(node: DocsNode) {
  return inlinePlainText(node.title || node.body_text || "Untitled");
}

function renderNodeTitleTemplate(
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

function mergeById<T extends { id: string }>(current: T[], next: T) {
  return current.some((item) => item.id === next.id)
    ? current.map((item) => (item.id === next.id ? next : item))
    : [...current, next];
}

function valueByNodeField(values: DocsFieldValue[]) {
  return new Map(values.map((value) => [`${value.node_id}:${value.field_id}`, value]));
}

function fieldsForNode(
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

function outlineRows(
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

function sidebarNodeMatches(
  node: DocsNode,
  query: string,
  childrenByParent: Map<string | null, DocsNode[]>,
  path = new Set<string>(),
) {
  if (!query) return true;
  if (nodeText(node).toLowerCase().includes(query)) return true;
  if (path.has(node.id)) return false;
  for (const child of childrenByParent.get(node.id) ?? []) {
    if (child.archived_at) continue;
    if (sidebarNodeMatches(child, query, childrenByParent, new Set([...path, node.id]))) return true;
  }
  return false;
}

function buildOutlineChildren(nodes: DocsNode[], placements: DocsState["placements"]) {
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

function searchTagIds(node: DocsNode) {
  const query = node.query_json;
  const clauses = Array.isArray(query?.and) ? query.and : [];
  return clauses
    .map((clause) => (clause && typeof clause === "object" && "tag" in clause ? clause.tag : null))
    .filter((value): value is string => typeof value === "string");
}

function searchQueryClauses(query: unknown) {
  const record = readConfigRecord(query);
  return Array.isArray(record.and)
    ? record.and.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
}

function replaceSearchClause(
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

function isSearchTextClause(clause: Record<string, unknown>) {
  return typeof clause.text === "string" || typeof clause.q === "string";
}

function isSearchFieldClause(clause: Record<string, unknown>) {
  return typeof (clause.field_id ?? clause.field) === "string";
}

function searchTextFilter(node: DocsNode) {
  const clause = searchQueryClauses(node.query_json).find(isSearchTextClause);
  return typeof clause?.text === "string" ? clause.text : typeof clause?.q === "string" ? clause.q : "";
}

function normalizeSearchFieldOp(value: unknown): SearchFieldFilterOp {
  return SEARCH_FIELD_FILTER_OPS.some((item) => item.value === value) ? value as SearchFieldFilterOp : "contains";
}

function searchFieldFilter(node: DocsNode): SearchFieldFilterDraft {
  const clause = searchQueryClauses(node.query_json).find(isSearchFieldClause);
  return {
    fieldId: typeof (clause?.field_id ?? clause?.field) === "string" ? String(clause?.field_id ?? clause?.field) : "",
    op: normalizeSearchFieldOp(clause?.op ?? clause?.operator),
    value: clause?.value === undefined || clause?.value === null ? "" : String(clause.value),
  };
}

function searchProjectScope(node: DocsNode) {
  const projectId = node.query_json?.project_id;
  return typeof projectId === "string" ? projectId : "";
}

function searchGroupBy(node: DocsNode) {
  const groupBy = node.query_json?.group_by;
  if (typeof groupBy === "string") return groupBy;
  if (groupBy && typeof groupBy === "object" && !Array.isArray(groupBy)) {
    const fieldId = (groupBy as Record<string, unknown>).field_id ?? (groupBy as Record<string, unknown>).field;
    return typeof fieldId === "string" ? fieldId : "";
  }
  return "";
}

function withSearchTextFilter(query: unknown, text: string) {
  const trimmed = text.trim();
  return replaceSearchClause(query, isSearchTextClause, trimmed ? { text: trimmed } : null);
}

function withSearchFieldFilter(query: unknown, filter: SearchFieldFilterDraft) {
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
function withSearchProjectScope(query: unknown, projectId: string) {
  const record = { ...readConfigRecord(query) };
  if (projectId) {
    record.project_id = projectId;
  } else {
    delete record.project_id;
  }
  return record;
}

function withSearchGroupBy(query: unknown, fieldId: string) {
  const record = { ...readConfigRecord(query) };
  const trimmed = fieldId.trim();
  if (trimmed) {
    record.group_by = trimmed;
  } else {
    delete record.group_by;
  }
  return record;
}

function safeNodeDisplayProps(node: DocsNode) {
  const rest = { ...(node.display_props ?? {}) };
  delete rest.placement_id;
  delete rest.placement_reference;
  delete rest.collapsed;
  return rest;
}

function nodeDateDelta(node: DocsNode, deltaDays: number) {
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

function titleTagNames(title: string) {
  return Array.from(title.matchAll(/(^|\s)#([\p{L}\p{N}_-]+)/gu)).map((match) => match[2]);
}

function titleWithoutTagTokens(title: string) {
  return title.replace(/(^|\s)#[\p{L}\p{N}_-]+/gu, " ").replace(/\s+/g, " ").trim();
}

function tagSetByNodeId(nodeSupertags: DocsState["node_supertags"]) {
  const map = new Map<string, Set<string>>();
  for (const relation of nodeSupertags) {
    const next = map.get(relation.node_id) ?? new Set<string>();
    next.add(relation.supertag_id);
    map.set(relation.node_id, next);
  }
  return map;
}

function tagIdsFromRelatedConfig(tag: DocsSupertag) {
  const related = tag.config_json.related_content ?? tag.config_json.relatedContent;
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

function searchView(node: DocsNode): SearchView {
  const view = node.view_json?.view;
  return view === "board" || view === "calendar" || view === "cards" || view === "list" || view === "table"
    ? view
    : "list";
}

function searchSort(node: DocsNode): SearchSort {
  const sort = node.query_json?.sort;
  return sort === "updated_desc" || sort === "updated_asc" || sort === "title_asc" || sort === "title_desc"
    ? sort
    : "";
}

function inlinePlainText(value: string) {
  return value
    .replace(/\[\[node:[0-9a-f-]{36}\|([^\]\n]+)\]\]/giu, "$1")
    .replace(/\[\[([^\]\n]+)\]\]/g, "$1")
    .trim();
}

function replaceInlineRange(text: string, range: Pick<InlineAutocomplete, "from" | "to">, replacement: string) {
  const before = text.slice(0, range.from).replace(/\s*$/, "");
  const after = text.slice(range.to);
  return `${before ? `${before} ` : ""}${replacement}${after.startsWith(" ") || !after ? " " : ""}${after}`.replace(/\s+$/g, " ");
}

function readConfigRecord(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function DocsWorkspace({ initialNodeId }: { initialNodeId?: string | null }) {
  const { allProjects } = useProject();
  const [state, setState] = useState<DocsState>(EMPTY_STATE);
  const [focusNodeId, setFocusNodeId] = useState<string | null>(initialNodeId ?? null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([]);
  const [selectionAnchorNodeId, setSelectionAnchorNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed());
  const [quickQuery, setQuickQuery] = useState("");
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandMode, setCommandMode] = useState<DocsCommandMode>({ kind: "root" });
  const [rightPanel, setRightPanel] = useState<"related" | "tags" | "search" | "trash">("related");
  const [splitNodeId, setSplitNodeId] = useState<string | null>(null);
  const [newTagName, setNewTagName] = useState("");
  const [slashNodeId, setSlashNodeId] = useState<string | null>(null);
  const [inlineAutocomplete, setInlineAutocomplete] = useState<InlineAutocomplete | null>(null);
  const [commandQuery, setCommandQuery] = useState("");
  const [tagPageId, setTagPageId] = useState<string | null>(null);
  const [focusRequestNodeId, setFocusRequestNodeId] = useState<string | null>(null);
  const [pageReferences, setPageReferences] = useState<ReferencesState>(EMPTY_REFERENCES);
  const [pageReferencesLoading, setPageReferencesLoading] = useState(false);
  const [sidebarContextMenu, setSidebarContextMenu] = useState<SidebarContextMenuState | null>(null);
  const [dragSidebarNodeId, setDragSidebarNodeId] = useState<string | null>(null);
  const [aiPreview, setAiPreview] = useState<DocsAiPreview | null>(null);
  const preserveSelectionOnNextFocusRef = useRef(false);
  const selectedNodeIdsRef = useRef<string[]>([]);
  const selectionAnchorNodeIdRef = useRef<string | null>(null);

  const projects = projectsFromContext(state.projects, allProjects);
  const nodesById = useMemo(() => new Map(state.nodes.map((node) => [node.id, node])), [state.nodes]);
  const childrenByParent = useMemo(() => buildOutlineChildren(state.nodes, state.placements), [state.nodes, state.placements]);
  const tagById = useMemo(() => new Map(state.supertags.map((tag) => [tag.id, tag])), [state.supertags]);
  const nodeTags = useMemo(() => {
    const map = new Map<string, DocsSupertag[]>();
    for (const relation of state.node_supertags) {
      const tag = tagById.get(relation.supertag_id);
      if (!tag) continue;
      const next = map.get(relation.node_id) ?? [];
      next.push(tag);
      map.set(relation.node_id, next);
    }
    return map;
  }, [state.node_supertags, tagById]);
  const fieldsByTag = useMemo(() => {
    const fieldsById = new Map(state.fields.map((field) => [field.id, field]));
    const map = new Map<string, DocsField[]>();
    for (const relation of state.supertag_fields) {
      const field = fieldsById.get(relation.field_id);
      if (!field) continue;
      const next = map.get(relation.supertag_id) ?? [];
      next.push({ ...field, sort_order: relation.sort_order, required: relation.required || field.required });
      map.set(relation.supertag_id, next);
    }
    for (const field of state.fields) {
      if (!field.supertag_id) continue;
      const next = map.get(field.supertag_id) ?? [];
      if (!next.some((item) => item.id === field.id)) next.push(field);
      map.set(field.supertag_id, next);
    }
    for (const [tagId, fields] of map.entries()) map.set(tagId, [...fields].sort((a, b) => a.sort_order - b.sort_order));
    return map;
  }, [state.fields, state.supertag_fields]);
  const fieldValuesByKey = useMemo(() => valueByNodeField(state.field_values), [state.field_values]);
  const tagSetByNode = useMemo(() => tagSetByNodeId(state.node_supertags), [state.node_supertags]);
  const selectedNodeIdSet = useMemo(() => new Set(selectedNodeIds), [selectedNodeIds]);

  const focusNode = focusNodeId ? nodesById.get(focusNodeId) ?? null : null;
  const focusNodeReferenceId = !tagPageId ? focusNode?.id ?? null : null;
  const selectedNode = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : focusNode;
  const activeTagPage = tagPageId && tagPageId !== SUPERTAGS_OVERVIEW_ID ? tagById.get(tagPageId) ?? null : null;
  const roots = useMemo(() => sortNodesByPosition(state.nodes.filter((node) => !node.parent_id && !node.archived_at)), [state.nodes]);
  const archivedNodes = useMemo(() => state.nodes.filter((node) => !!node.archived_at).sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? "")), [state.nodes]);
  const currentRows = useMemo(() => (focusNode ? outlineRows(focusNode.id, childrenByParent, collapsed) : []), [childrenByParent, collapsed, focusNode]);
  const splitNode = splitNodeId ? nodesById.get(splitNodeId) ?? null : null;
  const splitRows = useMemo(() => (splitNode ? outlineRows(splitNode.id, childrenByParent, collapsed) : []), [childrenByParent, collapsed, splitNode]);
  const selectedNodes = useMemo(() => selectedNodeIds.map((nodeId) => nodesById.get(nodeId)).filter((node): node is DocsNode => Boolean(node)), [nodesById, selectedNodeIds]);
  const actionNodes = selectedNodes.length > 1 ? selectedNodes : selectedNode ? [selectedNode] : [];
  const sidebarContextNode = sidebarContextMenu ? nodesById.get(sidebarContextMenu.nodeId) ?? null : null;
  const relatedNodes = useMemo(() => {
    if (!selectedNode) return [];
    const selectedTagIds = tagSetByNode.get(selectedNode.id) ?? new Set<string>();
    if (selectedTagIds.size === 0) return [];
    const configuredTagIds = new Set<string>();
    for (const tagId of selectedTagIds) {
      const tag = tagById.get(tagId);
      if (!tag) continue;
      for (const relatedTagId of tagIdsFromRelatedConfig(tag)) configuredTagIds.add(relatedTagId);
    }
    if (configuredTagIds.size === 0) return [];
    const acceptedTags = configuredTagIds;
    return state.nodes
      .filter((node) => node.id !== selectedNode.id && !node.archived_at)
      .filter((node) => {
        const tags = tagSetByNode.get(node.id) ?? new Set<string>();
        return Array.from(acceptedTags).some((tagId) => tags.has(tagId));
      })
      .slice(0, 20);
  }, [selectedNode, state.nodes, tagById, tagSetByNode]);
  const commandMoveTargets = useMemo(() => {
    if (!selectedNode) return [];
    const query = commandQuery.trim().toLowerCase();
    return state.nodes
      .filter((node) => !selectedNodeIdSet.has(node.id) && !selectedNodeIdSet.has(node.parent_id ?? "") && !node.archived_at)
      .filter((node) => {
        let parentId = node.parent_id;
        while (parentId) {
          if (selectedNodeIdSet.has(parentId)) return false;
          parentId = nodesById.get(parentId)?.parent_id ?? null;
        }
        return true;
      })
      .filter((node) => !query || nodeText(node).toLowerCase().includes(query))
      .slice(0, 500);
  }, [commandQuery, nodesById, selectedNode, selectedNodeIdSet, state.nodes]);
  const commandFields = useMemo(() => selectedNode ? fieldsForNode(selectedNode, nodeTags, fieldsByTag) : [], [fieldsByTag, nodeTags, selectedNode]);

  const openCommand = useCallback((mode: DocsCommandMode = { kind: "root" }) => {
    setCommandQuery("");
    setCommandMode(mode);
    setCommandOpen(true);
  }, []);

  const selectSingleNode = useCallback((nodeId: string | null) => {
    selectedNodeIdsRef.current = nodeId ? [nodeId] : [];
    selectionAnchorNodeIdRef.current = nodeId;
    setSelectedNodeId(nodeId);
    setSelectedNodeIds(nodeId ? [nodeId] : []);
    setSelectionAnchorNodeId(nodeId);
  }, []);

  const extendNodeSelection = useCallback((node: DocsNode, rows: Array<{ node: DocsNode; depth: number }>, direction: -1 | 1) => {
    const currentIndex = rows.findIndex((row) => row.node.id === node.id);
    if (currentIndex < 0) return;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? node.id;
    const anchorIndex = Math.max(0, rows.findIndex((row) => row.node.id === anchorId));
    const targetIndex = Math.max(0, Math.min(rows.length - 1, currentIndex + direction));
    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const nextIds = rows.slice(start, end + 1).map((row) => row.node.id);
    const targetNode = rows[targetIndex]?.node;
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = rows[anchorIndex]?.node.id ?? node.id;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(targetNode?.id ?? node.id);
    setSelectionAnchorNodeId(rows[anchorIndex]?.node.id ?? node.id);
    if (targetNode) {
      preserveSelectionOnNextFocusRef.current = true;
      setFocusRequestNodeId(targetNode.id);
    }
  }, [selectionAnchorNodeId]);

  const selectRangeToNode = useCallback((node: DocsNode, rows: Array<{ node: DocsNode; depth: number }>) => {
    const currentIndex = rows.findIndex((row) => row.node.id === node.id);
    if (currentIndex < 0) return;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? node.id;
    const anchorIndex = Math.max(0, rows.findIndex((row) => row.node.id === anchorId));
    const start = Math.min(anchorIndex, currentIndex);
    const end = Math.max(anchorIndex, currentIndex);
    const nextIds = rows.slice(start, end + 1).map((row) => row.node.id);
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = rows[anchorIndex]?.node.id ?? node.id;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(node.id);
    setSelectionAnchorNodeId(rows[anchorIndex]?.node.id ?? node.id);
  }, [selectionAnchorNodeId]);

  const selectDomRangeById = useCallback((nodeId: string, direction?: -1 | 1) => {
    const visibleIds = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-node-id]"))
      .map((element) => element.getAttribute("data-docs-node-id"))
      .filter((id): id is string => Boolean(id))
      .filter((id, index, all) => all.indexOf(id) === index);
    const currentIndex = visibleIds.indexOf(nodeId);
    if (currentIndex < 0) return false;
    const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? nodeId;
    const anchorIndex = Math.max(0, visibleIds.indexOf(anchorId));
    const targetIndex = typeof direction === "number"
      ? Math.max(0, Math.min(visibleIds.length - 1, currentIndex + direction))
      : currentIndex;
    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const nextIds = visibleIds.slice(start, end + 1);
    const targetId = visibleIds[targetIndex] ?? nodeId;
    selectedNodeIdsRef.current = nextIds;
    selectionAnchorNodeIdRef.current = visibleIds[anchorIndex] ?? nodeId;
    setSelectedNodeIds(nextIds);
    setSelectedNodeId(targetId);
    setSelectionAnchorNodeId(visibleIds[anchorIndex] ?? nodeId);
    if (direction && targetId) {
      preserveSelectionOnNextFocusRef.current = true;
      setFocusRequestNodeId(targetId);
    }
    return true;
  }, [selectionAnchorNodeId]);

  const selectedOutlineText = useMemo(() => {
    if (selectedNodeIds.length <= 1) return "";
    const rowById = new Map<string, { node: DocsNode; depth: number }>();
    for (const row of currentRows) rowById.set(row.node.id, row);
    for (const row of splitRows) rowById.set(row.node.id, row);
    return selectedNodeIds
      .map((nodeId) => {
        const row = rowById.get(nodeId);
        const node = row?.node ?? nodesById.get(nodeId);
        if (!node) return "";
        return `${"  ".repeat(row?.depth ?? 0)}- ${nodeText(node)}`;
      })
      .filter(Boolean)
      .join("\n");
  }, [currentRows, nodesById, selectedNodeIds, splitRows]);

  const load = useCallback(async (options: LoadOptions = {}) => {
    setLoading(true);
    try {
      const today = await apiFetch<TodayResponse>(`/api/docs/today${options.date ? `?date=${encodeURIComponent(options.date)}` : ""}`);
      const data = await apiFetch<DocsState>("/api/docs/bootstrap");
      const nextState = {
        ...EMPTY_STATE,
        ...data,
        nodes: mergeById(data.nodes ?? [], today.node),
        supertags: today.supertag ? mergeById(data.supertags ?? [], today.supertag) : data.supertags ?? [],
        node_supertags: today.node_supertags?.length
          ? [...(data.node_supertags ?? []), ...today.node_supertags.filter((item) => !(data.node_supertags ?? []).some((entry) => entry.node_id === item.node_id && entry.supertag_id === item.supertag_id))]
          : data.node_supertags ?? [],
      };
      setState(nextState);
      setFocusNodeId((current) => (options.focusToday ? today.node.id : current ?? today.node.id));
      setSelectedNodeId((current) => {
        const nextId = options.focusToday ? today.node.id : current ?? today.node.id;
        selectedNodeIdsRef.current = nextId ? [nextId] : [];
        selectionAnchorNodeIdRef.current = nextId;
        setSelectedNodeIds(nextId ? [nextId] : []);
        setSelectionAnchorNodeId(nextId);
        return nextId;
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docsの読み込みに失敗しました");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openToday = useCallback((date?: string) => {
    setTagPageId(null);
    void load({ focusToday: true, date });
  }, [load]);

  useEffect(() => {
    if (!focusNodeReferenceId) {
      setPageReferences(EMPTY_REFERENCES);
      setPageReferencesLoading(false);
      return;
    }
    let cancelled = false;
    setPageReferences(EMPTY_REFERENCES);
    setPageReferencesLoading(true);
    apiFetch<ReferencesState>(`/api/docs/nodes/${focusNodeReferenceId}/references`)
      .then((data) => {
        if (!cancelled) setPageReferences({ ...EMPTY_REFERENCES, ...data });
      })
      .catch((error) => {
        if (!cancelled) toast.error(error instanceof Error ? error.message : "参照の読み込みに失敗しました");
      })
      .finally(() => {
        if (!cancelled) setPageReferencesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [focusNodeReferenceId]);

  useEffect(() => {
    const visibleDomNodeIds = () => Array.from(document.querySelectorAll<HTMLElement>("[data-docs-node-id]"))
      .map((element) => element.getAttribute("data-docs-node-id"))
      .filter((nodeId): nodeId is string => Boolean(nodeId))
      .filter((nodeId, index, all) => all.indexOf(nodeId) === index);
    const selectDomRange = (activeNodeId: string, direction?: -1 | 1) => {
      const visibleIds = visibleDomNodeIds();
      const currentIndex = visibleIds.indexOf(activeNodeId);
      if (currentIndex < 0) return false;
      const anchorId = selectionAnchorNodeIdRef.current ?? selectionAnchorNodeId ?? activeNodeId;
      const anchorIndex = Math.max(0, visibleIds.indexOf(anchorId));
      const targetIndex = typeof direction === "number"
        ? Math.max(0, Math.min(visibleIds.length - 1, currentIndex + direction))
        : currentIndex;
      const start = Math.min(anchorIndex, targetIndex);
      const end = Math.max(anchorIndex, targetIndex);
      const nextIds = visibleIds.slice(start, end + 1);
      const targetId = visibleIds[targetIndex] ?? activeNodeId;
      selectedNodeIdsRef.current = nextIds;
      selectionAnchorNodeIdRef.current = visibleIds[anchorIndex] ?? activeNodeId;
      setSelectedNodeIds(nextIds);
      setSelectedNodeId(targetId);
      setSelectionAnchorNodeId(visibleIds[anchorIndex] ?? activeNodeId);
      if (direction && targetId) {
        preserveSelectionOnNextFocusRef.current = true;
        setFocusRequestNodeId(targetId);
      }
      return true;
    };
    const activeDocsNode = () => {
      const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
      if (!activeNodeId) return null;
      const node = nodesById.get(activeNodeId);
      const rows = currentRows.some((row) => row.node.id === activeNodeId) ? currentRows : splitRows;
      return node && rows.length > 0 ? { node, rows } : null;
    };
    const handleGlobalKey = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      const key = event.key.toLowerCase();
      if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        const active = activeDocsNode();
        const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
        if (activeNodeId && selectDomRange(activeNodeId, event.key === "ArrowUp" ? -1 : 1)) {
          event.preventDefault();
          event.stopImmediatePropagation();
        } else if (active) {
          event.preventDefault();
          event.stopImmediatePropagation();
          extendNodeSelection(active.node, active.rows, event.key === "ArrowUp" ? -1 : 1);
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        event.stopImmediatePropagation();
        openCommand();
      }
      if (event.ctrlKey && event.shiftKey && key === "d") {
        event.preventDefault();
        openToday();
      }
      if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight") && focusNode?.day_date) {
        const targetDate = nodeDateDelta(focusNode, event.key === "ArrowLeft" ? -1 : 1);
        if (targetDate) {
          event.preventDefault();
          openToday(targetDate);
        }
      }
    };
    const handleGlobalKeyUp = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        const active = activeDocsNode();
        const activeNodeId = (document.activeElement as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
        if (activeNodeId && selectDomRange(activeNodeId)) return;
        if (active) selectRangeToNode(active.node, active.rows);
      }
    };
    document.addEventListener("keydown", handleGlobalKey, true);
    document.addEventListener("keyup", handleGlobalKeyUp, true);
    window.addEventListener("keydown", handleGlobalKey, true);
    window.addEventListener("keyup", handleGlobalKeyUp, true);
    return () => {
      document.removeEventListener("keydown", handleGlobalKey, true);
      document.removeEventListener("keyup", handleGlobalKeyUp, true);
      window.removeEventListener("keydown", handleGlobalKey, true);
      window.removeEventListener("keyup", handleGlobalKeyUp, true);
    };
  }, [currentRows, extendNodeSelection, focusNode, nodesById, openCommand, openToday, selectRangeToNode, selectionAnchorNodeId, splitRows]);

  useEffect(() => {
    const handleCopy = (event: ClipboardEvent) => {
      if (selectedNodeIds.length <= 1 || !selectedOutlineText) return;
      event.preventDefault();
      event.clipboardData?.setData("text/plain", selectedOutlineText);
    };
    document.addEventListener("copy", handleCopy);
    return () => document.removeEventListener("copy", handleCopy);
  }, [selectedNodeIds.length, selectedOutlineText]);

  const patchNode = useCallback(async (nodeId: string, patch: NodePatch) => {
    const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, nodes: current.nodes.map((node) => (node.id === data.node.id ? data.node : node)) }));
    return data.node;
  }, []);

  const archiveNode = useCallback(async (nodeId: string) => {
    const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, { method: "DELETE" });
    setState((current) => ({ ...current, nodes: current.nodes.map((node) => (node.id === data.node.id ? data.node : node)) }));
    return data.node;
  }, []);

  const restoreNode = useCallback(async (nodeId: string) => {
    const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${nodeId}`, {
      method: "PATCH",
      body: JSON.stringify({ archived: false }),
    });
    setState((current) => ({ ...current, nodes: current.nodes.map((node) => (node.id === data.node.id ? data.node : node)) }));
    return data.node;
  }, []);

  const permanentlyDeleteNode = useCallback(async (nodeId: string) => {
    await apiFetch<{ ok: boolean }>(`/api/docs/nodes/${nodeId}?permanent=1`, { method: "DELETE" });
    setState((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== nodeId) }));
  }, []);

  const createNode = useCallback(async (parentId: string | null, afterNode?: DocsNode | null, title = "") => {
    const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
    const afterIndex = afterNode ? siblings.findIndex((node) => node.id === afterNode.id) : siblings.length - 1;
    const previous = afterIndex >= 0 ? siblings[afterIndex] : null;
    const next = afterIndex >= 0 ? siblings[afterIndex + 1] : siblings[0];
    const data = await apiFetch<{ node: DocsNode }>("/api/docs", {
      method: "POST",
      body: JSON.stringify({
        parent_id: parentId,
        title,
        body_text: title,
        node_type: "node",
        sort_order: midpointSortOrder(previous?.sort_order, next?.sort_order),
      }),
    });
    setState((current) => ({ ...current, nodes: [...current.nodes, data.node] }));
    selectSingleNode(data.node.id);
    setFocusRequestNodeId(data.node.id);
    return data.node;
  }, [childrenByParent, roots, selectSingleNode]);

  const toggleCollapsed = (nodeId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      writeCollapsed(next);
      return next;
    });
  };

  const openSidebarNode = useCallback((node: DocsNode, event?: ReactMouseEvent<HTMLElement>) => {
    if (event?.shiftKey || event?.ctrlKey || event?.metaKey) {
      const visibleIds = Array.from(document.querySelectorAll<HTMLElement>("[data-docs-sidebar-node-id]"))
        .map((element) => element.getAttribute("data-docs-sidebar-node-id"))
        .filter((nodeId): nodeId is string => Boolean(nodeId));
      const anchorId = selectionAnchorNodeIdRef.current ?? selectedNodeIdsRef.current[0] ?? node.id;
      if (event.shiftKey && visibleIds.includes(anchorId) && visibleIds.includes(node.id)) {
        const anchorIndex = visibleIds.indexOf(anchorId);
        const targetIndex = visibleIds.indexOf(node.id);
        const start = Math.min(anchorIndex, targetIndex);
        const end = Math.max(anchorIndex, targetIndex);
        const nextIds = visibleIds.slice(start, end + 1);
        selectedNodeIdsRef.current = nextIds;
        setSelectedNodeIds(nextIds);
        setSelectedNodeId(node.id);
        setSelectionAnchorNodeId(anchorId);
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        const current = new Set(selectedNodeIdsRef.current);
        if (current.has(node.id)) {
          current.delete(node.id);
        } else {
          current.add(node.id);
        }
        const nextIds = Array.from(current);
        selectedNodeIdsRef.current = nextIds;
        selectionAnchorNodeIdRef.current = node.id;
        setSelectedNodeIds(nextIds);
        setSelectedNodeId(node.id);
        setSelectionAnchorNodeId(node.id);
        return;
      }
    }
    setTagPageId(null);
    setFocusNodeId(node.id);
    selectSingleNode(node.id);
  }, [selectSingleNode]);

  const openSidebarNodeContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>, node: DocsNode) => {
    event.preventDefault();
    event.stopPropagation();
    selectSingleNode(node.id);
    setSidebarContextMenu({ x: event.clientX, y: event.clientY, nodeId: node.id });
  }, [selectSingleNode]);

  const dropSidebarNode = useCallback(async (targetNode: DocsNode) => {
    if (!dragSidebarNodeId || dragSidebarNodeId === targetNode.id) return;
    const draggedNode = nodesById.get(dragSidebarNodeId);
    if (!draggedNode) return;
    setDragSidebarNodeId(null);
    const targets = selectedNodeIdsRef.current.includes(draggedNode.id)
      ? selectedNodeIdsRef.current.map((nodeId) => nodesById.get(nodeId)).filter((node): node is DocsNode => Boolean(node))
      : [draggedNode];
    for (const node of targets) {
      if (node.id === targetNode.id) continue;
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${node.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: targetNode.id, leave_reference: false }),
      });
    }
    await load();
  }, [dragSidebarNodeId, load, nodesById]);

  const archiveSidebarNode = useCallback(async (node: DocsNode) => {
    setSidebarContextMenu(null);
    try {
      await archiveNode(node.id);
      const fallbackId = node.parent_id && nodesById.get(node.parent_id) && !nodesById.get(node.parent_id)?.archived_at
        ? node.parent_id
        : roots.find((root) => root.id !== node.id)?.id ?? null;
      if (focusNodeId === node.id) {
        setTagPageId(null);
        setFocusNodeId(fallbackId);
        selectSingleNode(fallbackId);
      } else if (selectedNodeId === node.id) {
        selectSingleNode(null);
      }
      if (splitNodeId === node.id) setSplitNodeId(null);
      toast.success("ノードをアーカイブしました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ノードのアーカイブに失敗しました");
    }
  }, [archiveNode, focusNodeId, nodesById, roots, selectSingleNode, selectedNodeId, splitNodeId]);

  async function renameSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const nextTitle = window.prompt("ノード名を変更", nodeText(node));
    if (!nextTitle || nextTitle.trim() === node.title) return;
    await patchNode(node.id, { title: nextTitle.trim() });
  }

  async function duplicateSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const duplicated = await createNode(node.parent_id, node, `${nodeText(node)} copy`);
    await patchNode(duplicated.id, {
      description: node.description,
      display_props: safeNodeDisplayProps(node),
      query_json: node.query_json,
      view_json: node.view_json,
      node_type: node.node_type,
      project_id: node.project_id,
    });
    for (const relation of state.node_supertags.filter((item) => item.node_id === node.id)) {
      await applyTag(duplicated, relation.supertag_id);
    }
    toast.success("ノードを複製しました");
  }

  async function exportSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    const rows = outlineRows(node.id, childrenByParent, new Set<string>());
    const text = [nodeText(node), ...rows.map((row) => `${"  ".repeat(row.depth + 1)}- ${nodeText(row.node)}`)].join("\n");
    await navigator.clipboard.writeText(text);
    toast.success("アウトラインをクリップボードへコピーしました");
  }

  async function pinSidebarNode(node: DocsNode) {
    setSidebarContextMenu(null);
    await updateDisplayProps(node, { pinned_sidebar: node.display_props?.pinned_sidebar !== true });
  }

  function moveSidebarNodeWithReference(node: DocsNode) {
    setSidebarContextMenu(null);
    selectSingleNode(node.id);
    openCommand({ kind: "move", leaveReference: true });
  }

  const applyTag = async (node: DocsNode, tagId: string) => {
    const current = state.node_supertags.filter((item) => item.node_id === node.id).map((item) => item.supertag_id);
    const nextIds = Array.from(new Set([...current, tagId]));
    const data = await apiFetch<{ node_supertags: DocsState["node_supertags"] }>(`/api/docs/nodes/${node.id}/supertags`, {
      method: "PUT",
      body: JSON.stringify({ supertag_ids: nextIds }),
    });
    setState((currentState) => ({
      ...currentState,
      node_supertags: [
        ...currentState.node_supertags.filter((item) => item.node_id !== node.id),
        ...data.node_supertags,
      ],
    }));
  };

  const saveField = async (node: DocsNode, field: DocsField, raw: string) => {
    const data = await apiFetch<{ field_values: DocsFieldValue[] }>(`/api/docs/nodes/${node.id}/fields`, {
      method: "PUT",
      body: JSON.stringify({ field_values: [{ field_id: field.id, value: fieldDraftToPayload(field, raw) }] }),
    });
    setState((current) => ({
      ...current,
      field_values: [
        ...current.field_values.filter((value) => !(value.node_id === node.id && value.field_id === field.id)),
        ...data.field_values,
      ],
    }));
  };

  const updateSuggestionStatus = async (suggestionId: string, status: "accepted" | "rejected" | "stale") => {
    const data = await apiFetch<{ suggestion: DocsAiSuggestion }>(`/api/docs/suggestions/${suggestionId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    setState((current) => ({
      ...current,
      ai_suggestions: current.ai_suggestions.map((item) => item.id === data.suggestion.id ? data.suggestion : item),
    }));
  };

  const taskStatusField = state.fields.find((field) => field.system_key === "task_status") ?? null;
  const isTaskNode = (node: DocsNode) => (nodeTags.get(node.id) ?? []).some((tag) => tag.system_key === "task");
  const taskTag = state.supertags.find((tag) => tag.system_key === "task") ?? null;
  const taskDoneMapping = readConfigRecord(taskTag?.config_json?.done_state_mapping ?? taskTag?.config_json?.doneStateMapping);
  const taskDoneValue = typeof taskDoneMapping.done_value === "string"
    ? taskDoneMapping.done_value
    : typeof taskDoneMapping.checked_value === "string"
      ? taskDoneMapping.checked_value
    : typeof taskDoneMapping.doneValue === "string"
      ? taskDoneMapping.doneValue
      : "closed";
  const taskOpenValue = typeof taskDoneMapping.open_value === "string"
    ? taskDoneMapping.open_value
    : typeof taskDoneMapping.unchecked_value === "string"
      ? taskDoneMapping.unchecked_value
    : typeof taskDoneMapping.openValue === "string"
      ? taskDoneMapping.openValue
      : "todo";
  const taskStatusForNode = (node: DocsNode) =>
    taskStatusField
      ? fieldValueToDraft(fieldValuesByKey.get(`${node.id}:${taskStatusField.id}`)).trim().toLowerCase()
      : "";
  const checkedForNode = (node: DocsNode) =>
    isTaskNode(node)
      ? [taskDoneValue, "done", "closed", "complete", "completed"].map((value) => value.toLowerCase()).includes(taskStatusForNode(node))
      : node.display_props?.checked === true;
  const toggleNodeCheckbox = async (node: DocsNode) => {
    if (isTaskNode(node) && taskStatusField) {
      const nextStatus = checkedForNode(node) ? taskOpenValue : taskDoneValue;
      await saveField(node, taskStatusField, nextStatus);
      if (node.display_props?.show_checkbox !== true) {
        await updateDisplayProps(node, { show_checkbox: true });
      }
      return;
    }
    await updateDisplayProps(node, { show_checkbox: true, checked: node.display_props?.checked !== true });
  };

  const updateDisplayProps = async (node: DocsNode, patch: Record<string, unknown>) => {
    const canonical = nodesById.get(node.id) ?? node;
    await patchNode(node.id, { display_props: { ...safeNodeDisplayProps(canonical), ...patch } });
  };

  const commitTitle = async (node: DocsNode, title: string) => {
    const matchedTags = titleTagNames(title)
      .map((name) => state.supertags.find((tag) => tag.name.toLowerCase() === name.toLowerCase()))
      .filter((tag): tag is DocsSupertag => Boolean(tag));
    const nextTitle = matchedTags.length > 0 ? titleWithoutTagTokens(title) : title;
    if (nextTitle !== node.title) {
      await patchNode(node.id, { title: nextTitle });
    }
    for (const tag of matchedTags) {
      await applyTag(node, tag.id);
    }
  };

  const createTag = async () => {
    const name = newTagName.replace(/^#/, "").trim();
    if (!name) return;
    const data = await apiFetch<{ supertag: DocsSupertag }>("/api/docs/supertags", {
      method: "POST",
      body: JSON.stringify({
        name,
        base_type: "note",
        color: "#2563eb",
        icon: "hash",
        config_json: {},
      }),
    });
    setState((current) => ({ ...current, supertags: mergeById(current.supertags, data.supertag) }));
    setNewTagName("");
    if (selectedNode) await applyTagToActionNodes(selectedNode, data.supertag.id);
  };

  const createField = async (tagId: string, name: string, fieldType: string) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const data = await apiFetch<{ field: DocsField }>("/api/docs/fields", {
      method: "POST",
      body: JSON.stringify({
        supertag_id: tagId,
        name: trimmed,
        field_type: fieldType,
        options_json: fieldType === "options" ? { values: ["todo", "doing", "done"] } : {},
      }),
    });
    setState((current) => ({
      ...current,
      fields: mergeById(current.fields, data.field),
      supertag_fields: current.supertag_fields.some((item) => item.supertag_id === tagId && item.field_id === data.field.id)
        ? current.supertag_fields
        : [
            ...current.supertag_fields,
            {
              supertag_id: tagId,
              field_id: data.field.id,
              sort_order: data.field.sort_order,
              required: data.field.required,
              show_in_template: true,
              optional: false,
            },
          ],
    }));
  };

  const applyTagToActionNodes = async (fallbackNode: DocsNode, tagId: string) => {
    const targets = actionNodes.length > 1 && selectedNodeIdSet.has(fallbackNode.id) ? actionNodes : [fallbackNode];
    for (const node of targets) {
      await applyTag(node, tagId);
    }
  };

  const updateField = async (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => {
    const data = await apiFetch<{ field: DocsField }>(`/api/docs/fields/${fieldId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, fields: current.fields.map((field) => (field.id === data.field.id ? data.field : field)) }));
  };

  const updateSupertag = async (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => {
    const data = await apiFetch<{ supertag: DocsSupertag }>(`/api/docs/supertags/${tagId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, supertags: current.supertags.map((tag) => (tag.id === data.supertag.id ? data.supertag : tag)) }));
  };

  const createSavedView = async (
    tag: DocsSupertag,
    draft: Pick<DocsSavedView, "name" | "layout" | "config_json">,
  ) => {
    const data = await apiFetch<{ view: DocsSavedView }>("/api/docs/views", {
      method: "POST",
      body: JSON.stringify({
        supertag_id: tag.id,
        name: draft.name,
        layout: draft.layout,
        config_json: draft.config_json,
      }),
    });
    setState((current) => ({ ...current, views: mergeById(current.views, data.view) }));
    return data.view;
  };

  const updateSavedView = async (
    viewId: string,
    patch: Partial<Pick<DocsSavedView, "name" | "layout" | "config_json" | "sort_order">>,
  ) => {
    const data = await apiFetch<{ view: DocsSavedView }>(`/api/docs/views/${viewId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setState((current) => ({ ...current, views: current.views.map((view) => (view.id === data.view.id ? data.view : view)) }));
    return data.view;
  };

  const createTaggedNode = async (tag: DocsSupertag) => {
    const node = await createNode(focusNode?.id ?? null, null, "");
    await applyTag(node, tag.id);
    setTagPageId(null);
    setFocusNodeId(node.id);
    selectSingleNode(node.id);
    setFocusRequestNodeId(node.id);
  };

  const moveActionNodes = async (fallbackNode: DocsNode, targetParentId: string, leaveReference: boolean) => {
    const targets = actionNodes.length > 1 && selectedNodeIdSet.has(fallbackNode.id) ? actionNodes : [fallbackNode];
    for (const node of targets) {
      await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${node.id}/move`, {
        method: "POST",
        body: JSON.stringify({ new_parent_id: targetParentId, leave_reference: leaveReference }),
      });
    }
    setCommandOpen(false);
    await load();
  };

  const setSearchNodeView = async (node: DocsNode, view: SearchView) => {
    await patchNode(node.id, { view_json: { ...node.view_json, view } });
  };

  const setSearchNodeSort = async (node: DocsNode, sort: SearchSort) => {
    const nextQuery = { ...readConfigRecord(node.query_json) };
    if (sort) {
      nextQuery.sort = sort;
    } else {
      delete nextQuery.sort;
    }
    await patchNode(node.id, { query_json: nextQuery });
  };

  const setSearchNodeQuery = async (node: DocsNode, query: Record<string, unknown>) => {
    await patchNode(node.id, { query_json: query });
  };

  const applyOutlineOperations = async (operations: OutlineOperation[]) => {
    const pendingNodes = new Map<number, DocsNode>();
    for (const operation of operations) {
      if (operation.type === "patch_title") {
        await patchNode(operation.nodeId, { title: operation.title });
        continue;
      }
      if (operation.type === "archive_node") {
        const node = nodesById.get(operation.nodeId);
        const hasChildren = node ? (childrenByParent.get(node.id) ?? []).some((child) => !child.archived_at) : false;
        if (hasChildren) continue;
        await archiveNode(operation.nodeId);
        continue;
      }
      if (operation.type === "restore_node") {
        await restoreNode(operation.nodeId);
        continue;
      }
      if (operation.type === "create_node") {
        const parentId = operation.parentId;
        const afterNode = operation.afterSiblingId ? nodesById.get(operation.afterSiblingId) ?? null : null;
        const created = await createNode(parentId, afterNode, operation.title);
        pendingNodes.set(operation.pendingLine, created);
        continue;
      }
      if (operation.type === "move_node") {
        const parentId = operation.parentId;
        const siblings = parentId ? childrenByParent.get(parentId) ?? [] : roots;
        const previous = operation.afterSiblingId ? nodesById.get(operation.afterSiblingId) ?? null : null;
        const previousIndex = previous ? siblings.findIndex((node) => node.id === previous.id) : -1;
        const next = previousIndex >= 0 ? siblings[previousIndex + 1] : siblings[0];
        await patchNode(operation.nodeId, {
          parent_id: parentId,
          sort_order: midpointSortOrder(previous?.sort_order, next?.sort_order),
        });
      }
    }
    if (operations.length > 0) await load();
  };

  const createSearchNode = async (tag: DocsSupertag) => {
    if (!focusNode) return;
    const node = await createNode(focusNode.id, currentRows.at(-1)?.node, `List of ${tag.name}`);
    await patchNode(node.id, {
      node_type: "search",
      query_json: { and: [{ tag: tag.id, include_descendants: true }], limit: 100 },
      view_json: { view: "table" },
    });
  };

  const runDocsAiCommand = async (node: DocsNode, command: "continue" | "extract_tasks" | "rewrite" | "fill_fields" = "continue", prompt?: string) => {
    const text = prompt ?? window.prompt("Docs AI prompt", command === "continue" ? nodeText(node) : "") ?? "";
    const data = await apiFetch<DocsAiCommandResult>("/api/ai/docs/command", {
      method: "POST",
      body: JSON.stringify({
        node_id: node.id,
        command,
        prompt: text,
      }),
    });
    const suggestion = data.suggestion;
    if (suggestion) {
      setState((current) => ({
        ...current,
        ai_suggestions: mergeById<DocsAiSuggestion>(current.ai_suggestions, suggestion),
      }));
    }
    const result = data.result;
    if (!result) return;
    if (command === "fill_fields") {
      toast.success(result.summary ?? "AIフィールド候補を保存しました");
      return;
    }
    if (
      (result.mode === "replace_title" && typeof result.replacement === "string") ||
      (result.mode === "insert_children" && Array.isArray(result.lines))
    ) {
      setAiPreview({
        node,
        command,
        suggestionId: suggestion?.id,
        result,
      });
      return;
    }
    toast.success(result.summary ?? "AI候補を保存しました");
  };

  const applyDocsAiPreview = async () => {
    if (!aiPreview) return;
    const { node, command, suggestionId, result } = aiPreview;
    if (result.mode === "replace_title" && typeof result.replacement === "string") {
      await patchNode(node.id, { title: result.replacement });
    } else if (result.mode === "insert_children" && Array.isArray(result.lines)) {
      let afterNode: DocsNode | null = (childrenByParent.get(node.id) ?? []).at(-1) ?? null;
      for (const line of result.lines) {
        const rawLine = String(line);
        const taskTag = state.supertags.find((tag) => tag.system_key === "task");
        const shouldBindTask = command === "extract_tasks" || /#(?:Task|タスク)\b/i.test(rawLine);
        const created = await createNode(node.id, afterNode, shouldBindTask ? titleWithoutTagTokens(rawLine) : rawLine);
        if (shouldBindTask && taskTag) await applyTag(created, taskTag.id);
        afterNode = created;
      }
      setCollapsed((current) => {
        const next = new Set(current);
        next.delete(node.id);
        return next;
      });
    }
    if (suggestionId) await updateSuggestionStatus(suggestionId, "accepted");
    setAiPreview(null);
    toast.success(result.summary ?? "AI候補を反映しました");
  };

  const rejectDocsAiPreview = async () => {
    if (aiPreview?.suggestionId) await updateSuggestionStatus(aiPreview.suggestionId, "rejected");
    setAiPreview(null);
  };

  const applyInlineTag = async (targetNode: DocsNode, tag: DocsSupertag) => {
    if (!inlineAutocomplete || inlineAutocomplete.nodeId !== targetNode.id) return;
    const nextTitle = replaceInlineRange(targetNode.title, inlineAutocomplete, "");
    setState((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === targetNode.id ? { ...node, title: nextTitle.trim() } : node)),
    }));
    await commitTitle(targetNode, nextTitle);
    await applyTag(targetNode, tag.id);
    setInlineAutocomplete(null);
    setSlashNodeId(null);
    setFocusRequestNodeId(targetNode.id);
  };

  const applyInlineReference = async (targetNode: DocsNode, referenceNode: DocsNode) => {
    if (!inlineAutocomplete || inlineAutocomplete.nodeId !== targetNode.id) return;
    const token = createDocsNodeWikilink(referenceNode.id, nodeText(referenceNode));
    const nextTitle = replaceInlineRange(targetNode.title, inlineAutocomplete, token);
    setState((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === targetNode.id ? { ...node, title: nextTitle } : node)),
    }));
    await commitTitle(targetNode, nextTitle);
    setInlineAutocomplete(null);
    setFocusRequestNodeId(targetNode.id);
  };

  const handleWorkspaceKeyDownCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (!event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    const nodeId = (event.target as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
    if (!nodeId) return;
    event.preventDefault();
    event.stopPropagation();
    selectDomRangeById(nodeId, event.key === "ArrowUp" ? -1 : 1);
  };

  const handleWorkspaceKeyUpCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (!event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    const nodeId = (event.target as HTMLElement | null)?.closest("[data-docs-node-id]")?.getAttribute("data-docs-node-id");
    if (!nodeId) return;
    event.stopPropagation();
    selectDomRangeById(nodeId);
  };

  const renderPanel = (node: DocsNode | null, rows: Array<{ node: DocsNode; depth: number }>, compact = false) => {
    const panelBreadcrumb = buildBreadcrumb(node, nodesById);
    const outlineEditorRows: OutlineEditorRow[] = rows.map((row) => ({
      ...row,
      checked: checkedForNode(row.node),
      tags: nodeTags.get(row.node.id) ?? [],
    }));
    return (
    <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="border-b px-5 py-3">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          {panelBreadcrumb.map((item) => (
            <button key={item.id} type="button" className="truncate hover:text-foreground" onClick={() => setFocusNodeId(item.id)}>
              {nodeText(item)}
            </button>
          ))}
        </div>
        {node ? (
          <div className="mt-2 flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <PageTitleEditor
                node={node}
                requestFocus={focusRequestNodeId === node.id}
                onFocused={() => setFocusRequestNodeId(null)}
                onChangeTitle={(title) => setState((current) => ({ ...current, nodes: current.nodes.map((item) => (item.id === node.id ? { ...item, title } : item)) }))}
                onCommitTitle={(title) => void commitTitle(node, title)}
              />
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {nodeTags.get(node.id)?.map((tag) => (
                  <button
                    key={tag.id}
                    type="button"
                    className="rounded border px-2 py-0.5 text-xs"
                    style={tagColorStyle(tag.color)}
                    onClick={() => {
                      setTagPageId(tag.id);
                      setRightPanel("tags");
                    }}
                  >
                    #{tag.name}
                  </button>
                ))}
                {node.node_type === "day" && <span className="rounded border px-2 py-0.5 text-xs text-sky-300">Daily</span>}
                <TaskBindingButton nodeId={node.id} />
              </div>
            </div>
            {!compact && (
              <Button variant="ghost" size="icon-sm" title="Open in right panel" onClick={() => setSplitNodeId(node.id)}>
                <Columns2 className="size-4" />
              </Button>
            )}
          </div>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        {node ? (
          <>
            <FieldRows
              node={node}
              fields={fieldsForNode(node, nodeTags, fieldsByTag)}
              values={fieldValuesByKey}
              nodes={state.nodes}
              projects={projects}
              suggestions={state.ai_suggestions}
              onSuggestionStatus={updateSuggestionStatus}
              onRunAi={() => void runDocsAiCommand(node, "fill_fields", nodeText(node))}
              onSave={saveField}
            />
            <div className="mt-3 space-y-0.5">
              <OutlineDocumentEditor
                rows={outlineEditorRows}
                selectedNodeIds={selectedNodeIdSet}
                requestFocusNodeId={focusRequestNodeId}
                onSelectNode={(nodeId) => {
                  if (!preserveSelectionOnNextFocusRef.current) selectSingleNode(nodeId);
                }}
                onOpenNode={(nodeId) => {
                  setTagPageId(null);
                  setFocusNodeId(nodeId);
                  selectSingleNode(nodeId);
                }}
                onToggleCheckbox={(nodeId) => {
                  const target = nodesById.get(nodeId);
                  if (target) void toggleNodeCheckbox(target);
                }}
                onApplyOperations={applyOutlineOperations}
                onFocused={(nodeId) => {
                  if (nodeId && preserveSelectionOnNextFocusRef.current) {
                    preserveSelectionOnNextFocusRef.current = false;
                    setSelectedNodeIds(selectedNodeIdsRef.current);
                    setSelectionAnchorNodeId(selectionAnchorNodeIdRef.current);
                    setSelectedNodeId(nodeId);
                  }
                  setFocusRequestNodeId(null);
                }}
              />
              {rows.map(({ node: rowNode, depth }) => (
                <div key={`${rowNode.id}:${rowNode.parent_id ?? "root"}:${depth}:${rowNode.sort_order}`}>
                  {slashNodeId === rowNode.id ? (
                    <SlashMenu
                      node={rowNode}
                      tags={state.supertags}
                      query={inlineAutocomplete?.nodeId === rowNode.id && inlineAutocomplete.kind === "slash" ? inlineAutocomplete.query : ""}
                      depth={depth}
                      onClose={() => setSlashNodeId(null)}
                      onToggleCheckbox={() => {
                        setSlashNodeId(null);
                        void toggleNodeCheckbox(rowNode);
                      }}
                      onAddChild={() => {
                        setSlashNodeId(null);
                        void createNode(rowNode.id, null);
                      }}
                      onRunAi={() => {
                        setSlashNodeId(null);
                        void runDocsAiCommand(rowNode, "continue");
                      }}
                      onMakeSearch={(tag) => {
                        setSlashNodeId(null);
                        void patchNode(rowNode.id, {
                          title: rowNode.title || `List of ${tag.name}`,
                          node_type: "search",
                          query_json: { and: [{ tag: tag.id, include_descendants: true }], limit: 100 },
                          view_json: { view: "table" },
                        });
                      }}
                      onApplyTag={(tag) => {
                        setSlashNodeId(null);
                        void applyTag(rowNode, tag.id);
                      }}
                    />
                  ) : null}
                  {inlineAutocomplete?.nodeId === rowNode.id && inlineAutocomplete.kind !== "slash" ? (
                    <InlineAutocompleteMenu
                      state={inlineAutocomplete}
                      depth={depth}
                      tags={state.supertags}
                      nodes={state.nodes}
                      onSelectTag={(tag) => void applyInlineTag(rowNode, tag)}
                      onSelectNode={(target) => void applyInlineReference(rowNode, target)}
                    />
                  ) : null}
                  {rowNode.node_type === "search" ? (
                    <SearchNodeResults
                      node={rowNode}
                      depth={depth + 1}
                      nodes={state.nodes}
                      nodeSupertags={state.node_supertags}
                      tags={state.supertags}
                      fields={state.fields}
                      fieldValues={state.field_values}
                      projects={projects}
                      fieldsByTag={fieldsByTag}
                      allSupertagFields={state.supertag_fields}
                      onSetView={(view) => void setSearchNodeView(rowNode, view)}
                      onSetSort={(sort) => void setSearchNodeSort(rowNode, sort)}
                      onSetQuery={(query) => void setSearchNodeQuery(rowNode, query)}
                      onOpenNode={(nodeId) => {
                        setFocusNodeId(nodeId);
                        selectSingleNode(nodeId);
                      }}
                    />
                  ) : null}
                </div>
              ))}
              <Button variant="ghost" size="sm" className="ml-5 mt-2" onClick={() => void createNode(node.id, rows.at(-1)?.node)}>
                <Plus className="size-4" />
                Add node
              </Button>
              {!compact ? (
                <ZoomReferences
                  references={pageReferences}
                  loading={pageReferencesLoading}
                  onOpenNode={(nodeId) => {
                    setTagPageId(null);
                    setFocusNodeId(nodeId);
                    selectSingleNode(nodeId);
                  }}
                />
              ) : null}
            </div>
          </>
        ) : (
          <div className="p-8 text-sm text-muted-foreground">ノードを選択してください。</div>
        )}
      </div>
    </section>
    );
  };

  const sidebarQuery = quickQuery.trim().toLowerCase();

  if (loading) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-[70vh] w-full" />
      </div>
    );
  }

  return (
    <div
      className="flex h-[calc(100vh-96px)] min-h-[640px] overflow-hidden border-t bg-background"
      onKeyDownCapture={handleWorkspaceKeyDownCapture}
      onKeyUpCapture={handleWorkspaceKeyUpCapture}
    >
      <aside className="flex w-72 shrink-0 flex-col border-r">
        <div className="flex items-center gap-2 border-b px-3 py-3">
          <Search className="size-4 text-muted-foreground" />
          <Input value={quickQuery} onChange={(event) => setQuickQuery(event.target.value)} placeholder="Search and open" className="h-8" />
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-2 py-2">
          <SidebarButton icon={CalendarDays} label="Today" active={focusNode?.node_type === "day"} onClick={() => openToday()} />
          <SidebarButton
            icon={Tags}
            label="Supertags"
            active={tagPageId === SUPERTAGS_OVERVIEW_ID}
            onClick={() => {
              setTagPageId(SUPERTAGS_OVERVIEW_ID);
              setRightPanel("tags");
            }}
          />
          <SidebarButton icon={ListFilter} label="Search nodes" active={rightPanel === "search"} onClick={() => setRightPanel("search")} />
          <SidebarButton icon={Archive} label="ゴミ箱" active={rightPanel === "trash"} onClick={() => setRightPanel("trash")} />
          <div className="mt-4 px-2 text-xs font-medium text-muted-foreground">Workspace</div>
          <div className="mt-1 space-y-0.5">
            {roots
              .filter((node) => sidebarNodeMatches(node, sidebarQuery, childrenByParent))
              .map((node) => (
                <DocsSidebarNode
                  key={`${node.id}:${node.parent_id ?? "root"}:${node.sort_order}`}
                  node={node}
                  depth={0}
                  focusNodeId={focusNodeId}
                  selectedNodeId={selectedNodeId}
                  selectedNodeIds={selectedNodeIds}
                  dragNodeId={dragSidebarNodeId}
                  childrenByParent={childrenByParent}
                  collapsed={collapsed}
                  query={sidebarQuery}
                  onToggle={toggleCollapsed}
                  onOpen={openSidebarNode}
                  onContextMenu={openSidebarNodeContextMenu}
                  onDragStart={(nodeId) => setDragSidebarNodeId(nodeId)}
                  onDropOnNode={(node) => void dropSidebarNode(node)}
                />
              ))}
          </div>
        </div>
      </aside>
      <main className="flex min-w-0 flex-1 overflow-hidden">
        {tagPageId ? (
          <SupertagPage
            tag={activeTagPage}
            tags={state.supertags}
            views={state.views}
            nodes={state.nodes}
            nodeSupertags={state.node_supertags}
            fields={state.fields}
            fieldValues={state.field_values}
            fieldsByTag={fieldsByTag}
            allSupertagFields={state.supertag_fields}
            onOpenTag={(tagId) => {
              setTagPageId(tagId);
              setRightPanel("tags");
            }}
            onOpenNode={(nodeId) => {
              setTagPageId(null);
              setFocusNodeId(nodeId);
              selectSingleNode(nodeId);
            }}
            onCreateTaggedNode={(tag) => void createTaggedNode(tag)}
            onCreateView={createSavedView}
            onUpdateView={updateSavedView}
          />
        ) : (
          renderPanel(focusNode, currentRows)
        )}
        {!tagPageId && splitNode ? <div className="hidden min-w-0 flex-1 border-l xl:flex">{renderPanel(splitNode, splitRows, true)}</div> : null}
      </main>
      <aside className="hidden w-80 shrink-0 overflow-auto border-l lg:block">
        <RightPanel
          mode={rightPanel}
          selectedNode={selectedNode}
          selectedTag={activeTagPage}
          tags={state.supertags}
          nodeTags={selectedNode ? nodeTags.get(selectedNode.id) ?? [] : []}
          fields={state.fields}
          fieldsByTag={fieldsByTag}
          newTagName={newTagName}
          setNewTagName={setNewTagName}
          onApplyTag={(tagId) => selectedNode && void applyTagToActionNodes(selectedNode, tagId)}
          onOpenTag={(tagId) => {
            setTagPageId(tagId);
            setRightPanel("tags");
          }}
          onCreateTag={() => void createTag()}
          onCreateField={(tagId, name, fieldType) => void createField(tagId, name, fieldType)}
          onUpdateSupertag={(tagId, patch) => void updateSupertag(tagId, patch)}
          onUpdateField={(fieldId, patch) => void updateField(fieldId, patch)}
          onCreateSearchNode={(tag) => void createSearchNode(tag)}
          searchNodes={state.nodes.filter((node) => node.node_type === "search")}
          archivedNodes={archivedNodes}
          relatedNodes={relatedNodes}
          onOpenNode={(nodeId) => {
            setFocusNodeId(nodeId);
            selectSingleNode(nodeId);
          }}
          onRestoreNode={(nodeId) => void restoreNode(nodeId)}
          onPermanentDeleteNode={(nodeId) => void permanentlyDeleteNode(nodeId)}
        />
      </aside>
      <DocsCommandPalette
        open={commandOpen}
        onOpenChange={(open) => {
          setCommandOpen(open);
          if (!open) setCommandMode({ kind: "root" });
        }}
        mode={commandMode}
        setMode={setCommandMode}
        selectedNode={selectedNode}
        selectionCount={actionNodes.length}
        tags={state.supertags}
        fields={commandFields}
        moveTargets={commandMoveTargets}
        onAddChild={(node) => void createNode(node.id, null)}
        onOpenSplit={(node) => setSplitNodeId(node.id)}
        onToggleCheckbox={(node) => void toggleNodeCheckbox(node)}
        onApplyTag={(node, tag) => void applyTagToActionNodes(node, tag.id)}
        onMove={(node, target, leaveReference) => void moveActionNodes(node, target.id, leaveReference)}
        onSetView={(node, view) => void setSearchNodeView(node, view)}
        onSetField={(node, field, value) => void saveField(node, field, value)}
        onRunAi={(node, command) => void runDocsAiCommand(node, command)}
        onGoBack={(node) => {
          if (!node.parent_id) return;
          setFocusNodeId(node.parent_id);
          selectSingleNode(node.parent_id);
        }}
      />
      <DocsSidebarContextMenu
        menu={sidebarContextMenu}
        node={sidebarContextNode}
        onClose={() => setSidebarContextMenu(null)}
        onOpen={(node) => {
          openSidebarNode(node);
          setSidebarContextMenu(null);
        }}
        onOpenSplit={(node) => {
          setSplitNodeId(node.id);
          selectSingleNode(node.id);
          setSidebarContextMenu(null);
        }}
        onRename={(node) => void renameSidebarNode(node)}
        onDuplicate={(node) => void duplicateSidebarNode(node)}
        onMoveWithReference={moveSidebarNodeWithReference}
        onExport={(node) => void exportSidebarNode(node)}
        onPin={(node) => void pinSidebarNode(node)}
        onArchive={(node) => void archiveSidebarNode(node)}
      />
      <DocsAiPreviewDialog
        preview={aiPreview}
        onApply={() => void applyDocsAiPreview()}
        onReject={() => void rejectDocsAiPreview()}
        onOpenChange={(open) => {
          if (!open) void rejectDocsAiPreview();
        }}
      />
    </div>
  );
}

function SidebarButton({ icon: Icon, label, active, onClick }: { icon: LucideIcon; label: string; active: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className={cn("flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent", active && "bg-accent")}>
      <Icon className="size-4 text-muted-foreground" />
      {label}
    </button>
  );
}

function DocsAiPreviewDialog({
  preview,
  onApply,
  onReject,
  onOpenChange,
}: {
  preview: DocsAiPreview | null;
  onApply: () => void;
  onReject: () => void;
  onOpenChange: (open: boolean) => void;
}) {
  const result = preview?.result;
  const lines = Array.isArray(result?.lines) ? result.lines.map(String) : [];
  return (
    <Dialog open={Boolean(preview)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>AI候補の確認</DialogTitle>
          <DialogDescription>
            反映前に内容を確認します。破棄すると保存済み候補は rejected として記録されます。
          </DialogDescription>
        </DialogHeader>
        {preview ? (
          <div className="space-y-3">
            <div className="rounded border bg-muted/20 p-3">
              <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Target</div>
              <div className="text-sm font-medium">{nodeText(preview.node)}</div>
            </div>
            {result?.mode === "replace_title" && typeof result.replacement === "string" ? (
              <div className="grid gap-2 md:grid-cols-2">
                <div className="rounded border p-3">
                  <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Current</div>
                  <div className="text-sm">{nodeText(preview.node)}</div>
                </div>
                <div className="rounded border border-primary/40 bg-primary/5 p-3">
                  <div className="mb-1 text-[11px] font-medium uppercase text-muted-foreground">Proposed</div>
                  <div className="text-sm">{result.replacement}</div>
                </div>
              </div>
            ) : null}
            {result?.mode === "insert_children" ? (
              <div className="rounded border border-primary/40 bg-primary/5 p-3">
                <div className="mb-2 text-[11px] font-medium uppercase text-muted-foreground">Child nodes to insert</div>
                <div className="max-h-72 overflow-auto font-mono text-xs leading-6">
                  {lines.map((line, index) => (
                    <div key={`${line}-${index}`} className="whitespace-pre-wrap border-b border-border/60 py-1 last:border-0">
                      {line}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onReject}>破棄</Button>
          <Button type="button" onClick={onApply}>反映</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DocsSidebarNode({
  node,
  depth,
  focusNodeId,
  selectedNodeId,
  selectedNodeIds,
  dragNodeId,
  childrenByParent,
  collapsed,
  query,
  path = new Set<string>(),
  onToggle,
  onOpen,
  onContextMenu,
  onDragStart,
  onDropOnNode,
}: {
  node: DocsNode;
  depth: number;
  focusNodeId: string | null;
  selectedNodeId: string | null;
  selectedNodeIds: string[];
  dragNodeId: string | null;
  childrenByParent: Map<string | null, DocsNode[]>;
  collapsed: Set<string>;
  query: string;
  path?: Set<string>;
  onToggle: (nodeId: string) => void;
  onOpen: (node: DocsNode, event?: ReactMouseEvent<HTMLElement>) => void;
  onContextMenu: (event: ReactMouseEvent<HTMLElement>, node: DocsNode) => void;
  onDragStart: (nodeId: string) => void;
  onDropOnNode: (node: DocsNode) => void;
}) {
  if (path.has(node.id)) return null;
  const nextPath = new Set([...path, node.id]);
  const children = (childrenByParent.get(node.id) ?? []).filter((child) => !child.archived_at && sidebarNodeMatches(child, query, childrenByParent, nextPath));
  const hasChildren = children.length > 0;
  const expanded = query ? true : !collapsed.has(node.id);
  return (
    <div>
      <div className="flex min-w-0 items-center" style={{ paddingLeft: depth * 14 }}>
        <button
          type="button"
          className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-accent"
          aria-label={hasChildren ? expanded ? "折りたたむ" : "展開する" : undefined}
          disabled={!hasChildren}
          onClick={(event) => {
            event.stopPropagation();
            if (hasChildren) onToggle(node.id);
          }}
        >
          {hasChildren ? expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" /> : <span className="size-1.5 rounded-full border border-current" />}
        </button>
        <button
          type="button"
          data-docs-sidebar-node-id={node.id}
          draggable
          onClick={(event) => onOpen(node, event)}
          onContextMenu={(event) => onContextMenu(event, node)}
          onDragStart={(event) => {
            event.dataTransfer.effectAllowed = "move";
            onDragStart(node.id);
          }}
          onDragOver={(event) => {
            if (dragNodeId && dragNodeId !== node.id) {
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
            }
          }}
          onDrop={(event) => {
            event.preventDefault();
            onDropOnNode(node);
          }}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent",
            focusNodeId === node.id && "bg-accent text-accent-foreground",
            selectedNodeIds.includes(node.id) && focusNodeId !== node.id && "bg-muted/60",
          )}
        >
          <Hash className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate">{nodeText(node)}</span>
        </button>
      </div>
      {hasChildren && expanded ? (
        <div className="space-y-0.5">
          {children.map((child) => (
            <DocsSidebarNode
              key={`${child.id}:${child.parent_id ?? "root"}:${child.sort_order}`}
              node={child}
              depth={depth + 1}
              focusNodeId={focusNodeId}
              selectedNodeId={selectedNodeId}
              selectedNodeIds={selectedNodeIds}
              dragNodeId={dragNodeId}
              childrenByParent={childrenByParent}
              collapsed={collapsed}
              query={query}
              path={nextPath}
              onToggle={onToggle}
              onOpen={onOpen}
              onContextMenu={onContextMenu}
              onDragStart={onDragStart}
              onDropOnNode={onDropOnNode}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DocsSidebarContextMenu({
  menu,
  node,
  onClose,
  onOpen,
  onOpenSplit,
  onRename,
  onDuplicate,
  onMoveWithReference,
  onExport,
  onPin,
  onArchive,
}: {
  menu: SidebarContextMenuState | null;
  node: DocsNode | null;
  onClose: () => void;
  onOpen: (node: DocsNode) => void;
  onOpenSplit: (node: DocsNode) => void;
  onRename: (node: DocsNode) => void;
  onDuplicate: (node: DocsNode) => void;
  onMoveWithReference: (node: DocsNode) => void;
  onExport: (node: DocsNode) => void;
  onPin: (node: DocsNode) => void;
  onArchive: (node: DocsNode) => void;
}) {
  const { ref, style } = useContextMenuPosition(
    menu ? { x: menu.x, y: menu.y } : null,
    { fallbackWidth: 192, fallbackHeight: 132 },
  );

  useEffect(() => {
    if (!menu) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menu, onClose, ref]);

  if (!menu || !node || typeof document === "undefined") return null;

  return createPortal(
    <div
      ref={ref}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      role="menu"
      onContextMenu={(event) => event.preventDefault()}
    >
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onOpen(node)}
      >
        <ExternalLink className="size-4" />
        開く
      </button>
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onOpenSplit(node)}
      >
        <Columns2 className="size-4" />
        右パネルで開く
      </button>
      <div className="my-1 h-px bg-border" />
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onRename(node)}
      >
        <Type className="size-4" />
        名前の変更
      </button>
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onDuplicate(node)}
      >
        <Plus className="size-4" />
        複製
      </button>
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onMoveWithReference(node)}
      >
        <Link2 className="size-4" />
        参照を残して移動
      </button>
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onExport(node)}
      >
        <ExternalLink className="size-4" />
        エクスポート
      </button>
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
        role="menuitem"
        onClick={() => onPin(node)}
      >
        <Hash className="size-4" />
        {node.display_props?.pinned_sidebar === true ? "ピン留め解除" : "ピン留め"}
      </button>
      <div className="my-1 h-px bg-border" />
      <button
        type="button"
        className="flex w-full cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive hover:bg-destructive/10"
        role="menuitem"
        onClick={() => onArchive(node)}
      >
        <Archive className="size-4" />
        アーカイブ
      </button>
    </div>,
    document.body,
  );
}

function DocsCommandPalette({
  open,
  onOpenChange,
  mode,
  setMode,
  selectedNode,
  selectionCount,
  tags,
  fields,
  moveTargets,
  onAddChild,
  onOpenSplit,
  onToggleCheckbox,
  onApplyTag,
  onMove,
  onSetView,
  onSetField,
  onRunAi,
  onGoBack,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: DocsCommandMode;
  setMode: (mode: DocsCommandMode) => void;
  selectedNode: DocsNode | null;
  selectionCount: number;
  tags: DocsSupertag[];
  fields: DocsField[];
  moveTargets: DocsNode[];
  onAddChild: (node: DocsNode) => void;
  onOpenSplit: (node: DocsNode) => void;
  onToggleCheckbox: (node: DocsNode) => void;
  onApplyTag: (node: DocsNode, tag: DocsSupertag) => void;
  onMove: (node: DocsNode, target: DocsNode, leaveReference: boolean) => void;
  onSetView: (node: DocsNode, view: SearchView) => void;
  onSetField: (node: DocsNode, field: DocsField, value: string) => void;
  onRunAi: (node: DocsNode, command: "continue" | "extract_tasks" | "rewrite" | "fill_fields") => void;
  onGoBack: (node: DocsNode) => void;
}) {
  const close = () => onOpenChange(false);
  const viewItems: Array<{ view: SearchView; label: string; icon: LucideIcon }> = [
    { view: "list", label: "View as list", icon: ListFilter },
    { view: "table", label: "View as table", icon: Table2 },
    { view: "board", label: "View as board", icon: KanbanSquare },
    { view: "calendar", label: "View as calendar", icon: CalendarDays },
    { view: "cards", label: "View as cards", icon: Columns2 },
  ];

  const fieldValueItems = selectedNode && mode.kind === "field"
    ? (() => {
        const field = fields.find((item) => item.id === mode.fieldId);
        if (!field) return [];
        const type = docsFieldType(field);
        if (type === "checkbox") return ["true", "false"].map((value) => ({ label: value, value, field }));
        const options = fieldOptions(field);
        if (options.length > 0) return options.map((value) => ({ label: value, value, field }));
        return [{ label: `Clear ${field.name}`, value: "", field }];
      })()
    : [];

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Docs command palette"
      description="Docs node commands"
      className="max-w-xl"
    >
      <Command>
        <CommandInput
          placeholder={
            mode.kind === "move"
              ? mode.leaveReference ? "Move and leave reference to..." : "Move to..."
              : mode.kind === "tag"
                ? "Add tag..."
                : mode.kind === "view"
                  ? "View as..."
                  : mode.kind === "field"
                    ? "Set field..."
                    : "Enter command..."
          }
        />
        <CommandList className="max-h-80">
          <CommandEmpty>見つかりません</CommandEmpty>
          {!selectedNode ? (
            <CommandGroup heading="Docs">
              <CommandItem disabled>ノードを選択してください</CommandItem>
            </CommandGroup>
          ) : mode.kind === "root" ? (
            <>
              {selectionCount > 1 ? (
                <CommandGroup heading="Selection">
                  <CommandItem disabled value={`${selectionCount} nodes selected`}>
                    <CheckSquare className="size-4" />
                    {selectionCount} nodes selected
                  </CommandItem>
                </CommandGroup>
              ) : null}
              {selectionCount > 1 ? <CommandSeparator /> : null}
              <CommandGroup heading="Command">
                <CommandItem onSelect={() => { onOpenSplit(selectedNode); close(); }} value="open in right panel">
                  <Columns2 className="size-4" />
                  Open in right panel
                </CommandItem>
                <CommandItem onSelect={() => { onAddChild(selectedNode); close(); }} value="add child node">
                  <Plus className="size-4" />
                  Add child node
                </CommandItem>
                <CommandItem onSelect={() => { onToggleCheckbox(selectedNode); close(); }} value="add checkbox toggle checkbox">
                  <CheckSquare className="size-4" />
                  {selectedNode.display_props?.show_checkbox === true ? "Toggle checkbox" : "Add checkbox"}
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "continue"); close(); }} value="ai continue generate children">
                  <Sparkles className="size-4" />
                  AI: continue as child nodes
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "rewrite"); close(); }} value="ai rewrite title">
                  <Sparkles className="size-4" />
                  AI: rewrite title
                </CommandItem>
                <CommandItem onSelect={() => { onRunAi(selectedNode, "extract_tasks"); close(); }} value="ai extract tasks">
                  <Sparkles className="size-4" />
                  AI: extract tasks
                </CommandItem>
                <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: false })} value="move to">
                  <ExternalLink className="size-4" />
                  Move to
                </CommandItem>
                <CommandItem onSelect={() => setMode({ kind: "move", leaveReference: true })} value="move and leave reference">
                  <Link2 className="size-4" />
                  Move and leave reference to
                </CommandItem>
                <CommandItem onSelect={() => setMode({ kind: "view" })} value="view as">
                  <Table2 className="size-4" />
                  View as
                </CommandItem>
                <CommandItem onSelect={() => { onGoBack(selectedNode); close(); }} value="go back parent">
                  <ChevronRight className="size-4 rotate-180" />
                  Go back
                </CommandItem>
              </CommandGroup>
              <CommandSeparator />
              {fields.length > 0 ? (
                <CommandGroup heading="Fields">
                  {fields.map((field) => (
                    <CommandItem key={field.id} value={`set ${field.name}`} onSelect={() => setMode({ kind: "field", fieldId: field.id })}>
                      <SlidersHorizontal className="size-4" />
                      Set {field.name}
                    </CommandItem>
                  ))}
                </CommandGroup>
              ) : null}
              <CommandGroup heading="Tags">
                {tags.map((tag) => (
                  <CommandItem key={tag.id} value={`tag ${tag.name}`} keywords={[tag.description ?? ""]} onSelect={() => { onApplyTag(selectedNode, tag); close(); }}>
                    <Hash className="size-4" style={tagColorStyle(tag.color)} />
                    Add tag #{tag.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            </>
          ) : mode.kind === "move" ? (
            <CommandGroup heading={mode.leaveReference ? "Move and leave reference to" : "Move to"}>
              {moveTargets.map((target) => (
                <CommandItem key={target.id} value={`${nodeText(target)} ${target.id}`} onSelect={() => { onMove(selectedNode, target, mode.leaveReference); close(); }}>
                  <ExternalLink className="size-4" />
                  <span className="truncate">{nodeText(target)}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          ) : mode.kind === "view" ? (
            <CommandGroup heading="View as">
              {viewItems.map((item) => {
                const Icon = item.icon;
                return (
                  <CommandItem key={item.view} value={item.label} onSelect={() => { onSetView(selectedNode, item.view); close(); }}>
                    <Icon className="size-4" />
                    {item.label}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          ) : mode.kind === "field" ? (
            <CommandGroup heading="Set field">
              {fieldValueItems.map((item) => (
                <CommandItem key={`${item.field.id}:${item.value}`} value={`${item.field.name} ${item.label}`} onSelect={() => { onSetField(selectedNode, item.field, item.value); close(); }}>
                  <Type className="size-4" />
                  {item.field.name}: {item.label || "empty"}
                </CommandItem>
              ))}
            </CommandGroup>
          ) : (
            <CommandGroup heading="Tags">
              {tags.map((tag) => (
                <CommandItem key={tag.id} value={`tag ${tag.name}`} onSelect={() => { onApplyTag(selectedNode, tag); close(); }}>
                  <Hash className="size-4" style={tagColorStyle(tag.color)} />
                  Add tag #{tag.name}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

function TaskBindingButton({ nodeId }: { nodeId: string }) {
  const router = useRouter();
  const [binding, setBinding] = useState<{
    nodeId: string;
    task: DocsTaskBinding | null;
  } | null>(null);
  const task = binding?.nodeId === nodeId ? binding.task : null;

  useEffect(() => {
    let cancelled = false;
    apiFetch<{ task: DocsTaskBinding | null }>(
      `/api/docs/nodes/${nodeId}/task-binding`,
    )
      .then((data) => {
        if (!cancelled) setBinding({ nodeId, task: data.task });
      })
      .catch(() => {
        if (!cancelled) setBinding({ nodeId, task: null });
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (!task) return null;

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="h-6 gap-1 px-2 text-xs"
      title={task.title}
      onClick={() => router.push(`/tasks/${task.id}`)}
    >
      <CheckSquare className="size-3" />
      タスクタブで開く
      <ExternalLink className="size-3" />
    </Button>
  );
}

function PageTitleEditor({
  node,
  requestFocus,
  onFocused,
  onChangeTitle,
  onCommitTitle,
}: {
  node: DocsNode;
  requestFocus: boolean;
  onFocused: () => void;
  onChangeTitle: (title: string) => void;
  onCommitTitle: (title: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    if (requestFocus && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.select();
      onFocused();
    }
  }, [requestFocus, onFocused]);

  return (
    <textarea
      ref={textareaRef}
      data-docs-node-id={node.id}
      value={node.title}
      rows={1}
      placeholder="Untitled"
      className="min-h-10 w-full resize-none border-0 bg-transparent p-0 text-3xl font-semibold leading-tight outline-none"
      onFocus={onFocused}
      onChange={(event) => onChangeTitle(event.target.value)}
      onBlur={(event) => onCommitTitle(event.target.value)}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) return;
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
      }}
    />
  );
}
function SlashMenu({
  node,
  tags,
  query,
  depth,
  onClose,
  onToggleCheckbox,
  onAddChild,
  onRunAi,
  onMakeSearch,
  onApplyTag,
}: {
  node: DocsNode;
  tags: DocsSupertag[];
  query: string;
  depth: number;
  onClose: () => void;
  onToggleCheckbox: () => void;
  onAddChild: () => void;
  onRunAi: () => void;
  onMakeSearch: (tag: DocsSupertag) => void;
  onApplyTag: (tag: DocsSupertag) => void;
}) {
  const normalized = query.trim().toLowerCase();
  const commandMatches = (label: string) => !normalized || label.toLowerCase().includes(normalized);
  const filteredTags = tags.filter((tag) => !normalized || tag.name.toLowerCase().includes(normalized));
  return (
    <div className="my-1 max-h-80 w-full max-w-sm overflow-auto rounded border bg-popover p-1 text-xs shadow-lg" style={{ marginLeft: depth * 24 + 28 }}>
      {commandMatches("checkbox") ? <SlashButton icon={CheckSquare} label={node.display_props?.show_checkbox === true ? "Toggle checkbox" : "Add checkbox"} onClick={onToggleCheckbox} /> : null}
      {commandMatches("child node") ? <SlashButton icon={Plus} label="Add child node" onClick={onAddChild} /> : null}
      {commandMatches("ai continue") ? <SlashButton icon={Sparkles} label="AI continue" onClick={onRunAi} /> : null}
      {commandMatches("search node") ? <SlashButton icon={Search} label="Search node" onClick={() => filteredTags[0] && onMakeSearch(filteredTags[0])} /> : null}
      {filteredTags.length > 0 ? <div className="px-2 pb-1 pt-2 text-[11px] text-muted-foreground">Tags and searches</div> : null}
      {filteredTags.map((tag) => (
        <div key={tag.id} className="grid grid-cols-[1fr_auto] gap-1">
          <button type="button" className="truncate rounded px-2 py-1 text-left hover:bg-accent" onClick={() => onApplyTag(tag)}>
            #{tag.name}
          </button>
          <button type="button" className="rounded px-2 py-1 text-muted-foreground hover:bg-accent" onClick={() => onMakeSearch(tag)}>
            List
          </button>
        </div>
      ))}
      <button type="button" className="mt-1 flex w-full items-center gap-2 rounded px-2 py-1 text-left text-muted-foreground hover:bg-accent" onClick={onClose}>
        <X className="size-3.5" />
        Close
      </button>
    </div>
  );
}

function SlashButton({ icon: Icon, label, onClick }: { icon: LucideIcon; label: string; onClick: () => void }) {
  return (
    <button type="button" className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent" onClick={onClick}>
      <Icon className="size-3.5 text-muted-foreground" />
      {label}
    </button>
  );
}

function InlineAutocompleteMenu({
  state,
  depth,
  tags,
  nodes,
  onSelectTag,
  onSelectNode,
}: {
  state: InlineAutocomplete;
  depth: number;
  tags: DocsSupertag[];
  nodes: DocsNode[];
  onSelectTag: (tag: DocsSupertag) => void;
  onSelectNode: (node: DocsNode) => void;
}) {
  const query = state.query.trim().toLowerCase();
  const tagMatches = tags.filter((tag) => !query || tag.name.toLowerCase().includes(query));
  const nodeMatches = nodes.filter((node) => !node.archived_at && (!query || nodeText(node).toLowerCase().includes(query))).slice(0, 100);
  const isTag = state.kind === "tag";
  const items = isTag ? tagMatches : nodeMatches;
  if (items.length === 0) return null;
  return (
    <div
      className="my-1 max-h-80 w-full max-w-md overflow-auto rounded border bg-popover p-1 text-xs shadow-lg"
      style={{ marginLeft: depth * 24 + 28 }}
      onMouseDown={(event) => event.preventDefault()}
    >
      {isTag
        ? tagMatches.map((tag) => (
            <button key={tag.id} type="button" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent" onMouseDown={() => onSelectTag(tag)}>
              <Hash className="size-3.5" style={tagColorStyle(tag.color)} />
              <span className="truncate">#{tag.name}</span>
              {tag.description ? <span className="ml-auto truncate text-[11px] text-muted-foreground">{tag.description}</span> : null}
            </button>
          ))
        : nodeMatches.map((node) => (
            <button key={node.id} type="button" className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent" onMouseDown={() => onSelectNode(node)}>
              <AtSign className="size-3.5 text-primary" />
              <span className="truncate">{nodeText(node)}</span>
            </button>
          ))}
    </div>
  );
}

function SearchNodeResults({
  node,
  depth,
  nodes,
  nodeSupertags,
  tags,
  fields,
  fieldValues: bootstrapFieldValues,
  projects,
  fieldsByTag,
  allSupertagFields,
  onSetView,
  onSetSort,
  onSetQuery,
  onOpenNode,
}: {
  node: DocsNode;
  depth: number;
  nodes: DocsNode[];
  nodeSupertags: DocsState["node_supertags"];
  tags: DocsSupertag[];
  fields: DocsField[];
  fieldValues: DocsFieldValue[];
  projects?: DocsProject[];
  fieldsByTag: Map<string, DocsField[]>;
  allSupertagFields: DocsState["supertag_fields"];
  onSetView: (view: SearchView) => void;
  onSetSort?: (sort: SearchSort) => void;
  onSetQuery?: (query: Record<string, unknown>) => void;
  onOpenNode: (nodeId: string) => void;
}) {
  const tagIds = searchTagIds(node);
  const textFilter = searchTextFilter(node);
  const fieldFilter = searchFieldFilter(node);
  const projectScope = searchProjectScope(node);
  const [textFilterDraft, setTextFilterDraft] = useState(textFilter);
  const [fieldFilterValueDraft, setFieldFilterValueDraft] = useState(fieldFilter.value);
  const [queryState, setQueryState] = useState<Pick<DocsState, "nodes" | "node_supertags" | "field_values">>({
    nodes: [],
    node_supertags: [],
    field_values: [],
  });
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [notingTaskIds, setNotingTaskIds] = useState<Set<string>>(
    () => new Set(),
  );

  useEffect(() => {
    setTextFilterDraft(textFilter);
    setFieldFilterValueDraft(fieldFilter.value);
  }, [fieldFilter.value, textFilter]);

  useEffect(() => {
    let cancelled = false;
    if (!node.query_json) {
      setQueryState({ nodes: [], node_supertags: [], field_values: [] });
      return;
    }
    setLoading(true);
    apiFetch<DocsQueryResponse>("/api/docs/query", {
      method: "POST",
      body: JSON.stringify({
        query_json: node.query_json,
        limit: typeof node.query_json.limit === "number" ? node.query_json.limit : 100,
      }),
    })
      .then((data) => {
        if (!cancelled) setQueryState({
          nodes: data.nodes ?? [],
          node_supertags: data.node_supertags ?? [],
          field_values: data.field_values ?? [],
        });
        if (!cancelled) setNextCursor(data.next_cursor ?? null);
      })
      .catch((error) => {
        if (!cancelled) toast.error(error instanceof Error ? error.message : "検索ノードの読み込みに失敗しました");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [node.query_json]);

  const loadMore = async () => {
    if (!node.query_json || !nextCursor || loading) return;
    setLoading(true);
    try {
      const data = await apiFetch<DocsQueryResponse>("/api/docs/query", {
        method: "POST",
        body: JSON.stringify({
          query_json: node.query_json,
          limit: typeof node.query_json.limit === "number" ? node.query_json.limit : 100,
          cursor: nextCursor,
        }),
      });
      setQueryState((current) => ({
        nodes: (data.nodes ?? []).reduce((items, nextNode) => mergeById(items, nextNode), current.nodes),
        node_supertags: [
          ...current.node_supertags,
          ...(data.node_supertags ?? []).filter((entry) =>
            !current.node_supertags.some((item) => item.node_id === entry.node_id && item.supertag_id === entry.supertag_id),
          ),
        ],
        field_values: [
          ...current.field_values.filter((value) =>
            !(data.field_values ?? []).some((next) => next.node_id === value.node_id && next.field_id === value.field_id),
          ),
          ...(data.field_values ?? []),
        ],
      }));
      setNextCursor(data.next_cursor ?? null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Search node pagination failed");
    } finally {
      setLoading(false);
    }
  };

  if (tagIds.length === 0 && !node.query_json) return null;
  const resultNodes = queryState.nodes.length > 0 || node.query_json ? queryState.nodes : nodes;
  const resultNodeSupertags = queryState.node_supertags.length > 0 ? queryState.node_supertags : nodeSupertags;
  const resultFieldValues = queryState.field_values.length > 0 ? queryState.field_values : bootstrapFieldValues;
  const tagSetByNode = tagSetByNodeId(resultNodeSupertags);
  const tagById = new Map(tags.map((tag) => [tag.id, tag]));
  const valuesByNode = new Map<string, DocsFieldValue[]>();
  for (const value of resultFieldValues) {
    const values = valuesByNode.get(value.node_id) ?? [];
    values.push(value);
    valuesByNode.set(value.node_id, values);
  }
  const limit = typeof node.query_json?.limit === "number" ? Math.max(1, Math.min(node.query_json.limit, 200)) : 100;
  const results = resultNodes.filter((item) => item.id !== node.id && !item.archived_at).slice(0, limit);
  const view = searchView(node);
  const sort = searchSort(node);
  const tagsFor = (nodeId: string) =>
    Array.from(tagSetByNode.get(nodeId) ?? [])
      .map((tagId) => tagById.get(tagId)?.name)
      .filter((name): name is string => Boolean(name));
  const tagObjectsFor = (nodeId: string) =>
    Array.from(tagSetByNode.get(nodeId) ?? [])
      .map((tagId) => tagById.get(tagId))
      .filter((tag): tag is DocsSupertag => Boolean(tag));
  const displayTitleFor = (item: DocsNode) =>
    renderNodeTitleTemplate(item, tagObjectsFor(item.id), fields, valuesByNode.get(item.id) ?? []);
  const rawCandidateFields = tagIds.length > 0
    ? tagIds.flatMap((tagId) => fieldsByTag.get(tagId) ?? [])
    : allSupertagFields.flatMap((relation) => fields.filter((field) => field.id === relation.field_id));
  const candidateFields = Array.from(new Map(rawCandidateFields.map((field) => [field.id, field])).values());
  const groupByFieldId = searchGroupBy(node);
  const groupableFields = candidateFields.filter((field) => field.field_type !== "long_text");
  const selectedGroupField = groupableFields.find((field) => field.id === groupByFieldId);
  const fallbackGroupField = candidateFields.find((field) => field.system_key === "task_status")
    ?? candidateFields.find((field) => field.field_type === "options" && /状態|status/i.test(field.name))
    ?? candidateFields.find((field) => field.field_type === "options");
  const groupField = selectedGroupField ?? fallbackGroupField;
  const dateField = fields.find((field) => field.field_type === "date");
  const groupFor = (item: DocsNode) => {
    if (!groupField) return "Results";
    const value = valuesByNode.get(item.id)?.find((entry) => entry.field_id === groupField.id);
    const label = fieldValueToDraft(value).trim();
    return label || "unset";
  };
  const dateFor = (item: DocsNode) => {
    if (item.day_date) return item.day_date.slice(0, 10);
    if (!dateField) return "";
    return valuesByNode.get(item.id)?.find((entry) => entry.field_id === dateField.id)?.value_datetime?.slice(0, 10) ?? "";
  };
  const fieldFilterNeedsValue = SEARCH_FIELD_FILTER_OPS.find((item) => item.value === fieldFilter.op)?.needsValue !== false;
  const persistTextFilter = () => {
    if (!onSetQuery || textFilterDraft.trim() === textFilter) return;
    onSetQuery(withSearchTextFilter(node.query_json, textFilterDraft));
  };
  const persistFieldFilter = (patch: Partial<SearchFieldFilterDraft>) => {
    if (!onSetQuery) return;
    const nextFilter = { ...fieldFilter, value: fieldFilterValueDraft, ...patch };
    onSetQuery(withSearchFieldFilter(node.query_json, nextFilter));
  };
  const viewButtons: Array<{ view: SearchView; label: string; icon: LucideIcon }> = [
    { view: "list", label: "List", icon: ListFilter },
    { view: "table", label: "Table", icon: Table2 },
    { view: "board", label: "Board", icon: KanbanSquare },
    { view: "calendar", label: "Calendar", icon: CalendarDays },
    { view: "cards", label: "Cards", icon: Columns2 },
  ];

  const virtualTaskId = (item: DocsNode) =>
    item.id.startsWith("task:") ? item.id.slice("task:".length) : null;

  const noteVirtualTask = async (item: DocsNode) => {
    const taskId = virtualTaskId(item);
    if (!taskId || notingTaskIds.has(taskId)) return;
    setNotingTaskIds((current) => new Set(current).add(taskId));
    try {
      const result = await apiFetch<{ node: { id: string }; created: boolean }>(
        `/api/tasks/${taskId}/docs-node`,
        { method: "POST" },
      );
      toast.success(
        result.created ? "Docsノートを作成しました" : "Docsノートを開きます",
      );
      onOpenNode(result.node.id);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Docsノート化に失敗しました",
      );
    } finally {
      setNotingTaskIds((current) => {
        const next = new Set(current);
        next.delete(taskId);
        return next;
      });
    }
  };

  const noteButton = (item: DocsNode) => {
    const taskId = virtualTaskId(item);
    if (!taskId) return null;
    return (
      <button
        type="button"
        className="ml-2 shrink-0 rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-60"
        disabled={notingTaskIds.has(taskId)}
        onClick={(event) => {
          event.stopPropagation();
          void noteVirtualTask(item);
        }}
      >
        {notingTaskIds.has(taskId) ? "作成中" : "ノート化"}
      </button>
    );
  };

  const updateResultTitle = async (item: DocsNode, title: string) => {
    const nextTitle = title.trim();
    if (!nextTitle || nextTitle === item.title) return;
    const taskId = virtualTaskId(item);
    if (taskId) {
      const data = await apiFetch<{ task: { title?: string | null } }>(`/api/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify({ title: nextTitle }),
      });
      setQueryState((current) => ({
        ...current,
        nodes: current.nodes.map((node) => node.id === item.id ? { ...node, title: data.task.title ?? nextTitle } : node),
      }));
      return;
    }
    const data = await apiFetch<{ node: DocsNode }>(`/api/docs/nodes/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ title: nextTitle }),
    });
    setQueryState((current) => ({
      ...current,
      nodes: current.nodes.map((node) => node.id === item.id ? data.node : node),
    }));
  };

  const titleEditor = (item: DocsNode) => (
    <Input
      defaultValue={nodeText(item)}
      className="h-7 min-w-0 border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-1"
      onClick={(event) => event.stopPropagation()}
      onBlur={(event) => void updateResultTitle(item, event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
      }}
    />
  );

  const openButton = (item: DocsNode, className: string) => (
    virtualTaskId(item) ? (
      <div
        key={item.id}
        className={cn(className, "flex items-center justify-between gap-2")}
      >
        {titleEditor(item)}
        {noteButton(item)}
      </div>
    ) : (
      <div key={item.id} className={cn(className, "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2")}>
        {titleEditor(item)}
        <button type="button" className="rounded px-1 text-[11px] text-muted-foreground hover:bg-accent" onClick={() => onOpenNode(item.id)}>
          Open
        </button>
      </div>
    )
  );

  return (
    <div className="my-2 space-y-2" style={{ paddingLeft: depth * 24 + 28 }}>
      <div className="flex flex-wrap items-center gap-1">
        {viewButtons.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.view}
              type="button"
              className={cn("flex items-center gap-1 rounded border px-2 py-1 text-[11px] hover:bg-accent", view === item.view && "bg-accent")}
              onClick={() => onSetView(item.view)}
              title={item.label}
            >
              <Icon className="size-3" />
              {item.label}
            </button>
          );
        })}
        {onSetSort ? (
          <select
            value={sort}
            onChange={(event) => onSetSort(event.target.value as SearchSort)}
            className="h-7 rounded border bg-background px-2 text-[11px]"
            title="Sort"
          >
            {SEARCH_SORT_OPTIONS.map((option) => (
              <option key={option.value || "default"} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        ) : null}
      </div>
      {onSetQuery ? (
        <div className="grid gap-2 rounded border bg-muted/20 p-2 text-[11px] md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,1.6fr)]">
          <label className="flex min-w-0 items-center gap-1.5">
            <Search className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              value={textFilterDraft}
              onChange={(event) => setTextFilterDraft(event.target.value)}
              onBlur={persistTextFilter}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  event.currentTarget.blur();
                }
              }}
              className="h-7 text-xs"
              placeholder="Text filter"
            />
          </label>
          <label className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-muted-foreground">Scope</span>
            <select
              value={projectScope}
              onChange={(event) => onSetQuery(withSearchProjectScope(node.query_json, event.target.value))}
              className="h-7 min-w-0 flex-1 rounded border bg-background px-2 text-[11px]"
              title="Project scope"
            >
              <option value="">All projects</option>
              {(projects ?? []).map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex min-w-0 items-center gap-1.5">
            <span className="shrink-0 text-muted-foreground">Group</span>
            <select
              value={selectedGroupField?.id ?? ""}
              onChange={(event) => onSetQuery(withSearchGroupBy(node.query_json, event.target.value))}
              className="h-7 min-w-0 flex-1 rounded border bg-background px-2 text-[11px]"
              title="Board group by"
            >
              <option value="">Auto group</option>
              {groupableFields.map((field) => (
                <option key={field.id} value={field.id}>
                  {field.name}
                </option>
              ))}
            </select>
          </label>
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_84px_minmax(0,1fr)_auto] gap-1">
            <select
              value={fieldFilter.fieldId}
              onChange={(event) => {
                setFieldFilterValueDraft("");
                persistFieldFilter({ fieldId: event.target.value, value: "" });
              }}
              className="h-7 min-w-0 rounded border bg-background px-2 text-[11px]"
              title="Field filter"
            >
              <option value="">Field filter</option>
              {candidateFields.map((field) => (
                <option key={field.id} value={field.id}>
                  {field.name}
                </option>
              ))}
            </select>
            <select
              value={fieldFilter.op}
              disabled={!fieldFilter.fieldId}
              onChange={(event) => persistFieldFilter({ op: event.target.value as SearchFieldFilterOp })}
              className="h-7 rounded border bg-background px-2 text-[11px] disabled:opacity-50"
              title="Field operator"
            >
              {SEARCH_FIELD_FILTER_OPS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <Input
              value={fieldFilterValueDraft}
              disabled={!fieldFilter.fieldId || !fieldFilterNeedsValue}
              onChange={(event) => setFieldFilterValueDraft(event.target.value)}
              onBlur={() => persistFieldFilter({ value: fieldFilterValueDraft })}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  event.currentTarget.blur();
                }
              }}
              className="h-7 text-xs disabled:opacity-50"
              placeholder={fieldFilterNeedsValue ? "Value" : ""}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              disabled={!fieldFilter.fieldId}
              title="Clear field filter"
              onClick={() => {
                setFieldFilterValueDraft("");
                persistFieldFilter({ fieldId: "", value: "" });
              }}
            >
              <X className="size-3.5" />
            </Button>
          </div>
        </div>
      ) : null}
      {loading ? <div className="text-xs text-muted-foreground">Loading query...</div> : null}
      {!loading && results.length === 0 ? <div className="text-xs text-muted-foreground">No matching nodes</div> : null}
      {view === "list" ? (
        <div className="space-y-0.5">
          {results.map((item) =>
            openButton(item, "flex min-h-7 w-full items-center gap-2 rounded border border-dashed px-2 py-1 text-left text-xs hover:bg-accent"),
          )}
        </div>
      ) : null}
      {view === "table" ? (
        <div className="overflow-hidden rounded border text-xs">
          <div className="grid grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_100px] border-b bg-muted/30 px-2 py-1 font-medium">
            <div>Title</div>
            <div>Tags</div>
            <div>Date</div>
          </div>
          {results.map((item) => {
            const taskId = virtualTaskId(item);
            const className = "grid w-full grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_100px] px-2 py-1 text-left hover:bg-accent";
            if (taskId) {
              return (
                <div key={item.id} className={className}>
                  <span className="flex min-w-0 items-center">
                    {titleEditor(item)}
                    {noteButton(item)}
                  </span>
                  <span className="truncate text-muted-foreground">{tagsFor(item.id).join(", ")}</span>
                  <span className="truncate text-muted-foreground">{dateFor(item)}</span>
                </div>
              );
            }
            return (
              <div key={item.id} className={className}>
                <span className="min-w-0">{titleEditor(item)}</span>
                <span className="truncate text-muted-foreground">{tagsFor(item.id).join(", ")}</span>
                <span className="truncate text-muted-foreground">{dateFor(item)}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      {view === "board" ? (
        <div className="grid gap-2 md:grid-cols-3">
          {Array.from(new Set(results.map(groupFor))).map((column) => (
            <div key={column} className="min-h-20 rounded border bg-muted/20 p-2">
              <div className="mb-2 text-[11px] font-medium text-muted-foreground">{column}</div>
              <div className="space-y-1">
                {results.filter((item) => groupFor(item) === column).map((item) => openButton(item, "block w-full truncate rounded border bg-background px-2 py-1 text-left text-xs hover:bg-accent"))}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {view === "calendar" ? (
        <CalendarMonthGrid results={results} dateFor={dateFor} onOpenNode={onOpenNode} />
      ) : null}
      {view === "cards" ? (
        <div className="grid gap-2 md:grid-cols-2">
          {results.map((item) => {
            const taskId = virtualTaskId(item);
            if (taskId) {
              return (
                <div key={item.id} className="min-h-20 rounded border p-2 text-left text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate font-medium">{displayTitleFor(item)}</div>
                    {noteButton(item)}
                  </div>
                  <div className="mt-2 line-clamp-2 text-muted-foreground">{item.description || item.body_text}</div>
                </div>
              );
            }
            return (
              <button key={item.id} type="button" onClick={() => onOpenNode(item.id)} className="min-h-20 rounded border p-2 text-left text-xs hover:bg-accent">
                <div className="truncate font-medium">{displayTitleFor(item)}</div>
                <div className="mt-2 line-clamp-2 text-muted-foreground">{item.description || item.body_text}</div>
              </button>
            );
          })}
        </div>
      ) : null}
      {nextCursor ? (
        <Button type="button" variant="ghost" size="sm" className="mt-2 h-7 text-xs" disabled={loading} onClick={() => void loadMore()}>
          Load more
        </Button>
      ) : null}
    </div>
  );
}

function SupertagPage({
  tag,
  tags,
  views,
  nodes,
  nodeSupertags,
  fields,
  fieldValues,
  fieldsByTag,
  allSupertagFields,
  onOpenTag,
  onOpenNode,
  onCreateTaggedNode,
  onCreateView,
  onUpdateView,
}: {
  tag: DocsSupertag | null;
  tags: DocsSupertag[];
  views: DocsSavedView[];
  nodes: DocsNode[];
  nodeSupertags: DocsState["node_supertags"];
  fields: DocsField[];
  fieldValues: DocsFieldValue[];
  fieldsByTag: Map<string, DocsField[]>;
  allSupertagFields: DocsState["supertag_fields"];
  onOpenTag: (tagId: string) => void;
  onOpenNode: (nodeId: string) => void;
  onCreateTaggedNode: (tag: DocsSupertag) => void;
  onCreateView: (tag: DocsSupertag, draft: Pick<DocsSavedView, "name" | "layout" | "config_json">) => Promise<DocsSavedView>;
  onUpdateView: (viewId: string, patch: Partial<Pick<DocsSavedView, "name" | "layout" | "config_json" | "sort_order">>) => Promise<DocsSavedView>;
}) {
  const savedViews = tag ? views.filter((view) => view.supertag_id === tag.id).sort((a, b) => a.sort_order - b.sort_order) : [];
  const defaultViews: DocsSavedView[] = tag
    ? [
        { id: `${tag.id}:list`, workspace_id: tag.workspace_id, supertag_id: tag.id, name: `${tag.name}`, layout: "list", config_json: {}, sort_order: 0, created_at: null, updated_at: null },
        { id: `${tag.id}:board`, workspace_id: tag.workspace_id, supertag_id: tag.id, name: `${tag.name} board`, layout: "board", config_json: {}, sort_order: 1, created_at: null, updated_at: null },
        { id: `${tag.id}:calendar`, workspace_id: tag.workspace_id, supertag_id: tag.id, name: `${tag.name} calendar`, layout: "calendar", config_json: {}, sort_order: 2, created_at: null, updated_at: null },
        { id: `${tag.id}:table`, workspace_id: tag.workspace_id, supertag_id: tag.id, name: `${tag.name} table`, layout: "table", config_json: {}, sort_order: 3, created_at: null, updated_at: null },
      ]
    : [];
  const viewList = savedViews.length > 0 ? savedViews : defaultViews;
  const [activeViewId, setActiveViewId] = useState<string | null>(null);
  const [showAddView, setShowAddView] = useState(false);
  const [newViewName, setNewViewName] = useState("");
  const [newViewLayout, setNewViewLayout] = useState<SearchView>("list");
  const [newViewQueryText, setNewViewQueryText] = useState("");
  const activeView = viewList.find((view) => view.id === activeViewId) ?? viewList[0] ?? null;
  const activeViewIsSaved = !!activeView && !activeView.id.includes(":");

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActiveViewId(null);
    setShowAddView(false);
  }, [tag?.id]);

  if (!tag) {
    return (
      <section className="min-w-0 flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto w-full max-w-3xl">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h1 className="text-2xl font-semibold">Supertags</h1>
            <Button type="button" variant="outline" size="sm">Browse templates</Button>
          </div>
          <Input className="mb-6 h-9" placeholder="Filter tags" />
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {tags.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onOpenTag(item.id)}
                className="min-h-20 rounded-md border p-3 text-left hover:bg-accent"
                style={{ backgroundColor: item.color ? `${item.color}33` : undefined, borderColor: item.color ?? undefined }}
              >
                <div className="font-medium">{item.name}</div>
                <Hash className="mt-6 size-4" style={tagColorStyle(item.color)} />
              </button>
            ))}
          </div>
        </div>
      </section>
    );
  }

  const layout = activeView?.layout === "board" || activeView?.layout === "calendar" || activeView?.layout === "table" || activeView?.layout === "cards"
    ? activeView.layout as SearchView
    : "list";
  const query = readConfigRecord(activeView?.config_json).query ?? { and: [{ tag: tag.id, include_descendants: true }], limit: 200 };
  const defaultNewViewQuery = {
    query: { and: [{ tag: tag.id, include_descendants: true }], limit: 200 },
  };
  const persistActiveViewQuery = (nextQuery: Record<string, unknown>) => {
    if (!activeViewIsSaved || !activeView) return;
    void onUpdateView(activeView.id, {
      config_json: {
        ...readConfigRecord(activeView.config_json),
        query: nextQuery,
      },
    });
  };
  const persistActiveViewSort = (sort: SearchSort) => {
    const nextQuery = { ...readConfigRecord(query) };
    if (sort) {
      nextQuery.sort = sort;
    } else {
      delete nextQuery.sort;
    }
    persistActiveViewQuery(nextQuery);
  };
  const persistActiveViewLayout = (view: SearchView) => {
    if (activeViewIsSaved && activeView) {
      void onUpdateView(activeView.id, { layout: view });
      return;
    }
    setActiveViewId(viewList.find((item) => item.layout === view)?.id ?? activeView?.id ?? null);
  };
  const beginAddView = () => {
    setNewViewName(`${tag.name} custom view`);
    setNewViewLayout("list");
    setNewViewQueryText(JSON.stringify(defaultNewViewQuery, null, 2));
    setShowAddView(true);
  };
  const submitAddView = () => {
    const name = newViewName.trim();
    if (!name) {
      toast.error("ビュー名を入力してください");
      return;
    }
    let configJson: Record<string, unknown>;
    try {
      const parsed = JSON.parse(newViewQueryText);
      configJson = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
    } catch {
      toast.error("AST JSONを確認してください");
      return;
    }
    void onCreateView(tag, { name, layout: newViewLayout, config_json: configJson }).then((view) => {
      setActiveViewId(view.id);
      setShowAddView(false);
    });
  };
  const searchNode: DocsNode = {
    id: `${tag.id}:${layout}:search`,
    workspace_id: tag.workspace_id,
    parent_id: null,
    root_page_id: null,
    project_id: null,
    title: `List of ${tag.name}`,
    description: "",
    body_json: {},
    body_text: "",
    node_type: "search",
    display_props: {},
    query_json: query as Record<string, unknown>,
    view_json: { view: layout },
    day_date: null,
    sort_order: 0,
    created_at: null,
    updated_at: null,
    archived_at: null,
  };

  return (
    <section className="min-w-0 flex-1 overflow-auto px-6 py-5">
      <div className="mx-auto w-full max-w-5xl">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="grid size-8 place-items-center rounded border text-lg" style={tagColorStyle(tag.color)}>#</span>
              <h1 className="truncate text-3xl font-semibold">{tag.name}</h1>
            </div>
            {tag.description ? <div className="mt-1 text-sm text-muted-foreground">{tag.description}</div> : null}
          </div>
          <Button type="button" size="sm" onClick={() => onCreateTaggedNode(tag)}>
            <Plus className="size-4" />
            Create new
          </Button>
        </div>
        <div className="mb-5 flex flex-wrap gap-1 border-b pb-2">
          {viewList.map((view) => (
            <button key={view.id} type="button" className={cn("rounded px-2 py-1 text-xs hover:bg-accent", activeView?.id === view.id && "bg-accent")} onClick={() => setActiveViewId(view.id)}>
              {view.name}
            </button>
          ))}
          <button type="button" className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent" onClick={beginAddView}>+ Add view</button>
        </div>
        {showAddView ? (
          <div className="mb-4 grid gap-2 rounded border bg-muted/20 p-3 text-xs md:grid-cols-[minmax(0,1fr)_150px_auto]">
            <Input
              value={newViewName}
              onChange={(event) => setNewViewName(event.target.value)}
              className="h-8 text-xs"
              placeholder="View name"
            />
            <select
              value={newViewLayout}
              onChange={(event) => setNewViewLayout(event.target.value as SearchView)}
              className="h-8 rounded border bg-background px-2 text-xs"
              title="View layout"
            >
              <option value="list">List</option>
              <option value="table">Table</option>
              <option value="board">Board</option>
              <option value="calendar">Calendar</option>
              <option value="cards">Cards</option>
            </select>
            <div className="flex gap-1">
              <Button type="button" size="sm" onClick={submitAddView}>保存</Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowAddView(false)}>閉じる</Button>
            </div>
            <textarea
              value={newViewQueryText}
              onChange={(event) => setNewViewQueryText(event.target.value)}
              className="min-h-24 rounded border bg-background p-2 font-mono text-[11px] md:col-span-3"
              spellCheck={false}
              aria-label="View query AST"
            />
          </div>
        ) : null}
        <SearchNodeResults
          node={searchNode}
          depth={0}
          nodes={nodes}
          nodeSupertags={nodeSupertags}
          tags={tags}
          fields={fields}
          fieldValues={fieldValues}
          fieldsByTag={fieldsByTag}
          allSupertagFields={allSupertagFields}
          onSetView={persistActiveViewLayout}
          onSetSort={activeViewIsSaved ? persistActiveViewSort : undefined}
          onSetQuery={activeViewIsSaved ? persistActiveViewQuery : undefined}
          onOpenNode={onOpenNode}
        />
      </div>
    </section>
  );
}

function CalendarMonthGrid({
  results,
  dateFor,
  onOpenNode,
}: {
  results: DocsNode[];
  dateFor: (node: DocsNode) => string;
  onOpenNode: (nodeId: string) => void;
}) {
  const dated = results.map((node) => ({ node, date: dateFor(node) })).filter((item) => /^\d{4}-\d{2}-\d{2}$/.test(item.date));
  const undated = results.filter((node) => !/^\d{4}-\d{2}-\d{2}$/.test(dateFor(node)));
  const base = dated[0]?.date ? new Date(`${dated[0].date}T00:00:00`) : new Date();
  const monthStart = new Date(base.getFullYear(), base.getMonth(), 1);
  const gridStart = new Date(monthStart);
  gridStart.setDate(monthStart.getDate() - monthStart.getDay());
  const byDate = new Map<string, DocsNode[]>();
  for (const item of dated) {
    const list = byDate.get(item.date) ?? [];
    list.push(item.node);
    byDate.set(item.date, list);
  }
  const cells = Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart);
    date.setDate(gridStart.getDate() + index);
    const iso = date.toISOString().slice(0, 10);
    return { date, iso, nodes: byDate.get(iso) ?? [] };
  });
  return (
    <div className="space-y-2 text-xs">
      <div className="grid grid-cols-7 gap-px overflow-hidden rounded border bg-border">
        {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => (
          <div key={day} className="bg-muted px-2 py-1 text-[11px] font-medium text-muted-foreground">{day}</div>
        ))}
        {cells.map((cell) => (
          <div key={cell.iso} className={cn("min-h-24 bg-background p-1", cell.date.getMonth() !== monthStart.getMonth() && "text-muted-foreground/50")}>
            <div className="mb-1 text-[11px]">{cell.date.getDate()}</div>
            <div className="space-y-1">
              {cell.nodes.map((node) => (
                <button key={node.id} type="button" className="block w-full truncate rounded bg-muted/50 px-1.5 py-0.5 text-left hover:bg-accent" onClick={() => onOpenNode(node.id)}>
                  {nodeText(node)}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      {undated.length > 0 ? (
        <div className="rounded border border-dashed p-2 text-muted-foreground">
          <div className="mb-1 font-medium">{undated.length} nodes with no dates</div>
          <div className="flex flex-wrap gap-1">
            {undated.slice(0, 20).map((node) => (
              <button key={node.id} type="button" className="rounded bg-muted px-2 py-0.5 hover:bg-accent hover:text-foreground" onClick={() => onOpenNode(node.id)}>
                {nodeText(node)}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function FieldRows({
  node,
  fields,
  values,
  nodes,
  projects,
  suggestions,
  onSuggestionStatus,
  onRunAi,
  onSave,
}: {
  node: DocsNode;
  fields: DocsField[];
  values: Map<string, DocsFieldValue>;
  nodes: DocsNode[];
  projects: DocsProject[];
  suggestions: DocsAiSuggestion[];
  onSuggestionStatus: (suggestionId: string, status: "accepted" | "rejected" | "stale") => Promise<void>;
  onRunAi: () => void;
  onSave: (node: DocsNode, field: DocsField, value: string) => Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  if (fields.length === 0) return null;
  const fieldSuggestions = suggestions
    .filter((suggestion) => suggestion.node_id === node.id && suggestion.status === "proposed")
    .flatMap((suggestion) => {
      const payloadFields = Array.isArray(suggestion.payload_json.fields)
        ? suggestion.payload_json.fields
        : [];
      return payloadFields.flatMap((item): Array<{ suggestion: DocsAiSuggestion; field: DocsField; value: string }> => {
        if (!item || typeof item !== "object") return [];
        const record = item as Record<string, unknown>;
        const name = String(record.name ?? record.field ?? "").trim().toLowerCase();
        const value = String(record.value ?? "").trim();
        if (!name || !value) return [];
        const field = fields.find((candidate) =>
          candidate.name.toLowerCase() === name ||
          candidate.system_key?.toLowerCase() === name,
        );
        return field ? [{ suggestion, field, value }] : [];
      });
    });
  return (
    <div className="mb-4 max-w-4xl space-y-0.5">
      <div className="mb-1 flex justify-end">
        <Button type="button" variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={onRunAi}>
          <Sparkles className="size-3.5" />
          AIで埋める
        </Button>
      </div>
      {fields.map((field) => {
        const key = `${node.id}:${field.id}`;
        const value = drafts[key] ?? fieldValueToDraft(values.get(key));
        const suggestion = fieldSuggestions.find((item) => item.field.id === field.id);
        return (
          <div key={field.id} className="grid min-h-7 grid-cols-[160px_minmax(0,1fr)] items-start gap-2 py-0.5 text-sm">
            <div className="truncate pt-1 text-xs text-muted-foreground">{field.name}:</div>
            <div className="min-w-0 space-y-1">
              <FieldControl
                field={field}
                value={value}
                nodes={nodes}
                projects={projects}
                currentNodeId={node.id}
                onChange={(next) => setDrafts((current) => ({ ...current, [key]: next }))}
                onCommit={(next) => {
                  setDrafts((current) => {
                    const copy = { ...current };
                    delete copy[key];
                    return copy;
                  });
                  void onSave(node, field, next);
                }}
              />
              {suggestion && !value ? (
                <div className="flex items-center gap-1 rounded border border-dashed bg-muted/20 px-2 py-1 text-xs text-muted-foreground">
                  <Sparkles className="size-3.5 shrink-0" />
                  <button
                    type="button"
                    className="min-w-0 flex-1 truncate text-left hover:text-foreground"
                    onClick={async () => {
                      await onSave(node, field, suggestion.value);
                      await onSuggestionStatus(suggestion.suggestion.id, "accepted");
                    }}
                  >
                    {suggestion.value}
                  </button>
                  <button type="button" className="rounded px-1 hover:bg-accent" onClick={() => void onSuggestionStatus(suggestion.suggestion.id, "rejected")}>
                    <X className="size-3.5" />
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RightPanel({
  mode,
  selectedNode,
  selectedTag,
  tags,
  nodeTags,
  fields,
  fieldsByTag,
  newTagName,
  setNewTagName,
  onApplyTag,
  onOpenTag,
  onCreateTag,
  onCreateField,
  onUpdateSupertag,
  onUpdateField,
  onCreateSearchNode,
  searchNodes,
  archivedNodes,
  relatedNodes,
  onOpenNode,
  onRestoreNode,
  onPermanentDeleteNode,
}: {
  mode: "related" | "tags" | "search" | "trash";
  selectedNode: DocsNode | null;
  selectedTag: DocsSupertag | null;
  tags: DocsSupertag[];
  nodeTags: DocsSupertag[];
  fields: DocsField[];
  fieldsByTag: Map<string, DocsField[]>;
  newTagName: string;
  setNewTagName: (value: string) => void;
  onApplyTag: (tagId: string) => void;
  onOpenTag: (tagId: string) => void;
  onCreateTag: () => void;
  onCreateField: (tagId: string, name: string, fieldType: string) => void;
  onUpdateSupertag: (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => void;
  onUpdateField: (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => void;
  onCreateSearchNode: (tag: DocsSupertag) => void;
  searchNodes: DocsNode[];
  archivedNodes: DocsNode[];
  relatedNodes: DocsNode[];
  onOpenNode: (nodeId: string) => void;
  onRestoreNode: (nodeId: string) => void;
  onPermanentDeleteNode: (nodeId: string) => void;
}) {
  const heading =
    mode === "tags"
      ? "Supertags"
      : mode === "search"
        ? "Search nodes"
        : mode === "trash"
          ? "ゴミ箱"
          : "Related content";
  return (
    <div className="p-3">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Settings2 className="size-4" />
        {heading}
      </div>
      {mode === "tags" ? (
        selectedTag ? (
          <SupertagConfigPanel
            tag={selectedTag}
            tags={tags}
            fields={fieldsByTag.get(selectedTag.id) ?? fields.filter((field) => field.supertag_id === selectedTag.id)}
            onCreateField={onCreateField}
            onUpdateSupertag={onUpdateSupertag}
            onUpdateField={onUpdateField}
          />
        ) : (
          <div className="space-y-2">
            <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
              <Input value={newTagName} onChange={(event) => setNewTagName(event.target.value)} placeholder="Filter or create tag" className="h-8" />
              <Button type="button" size="sm" variant="secondary" onClick={onCreateTag}>
                <Plus className="size-4" />
              </Button>
            </div>
            {tags
              .filter((tag) => tag.name.toLowerCase().includes(newTagName.toLowerCase()))
              .slice(0, 100)
              .map((tag) => (
                <div key={tag.id} className="rounded border p-2">
                  <button type="button" onClick={() => onOpenTag(tag.id)} className="flex w-full items-center justify-between rounded px-1 py-1 text-left text-sm hover:bg-accent">
                    <span style={tagColorStyle(tag.color)}>#{tag.name}</span>
                    {nodeTags.some((item) => item.id === tag.id) ? <span className="text-xs text-muted-foreground">applied</span> : null}
                  </button>
                  <div className="mt-1 flex gap-1">
                    <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => onApplyTag(tag.id)}>Apply</Button>
                    <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => onCreateSearchNode(tag)}>Search node</Button>
                  </div>
                </div>
              ))}
          </div>
        )
      ) : null}
      {mode === "search" ? (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground">タグから検索ノードを作成</div>
          {tags.slice(0, 20).map((tag) => (
            <button key={tag.id} type="button" onClick={() => onCreateSearchNode(tag)} className="flex w-full items-center gap-2 rounded border px-2 py-1.5 text-left text-sm hover:bg-accent">
              <ListFilter className="size-4 text-muted-foreground" />
              List of {tag.name}
            </button>
          ))}
          {searchNodes.length > 0 ? <div className="pt-3 text-xs text-muted-foreground">既存検索ノード</div> : null}
          {searchNodes.map((node) => (
            <button key={node.id} type="button" onClick={() => onOpenNode(node.id)} className="block w-full truncate rounded px-2 py-1 text-left text-sm hover:bg-accent">
              {node.title}
            </button>
          ))}
        </div>
      ) : null}
      {mode === "trash" ? (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground">アーカイブ済みノード</div>
          {archivedNodes.length === 0 ? <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">ゴミ箱は空です</div> : null}
          {archivedNodes.map((node) => (
            <div key={node.id} className="rounded border p-2">
              <div className="truncate text-sm font-medium">{nodeText(node)}</div>
              <div className="mt-1 text-[11px] text-muted-foreground">{node.archived_at ? `Archived: ${node.archived_at}` : ""}</div>
              <div className="mt-2 flex gap-1">
                <Button type="button" size="sm" variant="secondary" className="h-7 px-2 text-xs" onClick={() => onRestoreNode(node.id)}>復元</Button>
                <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs text-destructive" onClick={() => onPermanentDeleteNode(node.id)}>完全削除</Button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {mode === "related" ? (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground">{selectedNode ? `${nodeText(selectedNode)} のタグ関連ノード` : "ノードを選択してください"}</div>
          {selectedNode && relatedNodes.length === 0 ? <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">No related tag matches</div> : null}
          {relatedNodes.map((node) => (
            <button key={node.id} type="button" onClick={() => onOpenNode(node.id)} className="block w-full truncate rounded border px-2 py-1.5 text-left text-sm hover:bg-accent">
              {nodeText(node)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FieldCreator({ tagId, onCreateField }: { tagId: string; onCreateField: (tagId: string, name: string, fieldType: string) => void }) {
  const [name, setName] = useState("");
  const [fieldType, setFieldType] = useState("text");
  return (
    <div className="mt-2 grid grid-cols-[minmax(0,1fr)_96px_auto] gap-1">
      <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="New field" className="h-7 text-xs" />
      <select value={fieldType} onChange={(event) => setFieldType(event.target.value)} className="h-7 rounded border bg-background px-1 text-xs">
        {["text", "options", "date", "checkbox", "reference", "number"].map((type) => (
          <option key={type} value={type}>
            {type}
          </option>
        ))}
      </select>
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        onClick={() => {
          onCreateField(tagId, name, fieldType);
          setName("");
        }}
      >
        <Plus className="size-4" />
      </Button>
    </div>
  );
}

function SupertagConfigPanel({
  tag,
  tags,
  fields,
  onCreateField,
  onUpdateSupertag,
  onUpdateField,
}: {
  tag: DocsSupertag;
  tags: DocsSupertag[];
  fields: DocsField[];
  onCreateField: (tagId: string, name: string, fieldType: string) => void;
  onUpdateSupertag: (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => void;
  onUpdateField: (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => void;
}) {
  const config = readConfigRecord(tag.config_json);
  const templateBlocks = Array.isArray(tag.template_json?.blocks)
    ? tag.template_json.blocks.filter((block): block is Record<string, unknown> => Boolean(block && typeof block === "object"))
    : [];
  const templateText = templateBlocks.map((block) => typeof block.text === "string" ? block.text : "").join("\n");
  const optionsFields = fields.filter((field) => docsFieldType(field) === "options");
  const doneMapping = readConfigRecord(config.done_state_mapping ?? config.doneStateMapping);
  const selectedDoneField = fields.find((field) => field.id === doneMapping.field_id) ?? optionsFields[0] ?? null;
  const doneValue = typeof doneMapping.done_value === "string"
    ? doneMapping.done_value
    : typeof doneMapping.checked_value === "string"
      ? doneMapping.checked_value
      : "";
  const relatedTagId = tagIdsFromRelatedConfig(tag)[0] ?? "";

  const updateConfig = (patch: Record<string, unknown>) => {
    onUpdateSupertag(tag.id, { config_json: { ...config, ...patch } });
  };

  return (
    <div className="space-y-4 text-sm">
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <span className="rounded border px-2 py-1 text-xs" style={tagColorStyle(tag.color)}>#{tag.name}</span>
          <Input defaultValue={tag.color ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { color: event.target.value })} className="h-8" placeholder="Color" />
        </div>
        <Input defaultValue={tag.description ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { description: event.target.value })} className="h-8" placeholder="Description" />
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium text-muted-foreground">Content template</div>
        <textarea
          defaultValue={templateText}
          onBlur={(event) => {
            const lines = event.target.value.split(/\r?\n/);
            onUpdateSupertag(tag.id, {
              template_json: {
                format: "doc_block_template",
                blocks: lines.map((text) => ({ type: "paragraph", text })),
              },
            });
          }}
          className="min-h-20 w-full rounded border bg-background px-2 py-1 text-xs"
          placeholder="Default content lines"
        />
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium text-muted-foreground">Fields</div>
        <div className="space-y-2">
          {fields.map((field) => (
            <div key={field.id} className="grid grid-cols-[minmax(0,1fr)_92px_auto] items-center gap-1 rounded border px-2 py-1">
              <Input defaultValue={field.name} onBlur={(event) => onUpdateField(field.id, { name: event.target.value })} className="h-7 border-0 bg-transparent px-0" />
              <select
                defaultValue={docsFieldType(field)}
                onChange={(event) => onUpdateField(field.id, { field_type: event.target.value })}
                className="h-7 rounded border bg-background px-1 text-xs"
              >
                {["text", "long_text", "options", "date", "checkbox", "reference", "number", "url", "email", "user"].map((type) => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </select>
              <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                <input type="checkbox" defaultChecked={field.required} onChange={(event) => onUpdateField(field.id, { required: event.target.checked })} />
                req
              </label>
              {docsFieldType(field) === "options" ? (
                <input
                  defaultValue={fieldOptions(field).join(", ")}
                  onBlur={(event) => onUpdateField(field.id, { options_json: { values: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) } })}
                  className="col-span-3 h-7 border-0 bg-transparent px-0 text-xs outline-none"
                  placeholder="options"
                />
              ) : null}
            </div>
          ))}
        </div>
        <FieldCreator tagId={tag.id} onCreateField={onCreateField} />
      </section>

      <section className="space-y-2 border-t pt-3">
        <label className="flex items-center justify-between gap-2 text-xs">
          <span>Show checkbox</span>
          <input type="checkbox" defaultChecked={config.show_checkbox === true} onChange={(event) => updateConfig({ show_checkbox: event.target.checked })} />
        </label>
        <div className="grid grid-cols-[1fr_1fr] gap-2">
          <select
            value={selectedDoneField?.id ?? ""}
            onChange={(event) => updateConfig({ done_state_mapping: { ...doneMapping, field_id: event.target.value } })}
            className="h-8 rounded border bg-background px-2 text-xs"
          >
            <option value="">Done field</option>
            {optionsFields.map((field) => <option key={field.id} value={field.id}>{field.name}</option>)}
          </select>
          <select
            value={doneValue}
            onChange={(event) => updateConfig({ done_state_mapping: { ...doneMapping, done_value: event.target.value, field_id: selectedDoneField?.id ?? "" } })}
            className="h-8 rounded border bg-background px-2 text-xs"
          >
            <option value="">Checked value</option>
            {(selectedDoneField ? fieldOptions(selectedDoneField) : []).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </div>
      </section>

      <section className="space-y-2 border-t pt-3">
        <div className="text-xs font-medium text-muted-foreground">Advanced options</div>
        <Input defaultValue={tag.title_template ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { title_template: event.target.value })} className="h-8" placeholder="Title expression" />
        <select
          value={typeof config.default_child_supertag_id === "string" ? config.default_child_supertag_id : ""}
          onChange={(event) => updateConfig({ default_child_supertag_id: event.target.value || null })}
          className="h-8 w-full rounded border bg-background px-2 text-xs"
        >
          <option value="">Default child supertag</option>
          {tags.map((item) => <option key={item.id} value={item.id}>#{item.name}</option>)}
        </select>
        <select
          value={relatedTagId}
          onChange={(event) => updateConfig({
            related_content: event.target.value
              ? { query: { and: [{ tag: event.target.value, include_descendants: true }] } }
              : null,
          })}
          className="h-8 w-full rounded border bg-background px-2 text-xs"
        >
          <option value="">Related content tag</option>
          {tags.map((item) => <option key={item.id} value={item.id}>#{item.name}</option>)}
        </select>
      </section>
    </div>
  );
}

function ZoomReferences({ references, loading, onOpenNode }: { references: ReferencesState; loading: boolean; onOpenNode: (nodeId: string) => void }) {
  const fieldGroups = new Map<string, DocsReference[]>();
  for (const item of references.field_refs) {
    const fieldName = item.field_name || "field";
    const items = fieldGroups.get(fieldName) ?? [];
    items.push(item);
    fieldGroups.set(fieldName, items);
  }
  const hasAny = references.referenced_in.length > 0 || references.mentioned_in.length > 0 || references.field_refs.length > 0;

  return (
    <section className="mt-8 border-t pt-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Link2 className="size-4 text-muted-foreground" />
        References
      </div>
      {loading ? <div className="mb-3 text-xs text-muted-foreground">Loading...</div> : null}
      {!loading && !hasAny ? <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">No references</div> : null}
      <div className="grid gap-3 lg:grid-cols-3">
        <ReferenceSection title="Referenced in" items={references.referenced_in} onOpenNode={onOpenNode} />
        <ReferenceSection title="Mentioned in" items={references.mentioned_in} onOpenNode={onOpenNode} />
        <div className="space-y-3">
          {fieldGroups.size === 0 ? (
            <ReferenceSection title="Appears as field in" items={[]} onOpenNode={onOpenNode} />
          ) : (
            Array.from(fieldGroups.entries()).map(([fieldName, items]) => (
              <ReferenceSection key={fieldName} title={`Appears as ${fieldName} in`} items={items} onOpenNode={onOpenNode} />
            ))
          )}
        </div>
      </div>
    </section>
  );
}

function ReferenceSection({ title, items, onOpenNode }: { title: string; items: DocsReference[]; onOpenNode: (nodeId: string) => void }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length === 0 ? <div className="rounded border border-dashed p-2 text-xs text-muted-foreground">None</div> : null}
      <div className="space-y-1">
        {items.map((item) => (
          <button key={`${item.kind}:${item.field_name ?? ""}:${item.node.id}:${item.snippet}`} type="button" onClick={() => onOpenNode(item.node.id)} className="block w-full rounded border px-2 py-1.5 text-left text-xs hover:bg-accent">
            <div className="truncate font-medium">{nodeText(item.node)}</div>
            <div className="truncate text-muted-foreground">{item.field_name ? `${item.field_name}: ` : ""}{item.snippet}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
