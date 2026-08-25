"use client";

import { AppSelect } from "@/components/ui/app-select";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { observeElementRect as observeVirtualElementRect, useVirtualizer } from "@tanstack/react-virtual";
import {
  CalendarDays,
  Columns2,
  KanbanSquare,
  ListFilter,
  Plus,
  Search,
  SlidersHorizontal,
  Table2,
  X,
  type LucideIcon,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { FieldControl } from "./field-control";
import { fieldDraftToPayload, fieldValueToDraft } from "./docs-utils";
import type {
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsProject,
  DocsState,
  DocsSupertag,
} from "./types";
import {
  SEARCH_FIELD_FILTER_OPS,
  SEARCH_SORT_OPTIONS,
  mergeById,
  normalizeSearchQuery,
  nodeText,
  renderNodeTitleTemplate,
  searchFieldFilter,
  searchGroupBy,
  searchSort,
  searchTagIds,
  searchTextFilter,
  searchView,
  tagSetByNodeId,
  withSearchFieldFilter,
  withSearchGroupBy,
  withSearchTextFilter,
  type DocsApiFetch,
  type DocsQueryResponse,
  type SearchFieldFilterDraft,
  type SearchFieldFilterOp,
  type SearchSort,
  type SearchView,
} from "./docs-workspace-shared";

function VirtualizedSearchList<T>({
  items,
  estimateSize,
  className,
  renderItem,
}: {
  items: T[];
  estimateSize: number;
  className?: string;
  renderItem: (item: T, index: number) => ReactNode;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimateSize,
    overscan: 8,
    initialRect: { width: 0, height: 512 },
    observeElementRect: (instance, callback) =>
      observeVirtualElementRect(instance, (rect) =>
        callback(rect.height > 0 ? rect : { ...rect, height: 512 }),
      ),
  });
  return (
    <div ref={scrollRef} className={cn("max-h-[32rem] overflow-auto", className)}>
      <div className="relative w-full" style={{ height: `${virtualizer.getTotalSize()}px` }}>
        {virtualizer.getVirtualItems().map((virtualItem) => {
          const item = items[virtualItem.index];
          if (item === undefined) return null;
          return (
            <div
              key={virtualItem.key}
              data-index={virtualItem.index}
              ref={virtualizer.measureElement}
              className="absolute left-0 top-0 w-full"
              style={{ transform: `translateY(${virtualItem.start}px)` }}
            >
              {renderItem(item, virtualItem.index)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SearchNodeResults({
  apiFetch,
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
  context = "zoom",
  documentExpanded = false,
  onSetView,
  onSetSort,
  onSetQuery,
  onOpenNode,
  onCreateRow,
  onFieldValuesChanged,
}: {
  apiFetch: DocsApiFetch;
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
  context?: "document" | "zoom";
  documentExpanded?: boolean;
  onSetView: (view: SearchView) => void;
  onSetSort?: (sort: SearchSort) => void;
  onSetQuery?: (query: Record<string, unknown>) => void;
  onOpenNode: (nodeId: string) => void;
  onCreateRow?: () => Promise<DocsNode>;
  onFieldValuesChanged?: (nodeId: string, fieldId: string, fieldValues: DocsFieldValue[]) => void;
}) {
  const tagIds = searchTagIds(node);
  const textFilter = searchTextFilter(node);
  const fieldFilter = searchFieldFilter(node);
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
  const [queryEditorOpen, setQueryEditorOpen] = useState(false);
  const [creatingRow, setCreatingRow] = useState(false);
  const queryCacheKeyRef = useRef<string | null>(null);
  const queryResultCacheRef = useRef(new Map<string, DocsQueryResponse>());
  const queryInFlightRef = useRef(new Map<string, Promise<DocsQueryResponse>>());
  const queryJson = useMemo(
    () => (node.query_json ? normalizeSearchQuery(node.query_json) : null),
    [node.query_json],
  );
  const queryKey = useMemo(() => (queryJson ? JSON.stringify(queryJson) : null), [queryJson]);
  const latestQueryKeyRef = useRef(queryKey);
  latestQueryKeyRef.current = queryKey;

  useEffect(() => {
    setTextFilterDraft(textFilter);
    setFieldFilterValueDraft(fieldFilter.value);
  }, [fieldFilter.value, textFilter]);

  useEffect(() => {
    if (queryCacheKeyRef.current === queryKey) return;
    setQueryState({ nodes: [], node_supertags: [], field_values: [] });
    setNextCursor(null);
  }, [queryKey]);

  useEffect(() => {
    if (!queryJson) {
      queryCacheKeyRef.current = null;
      setLoading(false);
      return;
    }
    // Document search nodes are live only while expanded. The local queryState is the per-node cache,
    // so collapsing and reopening a node does not issue another request for the same query_json.
    if (context === "document" && !documentExpanded) return;
    if (queryCacheKeyRef.current === queryKey) return;
    if (!queryKey) return;
    const cached = queryResultCacheRef.current.get(queryKey);
    if (cached) {
      queryCacheKeyRef.current = queryKey;
      setQueryState({
        nodes: cached.nodes ?? [],
        node_supertags: cached.node_supertags ?? [],
        field_values: cached.field_values ?? [],
      });
      setNextCursor(cached.next_cursor ?? null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const request = queryInFlightRef.current.get(queryKey) ?? Promise.resolve().then(() => apiFetch<DocsQueryResponse>("/api/docs/query", {
      method: "POST",
      body: JSON.stringify({
        query_json: queryJson,
        limit: typeof queryJson.limit === "number" ? queryJson.limit : 100,
      }),
    }));
    queryInFlightRef.current.set(queryKey, request);
    request
      .then((data) => {
        queryResultCacheRef.current.set(queryKey, data);
        if (latestQueryKeyRef.current !== queryKey) return;
        queryCacheKeyRef.current = queryKey;
        setQueryState({
          nodes: data.nodes ?? [],
          node_supertags: data.node_supertags ?? [],
          field_values: data.field_values ?? [],
        });
        setNextCursor(data.next_cursor ?? null);
      })
      .catch((error) => {
        if (latestQueryKeyRef.current === queryKey) {
          toast.error(error instanceof Error ? error.message : "検索ノードの読み込みに失敗しました");
        }
      })
      .finally(() => {
        if (queryInFlightRef.current.get(queryKey) === request) queryInFlightRef.current.delete(queryKey);
        if (latestQueryKeyRef.current === queryKey) setLoading(false);
      });
  }, [apiFetch, context, documentExpanded, queryJson, queryKey]);

  const loadMore = async () => {
    if (!queryJson || !nextCursor || loading) return;
    setLoading(true);
    try {
      const data = await apiFetch<DocsQueryResponse>("/api/docs/query", {
        method: "POST",
        body: JSON.stringify({
          query_json: queryJson,
          limit: typeof queryJson.limit === "number" ? queryJson.limit : 100,
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

  const resultNodes = useMemo(
    () => (queryState.nodes.length > 0 || node.query_json ? queryState.nodes : nodes),
    [node.query_json, nodes, queryState.nodes],
  );
  const resultNodeSupertags = queryState.node_supertags.length > 0 ? queryState.node_supertags : nodeSupertags;
  const resultFieldValues = queryState.field_values.length > 0 ? queryState.field_values : bootstrapFieldValues;
  const tagSetByNode = useMemo(() => tagSetByNodeId(resultNodeSupertags), [resultNodeSupertags]);
  const tagById = useMemo(() => new Map(tags.map((tag) => [tag.id, tag])), [tags]);
  const valuesByNode = useMemo(() => {
    const map = new Map<string, DocsFieldValue[]>();
    for (const value of resultFieldValues) {
      const values = map.get(value.node_id) ?? [];
      values.push(value);
      map.set(value.node_id, values);
    }
    return map;
  }, [resultFieldValues]);
  const limit = typeof node.query_json?.limit === "number" ? Math.max(1, Math.min(node.query_json.limit, 200)) : 100;
  const results = useMemo(
    () => resultNodes.filter((item) => item.id !== node.id && !item.archived_at).slice(0, limit),
    [limit, node.id, resultNodes],
  );
  const view = searchView(node);
  const sort = searchSort(node);
  const tagObjectsFor = (nodeId: string) =>
    Array.from(tagSetByNode.get(nodeId) ?? [])
      .map((tagId) => tagById.get(tagId))
      .filter((tag): tag is DocsSupertag => Boolean(tag));
  const displayTitleFor = (item: DocsNode) =>
    renderNodeTitleTemplate(item, tagObjectsFor(item.id), fields, valuesByNode.get(item.id) ?? []);
  const candidateFields = useMemo(() => {
    const rawCandidateFields = tagIds.length > 0
      ? tagIds.flatMap((tagId) => fieldsByTag.get(tagId) ?? [])
      : allSupertagFields.flatMap((relation) => fields.filter((field) => field.id === relation.field_id));
    return Array.from(new Map(rawCandidateFields.map((field) => [field.id, field])).values());
  }, [allSupertagFields, fields, fieldsByTag, tagIds]);
  const tableFields = useMemo(
    () => [...candidateFields].sort(
      (left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name),
    ),
    [candidateFields],
  );
  const groupByFieldId = searchGroupBy(node);
  const groupableFields = useMemo(() => candidateFields.filter((field) => field.field_type !== "long_text"), [candidateFields]);
  const selectedGroupField = useMemo(
    () => groupableFields.find((field) => field.id === groupByFieldId),
    [groupByFieldId, groupableFields],
  );
  const groupField = useMemo(() => {
    const fallbackGroupField = candidateFields.find((field) => field.system_key === "task_status")
      ?? candidateFields.find((field) => field.field_type === "options" && /状態|status/i.test(field.name))
      ?? candidateFields.find((field) => field.field_type === "options");
    return selectedGroupField ?? fallbackGroupField;
  }, [candidateFields, groupByFieldId, groupableFields]);
  const dateField = useMemo(() => fields.find((field) => field.field_type === "date"), [fields]);
  if (tagIds.length === 0 && !node.query_json) return null;
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

  const createRow = async () => {
    if (!onCreateRow || creatingRow) return;
    setCreatingRow(true);
    try {
      const created = await onCreateRow();
      setQueryState((current) => ({
        ...current,
        nodes: mergeById(current.nodes, created),
        node_supertags: [
          ...current.node_supertags,
          ...tagIds
            .filter((tagId) => !current.node_supertags.some(
              (item) => item.node_id === created.id && item.supertag_id === tagId,
            ))
            .map((tagId) => ({ node_id: created.id, supertag_id: tagId })),
        ],
      }));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "行の作成に失敗しました");
    } finally {
      setCreatingRow(false);
    }
  };

  const updateResultField = async (item: DocsNode, field: DocsField, draft: string) => {
    if (virtualTaskId(item)) return;
    try {
      const data = await apiFetch<{ field_values: DocsFieldValue[] }>(
        `/api/docs/nodes/${item.id}/fields`,
        {
          method: "PUT",
          body: JSON.stringify({
            field_values: [
              { field_id: field.id, value: fieldDraftToPayload(field, draft) },
            ],
          }),
        },
      );
      setQueryState((current) => ({
        ...current,
        field_values: [
          ...current.field_values.filter(
            (value) => !(value.node_id === item.id && value.field_id === field.id),
          ),
          ...data.field_values,
        ],
      }));
      onFieldValuesChanged?.(item.id, field.id, data.field_values);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : `${field.name}の保存に失敗しました`);
    }
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

  const statusField = fields.find((field) => field.system_key === "task_status");
  const dueField = fields.find((field) => field.system_key === "task_due");
  const compactFieldValue = (item: DocsNode, field: DocsField | undefined) => {
    if (!field) return "";
    return fieldValueToDraft(valuesByNode.get(item.id)?.find((entry) => entry.field_id === field.id)).trim();
  };

  // zoom / document 双方で再利用するクエリ編集UIパーツ。
  const sortSelect = onSetSort ? (
    <AppSelect
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
    </AppSelect>
  ) : null;

  const queryFilterGrid = onSetQuery ? (
    <div className="grid gap-2 rounded-md border border-border bg-card p-3 text-[11px] md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)_minmax(0,1.6fr)]">
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
        <span className="shrink-0 text-muted-foreground">Group</span>
        <AppSelect
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
        </AppSelect>
      </label>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_84px_minmax(0,1fr)_auto] gap-1">
        <AppSelect
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
        </AppSelect>
        <AppSelect
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
        </AppSelect>
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
  ) : null;

  if (context === "document") {
    if (!documentExpanded) return null;
    return (
      <div className="my-1 space-y-1" style={{ paddingLeft: depth * 24 + 28 }}>
        <div
          data-testid="docs-search-node-controls"
          className="flex min-h-7 w-full max-w-3xl items-center justify-end gap-2 border-b border-border px-2 pb-1 text-xs"
        >
           <span className="shrink-0 rounded border border-border bg-card px-1.5 py-0.5 text-xs text-muted-foreground">
            {loading ? "..." : `${results.length}件`}
          </span>
          {onSetQuery ? (
            <button
              type="button"
              title="クエリを編集"
              aria-pressed={queryEditorOpen}
              className={cn(
                "shrink-0 rounded p-1 text-muted-foreground hover:bg-background hover:text-foreground",
                queryEditorOpen && "bg-background text-foreground",
              )}
              onClick={() => setQueryEditorOpen((current) => !current)}
            >
              <SlidersHorizontal className="size-3.5" />
            </button>
          ) : null}
        </div>
        {queryEditorOpen && (onSetSort || onSetQuery) ? (
          <div className="ml-5 flex max-w-3xl flex-col gap-2">
            {sortSelect ? <div className="flex items-center gap-1">{sortSelect}</div> : null}
            {queryFilterGrid}
          </div>
        ) : null}
        <div data-testid="docs-search-node-results" className="ml-5 max-w-3xl space-y-1">
           {loading ? <div className="rounded-md border border-dashed border-border p-3 text-sm text-muted-foreground">Loading query...</div> : null}
           {!loading && results.length === 0 ? <div className="rounded-md border border-dashed border-border p-3 text-sm text-muted-foreground">No matching nodes</div> : null}
          <VirtualizedSearchList
            items={results}
            estimateSize={28}
            renderItem={(item) => {
              const status = compactFieldValue(item, statusField);
              const due = compactFieldValue(item, dueField).slice(0, 10);
              return (
                <button
                  type="button"
                   className="grid min-h-8 w-full grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-1.5 rounded-md border border-border bg-card px-3 text-left text-sm transition-colors hover:border-primary/50 hover:bg-muted/50"
                  onClick={() => onOpenNode(item.id)}
                >
                  <span className="min-w-0 truncate">{displayTitleFor(item)}</span>
                   {status ? <span className="shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-xs text-muted-foreground">{status}</span> : null}
                   {due ? <span className="shrink-0 rounded border border-border bg-background px-1.5 py-0.5 text-xs text-muted-foreground">期日 {due}</span> : null}
                </button>
              );
            }}
          />
          {nextCursor ? (
            <Button type="button" variant="ghost" size="sm" className="h-7 text-xs" disabled={loading} onClick={() => void loadMore()}>
              Load more
            </Button>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div className="my-2 space-y-2" style={{ paddingLeft: depth * 24 + 28 }}>
      <div className="flex flex-wrap items-center gap-1 rounded-md border border-border bg-card p-2">
        {viewButtons.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.view}
              type="button"
              className={cn("flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] transition-colors hover:border-primary/50 hover:bg-muted/50", view === item.view && "border-primary/50 bg-primary/5 text-primary")}
              onClick={() => onSetView(item.view)}
              title={item.label}
            >
              <Icon className="size-3" />
              {item.label}
            </button>
          );
        })}
        {sortSelect}
        {view === "table" && onCreateRow ? (
          <Button type="button" variant="outline" size="sm" className="ml-auto h-7 text-xs" disabled={creatingRow} onClick={() => void createRow()}>
            <Plus className="size-3.5" />
            {creatingRow ? "追加中" : "行を追加"}
          </Button>
        ) : null}
      </div>
      {queryFilterGrid}
      {loading ? <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">Loading query...</div> : null}
      {!loading && results.length === 0 ? <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">No matching nodes</div> : null}
      {view === "list" ? (
        <VirtualizedSearchList
          items={results}
          estimateSize={34}
           renderItem={(item) => openButton(item, "flex min-h-9 w-full items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-left text-xs transition-colors hover:border-primary/50 hover:bg-muted/50")}
        />
      ) : null}
      {view === "table" ? (
        <div className="overflow-x-auto rounded-md border border-border bg-card text-xs">
          <div className="min-w-max">
            <div
              className="grid border-b border-border bg-muted/30 font-medium"
              style={{ gridTemplateColumns: `minmax(240px, 1.5fr) repeat(${tableFields.length}, minmax(160px, 1fr))` }}
            >
              <div className="px-2 py-2">Title</div>
              {tableFields.map((field) => (
                <div key={field.id} className="border-l px-2 py-2">{field.name}</div>
              ))}
            </div>
            <VirtualizedSearchList
              items={results}
              estimateSize={42}
              className="min-w-max"
              renderItem={(item) => (
                <div
                   className="grid border-b border-border last:border-b-0 hover:bg-muted/40"
                  style={{ gridTemplateColumns: `minmax(240px, 1.5fr) repeat(${tableFields.length}, minmax(160px, 1fr))` }}
                >
                  <div className="flex min-w-0 items-center gap-1 px-1 py-1">
                    <span className="min-w-0 flex-1">{titleEditor(item)}</span>
                    {noteButton(item)}
                    {!virtualTaskId(item) ? (
                      <button
                        type="button"
                        className="rounded px-1 text-[11px] text-muted-foreground hover:bg-accent"
                        onClick={() => onOpenNode(item.id)}
                      >
                        Open
                      </button>
                    ) : null}
                  </div>
                  {tableFields.map((field) => {
                    const value = valuesByNode.get(item.id)?.find((entry) => entry.field_id === field.id);
                    return (
                      <div key={field.id} className="min-w-0 border-l p-1">
                        {virtualTaskId(item) ? (
                          <div className="min-h-8 truncate px-2 py-1.5 text-muted-foreground">
                            {fieldValueToDraft(value)}
                          </div>
                        ) : (
                          <FieldControl
                            field={field}
                            value={fieldValueToDraft(value)}
                            nodes={resultNodes}
                            projects={projects ?? []}
                            currentNodeId={item.id}
                            onChange={() => undefined}
                            onCommit={(draft) => void updateResultField(item, field, draft)}
                          />
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            />
          </div>
        </div>
      ) : null}
      {view === "board" ? (
        <div className="grid gap-2 md:grid-cols-3">
          {Array.from(new Set(results.map(groupFor))).map((column) => (
               <div key={column} className="min-h-20 rounded-md border border-border bg-card p-2">
                <div className="mb-2 text-[11px] font-medium text-muted-foreground">{column}</div>
                <VirtualizedSearchList
                  items={results.filter((item) => groupFor(item) === column)}
                  estimateSize={34}
                   renderItem={(item) => openButton(item, "block w-full truncate rounded border border-border bg-background px-2 py-1 text-left text-xs hover:border-primary/50 hover:bg-muted/50")}
                />
              </div>
          ))}
        </div>
      ) : null}
      {view === "calendar" ? (
        <CalendarMonthGrid results={results} dateFor={dateFor} onOpenNode={onOpenNode} />
      ) : null}
      {view === "cards" ? (
        <VirtualizedSearchList
          items={results}
          estimateSize={90}
          className="grid gap-2 md:grid-cols-2"
          renderItem={(item) => {
            const taskId = virtualTaskId(item);
            if (taskId) {
              return (
                 <div className="min-h-20 rounded-md border border-border bg-card p-3 text-left text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate font-medium">{displayTitleFor(item)}</div>
                    {noteButton(item)}
                  </div>
                  <div className="mt-2 line-clamp-2 text-muted-foreground">{item.description || item.body_text}</div>
                </div>
              );
            }
            return (
               <button type="button" onClick={() => onOpenNode(item.id)} className="min-h-20 rounded-md border border-border bg-card p-3 text-left text-xs transition-colors hover:border-primary/50 hover:bg-muted/50">
                <div className="truncate font-medium">{displayTitleFor(item)}</div>
                <div className="mt-2 line-clamp-2 text-muted-foreground">{item.description || item.body_text}</div>
              </button>
            );
          }}
        />
      ) : null}
      {nextCursor ? (
        <Button type="button" variant="ghost" size="sm" className="mt-2 h-7 text-xs" disabled={loading} onClick={() => void loadMore()}>
          Load more
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Compact, data-backed query summary used by the Docs context rail.  The
 * summary intentionally exposes only values persisted on the search node;
 * it does not invent result statistics, AI suggestions, or export actions.
 */
export function SearchNodeMetadata({
  node,
  tags = [],
}: {
  node: DocsNode;
  tags?: DocsSupertag[];
}) {
  const query = node.query_json ? normalizeSearchQuery(node.query_json) : null;
  const text = searchTextFilter(node);
  const field = searchFieldFilter(node);
  const sort = searchSort(node);
  const view = searchView(node);
  const groupBy = searchGroupBy(node);
  const tagNames = searchTagIds(node)
    .map((tagId) => tags.find((tag) => tag.id === tagId)?.name ?? tagId)
    .filter(Boolean);
  const rows: Array<[string, string]> = [
    ["View", view],
    ["Sort", sort || "Default"],
    ["Group", groupBy || "Auto"],
  ];
  if (text) rows.unshift(["Text", text]);
  if (field.fieldId) rows.push([
    "Field",
    `${field.fieldId}${field.op ? ` ${field.op}` : ""}${field.value ? ` ${field.value}` : ""}`,
  ]);
  if (tagNames.length > 0) rows.push(["Tags", tagNames.map((name) => `#${name}`).join(", ")]);
  if (typeof query?.limit === "number") rows.push(["Limit", String(query.limit)]);
  return (
    <div className="space-y-4 p-4 text-xs" data-docs-query-metadata>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Query metadata</span>
        <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/5 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-primary">
          <span className="size-1.5 rounded-full bg-primary" /> Live
        </span>
      </div>
      <div className="space-y-1 rounded-md border border-border bg-card p-3">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Definition</div>
        <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{node.id}</div>
      </div>
      <dl className="divide-y divide-border rounded-md border border-border bg-card">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2 px-3 py-2">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0 break-words text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="border-t border-border pt-3 text-[11px] text-muted-foreground">
        Query results are loaded from the Docs query endpoint when this node is expanded.
      </div>
    </div>
  );
}

export function CalendarMonthGrid({
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
