"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  CheckSquare,
  ChevronRight,
  ExternalLink,
  Link2,
  Sparkles,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { FieldControl } from "./field-control";
import { DocsSupertagChip } from "./docs-supertag-chip";
import type {
  DocsAiSuggestion,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsProject,
  DocsReference,
  DocsSupertag,
  ReferencesState,
} from "./types";
import {
  docsFieldType,
  fieldValueToDraft,
  formatFieldSummaryValue,
  tagColorStyle,
} from "./docs-utils";
import { nodeText, type DocsTaskBinding } from "./docs-workspace-shared";

export function TaskBindingButton({ task, onOpenTask }: { task: DocsTaskBinding | null; onOpenTask: (taskId: string) => void }) {
  if (!task) return null;

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="h-6 gap-1 px-2 text-xs"
      title={task.title}
      onClick={() => onOpenTask(task.id)}
    >
      <CheckSquare className="size-3" />
      タスクタブで開く
      <ExternalLink className="size-3" />
    </Button>
  );
}

export function PageTitleEditor({
  node,
  tags = [],
  requestFocus,
  onFocused,
  onChangeTitle,
  onCommitTitle,
  onRemoveTag = () => {},
  onOpenTag = () => {},
  onNavigateDown = () => {},
}: {
  node: DocsNode;
  tags?: DocsSupertag[];
  requestFocus: boolean;
  onFocused: () => void;
  onChangeTitle: (title: string) => void;
  onCommitTitle: (title: string) => void;
  onRemoveTag?: (tag: DocsSupertag) => void;
  onOpenTag?: (tag: DocsSupertag) => void;
  onNavigateDown?: () => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const committedOnEnterRef = useRef(false);
  const renderedNodeIdRef = useRef(node.id);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    const nodeChanged = renderedNodeIdRef.current !== node.id;
    if (nodeChanged || document.activeElement !== input) input.value = node.title;
    renderedNodeIdRef.current = node.id;
    if (requestFocus) {
      input.focus();
      input.select();
    }
  }, [node.id, node.title, requestFocus]);

  return (
    <div className="flex min-w-0 items-center gap-1.5" data-docs-page-node-line={node.id}>
      <input
        ref={inputRef}
        data-docs-page-title
        data-docs-node-id={node.id}
        aria-label="ページタイトル"
        defaultValue={node.title}
        placeholder="Untitled"
        className="h-9 min-w-[12ch] flex-1 border-0 bg-transparent p-0 text-2xl font-semibold outline-none hover:bg-muted/30 focus:bg-muted/30 focus:ring-1 focus:ring-ring/30"
        onFocus={onFocused}
        onChange={(event) => onChangeTitle(event.target.value)}
        onBlur={(event) => {
          if (committedOnEnterRef.current) {
            committedOnEnterRef.current = false;
            return;
          }
          onCommitTitle(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Enter") {
            event.preventDefault();
            committedOnEnterRef.current = true;
            onCommitTitle(event.currentTarget.value);
            onNavigateDown();
            return;
          }
          if (event.key === "ArrowDown") {
            event.preventDefault();
            onNavigateDown();
            return;
          }
          if (
            event.key === "ArrowRight" &&
            !event.shiftKey &&
            !event.ctrlKey &&
            !event.metaKey &&
            event.currentTarget.selectionStart === event.currentTarget.value.length &&
            event.currentTarget.selectionEnd === event.currentTarget.value.length &&
            tags.length > 0
          ) {
            event.preventDefault();
            event.currentTarget.parentElement?.querySelector<HTMLButtonElement>("[data-docs-supertag-chip]")?.focus();
          }
        }}
      />
      {tags.map((tag, index) => (
        <DocsSupertagChip
          key={tag.id}
          tag={tag}
          onOpen={() => onOpenTag(tag)}
          onRemove={() => {
            onRemoveTag(tag);
            inputRef.current?.focus();
          }}
          onNavigate={(direction) => {
            if (direction === "text" || direction === "previous" && index === 0) {
              inputRef.current?.focus();
              inputRef.current?.setSelectionRange(inputRef.current.value.length, inputRef.current.value.length);
              return;
            }
            const chips = Array.from(inputRef.current?.parentElement?.querySelectorAll<HTMLButtonElement>("[data-docs-supertag-chip]") ?? []);
            const target = direction === "previous" ? chips[index - 1] : chips[index + 1];
            if (target) target.focus();
            else onNavigateDown();
          }}
        />
      ))}
    </div>
  );
}

export function FieldRows({
  node,
  fields,
  tags,
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
  tags: DocsSupertag[];
  values: Map<string, DocsFieldValue>;
  nodes: DocsNode[];
  projects: DocsProject[];
  suggestions: DocsAiSuggestion[];
  onSuggestionStatus: (suggestionId: string, status: "accepted" | "rejected" | "stale") => Promise<void>;
  onRunAi: () => void;
  onSave: (node: DocsNode, field: DocsField, value: string) => Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const storageKey = `aoitalk:docs:fields-expanded:${node.id}`;
  const subscribeToFieldExpansion = useCallback((onStoreChange: () => void) => {
    window.addEventListener("aoitalk:docs:fields-expanded", onStoreChange);
    window.addEventListener("storage", onStoreChange);
    return () => {
      window.removeEventListener("aoitalk:docs:fields-expanded", onStoreChange);
      window.removeEventListener("storage", onStoreChange);
    };
  }, []);
  const expanded = useSyncExternalStore(
    subscribeToFieldExpansion,
    () => window.localStorage.getItem(storageKey) === "true",
    () => false,
  );
  if (fields.length === 0) return null;
  const toggleExpanded = () => {
    window.localStorage.setItem(storageKey, String(!expanded));
    window.dispatchEvent(new Event("aoitalk:docs:fields-expanded"));
  };
  const previewItems = fields.flatMap((field) => {
    const key = `${node.id}:${field.id}`;
    const display = formatFieldSummaryValue(field, values.get(key), nodes, projects);
    return display ? [{ field, display }] : [];
  }).slice(0, 3);
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
    <div className="mx-auto mb-4 w-full max-w-3xl space-y-1">
      <div className="flex min-h-9 flex-wrap items-center gap-2 rounded-md border bg-muted/20 px-2 py-1">
        <button
          type="button"
          className="flex min-w-0 flex-1 basis-64 items-center gap-2 text-left text-xs"
          aria-expanded={expanded}
          onClick={toggleExpanded}
        >
          <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-90")} />
          <span className="shrink-0 font-medium text-foreground">{fields.length} fields</span>
          {!expanded ? (
            <span className="flex min-w-0 flex-wrap items-center gap-1">
              {previewItems.map(({ field, display }) => {
                const tag = tags.find((item) => item.id === field.supertag_id);
                const fieldType = docsFieldType(field);
                const valueClass =
                  field.system_key?.includes("status")
                    ? "text-emerald-600 dark:text-emerald-300"
                    : fieldType === "date" || fieldType === "number" || fieldType === "url" || fieldType === "email"
                      ? "text-primary"
                      : "text-foreground";
                return (
                  <span key={field.id} className="inline-flex max-w-48 gap-1 truncate rounded-full border px-2 py-0.5" style={{ borderColor: tag?.color ?? undefined }}>
                    <span style={tagColorStyle(tag?.color ?? null)}>{field.name}:</span>
                    <span className={cn("truncate", valueClass)}>{display}</span>
                  </span>
                );
              })}
            </span>
          ) : null}
        </button>
        <Button type="button" variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs" onClick={onRunAi}>
          <Sparkles className="size-3.5" />
          AIで埋める
        </Button>
      </div>
      {expanded ? fields.map((field) => {
        const key = `${node.id}:${field.id}`;
        const value = drafts[key] ?? fieldValueToDraft(values.get(key));
        const suggestion = fieldSuggestions.find((item) => item.field.id === field.id);
        const tag = tags.find((item) => item.id === field.supertag_id);
        return (
          <div key={field.id} className="flex min-h-7 flex-wrap items-start gap-x-2 gap-y-1 py-0.5 text-sm">
            <div className="flex w-36 min-w-0 shrink-0 items-center gap-1.5 truncate pt-1 text-xs text-muted-foreground">
              <span
                className="size-1.5 shrink-0 rounded-full border"
                style={{ ...tagColorStyle(tag?.color ?? null), backgroundColor: tag?.color ?? "currentColor" }}
              />
              <span className="truncate">{field.name}:</span>
            </div>
            <div className="min-w-40 flex-1 space-y-1">
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
      }) : null}
    </div>
  );
}

export function ZoomReferences({ references, loading, onOpenNode }: { references: ReferencesState; loading: boolean; onOpenNode: (nodeId: string) => void }) {
  const fieldGroups = new Map<string, DocsReference[]>();
  for (const item of references.field_refs) {
    const fieldName = item.field_name || "field";
    const items = fieldGroups.get(fieldName) ?? [];
    items.push(item);
    fieldGroups.set(fieldName, items);
  }
  // Linked mentions は backlinks と referenced_in を node.id で重複除去して合成する(mentioned_in は使用しない)。
  const linkedMentions: DocsReference[] = [];
  const seenLinkedNodeIds = new Set<string>();
  for (const item of [...references.backlinks, ...references.referenced_in]) {
    if (seenLinkedNodeIds.has(item.node.id)) continue;
    seenLinkedNodeIds.add(item.node.id);
    linkedMentions.push(item);
  }
  const hasAny = linkedMentions.length > 0 || references.outgoing.length > 0 || references.field_refs.length > 0;

  return (
    <section className="mt-8 border-t pt-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Link2 className="size-4 text-muted-foreground" />
        References
      </div>
      {loading ? <div className="mb-3 text-xs text-muted-foreground">Loading...</div> : null}
      {!loading && !hasAny ? <div className="rounded border border-dashed p-3 text-xs text-muted-foreground">No references</div> : null}
      {!loading && hasAny ? (
        <div className="grid gap-3 lg:grid-cols-3">
          {linkedMentions.length > 0 ? (
            <ReferenceSection title="Linked mentions" items={linkedMentions} onOpenNode={onOpenNode} />
          ) : null}
          {references.outgoing.length > 0 ? (
            <ReferenceSection title="Outgoing links" items={references.outgoing} onOpenNode={onOpenNode} />
          ) : null}
          {fieldGroups.size > 0 ? (
            <div className="space-y-3">
              {Array.from(fieldGroups.entries()).map(([fieldName, items]) => (
                <ReferenceSection key={fieldName} title={`Appears as ${fieldName} in`} items={items} onOpenNode={onOpenNode} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export function ReferenceSection({ title, items, onOpenNode }: { title: string; items: DocsReference[]; onOpenNode: (nodeId: string) => void }) {
  return (
    <div>
      <h3 className="mb-1 text-xs font-medium text-muted-foreground">{title}</h3>
      {items.length === 0 ? <div className="rounded border border-dashed p-2 text-xs text-muted-foreground">None</div> : null}
      <div className="space-y-1">
        {items.map((item) => (
          <button key={`${item.kind}:${item.field_name ?? ""}:${item.node.id}:${item.snippet}`} type="button" onClick={() => onOpenNode(item.node.id)} className="block w-full min-w-0 rounded border px-2 py-1.5 text-left text-xs hover:bg-accent [overflow-wrap:anywhere]">
            <div className="whitespace-normal break-words font-medium">{nodeText(item.node)}</div>
            <div className="whitespace-normal break-words text-muted-foreground">{item.field_name ? `${item.field_name}: ` : ""}{item.snippet}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
