"use client";

import { useEffect, useState } from "react";
import { Hash, Plus, Table2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { tagColorStyle } from "./docs-utils";
import type {
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsProject,
  DocsSavedView,
  DocsState,
  DocsSupertag,
} from "./types";
import {
  readConfigRecord,
  type DocsApiFetch,
  type SearchSort,
  type SearchView,
} from "./docs-workspace-shared";
import { SearchNodeResults } from "./docs-search-panels";

export function SupertagPage({
  apiFetch,
  tag,
  tags,
  views,
  nodes,
  nodeSupertags,
  fields,
  fieldValues,
  projects,
  fieldsByTag,
  allSupertagFields,
  onDocument,
  onOpenTag,
  onOpenNode,
  onCreateTaggedNode,
  onCreateTableRow,
  onCreateTable,
  onFieldValuesChanged,
  onCreateView,
  onUpdateView,
}: {
  apiFetch: DocsApiFetch;
  tag: DocsSupertag | null;
  tags: DocsSupertag[];
  views: DocsSavedView[];
  nodes: DocsNode[];
  nodeSupertags: DocsState["node_supertags"];
  fields: DocsField[];
  fieldValues: DocsFieldValue[];
  projects: DocsProject[];
  fieldsByTag: Map<string, DocsField[]>;
  allSupertagFields: DocsState["supertag_fields"];
  onDocument: () => void;
  onOpenTag: (tagId: string) => void;
  onOpenNode: (nodeId: string) => void;
  onCreateTaggedNode: (tag: DocsSupertag) => Promise<DocsNode>;
  onCreateTableRow: (tag: DocsSupertag) => Promise<DocsNode>;
  onCreateTable: (name: string) => Promise<DocsSupertag>;
  onFieldValuesChanged: (nodeId: string, fieldId: string, fieldValues: DocsFieldValue[]) => void;
  onCreateView: (tag: DocsSupertag, draft: Pick<DocsSavedView, "name" | "layout" | "config_json">) => Promise<DocsSavedView>;
  onUpdateView: (viewId: string, patch: Partial<Pick<DocsSavedView, "name" | "layout" | "config_json" | "sort_order">>) => Promise<DocsSavedView>;
}) {
  const savedViews = tag ? views.filter((view) => view.supertag_id === tag.id).sort((a, b) => a.sort_order - b.sort_order) : [];
  const defaultLayout = readConfigRecord(tag?.config_json).default_layout === "table" || tag?.base_type === "record"
    ? "table"
    : "list";
  const defaultLayouts: SearchView[] = defaultLayout === "table"
    ? ["table", "list", "board", "calendar"]
    : ["list", "board", "calendar", "table"];
  const defaultViews: DocsSavedView[] = tag
    ? defaultLayouts.map((layout, sortOrder) => ({
        id: `${tag.id}:${layout}`,
        workspace_id: tag.workspace_id,
        supertag_id: tag.id,
        name: layout === "list" ? tag.name : `${tag.name} ${layout}`,
        layout,
        config_json: {},
        sort_order: sortOrder,
        created_at: null,
        updated_at: null,
      }))
    : [];
  const viewList = savedViews.length > 0 ? savedViews : defaultViews;
  const [activeViewId, setActiveViewId] = useState<string | null>(null);
  const [showAddView, setShowAddView] = useState(false);
  const [newViewName, setNewViewName] = useState("");
  const [newViewLayout, setNewViewLayout] = useState<SearchView>("list");
  const [newViewQueryText, setNewViewQueryText] = useState("");
  const [showCreateTable, setShowCreateTable] = useState(false);
  const [newTableName, setNewTableName] = useState("");
  const [creatingTable, setCreatingTable] = useState(false);
  const [creatingTaggedNode, setCreatingTaggedNode] = useState(false);
  const activeView = viewList.find((view) => view.id === activeViewId) ?? viewList[0] ?? null;
  const activeViewIsSaved = !!activeView && !activeView.id.includes(":");

  const createTaggedNode = async () => {
    if (!tag || creatingTaggedNode) return;
    setCreatingTaggedNode(true);
    try {
      await onCreateTaggedNode(tag);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ノードの作成に失敗しました");
    } finally {
      setCreatingTaggedNode(false);
    }
  };

  useEffect(() => {
    setActiveViewId(null);
    setShowAddView(false);
  }, [tag?.id]);

  if (!tag) {
    const submitTable = async () => {
      const name = newTableName.trim();
      if (!name || creatingTable) return;
      setCreatingTable(true);
      try {
        await onCreateTable(name);
        setNewTableName("");
        setShowCreateTable(false);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "テーブルの作成に失敗しました");
      } finally {
        setCreatingTable(false);
      }
    };
    return (
      <section className="min-w-0 flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto w-full max-w-3xl">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h1 className="text-2xl font-semibold">Supertags</h1>
            <Button type="button" size="sm" onClick={() => setShowCreateTable((current) => !current)}>
              <Table2 className="size-4" />
              新規テーブル
            </Button>
          </div>
          {showCreateTable ? (
            <div className="mb-4 flex gap-2 rounded border bg-muted/20 p-3">
              <Input
                value={newTableName}
                onChange={(event) => setNewTableName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void submitTable();
                }}
                placeholder="例: 申請台帳"
                autoFocus
              />
              <Button type="button" onClick={() => void submitTable()} disabled={!newTableName.trim() || creatingTable}>
                {creatingTable ? "作成中" : "作成"}
              </Button>
            </div>
          ) : null}
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
    system_key: null,
    title: `List of ${tag.name}`,
    aliases: [],
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
              <button type="button" className="rounded border px-2 py-0.5 text-xs" style={tagColorStyle(tag.color)}>
                #{tag.name}
              </button>
            </div>
            {tag.description ? <div className="mt-1 text-sm text-muted-foreground">{tag.description}</div> : null}
          </div>
          <div className="flex items-center gap-1">
            <Button type="button" variant="ghost" size="sm" onClick={onDocument}>
              Document
            </Button>
            <Button type="button" size="sm" disabled={creatingTaggedNode} onClick={() => void createTaggedNode()}>
              <Plus className="size-4" />
              {creatingTaggedNode ? "Creating" : "Create new"}
            </Button>
          </div>
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
          apiFetch={apiFetch}
          node={searchNode}
          depth={0}
          nodes={nodes}
          nodeSupertags={nodeSupertags}
          tags={tags}
          fields={fields}
          fieldValues={fieldValues}
          projects={projects}
          fieldsByTag={fieldsByTag}
          allSupertagFields={allSupertagFields}
          context="zoom"
          onCreateRow={() => onCreateTableRow(tag)}
          onFieldValuesChanged={onFieldValuesChanged}
          onSetView={persistActiveViewLayout}
          onSetSort={activeViewIsSaved ? persistActiveViewSort : undefined}
          onSetQuery={activeViewIsSaved ? persistActiveViewQuery : undefined}
          onOpenNode={onOpenNode}
        />
      </div>
    </section>
  );
}
