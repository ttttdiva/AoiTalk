"use client";

import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { ExternalLink, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fieldValueToDraft } from "./docs-utils";
import { FieldControl } from "./field-control";
import type { DocsField, DocsFieldValue, DocsNode, DocsProject } from "./types";

function EditableTitle({
  node,
  onCommit,
}: {
  node: DocsNode;
  onCommit: (node: DocsNode, title: string) => void;
}) {
  const [draft, setDraft] = useState(node.title);
  const skipNextBlurCommitRef = useRef(false);

  const commit = () => {
    if (skipNextBlurCommitRef.current) {
      skipNextBlurCommitRef.current = false;
      return;
    }
    const next = draft.trim();
    if (!next) {
      setDraft(node.title);
      return;
    }
    if (next !== node.title) onCommit(node, next);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      skipNextBlurCommitRef.current = true;
      setDraft(node.title);
      event.currentTarget.blur();
    }
  };

  return (
    <Input
      aria-label={`Title ${node.title}`}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={onKeyDown}
      className="h-8 min-w-56 border-0 bg-transparent px-1 text-sm shadow-none focus-visible:ring-1"
    />
  );
}

export function DocsChildrenTable({
  rows,
  fieldsForRow,
  fieldValuesByKey,
  nodes,
  projects,
  onCommitTitle,
  onCommitField,
  onOpenNode,
  onAddRow,
  hasMoreRows = false,
  loadingMore = false,
  onLoadMoreRows,
}: {
  rows: DocsNode[];
  fieldsForRow: (node: DocsNode) => DocsField[];
  fieldValuesByKey: Map<string, DocsFieldValue>;
  nodes: DocsNode[];
  projects: DocsProject[];
  onCommitTitle: (node: DocsNode, title: string) => void;
  onCommitField: (node: DocsNode, field: DocsField, value: string) => void;
  onOpenNode: (nodeId: string) => void;
  onAddRow: () => void;
  hasMoreRows?: boolean;
  loadingMore?: boolean;
  onLoadMoreRows?: () => void;
}) {
  const fieldsByNodeId = useMemo(
    () => new Map(rows.map((row) => [row.id, fieldsForRow(row)])),
    [fieldsForRow, rows],
  );
  const columns = useMemo(
    () =>
      Array.from(
        new Map(
          rows
            .flatMap((row) => fieldsByNodeId.get(row.id) ?? [])
            .map((field) => [field.id, field]),
        ).values(),
      ).sort(
        (left, right) =>
          left.sort_order - right.sort_order ||
          left.name.localeCompare(right.name),
      ),
    [fieldsByNodeId, rows],
  );
  const gridTemplateColumns = `minmax(280px, 1.5fr) repeat(${columns.length}, minmax(170px, 1fr)) 42px`;

  return (
    <div
      data-testid="docs-children-table"
      className="rounded-md border bg-background text-xs"
    >
      <div className="flex items-center justify-between gap-3 border-b bg-muted/20 px-2 py-1.5">
        <span className="text-muted-foreground">{rows.length} rows</span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={onAddRow}
        >
          <Plus className="size-3.5" />
          行を追加
        </Button>
      </div>
      <div className="overflow-x-auto">
        <div className="min-w-max">
          <div
            className="grid border-b bg-muted/30 font-medium"
            style={{ gridTemplateColumns }}
          >
            <div className="px-2 py-2">Title</div>
            {columns.map((field) => (
              <div key={field.id} className="border-l px-2 py-2">
                {field.name}
              </div>
            ))}
            <div className="border-l" aria-hidden="true" />
          </div>
          {rows.length === 0 ? (
            <div className="px-3 py-6 text-center text-muted-foreground">
              行がありません
            </div>
          ) : (
            rows.map((row) => {
              const rowFieldIds = new Set(
                (fieldsByNodeId.get(row.id) ?? []).map((field) => field.id),
              );
              return (
                <div
                  key={row.id}
                  className="grid border-b last:border-b-0 hover:bg-accent/20"
                  style={{ gridTemplateColumns }}
                >
                  <div className="min-w-0 px-1 py-1">
                    <EditableTitle
                      key={`${row.id}:${row.title}`}
                      node={row}
                      onCommit={onCommitTitle}
                    />
                  </div>
                  {columns.map((field) => (
                    <div key={field.id} className="min-w-0 border-l p-1">
                      {rowFieldIds.has(field.id) ? (
                        <FieldControl
                          field={field}
                          value={fieldValueToDraft(
                            fieldValuesByKey.get(`${row.id}:${field.id}`),
                          )}
                          nodes={nodes}
                          projects={projects}
                          currentNodeId={row.id}
                          onChange={() => undefined}
                          onCommit={(value) => onCommitField(row, field, value)}
                        />
                      ) : (
                        <div className="h-8 px-2 py-1.5 text-muted-foreground">
                          —
                        </div>
                      )}
                    </div>
                  ))}
                  <div className="grid place-items-center border-l">
                    <button
                      type="button"
                      aria-label={`${row.title}を開く`}
                      title="ノードを開く"
                      className="grid size-7 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
                      onClick={() => onOpenNode(row.id)}
                    >
                      <ExternalLink className="size-3.5" />
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
      {hasMoreRows ? (
        <div className="border-t p-2 text-center">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loadingMore}
            onClick={onLoadMoreRows}
          >
            {loadingMore ? "読み込み中…" : "さらに読み込む"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
