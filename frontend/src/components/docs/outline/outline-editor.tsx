"use client";

import { useEffect, useMemo, useRef } from "react";
import { Prec, EditorState, RangeSetBuilder } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView, keymap, ViewPlugin, type ViewUpdate, WidgetType } from "@codemirror/view";
import { baseTextEditorExtensions } from "@/components/editor/code-mirror-shared";
import type { DocsNode, DocsSupertag } from "@/components/docs/types";
import { cn } from "@/lib/utils";
import {
  reconcileOutlineText,
  type OutlineOperation,
  type SerializedOutline,
} from "./outline-doc";

export type OutlineEditorRow = {
  node: DocsNode;
  depth: number;
  checked: boolean;
  tags: DocsSupertag[];
};

function rowsToSerializedOutline(rows: OutlineEditorRow[]): SerializedOutline {
  const lineMap = rows.map((row) => ({
    nodeId: row.node.id,
    depth: row.depth,
    text: row.node.title,
  }));
  return {
    text: lineMap.map((line) => `${"\t".repeat(line.depth)}${line.text}`).join("\n"),
    lineMap,
  };
}

function lineNodeId(view: EditorView, before: SerializedOutline) {
  const lineNo = view.state.doc.lineAt(view.state.selection.main.head).number;
  return before.lineMap[lineNo - 1]?.nodeId ?? null;
}

class InlineTokenWidget extends WidgetType {
  constructor(
    private readonly label: string,
    private readonly kind: "tag" | "ref" | "bold" | "italic" | "mark" | "code",
  ) {
    super();
  }

  eq(other: InlineTokenWidget) {
    return other.label === this.label && other.kind === this.kind;
  }

  toDOM() {
    const element = document.createElement(this.kind === "bold" ? "strong" : this.kind === "italic" ? "em" : "span");
    element.textContent = this.kind === "tag" ? `#${this.label}` : this.label;
    if (this.kind === "tag") {
      element.className = "mx-0.5 inline-flex rounded border px-1.5 py-0.5 text-[0.85em] leading-4";
    } else if (this.kind === "ref") {
      element.className = "mx-0.5 inline-flex rounded bg-primary/15 px-1.5 py-0.5 text-[0.85em] leading-4 text-primary";
    } else if (this.kind === "mark") {
      element.className = "rounded bg-yellow-400/20 px-0.5";
    } else if (this.kind === "code") {
      element.className = "rounded bg-muted px-1 py-0.5 text-[0.9em]";
    }
    return element;
  }

  ignoreEvent() {
    return false;
  }
}

function inlineTokenDecorations(view: EditorView) {
  const builder = new RangeSetBuilder<Decoration>();
  const pattern = /(\[\[node:([0-9a-f-]{36})\|([^\]\n]+)\]\]|#([\p{L}\p{N}_-]+)|\*\*([^*\n]+)\*\*|_([^_\n]+)_|==([^=\n]+)==|`([^`\n]+)`)/giu;
  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    for (const match of text.matchAll(pattern)) {
      const index = match.index ?? 0;
      const start = from + index;
      const end = start + (match[0]?.length ?? 0);
      const widget = match[3]
        ? new InlineTokenWidget(match[3], "ref")
        : match[4]
          ? new InlineTokenWidget(match[4], "tag")
          : match[5]
            ? new InlineTokenWidget(match[5], "bold")
            : match[6]
              ? new InlineTokenWidget(match[6], "italic")
              : match[7]
                ? new InlineTokenWidget(match[7], "mark")
                : match[8]
                  ? new InlineTokenWidget(match[8], "code")
                  : null;
      if (widget) builder.add(start, end, Decoration.replace({ widget }));
    }
  }
  return builder.finish();
}

const inlineTokenPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = inlineTokenDecorations(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged || update.selectionSet) {
        this.decorations = inlineTokenDecorations(update.view);
      }
    }
  },
  {
    decorations: (value) => value.decorations,
  },
);

type OutlineDocumentEditorProps = {
  rows: OutlineEditorRow[];
  selectedNodeIds: Set<string>;
  requestFocusNodeId: string | null;
  className?: string;
  onSelectNode: (nodeId: string) => void;
  onOpenNode: (nodeId: string) => void;
  onToggleCheckbox: (nodeId: string) => void;
  onApplyOperations: (operations: OutlineOperation[]) => Promise<void>;
  onFocused: (nodeId: string | null) => void;
};

export function OutlineDocumentEditor({
  rows,
  selectedNodeIds,
  requestFocusNodeId,
  className,
  onSelectNode,
  onOpenNode,
  onToggleCheckbox,
  onApplyOperations,
  onFocused,
}: OutlineDocumentEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const beforeRef = useRef<SerializedOutline>({ text: "", lineMap: [] });
  const applyingRef = useRef(false);
  const callbacksRef = useRef({
    onSelectNode,
    onOpenNode,
    onToggleCheckbox,
    onApplyOperations,
    onFocused,
  });
  const serialized = useMemo(() => rowsToSerializedOutline(rows), [rows]);
  const initialTextRef = useRef(serialized.text);
  const selectedKey = useMemo(() => Array.from(selectedNodeIds).sort().join(","), [selectedNodeIds]);

  useEffect(() => {
    callbacksRef.current = {
      onSelectNode,
      onOpenNode,
      onToggleCheckbox,
      onApplyOperations,
      onFocused,
    };
  }, [onApplyOperations, onFocused, onOpenNode, onSelectNode, onToggleCheckbox]);

  useEffect(() => {
    beforeRef.current = serialized;
    const view = viewRef.current;
    if (!view || applyingRef.current) return;
    const currentText = view.state.doc.toString();
    if (currentText !== serialized.text) {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: serialized.text },
      });
    }
  }, [serialized.text, serialized]);

  useEffect(() => {
    if (!requestFocusNodeId || !viewRef.current) return;
    const index = beforeRef.current.lineMap.findIndex((line) => line.nodeId === requestFocusNodeId);
    if (index < 0) return;
    const line = viewRef.current.state.doc.line(index + 1);
    viewRef.current.focus();
    viewRef.current.dispatch({ selection: { anchor: line.from, head: line.to } });
  }, [requestFocusNodeId]);

  useEffect(() => {
    if (!hostRef.current || viewRef.current) return;

    const commit = async (view: EditorView) => {
      if (applyingRef.current) return true;
      const before = beforeRef.current;
      const afterText = view.state.doc.toString();
      const operations = reconcileOutlineText({ before, afterText });
      if (operations.length === 0) return true;
      applyingRef.current = true;
      try {
        await callbacksRef.current.onApplyOperations(operations);
      } finally {
        applyingRef.current = false;
      }
      return true;
    };

    const theme = EditorView.theme({
      "&": {
        background: "transparent",
        border: "0",
        fontSize: "14px",
      },
      ".cm-scroller": {
        fontFamily: "inherit",
        lineHeight: "1.65",
        overflow: "visible",
      },
      ".cm-content": {
        padding: "0",
      },
      ".cm-line": {
        padding: "1px 4px",
      },
      ".cm-focused": {
        outline: "none",
      },
      ".cm-selectionBackground": {
        backgroundColor: "color-mix(in oklab, var(--primary) 22%, transparent) !important",
      },
    });

    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: initialTextRef.current,
        extensions: [
          ...baseTextEditorExtensions({ language: "markdown", includeSearch: true }),
          theme,
          inlineTokenPlugin,
          Prec.highest(
            keymap.of([
              {
                key: "Mod-s",
                run(view) {
                  void commit(view);
                  return true;
                },
              },
              {
                key: "Mod-Enter",
                run(view) {
                  const nodeId = lineNodeId(view, beforeRef.current);
                  if (nodeId) callbacksRef.current.onToggleCheckbox(nodeId);
                  return true;
                },
              },
              {
                key: "Enter",
                run() {
                  return false;
                },
              },
            ]),
          ),
          EditorView.domEventHandlers({
            blur(_event, view) {
              void commit(view);
              return false;
            },
            dblclick(_event, view) {
              const nodeId = lineNodeId(view, beforeRef.current);
              if (nodeId) callbacksRef.current.onOpenNode(nodeId);
              return false;
            },
          }),
          EditorView.updateListener.of((update) => {
            if (!update.selectionSet && !update.docChanged) return;
            const nodeId = lineNodeId(update.view, beforeRef.current);
            if (nodeId) callbacksRef.current.onSelectNode(nodeId);
            callbacksRef.current.onFocused(nodeId);
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || !selectedKey) return;
    const first = beforeRef.current.lineMap.find((line) => line.nodeId && selectedNodeIds.has(line.nodeId));
    if (!first?.nodeId) return;
    const index = beforeRef.current.lineMap.findIndex((line) => line.nodeId === first.nodeId);
    if (index < 0) return;
    const line = view.state.doc.line(index + 1);
    view.dispatch({ selection: { anchor: line.from, head: line.to } });
  }, [selectedKey, selectedNodeIds]);

  return (
    <div className={cn("docs-outline-editor min-w-0", className)}>
      <div ref={hostRef} data-docs-outline-editor="single" />
    </div>
  );
}
