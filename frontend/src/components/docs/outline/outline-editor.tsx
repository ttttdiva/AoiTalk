"use client";

import {
  Fragment,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { createPortal, flushSync } from "react-dom";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { Compartment, EditorSelection, EditorState, Prec, RangeSetBuilder } from "@codemirror/state";
import { Decoration, drawSelection, dropCursor, EditorView, keymap, ViewPlugin, WidgetType, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { Check, CheckSquare, ChevronDown, ChevronRight, Copy, ExternalLink, FileText, GripVertical, ListFilter, LoaderCircle, MoreVertical, MoveRight, Plus, Tag, Trash2 } from "lucide-react";
import Image from "next/image";
import { toast } from "sonner";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { observeElementRect as observeVirtualElementRect, useVirtualizer } from "@tanstack/react-virtual";
import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import {
  blockJsonForKind,
  docsBlockKind,
  markdownShortcutPatchForTitle,
  markdownShortcutPrefixForTitle,
  parseIndentedMarkdownBlocks,
  serializeBlocksToIndentedMarkdown,
  splitBlockTitle,
  type DocsBlockKind,
  type MarkdownBlock,
} from "@/lib/docs-block-model";
import { renderDocsInlineHtml, docsRowBlockClass } from "@/lib/docs-block-render";
import { selectNextOccurrenceKeymap } from "@/components/editor/code-mirror-shared";
import { cn } from "@/lib/utils";
import type { DocsAiSuggestion, DocsAttachment, DocsField, DocsFieldValue, DocsNode, DocsProject, DocsSupertag } from "@/components/docs/types";
import { FieldControl } from "@/components/docs/field-control";
import { DocsSupertagChip } from "@/components/docs/docs-supertag-chip";
import { fieldValueToDraft } from "@/components/docs/docs-utils";
import type { Task } from "@/lib/task-api";

export type OutlineEditorRow = {
  node: DocsNode;
  depth: number;
  checked: boolean;
  tags: DocsSupertag[];
  fields?: DocsField[];
  fieldValues?: DocsFieldValue[];
  attachments?: DocsAttachment[];
  taskBinding?: DocsTaskBinding | null;
};

export type DocsTaskBinding = {
  id: string;
  project_id: string | null;
  knowledge_node_id: string | null;
  title: string;
  status: string | null;
};

export type BlockCreateInput = {
  parentId: string | null;
  afterNodeId: string | null;
  title: string;
  kind?: DocsBlockKind;
  checked?: boolean;
};

export type BlockMoveInput = {
  nodeId: string;
  parentId: string | null;
  afterNodeId: string | null;
};

type UrlChoice = {
  nodeId: string;
  url: string;
  from: number;
  to: number;
  x: number;
  y: number;
};

type InlineSuggestion = {
  kind: "tag" | "ref" | "user" | "mention" | "field" | "task";
  nodeId: string;
  query: string;
  from: number;
  to: number;
  x: number;
  y: number;
};

export type DocsMentionUser = {
  id: string;
  username: string;
  display_name: string | null;
};

type SearchReplaceState = {
  find: string;
  replace: string;
  scope: "page" | "workspace";
};

type SearchPanelMode = "find" | "replace";

type SearchHit = {
  nodeId: string;
  node: DocsNode;
  occurrence: number;
};

export type OutlineDropIntent = "before" | "inside" | "after";

export function outlineDropIntentFromPointer(event: Pick<ReactDragEvent<HTMLElement>, "clientY" | "currentTarget">): OutlineDropIntent {
  const bounds = event.currentTarget.getBoundingClientRect();
  const ratio = bounds.height > 0 ? (event.clientY - bounds.top) / bounds.height : 0.5;
  if (ratio < 0.25) return "before";
  if (ratio > 0.75) return "after";
  return "inside";
}

export function outlineDropMove(
  rows: OutlineEditorRow[],
  draggedNodeId: string,
  targetNodeId: string,
  intent: OutlineDropIntent,
): BlockMoveInput | null {
  if (draggedNodeId === targetNodeId) return null;
  const draggedIndex = rows.findIndex((row) => row.node.id === draggedNodeId);
  const targetIndex = rows.findIndex((row) => row.node.id === targetNodeId);
  if (draggedIndex < 0 || targetIndex < 0) return null;
  const dragged = rows[draggedIndex];
  const target = rows[targetIndex];
  if (!dragged || !target) return null;
  for (let cursor = draggedIndex + 1; cursor < rows.length && (rows[cursor]?.depth ?? -1) > dragged.depth; cursor += 1) {
    if (rows[cursor]?.node.id === targetNodeId) return null;
  }
  if (intent === "inside") return { nodeId: draggedNodeId, parentId: targetNodeId, afterNodeId: null };
  const siblingIds = rows
    .filter((row) => row.depth === target.depth && row.node.parent_id === target.node.parent_id && row.node.id !== draggedNodeId)
    .map((row) => row.node.id);
  const targetSiblingIndex = siblingIds.indexOf(targetNodeId);
  if (targetSiblingIndex < 0) return null;
  const afterNodeId = intent === "after" ? targetNodeId : siblingIds[targetSiblingIndex - 1] ?? null;
  return { nodeId: draggedNodeId, parentId: target.node.parent_id, afterNodeId };
}

type FieldCommandState = {
  nodeId: string;
  fieldName: string;
  prefix: string;
};

type MovePageCandidate = {
  id: string;
  title: string;
  node_type: string;
  breadcrumb: string[];
};

type MoveDialogState = {
  row: OutlineEditorRow;
  query: string;
  items: MovePageCandidate[];
  activeIndex: number;
  loading: boolean;
  error: string | null;
};

type OutlineRowMemoEntry = {
  inputs: readonly unknown[];
  element: ReactNode;
};

// OutlineBlockEditor へ渡す安定コールバック群。DocsWorkspace 側の 1 箇所で束ねて Context 供給し、
// 深い prop-drilling を避ける。頻繁に変わる状態値・render 中クロージャ（rows / selectedNodeIds /
// emptyParentId / onLoadMoreRows / renderBelowRow など）や、シェブロン表示判定などの query 系
// predicate は再レンダ特性を保つため Context に含めず props のまま残す。
// optional 指定は移設前の props と同一に保つ。一部の実装は `if (onXxx)` で存在チェックし
// メニュー項目の出し分けをしているため、未指定＝undefined を維持し挙動不変を守る。
export type DocsEditorContextValue = {
  onSelectNode: (nodeId: string) => void;
  onOpenNode: (nodeId: string) => void;
  onOpenTask?: (taskId: string) => void;
  onFocused: (nodeId: string | null) => void;
  onCommitPending?: (operation: Promise<boolean> | null) => void;
  onCommitTitle: (node: DocsNode, title: string, patch?: Partial<Pick<DocsNode, "body_json" | "node_type" | "display_props" | "description">>) => Promise<void> | void;
  onDraftChange?: (node: DocsNode, title: string) => void;
  onCommitSuccess?: (nodeId: string, committedDraft: string) => void;
  onCreateNode: (input: BlockCreateInput) => DocsNode | Promise<DocsNode>;
  onArchiveNode: (node: DocsNode) => Promise<void> | void;
  onMoveNode: (input: BlockMoveInput) => Promise<void> | void;
  onToggleCheckbox: (node: DocsNode) => Promise<void> | void;
  onToggleCollapsed: (nodeId: string) => void;
  onDuplicateNode: (node: DocsNode) => Promise<void> | void;
  onApplyTag: (node: DocsNode, tag: DocsSupertag) => Promise<void> | void;
  onRemoveTag?: (node: DocsNode, tag: DocsSupertag) => Promise<void> | void;
  onOpenTag?: (tag: DocsSupertag) => void;
  onSaveField: (node: DocsNode, field: DocsField, value: string) => Promise<void> | void;
  onDeleteAttachment?: (attachment: DocsAttachment) => Promise<void> | void;
  onMoveToPage?: (node: DocsNode, page: MovePageCandidate) => Promise<void> | void;
  onReplaceTitles: (updates: Array<{ node: DocsNode; title: string }>) => Promise<void> | void;
  // タイトルが `フィールド名:: 値` 記法にマッチした時に呼ぶ。true を返すと通常のタイトルコミットをスキップする。
  onFieldShorthand?: (row: OutlineEditorRow, fieldName: string, rawValue: string) => Promise<boolean> | boolean;
  // スラッシュコマンド「エイリアス」で呼ぶ。行ノードのエイリアス編集を親側で開く。
  onOpenAliasEditor?: (row: OutlineEditorRow) => void;
  // スラッシュコマンド「Search node」で呼ぶ。行ノードを起点に検索ノードを親側で作成する。
  onCreateSearchNode?: (row: OutlineEditorRow) => void;
  onSuggestFields?: (row: OutlineEditorRow) => void;
  onCreateFieldCandidate?: (row: OutlineEditorRow, name: string) => Promise<boolean> | boolean;
  onSuggestionStatus?: (suggestionId: string, status: "accepted" | "rejected" | "stale") => Promise<void> | void;
};

const DocsEditorContext = createContext<DocsEditorContextValue | null>(null);

export function DocsEditorProvider({ value, children }: { value: DocsEditorContextValue; children: ReactNode }) {
  return <DocsEditorContext.Provider value={value}>{children}</DocsEditorContext.Provider>;
}

function useDocsEditorContext(): DocsEditorContextValue {
  const context = useContext(DocsEditorContext);
  if (!context) {
    throw new Error("OutlineBlockEditor は DocsEditorProvider の内側で描画する必要があります");
  }
  return context;
}

// テスト用途で Context 値を組み立てるヘルパ。必須ハンドラのみ no-op 既定を埋め、
// optional ハンドラは overrides で明示された時だけ設定して存在チェックの挙動を保つ。
export function createDocsEditorContextValue(
  overrides: Partial<DocsEditorContextValue> = {},
): DocsEditorContextValue {
  return {
    onSelectNode: () => {},
    onOpenNode: () => {},
    onFocused: () => {},
    onCommitTitle: () => {},
    onCreateNode: () => {
      throw new Error("onCreateNode が指定されていません");
    },
    onArchiveNode: () => {},
    onMoveNode: () => {},
    onToggleCheckbox: () => {},
    onToggleCollapsed: () => {},
    onDuplicateNode: () => {},
    onApplyTag: () => {},
    onSaveField: () => {},
    onReplaceTitles: () => {},
    ...overrides,
  };
}

type OutlineBlockEditorProps = {
  rows: OutlineEditorRow[];
  documentRow?: OutlineEditorRow | null;
  selectedNodeIds: Set<string>;
  requestFocusNodeId: string | null;
  nodes: DocsNode[];
  projects: DocsProject[];
  supertags: DocsSupertag[];
  users?: DocsMentionUser[];
  suggestions?: DocsAiSuggestion[];
  className?: string;
  emptyParentId?: string | null;
  hasMoreRows?: boolean;
  onLoadMoreRows?: () => Promise<unknown> | void;
  onNavigateToDocumentTitle?: () => void;
  isCollapsed: (nodeId: string) => boolean;
  // 折りたたみ中で子が rows に無いノードのシェブロン表示判定に使う（未指定時は collapsed 判定のみ）
  nodeHasChildren?: (nodeId: string) => boolean;
  isNodeLoading?: (nodeId: string) => boolean;
  // 各行の直下に任意の内容を描画する拡張点（未指定なら従来通り）
  renderBelowRow?: (row: OutlineEditorRow, index: number) => ReactNode;
  fieldCandidatesForRow?: (row: OutlineEditorRow) => DocsField[];
};

type SlashCommandId =
  | "checkbox"
  | "field"
  | "field_ai"
  | "alias"
  | "search_node"
  | "move";

type SlashCommandDef = { id: SlashCommandId; label: string; keywords: string[] };

type SlashCommandState = {
  nodeId: string;
  query: string;
  from: number;
  to: number;
  x: number;
  y: number;
};

const SLASH_COMMANDS: SlashCommandDef[] = [
  { id: "checkbox", label: "/checkbox — チェックボックス", keywords: ["checkbox", "todo", "check", "チェック"] },
  { id: "field", label: "/field — フィールド", keywords: ["field", "フィールド", "属性"] },
  { id: "field_ai", label: "/field ai — AIでField候補", keywords: ["field ai", "ai field", "AI候補"] },
  { id: "alias", label: "/alias — エイリアス", keywords: ["alias", "エイリアス", "別名"] },
  { id: "search_node", label: "/search — Search node", keywords: ["search", "query", "検索", "サーチ"] },
  { id: "move", label: "/move — 別ページへ移動", keywords: ["move", "移動", "別ページ"] },
];

function filterSlashCommands(query: string): SlashCommandDef[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter(
    (command) =>
      command.label.toLowerCase().includes(normalized) ||
      command.keywords.some((keyword) => keyword.toLowerCase().includes(normalized)),
  );
}

const SLASH_BLOCK_KIND: Partial<Record<SlashCommandId, DocsBlockKind>> = {
  checkbox: "checkbox",
};

function hasChildren(row: OutlineEditorRow, rows: OutlineEditorRow[], index: number) {
  return (rows[index + 1]?.depth ?? -1) > row.depth;
}

function previousVisibleNode(rows: OutlineEditorRow[], nodeId: string) {
  const index = rows.findIndex((row) => row.node.id === nodeId);
  return index > 0 ? rows[index - 1] : null;
}

function nextVisibleNode(rows: OutlineEditorRow[], nodeId: string) {
  const index = rows.findIndex((row) => row.node.id === nodeId);
  return index >= 0 ? rows[index + 1] ?? null : null;
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function countMatches(value: string, query: string) {
  if (!query) return 0;
  return Array.from(value.matchAll(new RegExp(escapeRegExp(query), "gi"))).length;
}

function replaceMatches(value: string, query: string, replacement: string) {
  if (!query) return value;
  return value.replace(new RegExp(escapeRegExp(query), "gi"), replacement);
}

function renderSearchHighlightedTitle(title: string, query: string, activeOccurrence: number | null) {
  if (!query) return renderDocsInlineHtml(title);
  const pattern = new RegExp(escapeRegExp(query), "gi");
  let lastIndex = 0;
  let occurrence = 0;
  let html = "";
  for (const match of title.matchAll(pattern)) {
    const index = match.index ?? 0;
    html += escapeHtml(title.slice(lastIndex, index));
    const active = activeOccurrence === occurrence;
    html += `<mark class="${active ? "bg-primary text-primary-foreground" : "bg-yellow-400/30"} rounded px-0.5">${escapeHtml(match[0])}</mark>`;
    lastIndex = index + match[0].length;
    occurrence += 1;
  }
  html += escapeHtml(title.slice(lastIndex));
  return html;
}

function parentForDepth(rows: OutlineEditorRow[], index: number, depth: number) {
  if (depth <= 0) return null;
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = rows[cursor];
    if (candidate && candidate.depth === depth - 1) return candidate.node.id;
  }
  return null;
}

function previousSiblingForDepth(rows: OutlineEditorRow[], index: number, depth: number) {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = rows[cursor];
    if (!candidate) continue;
    if (candidate.depth < depth) return null;
    if (candidate.depth === depth) return candidate.node.id;
  }
  return null;
}

function docsLineEditorTheme(lineHeight: number, fontSize: number, fontWeight: number) {
  return EditorView.theme({
    "&": {
      minHeight: `${lineHeight}px`,
      border: "0",
      borderRadius: "0",
      background: "transparent",
      boxShadow: "none",
      overflow: "visible",
      color: "var(--foreground)",
      display: "inline-block",
      fontSize: `${fontSize}px`,
      fontWeight: String(fontWeight),
      letterSpacing: "0",
      verticalAlign: "baseline",
    },
    "&.cm-focused": { outline: "none" },
    ".cm-scroller": {
      minHeight: `${lineHeight}px`,
      lineHeight: `${lineHeight}px`,
      overflow: "visible",
      fontFamily: "inherit",
      letterSpacing: "0",
    },
    ".cm-content": {
      minHeight: `${lineHeight}px`,
      // 行末でカーソルが縁に潰れて消えるため右に余白を確保する
      padding: "0 3px 0 0",
      caretColor: "var(--primary)",
      fontFamily: "inherit",
      fontWeight: String(fontWeight),
      letterSpacing: "0",
    },
    ".cm-line": {
      padding: "0",
      caretColor: "var(--primary)",
      lineHeight: `${lineHeight}px`,
      fontWeight: String(fontWeight),
      letterSpacing: "0",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "var(--primary)",
      borderLeftWidth: "2px",
      marginLeft: "0",
    },
    ".cm-selectionBackground": {
      backgroundColor: "color-mix(in srgb, var(--primary) 48%, var(--card))",
    },
    "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": {
      // CodeMirror の高詳細度な既定色より優先し、ライトテーマでも暗色へ戻さない。
      backgroundColor: "color-mix(in srgb, var(--primary) 48%, var(--card))",
    },
  }, { dark: true });
}

function fontSizeForKind(kind: DocsBlockKind) {
  if (kind === "heading_1") return 24;
  if (kind === "heading_2") return 20;
  if (kind === "heading_3") return 18;
  return 14;
}

function lineHeightForKind(kind: DocsBlockKind) {
  if (kind === "heading_1") return 36;
  if (kind === "heading_2") return 32;
  if (kind === "heading_3") return 28;
  return 28;
}

function fontWeightForKind(kind: DocsBlockKind) {
  return kind.startsWith("heading") ? 600 : 400;
}

function formatSelection(view: EditorView, before: string, after = before) {
  const selection = view.state.selection.main;
  const selected = view.state.sliceDoc(selection.from, selection.to);
  const insert = `${before}${selected}${after}`;
  const anchor = selection.from + before.length;
  const head = anchor + selected.length;
  view.dispatch({
    changes: { from: selection.from, to: selection.to, insert },
    selection: EditorSelection.range(anchor, head),
  });
  return true;
}

function linkSelection(view: EditorView) {
  const selection = view.state.selection.main;
  const selected = view.state.sliceDoc(selection.from, selection.to) || "text";
  const insert = `[${selected}](https://)`;
  const urlStart = selection.from + selected.length + 3;
  view.dispatch({
    changes: { from: selection.from, to: selection.to, insert },
    selection: EditorSelection.range(urlStart, urlStart + "https://".length),
  });
  return true;
}

class InlinePreviewWidget extends WidgetType {
  constructor(
    private readonly label: string,
    private readonly className: string,
    private readonly href?: string,
    private readonly taskId?: string,
  ) {
    super();
  }

  eq(other: InlinePreviewWidget) {
    return this.label === other.label && this.className === other.className && this.href === other.href && this.taskId === other.taskId;
  }

  toDOM() {
    const element = document.createElement(this.taskId ? "button" : this.href ? "a" : "span");
    element.textContent = this.label;
    element.className = this.className;
    if (this.taskId) {
      element.setAttribute("type", "button");
      element.setAttribute("data-docs-task-id", this.taskId);
    }
    if (this.href) {
      element.setAttribute("href", this.href);
      // 内部リンク(/docs, /filer 等)は同タブ遷移＋Ctrl/Cmd+クリックで新規タブ。外部URLのみ別タブ。
      if (/^https?:\/\//i.test(this.href)) {
        element.setAttribute("target", "_blank");
        element.setAttribute("rel", "noreferrer");
      }
    }
    return element;
  }

  ignoreEvent() {
    // リンク付きチップ(node/file/url)はブラウザのアンカー遷移に任せる。
    // false のままだとCodeMirrorがクリックを処理してしまい遷移できない。
    return Boolean(this.href || this.taskId);
  }
}

type InlineDecoration = { from: number; to: number; decoration: Decoration };

function selectionTouches(view: EditorView, from: number, to: number) {
  return view.state.selection.ranges.some((range) =>
    range.empty
      ? range.from >= from && range.from <= to
      : range.from < to && range.to > from,
  );
}

function overlaps(ranges: Array<{ from: number; to: number }>, from: number, to: number) {
  return ranges.some((range) => from < range.to && to > range.from);
}

function buildInlinePreviewDecorations(view: EditorView): DecorationSet {
  const text = view.state.doc.toString();
  const decorations: InlineDecoration[] = [];
  const replaced: Array<{ from: number; to: number }> = [];

  const addReplacement = (from: number, to: number, label: string, className: string, href?: string, taskId?: string) => {
    if (!label || selectionTouches(view, from, to) || overlaps(replaced, from, to)) return;
    decorations.push({
      from,
      to,
      decoration: Decoration.replace({ widget: new InlinePreviewWidget(label, className, href, taskId) }),
    });
    replaced.push({ from, to });
  };

  for (const match of text.matchAll(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g)) {
    addReplacement(
      match.index ?? 0,
      (match.index ?? 0) + match[0].length,
      match[1] ?? "",
      "text-primary underline underline-offset-2",
      match[2],
    );
  }
  for (const match of text.matchAll(/\[\[node:([0-9a-f-]{36})\|([^\]\n]+)\]\]/giu)) {
    addReplacement(
      match.index ?? 0,
      (match.index ?? 0) + match[0].length,
      match[2] ?? "",
      "rounded bg-primary/15 px-1.5 py-0.5 text-primary cursor-pointer hover:bg-primary/25",
      `/docs/${(match[1] ?? "").toLowerCase()}`,
    );
  }
  for (const match of text.matchAll(/\[\[file:([^\]|\n]+)\|([^\]\n]+)\]\]/giu)) {
    addReplacement(
      match.index ?? 0,
      (match.index ?? 0) + match[0].length,
      `📎 ${match[2] ?? ""}`,
      "rounded bg-muted px-1.5 py-0.5 text-primary cursor-pointer hover:bg-muted/70",
      `/filer?open=${encodeURIComponent(match[1] ?? "")}`,
    );
  }
  for (const match of text.matchAll(/\[\[task:([0-9a-f-]{36})\|([^\]\n]+)\]\]/giu)) {
    addReplacement(
      match.index ?? 0,
      (match.index ?? 0) + match[0].length,
      `☑ ${match[2] ?? ""}`,
      "rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary cursor-pointer hover:bg-primary/20",
      undefined,
      (match[1] ?? "").toLowerCase(),
    );
  }
  for (const match of text.matchAll(/(^|\s)#([\p{L}\p{N}_-]+)/gu)) {
    const from = (match.index ?? 0) + (match[1]?.length ?? 0);
    addReplacement(
      from,
      from + (match[0].length - (match[1]?.length ?? 0)),
      `#${match[2] ?? ""}`,
      "rounded border px-1.5 py-0.5 text-[0.85em]",
    );
  }

  const addDelimitedMark = (regex: RegExp, className: string, delimiterLength: number) => {
    for (const match of text.matchAll(regex)) {
      const from = match.index ?? 0;
      const to = from + match[0].length;
      if (selectionTouches(view, from, to) || overlaps(replaced, from, to)) continue;
      decorations.push({ from, to: from + delimiterLength, decoration: Decoration.replace({}) });
      decorations.push({ from: to - delimiterLength, to, decoration: Decoration.replace({}) });
      decorations.push({ from: from + delimiterLength, to: to - delimiterLength, decoration: Decoration.mark({ class: className }) });
    }
  };
  addDelimitedMark(/\*\*([^*\n]+)\*\*/g, "font-semibold", 2);
  addDelimitedMark(/==([^=\n]+)==/g, "rounded bg-yellow-400/20 px-0.5", 2);
  addDelimitedMark(/`([^`\n]+)`/g, "rounded bg-muted px-1 py-0.5 text-[0.9em] font-mono", 1);

  const builder = new RangeSetBuilder<Decoration>();
  for (const item of decorations.sort((a, b) => a.from - b.from || a.to - b.to)) {
    builder.add(item.from, item.to, item.decoration);
  }
  return builder.finish();
}

const inlinePreviewPlugin = ViewPlugin.fromClass(class {
  decorations: DecorationSet;

  constructor(view: EditorView) {
    this.decorations = buildInlinePreviewDecorations(view);
  }

  update(update: ViewUpdate) {
    if (update.docChanged || update.selectionSet || update.viewportChanged) {
      this.decorations = buildInlinePreviewDecorations(update.view);
    }
  }
}, {
  decorations: (plugin) => plugin.decorations,
});

function hostLabelForUrl(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function getCaretColumnFromPoint(event: ReactMouseEvent<HTMLElement>, fallbackLength: number) {
  const doc = event.currentTarget.ownerDocument;
  const title = event.currentTarget.querySelector<HTMLElement>("[data-docs-title-display]");
  if (!title) return fallbackLength;
  const bounds = title.getBoundingClientRect();
  if (event.clientX <= bounds.left) return 0;
  if (event.clientX >= bounds.right) return fallbackLength;
  const pointDoc = doc as Document & {
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  const range = pointDoc.caretRangeFromPoint?.(event.clientX, event.clientY);
  if (range && title.contains(range.startContainer)) {
    const prefix = doc.createRange();
    prefix.selectNodeContents(title);
    prefix.setEnd(range.startContainer, range.startOffset);
    return Math.max(0, Math.min(fallbackLength, prefix.toString().length));
  }
  const position = pointDoc.caretPositionFromPoint?.(event.clientX, event.clientY);
  if (position && title.contains(position.offsetNode)) {
    const prefix = doc.createRange();
    prefix.selectNodeContents(title);
    prefix.setEnd(position.offsetNode, position.offset);
    return Math.max(0, Math.min(fallbackLength, prefix.toString().length));
  }
  return fallbackLength;
}

function VerbatimContentBlock({ content }: { content: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  const copyContent = async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard) {
      setCopyState("failed");
      return;
    }
    try {
      await navigator.clipboard.writeText(content);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <div
      className="mt-2 max-w-full rounded-md border bg-muted/20"
      data-testid="docs-verbatim-content"
      onClick={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
    >
      <div className="flex justify-end border-b px-2 py-1">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7 gap-1.5 px-2 text-xs"
          aria-label="逐語本文をコピー"
          onClick={(event) => {
            event.stopPropagation();
            void copyContent();
          }}
        >
          {copyState === "copied" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copyState === "copied" ? "コピー済み" : copyState === "failed" ? "コピー失敗" : "全文をコピー"}
        </Button>
      </div>
      <pre className="max-w-full overflow-x-auto whitespace-pre p-3 text-xs leading-relaxed">{content}</pre>
    </div>
  );
}

export function OutlineBlockEditor({
  rows,
  documentRow,
  selectedNodeIds,
  requestFocusNodeId,
  nodes,
  projects,
  supertags,
  users = [],
  suggestions = [],
  className,
  emptyParentId,
  hasMoreRows = false,
  onLoadMoreRows,
  onNavigateToDocumentTitle,
  isCollapsed,
  nodeHasChildren,
  isNodeLoading,
  renderBelowRow,
  fieldCandidatesForRow,
}: OutlineBlockEditorProps) {
  const {
    onSelectNode,
    onOpenNode,
    onOpenTask,
    onFocused,
    onCommitPending,
    onCommitTitle,
    onDraftChange,
    onCommitSuccess,
    onCreateNode,
    onArchiveNode,
    onMoveNode,
    onToggleCheckbox,
    onToggleCollapsed,
    onDuplicateNode,
    onApplyTag,
    onRemoveTag,
    onOpenTag,
    onSaveField,
    onDeleteAttachment,
    onMoveToPage,
    onReplaceTitles,
    onFieldShorthand,
    onOpenAliasEditor,
    onCreateSearchNode,
    onSuggestFields,
    onCreateFieldCandidate,
    onSuggestionStatus,
  } = useDocsEditorContext();
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const editingNodeIdRef = useRef<string | null>(null);
  editingNodeIdRef.current = editingNodeId;
  const [emptyDraft, setEmptyDraft] = useState("");
  const [draft, setDraft] = useState("");
  const [caretColumn, setCaretColumn] = useState(0);
  const [urlChoice, setUrlChoice] = useState<UrlChoice | null>(null);
  const [searchMode, setSearchMode] = useState<SearchPanelMode | null>(null);
  const [searchState, setSearchState] = useState<SearchReplaceState>({ find: "", replace: "", scope: "page" });
  const [currentSearchIndex, setCurrentSearchIndex] = useState(0);
  const [replaceArmed, setReplaceArmed] = useState(false);
  const setSearchOpen = (open: boolean) => setSearchMode(open ? "replace" : null);
  const [menuNodeId, setMenuNodeId] = useState<string | null>(null);
  const [contextMenuPosition, setContextMenuPosition] = useState<{ nodeId: string; x: number; y: number } | null>(null);
  const [moveDialog, setMoveDialog] = useState<MoveDialogState | null>(null);
  const [dragNodeId, setDragNodeId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<{ nodeId: string; intent: OutlineDropIntent } | null>(null);
  const [pendingFocusDraft, setPendingFocusDraft] = useState<{ nodeId: string; title: string } | null>(null);
  const [inlineSuggestion, setInlineSuggestion] = useState<InlineSuggestion | null>(null);
  const [inlineIndex, setInlineIndex] = useState(0);
  const [taskCandidates, setTaskCandidates] = useState<Task[]>([]);
  const [slashCommand, setSlashCommand] = useState<SlashCommandState | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const editorHostRef = useRef<HTMLDivElement | null>(null);
  const editorViewRef = useRef<EditorView | null>(null);
  const pendingTagFocusRef = useRef<{ nodeId: string; tagId: string } | null>(null);
  const editorThemeCompartmentRef = useRef<Compartment | null>(null);
  const menuSurfaceRef = useRef<HTMLDivElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const moveInputRef = useRef<HTMLInputElement | null>(null);
  const composingRef = useRef(false);
  const applyingShortcutRef = useRef(false);
  const commitPromisesRef = useRef(new Map<string, Promise<boolean>>());
  const caretColumnRef = useRef(caretColumn);
  const rowsRef = useRef(rows);
  const draftRef = useRef(draft);
  const slashCommandRef = useRef<SlashCommandState | null>(null);
  const slashMenuRef = useRef<HTMLDivElement | null>(null);
  const slashIndexRef = useRef(0);
  const inlineSuggestionRef = useRef<InlineSuggestion | null>(null);
  const inlineIndexRef = useRef(0);
  const taskCandidatesRef = useRef<Task[]>([]);
  const fieldCommandRef = useRef<FieldCommandState | null>(null);
  const rowRenderCacheRef = useRef(new Map<string, OutlineRowMemoEntry>());
  const outlineScrollRef = useRef<HTMLDivElement | null>(null);
  const editorRootRef = useRef<HTMLDivElement | null>(null);
  // 少数行は通常描画し、大量行は可視範囲だけを描画する。
  const outlineVirtualized = rows.length > 50;
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getItemKey: (index) => rows[index]?.node.id ?? index,
    getScrollElement: () => outlineScrollRef.current?.closest<HTMLElement>("[data-docs-scroll-container]") ?? null,
    estimateSize: () => 32,
    overscan: 12,
    initialRect: { width: 0, height: 600 },
    scrollMargin: outlineScrollRef.current?.offsetTop ?? 0,
    enabled: outlineVirtualized,
    observeElementRect: (instance, callback) =>
      observeVirtualElementRect(instance, (rect) =>
        callback(rect.height > 0 ? rect : { ...rect, height: 600 }),
      ),
  });

  useLayoutEffect(() => {
    rowsRef.current = rows;
  }, [rows]);
  useLayoutEffect(() => {
    const pending = pendingTagFocusRef.current;
    if (!pending) return;
    const chip = editorRootRef.current?.querySelector<HTMLButtonElement>(
      `[data-docs-block-id="${pending.nodeId}"] [data-docs-supertag-id="${pending.tagId}"]`,
    );
    if (!chip) return;
    chip.focus();
    pendingTagFocusRef.current = null;
  });
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);
  useEffect(() => {
    caretColumnRef.current = caretColumn;
  }, [caretColumn]);
  useEffect(() => {
    slashCommandRef.current = slashCommand;
  }, [slashCommand]);
  useEffect(() => {
    slashIndexRef.current = slashIndex;
  }, [slashIndex]);
  useEffect(() => {
    inlineSuggestionRef.current = inlineSuggestion;
  }, [inlineSuggestion]);
  const updateInlineSuggestion = useCallback((next: InlineSuggestion | null) => {
    inlineSuggestionRef.current = next;
    setInlineSuggestion(next);
  }, []);
  useEffect(() => {
    inlineIndexRef.current = inlineIndex;
  }, [inlineIndex]);
  useEffect(() => {
    taskCandidatesRef.current = taskCandidates;
  }, [taskCandidates]);
  useLayoutEffect(() => {
    slashMenuRef.current
      ?.querySelector<HTMLElement>("[role='option'][aria-selected='true']")
      ?.scrollIntoView({ block: "nearest" });
  }, [slashCommand?.query, slashIndex]);
  useEffect(() => {
    if (inlineSuggestion?.kind !== "task") {
      setTaskCandidates([]);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void fetch("/api/tasks", { signal: controller.signal })
        .then((response) => response.ok ? response.json() : Promise.reject(new Error("task search failed")))
        .then((payload: Task[] | { tasks?: Task[] }) => {
          const tasks = Array.isArray(payload) ? payload : payload.tasks ?? [];
          const query = inlineSuggestion.query.trim().toLowerCase().replace(/-/g, "");
          setTaskCandidates(tasks
            .filter((task) => task.status !== "closed")
            .filter((task) => !query || task.title.toLowerCase().includes(query) || task.id.toLowerCase().replace(/-/g, "").includes(query))
            .slice(0, 8));
          setInlineIndex(0);
        })
        .catch((error: unknown) => {
          if (!(error instanceof DOMException && error.name === "AbortError")) setTaskCandidates([]);
        });
    }, 150);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [inlineSuggestion?.kind, inlineSuggestion?.query]);

  const editingRow = useMemo(() => rows.find((row) => row.node.id === editingNodeId) ?? null, [editingNodeId, rows]);
  const editingKind = editingRow ? docsBlockKind(editingRow.node) : null;
  const pageRootId = rows[0]?.node.root_page_id ?? rows[0]?.node.id ?? null;
  const searchTargets = useMemo(
    () => {
      if (searchState.scope === "workspace") return nodes.filter((node) => !node.archived_at);
      if (!pageRootId) return rows.map((row) => row.node);
      const pageNodes = nodes.filter((node) => !node.archived_at && (node.id === pageRootId || node.root_page_id === pageRootId));
      return pageNodes.length > 0 ? pageNodes : rows.map((row) => row.node);
    },
    [nodes, pageRootId, rows, searchState.scope],
  );
  const searchHits = useMemo<SearchHit[]>(() => {
    if (!searchState.find) return [];
    return searchTargets.flatMap((node) =>
      Array.from({ length: countMatches(node.title, searchState.find) }, (_unused, occurrence) => ({
        node,
        nodeId: node.id,
        occurrence,
      })),
    );
  }, [searchState.find, searchTargets]);
  const currentSearchHit = searchHits.length > 0 ? searchHits[Math.min(currentSearchIndex, searchHits.length - 1)] ?? null : null;
  const replaceUpdates = useMemo(() => {
    if (!searchState.find) return [];
    const seen = new Set<string>();
    return searchTargets.flatMap((node) => {
      if (seen.has(node.id) || !countMatches(node.title, searchState.find)) return [];
      seen.add(node.id);
      const title = replaceMatches(node.title, searchState.find, searchState.replace);
      return title !== node.title ? [{ node, title }] : [];
    });
  }, [searchState.find, searchState.replace, searchTargets]);

  useEffect(() => {
    if (!searchMode) return;
    window.setTimeout(() => searchInputRef.current?.focus(), 0);
  }, [searchMode]);

  const openMoveDialog = useCallback((row: OutlineEditorRow) => {
    setMenuNodeId(null);
    setContextMenuPosition(null);
    setMoveDialog({ row, query: "", items: [], activeIndex: 0, loading: true, error: null });
    window.setTimeout(() => moveInputRef.current?.focus(), 0);
  }, []);

  useEffect(() => {
    if (!moveDialog) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({ q: moveDialog.query, limit: "50" });
      fetch(`/api/docs/pages?${query.toString()}`, { signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error("移動先を検索できませんでした");
          return response.json() as Promise<{ pages?: MovePageCandidate[] }>;
        })
        .then((data) => {
          setMoveDialog((current) => current && current.row.node.id === moveDialog.row.node.id && current.query === moveDialog.query
            ? {
                ...current,
                // Docsでは全ノードをズームしてページとして開けるため、node_typeで候補を狭めない。
                items: (data.pages ?? []).filter((page) => page.id !== current.row.node.id),
                activeIndex: 0,
                loading: false,
                error: null,
              }
            : current);
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setMoveDialog((current) => current ? { ...current, loading: false, error: error instanceof Error ? error.message : "移動先を検索できませんでした" } : current);
        });
    }, 120);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [moveDialog?.query, moveDialog?.row.node.id]);

  useEffect(() => {
    setCurrentSearchIndex((current) => Math.max(0, Math.min(current, Math.max(0, searchHits.length - 1))));
    setReplaceArmed(false);
  }, [searchHits.length, searchState.find, searchState.replace, searchState.scope]);

  useEffect(() => {
    if (!currentSearchHit) return;
    const element = document.querySelector(`[data-docs-node-id="${currentSearchHit.nodeId}"]`);
    element?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [currentSearchHit]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key !== "f" && key !== "r") return;
      event.preventDefault();
      event.stopPropagation();
      if (key === "f") {
        setSearchState((current) => ({ ...current, scope: "page" }));
        setSearchMode("find");
      } else {
        setSearchState((current) => ({ ...current, scope: "page" }));
        setSearchMode("replace");
      }
    };
    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, []);

  // 三点メニューはメニュー外の mousedown で閉じる（トリガー再クリック以外での閉じ手段）
  useEffect(() => {
    if (!menuNodeId) return;
    const handlePointerDown = (event: MouseEvent) => {
      const surface = menuSurfaceRef.current;
      if (surface && event.target instanceof Node && surface.contains(event.target)) return;
      setMenuNodeId(null);
      setContextMenuPosition(null);
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [menuNodeId]);

  useEffect(() => {
    if (!urlChoice && !inlineSuggestion && !slashCommand) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-docs-inline-popup]")) return;
      setUrlChoice(null);
      updateInlineSuggestion(null);
      setSlashCommand(null);
    };
    document.addEventListener("mousedown", handlePointerDown, true);
    return () => document.removeEventListener("mousedown", handlePointerDown, true);
  }, [inlineSuggestion, slashCommand, updateInlineSuggestion, urlChoice]);

  useLayoutEffect(() => {
    if (!pendingFocusDraft) return;
    if (!rows.some((row) => row.node.id === pendingFocusDraft.nodeId)) return;
    draftRef.current = pendingFocusDraft.title;
    setEditingNodeId(pendingFocusDraft.nodeId);
    setDraft(pendingFocusDraft.title);
    setCaretColumn(0);
    setPendingFocusDraft(null);
  }, [pendingFocusDraft, rows]);

  useLayoutEffect(() => {
    const view = editorViewRef.current;
    const compartment = editorThemeCompartmentRef.current;
    if (!view || !compartment || !editingKind) return;
    view.dispatch({
      effects: compartment.reconfigure(
        docsLineEditorTheme(
          lineHeightForKind(editingKind),
          fontSizeForKind(editingKind),
          fontWeightForKind(editingKind),
        ),
      ),
    });
  }, [editingKind, editingRow?.node.id]);

  // 戻り値: フィールド記法として処理された場合 true（Enter 後の新規ノード作成をスキップさせる）。
  const commitCurrent = useCallback((): Promise<boolean> => {
    const row = editingNodeId ? rowsRef.current.find((item) => item.node.id === editingNodeId) : null;
    if (!row || composingRef.current) return Promise.resolve(false);
    const draftAtCommit = draftRef.current;
    const commitKey = `${row.node.id}\u0000${draftAtCommit}`;
    const existing = commitPromisesRef.current.get(commitKey);
    if (existing) return existing;
    const operation = (async () => {
      try {
        // Field は `/field` から開始した入力だけを処理する。通常の `>` はMarkdown引用として予約する。
        const fieldCommand = fieldCommandRef.current?.nodeId === row.node.id ? fieldCommandRef.current : null;
        if (onFieldShorthand && fieldCommand) {
          const rawValue = draftAtCommit.startsWith(fieldCommand.prefix)
            ? draftAtCommit.slice(fieldCommand.prefix.length)
            : draftAtCommit;
          fieldCommandRef.current = null;
          if (fieldCommand.fieldName) {
            const handled = await onFieldShorthand(row, fieldCommand.fieldName, rawValue);
            if (handled) {
              onCommitSuccess?.(row.node.id, draftAtCommit);
              return true;
            }
          }
        }
        const currentKind = docsBlockKind(row.node);
        const shortcut = markdownShortcutPatchForTitle(draftAtCommit, currentKind);
        const patch =
          shortcut && (shortcut.kind !== currentKind || shortcut.kind === "checkbox")
            ? {
                body_json: {
                  ...row.node.body_json,
                  ...blockJsonForKind(shortcut.kind, shortcut.checked),
                },
                node_type: shortcut.kind === "search" ? "search" : row.node.node_type,
                display_props: {
                  ...row.node.display_props,
                  ...(shortcut.kind === "checkbox" ? { show_checkbox: true, checked: shortcut.checked === true } : {}),
                },
              }
            : undefined;
        await onCommitTitle(row.node, shortcut?.title ?? draftAtCommit, patch);
        onCommitSuccess?.(row.node.id, draftAtCommit);
        return false;
      } finally {
        commitPromisesRef.current.delete(commitKey);
        onCommitPending?.(Array.from(commitPromisesRef.current.values()).at(-1) ?? null);
      }
    })();
    commitPromisesRef.current.set(commitKey, operation);
    onCommitPending?.(operation);
    return operation;
  }, [editingNodeId, onCommitPending, onCommitSuccess, onCommitTitle, onFieldShorthand]);

  const executeSlashCommand = useCallback((commandId: SlashCommandId) => {
    const state = slashCommandRef.current;
    if (!state) return;
    const row = rowsRef.current.find((item) => item.node.id === state.nodeId);
    if (!row) {
      setSlashCommand(null);
      return;
    }
    const view = editorViewRef.current;
    const fullText = view ? view.state.doc.toString() : draftRef.current;
    // 行末の `/入力文字列` を除去したタイトル。
    const nextTitle = (fullText.slice(0, state.from) + fullText.slice(state.to)).replace(/\s+$/, "");
    setSlashCommand(null);
    if (commandId === "field") {
      const fieldStart = nextTitle.length + (nextTitle ? 1 : 0);
      const fieldPrefix = `${nextTitle}${nextTitle ? " " : ""}`;
      view?.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: fieldPrefix },
        selection: EditorSelection.cursor(fieldPrefix.length),
      });
      setDraft(fieldPrefix);
      draftRef.current = fieldPrefix;
      const coords = view?.coordsAtPos(fieldPrefix.length);
      const fallback = view?.dom.getBoundingClientRect();
      updateInlineSuggestion({
        kind: "field",
        nodeId: row.node.id,
        query: "",
        from: fieldStart,
        to: fieldStart,
        x: coords?.left ?? fallback?.left ?? 0,
        y: (coords?.bottom ?? fallback?.bottom ?? 0) + 8,
      });
      setInlineIndex(0);
      return;
    }
    setDraft(nextTitle);
    draftRef.current = nextTitle;
    const closeEditing = () => {
      setEditingNodeId(null);
      onFocused(null);
    };
    const blockKind = SLASH_BLOCK_KIND[commandId];
    if (blockKind) {
      void (async () => {
        await onCommitTitle(row.node, nextTitle, {
          body_json: { ...row.node.body_json, ...blockJsonForKind(blockKind) },
          node_type: row.node.node_type === "search" ? "node" : row.node.node_type,
          display_props:
            blockKind === "checkbox"
              ? { ...row.node.display_props, show_checkbox: true, checked: false }
              : { ...row.node.display_props, show_checkbox: false },
        });
        closeEditing();
      })();
      return;
    }
    void (async () => {
      await onCommitTitle(row.node, nextTitle);
      if (commandId === "field_ai") {
        onSuggestFields?.(row);
      } else if (commandId === "alias") {
        onOpenAliasEditor?.(row);
      } else if (commandId === "search_node") {
        onCreateSearchNode?.(row);
      } else if (commandId === "move") {
        openMoveDialog(row);
      }
      closeEditing();
    })();
  }, [onCommitTitle, onFocused, onOpenAliasEditor, onCreateSearchNode, onSuggestFields, openMoveDialog, updateInlineSuggestion]);

  const executeSlashCommandRef = useRef(executeSlashCommand);
  useEffect(() => {
    executeSlashCommandRef.current = executeSlashCommand;
  }, [executeSlashCommand]);

  const focusNode = useCallback((nodeId: string, column = caretColumnRef.current, fallbackTitle = "") => {
    const targetIndex = rowsRef.current.findIndex((item) => item.node.id === nodeId);
    if (outlineVirtualized && targetIndex >= 0) rowVirtualizer.scrollToIndex(targetIndex, { align: "auto" });
    const row = rowsRef.current.find((item) => item.node.id === nodeId);
    if (editingNodeIdRef.current === nodeId && editorViewRef.current) {
      const position = Math.max(0, Math.min(column, editorViewRef.current.state.doc.length));
      editorViewRef.current.dispatch({ selection: EditorSelection.cursor(position) });
      editorViewRef.current.focus();
      onSelectNode(nodeId);
      onFocused(nodeId);
      return;
    }
    if (!row) {
      draftRef.current = fallbackTitle;
      setPendingFocusDraft({ nodeId, title: fallbackTitle });
      setEditingNodeId(nodeId);
      setDraft(fallbackTitle);
      setCaretColumn(column);
      onSelectNode(nodeId);
      onFocused(nodeId);
      return;
    }
    draftRef.current = row.node.title;
    setEditingNodeId(nodeId);
    setDraft(row.node.title);
    setCaretColumn(Math.max(0, column));
    onSelectNode(nodeId);
    onFocused(nodeId);
  }, [onFocused, onSelectNode, outlineVirtualized, rowVirtualizer]);

  const fieldControlsForNode = useCallback((nodeId: string) => (
    Array.from(
      editorRootRef.current?.querySelectorAll<HTMLElement>(
        `[data-docs-block-id="${nodeId}"] [data-docs-field-control]`,
      ) ?? [],
    ).filter((element) => !element.closest("[hidden]"))
  ), []);

  const attachmentControlsForNode = useCallback((nodeId: string) => (
    Array.from(
      editorRootRef.current?.querySelectorAll<HTMLElement>(
        `[data-docs-block-id="${nodeId}"] [data-docs-attachment-control]`,
      ) ?? [],
    ).filter((element) => !element.closest("[hidden]"))
  ), []);

  const focusFirstField = useCallback((nodeId: string) => {
    const first = fieldControlsForNode(nodeId)[0];
    if (!first) return false;
    first.focus();
    return true;
  }, [fieldControlsForNode]);

  const focusLastField = useCallback((nodeId: string) => {
    const fields = fieldControlsForNode(nodeId);
    const last = fields.at(-1);
    if (!last) return false;
    last.focus();
    return true;
  }, [fieldControlsForNode]);

  const focusFirstAttachment = useCallback((nodeId: string) => {
    const first = attachmentControlsForNode(nodeId)[0];
    if (!first) return false;
    first.focus();
    return true;
  }, [attachmentControlsForNode]);

  const focusLastAttachment = useCallback((nodeId: string) => {
    const last = attachmentControlsForNode(nodeId).at(-1);
    if (!last) return false;
    last.focus();
    return true;
  }, [attachmentControlsForNode]);

  const focusFirstDetail = useCallback((nodeId: string) => (
    focusFirstField(nodeId) || focusFirstAttachment(nodeId)
  ), [focusFirstAttachment, focusFirstField]);

  const focusLastDetail = useCallback((nodeId: string) => (
    focusLastAttachment(nodeId) || focusLastField(nodeId)
  ), [focusLastAttachment, focusLastField]);

  const focusAdjacentNode = useCallback((nodeId: string, direction: -1 | 1) => {
    const index = rowsRef.current.findIndex((row) => row.node.id === nodeId);
    const target = rowsRef.current[index + direction];
    if (!target) return false;
    if (direction < 0 && focusLastDetail(target.node.id)) return true;
    focusNode(target.node.id, direction < 0 ? target.node.title.length : 0);
    return true;
  }, [focusLastDetail, focusNode]);

  const copyAttachment = useCallback(async (attachment: DocsAttachment) => {
    const url = `/api/docs/attachments/${attachment.id}`;
    if (!attachment.mime_type?.startsWith("image/") || !navigator.clipboard?.write || typeof ClipboardItem === "undefined") {
      await navigator.clipboard.writeText(new URL(url, window.location.origin).href);
      return;
    }
    const response = await fetch(url);
    if (!response.ok) throw new Error("画像を取得できませんでした");
    const source = await response.blob();
    const png = source.type === "image/png"
      ? source
      : await (async () => {
          const bitmap = await createImageBitmap(source);
          const canvas = document.createElement("canvas");
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
          const context = canvas.getContext("2d");
          if (!context) throw new Error("画像をコピーできませんでした");
          context.drawImage(bitmap, 0, 0);
          bitmap.close();
          return new Promise<Blob>((resolveBlob, reject) => {
            canvas.toBlob((blob) => blob ? resolveBlob(blob) : reject(new Error("画像をコピーできませんでした")), "image/png");
          });
        })();
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
  }, []);

  const focusFirstTag = useCallback((nodeId: string) => {
    const chip = editorRootRef.current?.querySelector<HTMLButtonElement>(
      `[data-docs-block-id="${nodeId}"] [data-docs-supertag-chip]`,
    );
    if (!chip) return false;
    chip.focus();
    return true;
  }, []);

  useEffect(() => {
    if (requestFocusNodeId) focusNode(requestFocusNodeId);
  }, [focusNode, requestFocusNodeId]);

  useLayoutEffect(() => {
    if (!editorHostRef.current || !editingRow) return;
    editorViewRef.current?.destroy();
    const editorDraft = draftRef.current;
    const kind = docsBlockKind(editingRow.node);
    const lineHeight = lineHeightForKind(kind);
    const fontSize = fontSizeForKind(kind);
    const fontWeight = fontWeightForKind(kind);
    const themeCompartment = new Compartment();
    editorThemeCompartmentRef.current = themeCompartment;
    const applyInputMarkdownShortcut = (view: EditorView) => {
      if (composingRef.current || applyingShortcutRef.current) return false;
      const text = view.state.doc.toString();
      if (text.includes("\n")) return false;
      const prefix = markdownShortcutPrefixForTitle(text);
      if (!prefix) return false;
      const patch = {
        body_json: {
          ...editingRow.node.body_json,
          ...blockJsonForKind(prefix.kind, prefix.checked),
        },
        node_type: editingRow.node.node_type,
        display_props: {
          ...editingRow.node.display_props,
          ...(prefix.kind === "checkbox" ? { show_checkbox: true, checked: prefix.checked === true } : {}),
        },
      };
      applyingShortcutRef.current = true;
      try {
        view.dispatch({
          changes: { from: 0, to: prefix.prefixLength, insert: "" },
          selection: EditorSelection.cursor(0),
        });
      } finally {
        applyingShortcutRef.current = false;
      }
      setDraft("");
      draftRef.current = "";
      setCaretColumn(0);
      void onCommitTitle(editingRow.node, "", patch);
      return true;
    };
    const view = new EditorView({
      parent: editorHostRef.current,
      state: EditorState.create({
        doc: editorDraft,
        selection: {
          anchor: Math.min(caretColumnRef.current, editorDraft.length),
          head: Math.min(caretColumnRef.current, editorDraft.length),
        },
        extensions: [
          drawSelection(),
          dropCursor(),
          history(),
          EditorState.allowMultipleSelections.of(true),
          selectNextOccurrenceKeymap,
          inlinePreviewPlugin,
          themeCompartment.of(docsLineEditorTheme(lineHeight, fontSize, fontWeight)),
          Prec.highest(keymap.of([
            {
              key: "Mod-b",
              run: (view) => formatSelection(view, "**"),
            },
            {
              key: "Mod-i",
              run: (view) => formatSelection(view, "*"),
            },
            {
              key: "Mod-k",
              run: linkSelection,
            },
            {
              key: "Alt-ArrowUp",
              run: () => {
                const index = rowsRef.current.findIndex((row) => row.node.id === editingRow.node.id);
                const current = rowsRef.current[index];
                if (!current) return true;
                const previous = previousSiblingForDepth(rowsRef.current, index, current.depth);
                if (!previous) return true;
                const previousIndex = rowsRef.current.findIndex((row) => row.node.id === previous);
                const beforePrevious = previousSiblingForDepth(rowsRef.current, previousIndex, current.depth);
                void onMoveNode({ nodeId: editingRow.node.id, parentId: current.node.parent_id, afterNodeId: beforePrevious });
                return true;
              },
            },
            {
              key: "Alt-ArrowDown",
              run: () => {
                const index = rowsRef.current.findIndex((row) => row.node.id === editingRow.node.id);
                const current = rowsRef.current[index];
                if (!current) return true;
                for (let cursor = index + 1; cursor < rowsRef.current.length; cursor += 1) {
                  const candidate = rowsRef.current[cursor];
                  if (!candidate || candidate.depth < current.depth) break;
                  if (candidate.depth === current.depth) {
                    void onMoveNode({ nodeId: editingRow.node.id, parentId: current.node.parent_id, afterNodeId: candidate.node.id });
                    return true;
                  }
                }
                return true;
              },
            },
            {
              key: "Enter",
              run: (view) => {
                if (composingRef.current) return true;
                const tagSuggestion = inlineSuggestionRef.current?.kind === "tag" ? inlineSuggestionRef.current : null;
                if (tagSuggestion) {
                  const row = rowsRef.current.find((item) => item.node.id === tagSuggestion.nodeId);
                  const candidates = supertags
                    .filter((tag) => tag.name.toLowerCase().includes(tagSuggestion.query.toLowerCase()))
                    .slice(0, 8);
                  const selected = candidates[Math.min(inlineIndexRef.current, Math.max(0, candidates.length - 1))];
                  if (row && selected) {
                    caretColumnRef.current = tagSuggestion.from;
                    view.dispatch({
                      changes: { from: tagSuggestion.from, to: tagSuggestion.to, insert: "" },
                      selection: EditorSelection.cursor(tagSuggestion.from),
                    });
                    pendingTagFocusRef.current = { nodeId: row.node.id, tagId: selected.id };
                    void Promise.resolve(onApplyTag(row.node, selected)).then(() => {
                      requestAnimationFrame(() => {
                        const chip = editorRootRef.current?.querySelector<HTMLButtonElement>(
                          `[data-docs-block-id="${row.node.id}"] [data-docs-supertag-id="${selected.id}"]`,
                        );
                        if (chip) {
                          chip.focus();
                          pendingTagFocusRef.current = null;
                        }
                      });
                    });
                    updateInlineSuggestion(null);
                  }
                  return true;
                }
                const fieldSuggestion = inlineSuggestionRef.current?.kind === "field" ? inlineSuggestionRef.current : null;
                if (fieldSuggestion) {
                  const row = rowsRef.current.find((item) => item.node.id === fieldSuggestion.nodeId);
                  const candidates = row
                    ? (fieldCandidatesForRow?.(row) ?? row.fields ?? [])
                        .filter((field) => field.name.toLowerCase().includes(fieldSuggestion.query.toLowerCase()))
                    : [];
                  const selected = candidates[Math.min(inlineIndexRef.current, Math.max(0, candidates.length - 1))];
                  if (selected) {
                    const value = `${selected.name}: `;
                    const nextCaret = fieldSuggestion.from + value.length;
                    fieldCommandRef.current = {
                      nodeId: fieldSuggestion.nodeId,
                      fieldName: selected.name,
                      prefix: view.state.sliceDoc(0, fieldSuggestion.from) + value,
                    };
                    caretColumnRef.current = nextCaret;
                    flushSync(() => updateInlineSuggestion(null));
                    view.dispatch({
                      changes: { from: fieldSuggestion.from, to: fieldSuggestion.to, insert: value },
                      selection: EditorSelection.cursor(nextCaret),
                    });
                    view.focus();
                  } else if (row && fieldSuggestion.query.trim() && onCreateFieldCandidate) {
                    void Promise.resolve(onCreateFieldCandidate(row, fieldSuggestion.query.trim())).then((created) => {
                      if (!created) return;
                      const fieldName = fieldSuggestion.query.trim();
                      const value = `${fieldName}: `;
                      fieldCommandRef.current = {
                        nodeId: fieldSuggestion.nodeId,
                        fieldName,
                        prefix: view.state.sliceDoc(0, fieldSuggestion.from) + value,
                      };
                      caretColumnRef.current = fieldSuggestion.from + value.length;
                      flushSync(() => updateInlineSuggestion(null));
                      view.dispatch({
                        changes: { from: fieldSuggestion.from, to: fieldSuggestion.to, insert: value },
                        selection: EditorSelection.cursor(fieldSuggestion.from + value.length),
                      });
                      view.focus();
                    });
                  }
                  return true;
                }
                const taskSuggestion = inlineSuggestionRef.current?.kind === "task" ? inlineSuggestionRef.current : null;
                if (taskSuggestion) {
                  const candidates = taskCandidatesRef.current;
                  const selected = candidates[Math.min(inlineIndexRef.current, Math.max(0, candidates.length - 1))];
                  if (selected) void applyInlineSuggestion(taskSuggestion, `[[task:${selected.id}|${selected.title}]] `);
                  return true;
                }
                if (slashCommandRef.current) {
                  const commands = filterSlashCommands(slashCommandRef.current.query);
                  const command = commands[Math.min(slashIndexRef.current, commands.length - 1)];
                  if (command) {
                    executeSlashCommandRef.current(command.id);
                  } else {
                    setSlashCommand(null);
                  }
                  return true;
                }
                  const cursor = view.state.selection.main.head;
                  const parts = splitBlockTitle(view.state.doc.toString(), cursor);
                  void (async () => {
                    setDraft(parts.before);
                    draftRef.current = parts.before;
                    // `/field` 入力だけはField保存結果を確認してから兄弟ノード作成を判断する。
                    const isFieldCommand = fieldCommandRef.current?.nodeId === editingRow.node.id;
                    if (isFieldCommand) {
                      const handledAsField = await commitCurrent();
                      if (handledAsField) return;
                    } else {
                      void commitCurrent();
                    }
                    const createdOrPromise = onCreateNode({
                    parentId: editingRow.node.parent_id,
                    afterNodeId: editingRow.node.id,
                    title: parts.after,
                    kind: docsBlockKind(editingRow.node),
                  });
                  if (createdOrPromise instanceof Promise) {
                    const created = await createdOrPromise;
                    focusNode(created.id, 0, parts.after);
                  } else {
                    focusNode(createdOrPromise.id, 0, parts.after);
                  }
                })();
                return true;
              },
            },
            {
              key: "Tab",
              run: () => {
                const index = rowsRef.current.findIndex((row) => row.node.id === editingRow.node.id);
                const current = rowsRef.current[index];
                if (!current) return true;
                const previousSiblingId = previousSiblingForDepth(rowsRef.current, index, current.depth);
                if (!previousSiblingId) return true;
                void onMoveNode({ nodeId: editingRow.node.id, parentId: previousSiblingId, afterNodeId: null });
                return true;
              },
            },
            {
              key: "Shift-Tab",
              run: () => {
                const index = rowsRef.current.findIndex((row) => row.node.id === editingRow.node.id);
                const current = rowsRef.current[index];
                if (!current || current.depth <= 0) return true;
                const parentId = parentForDepth(rowsRef.current, index, current.depth - 1);
                const afterNodeId = previousSiblingForDepth(rowsRef.current, index, current.depth - 1);
                void onMoveNode({ nodeId: editingRow.node.id, parentId, afterNodeId });
                return true;
              },
            },
            {
              key: "ArrowRight",
              run: (view) => {
                const selection = view.state.selection.main;
                if (!selection.empty || selection.head !== view.state.doc.length) return false;
                return focusFirstTag(editingRow.node.id);
              },
            },
            {
              key: "ArrowUp",
              run: (view) => {
                const tagSuggestion = inlineSuggestionRef.current?.kind === "tag" ? inlineSuggestionRef.current : null;
                if (tagSuggestion) {
                  const count = supertags.filter((tag) => tag.name.toLowerCase().includes(tagSuggestion.query.toLowerCase())).slice(0, 8).length;
                  if (count > 0) setInlineIndex((current) => (current - 1 + count) % count);
                  return true;
                }
                const fieldSuggestion = inlineSuggestionRef.current?.kind === "field" ? inlineSuggestionRef.current : null;
                if (fieldSuggestion) {
                  const row = rowsRef.current.find((item) => item.node.id === fieldSuggestion.nodeId);
                  const count = row
                    ? (fieldCandidatesForRow?.(row) ?? row.fields ?? []).filter((field) => field.name.toLowerCase().includes(fieldSuggestion.query.toLowerCase())).length
                    : 0;
                  if (count > 0) setInlineIndex((current) => (current - 1 + count) % count);
                  return true;
                }
                if (inlineSuggestionRef.current?.kind === "task") {
                  const count = taskCandidatesRef.current.length;
                  if (count > 0) setInlineIndex((current) => (current - 1 + count) % count);
                  return true;
                }
                if (slashCommandRef.current) {
                  const commands = filterSlashCommands(slashCommandRef.current.query);
                  if (commands.length > 0) setSlashIndex((current) => (current - 1 + commands.length) % commands.length);
                  return true;
                }
                const column = view.state.selection.main.head;
                void commitCurrent();
                const previous = previousVisibleNode(rowsRef.current, editingRow.node.id);
                if (!previous) {
                  onNavigateToDocumentTitle?.();
                  return true;
                }
                if (focusLastDetail(previous.node.id)) return true;
                focusNode(previous.node.id, column);
                return true;
              },
            },
            {
              key: "ArrowDown",
              run: (view) => {
                const tagSuggestion = inlineSuggestionRef.current?.kind === "tag" ? inlineSuggestionRef.current : null;
                if (tagSuggestion) {
                  const count = supertags.filter((tag) => tag.name.toLowerCase().includes(tagSuggestion.query.toLowerCase())).slice(0, 8).length;
                  if (count > 0) setInlineIndex((current) => (current + 1) % count);
                  return true;
                }
                const fieldSuggestion = inlineSuggestionRef.current?.kind === "field" ? inlineSuggestionRef.current : null;
                if (fieldSuggestion) {
                  const row = rowsRef.current.find((item) => item.node.id === fieldSuggestion.nodeId);
                  const count = row
                    ? (fieldCandidatesForRow?.(row) ?? row.fields ?? []).filter((field) => field.name.toLowerCase().includes(fieldSuggestion.query.toLowerCase())).length
                    : 0;
                  if (count > 0) setInlineIndex((current) => (current + 1) % count);
                  return true;
                }
                if (inlineSuggestionRef.current?.kind === "task") {
                  const count = taskCandidatesRef.current.length;
                  if (count > 0) setInlineIndex((current) => (current + 1) % count);
                  return true;
                }
                if (slashCommandRef.current) {
                  const commands = filterSlashCommands(slashCommandRef.current.query);
                  if (commands.length > 0) setSlashIndex((current) => (current + 1) % commands.length);
                  return true;
                }
                const column = view.state.selection.main.head;
                void commitCurrent();
                if (focusFirstDetail(editingRow.node.id)) return true;
                const target = nextVisibleNode(rowsRef.current, editingRow.node.id);
                if (!target) return false;
                focusNode(target.node.id, column);
                return true;
              },
            },
            {
              key: "Backspace",
              run: (view) => {
                if (view.state.selection.main.head !== 0) return false;
                const currentKind = docsBlockKind(editingRow.node);
                if (currentKind === "heading_1" || currentKind === "heading_2" || currentKind === "heading_3") {
                  void onCommitTitle(editingRow.node, view.state.doc.toString(), {
                    body_json: { ...editingRow.node.body_json, ...blockJsonForKind("paragraph") },
                    display_props: { ...editingRow.node.display_props, show_checkbox: false },
                  });
                  return true;
                }
                const previous = previousVisibleNode(rowsRef.current, editingRow.node.id);
                if (!previous) return false;
                const merged = `${previous.node.title}${view.state.doc.toString()}`;
                void (async () => {
                  await onCommitTitle(previous.node, merged);
                  await onArchiveNode(editingRow.node);
                  focusNode(previous.node.id, previous.node.title.length);
                })();
                return true;
              },
            },
            {
              key: "Delete",
              run: (view) => {
                if (view.state.selection.main.head !== view.state.doc.length) return false;
                const next = nextVisibleNode(rowsRef.current, editingRow.node.id);
                if (!next) return false;
                const merged = `${view.state.doc.toString()}${next.node.title}`;
                void (async () => {
                  setDraft(merged);
                  draftRef.current = merged;
                  await onCommitTitle(editingRow.node, merged);
                  await onArchiveNode(next.node);
                  focusNode(editingRow.node.id, view.state.doc.length);
                })();
                return true;
              },
            },
            {
              key: "Escape",
              run: () => {
                if (inlineSuggestionRef.current) {
                  updateInlineSuggestion(null);
                  return true;
                }
                if (slashCommandRef.current) {
                  setSlashCommand(null);
                  return true;
                }
                if (composingRef.current) return false;
                void commitCurrent().then(() => {
                  setEditingNodeId(null);
                  onFocused(null);
                  requestAnimationFrame(() => editorRootRef.current?.focus());
                });
                return true;
              },
            },
            ...historyKeymap,
            ...defaultKeymap,
          ])),
          EditorView.domEventHandlers({
            compositionstart() {
              composingRef.current = true;
              return false;
            },
            compositionend() {
              composingRef.current = false;
              const currentView = editorViewRef.current;
              if (currentView) applyInputMarkdownShortcut(currentView);
              return false;
            },
            blur(_event, view) {
              setCaretColumn(view.state.selection.main.head);
              setUrlChoice(null);
              updateInlineSuggestion(null);
              setSlashCommand(null);
              // 初回focus直後や展開・格納による再描画では、draft同期の
              // passive effectより先にblurする場合がある。常に現在表示中の
              // CodeMirror文書を正本にしてから保存し、空の古いrefで既存
              // タイトルを上書きしない。
              draftRef.current = view.state.doc.toString();
              void commitCurrent();
              return false;
            },
            click(event) {
              const taskTarget = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-docs-task-id]") : null;
              const taskId = taskTarget?.dataset.docsTaskId;
              if (!taskId) return false;
              event.preventDefault();
              onOpenTask?.(taskId);
              return true;
            },
            dblclick(event) {
              if (editingRow.taskBinding?.id) {
                event.preventDefault();
                onOpenTask?.(editingRow.taskBinding.id);
                return true;
              }
              return false;
            },
            paste(event, view) {
              const text = event.clipboardData?.getData("text/plain") ?? "";
              if (!text) return false;
              const urlMatch = text.trim().match(/^https?:\/\/\S+$/);
              if (urlMatch) {
                event.preventDefault();
                const selection = view.state.selection.main;
                view.dispatch({
                  changes: { from: selection.from, to: selection.to, insert: urlMatch[0] },
                  selection: EditorSelection.cursor(selection.from + urlMatch[0].length),
                });
                const coords = view.coordsAtPos(selection.from + urlMatch[0].length);
                const fallbackRect = view.dom.getBoundingClientRect();
                setUrlChoice({
                  nodeId: editingRow.node.id,
                  url: urlMatch[0],
                  from: selection.from,
                  to: selection.from + urlMatch[0].length,
                  x: coords?.left ?? fallbackRect.left,
                  y: (coords?.bottom ?? fallbackRect.bottom) + 8,
                });
                return true;
              }
              if (text.includes("\n")) {
                event.preventDefault();
                const blocks = parseIndentedMarkdownBlocks(text);
                void insertMarkdownBlocks(editingRow, blocks);
                return true;
              }
              return false;
            },
          }),
          EditorView.updateListener.of((update: ViewUpdate) => {
            if (!update.docChanged && !update.selectionSet) return;
            const nextText = update.state.doc.toString();
            const head = update.state.selection.main.head;
            setDraft(nextText);
            draftRef.current = nextText;
            setCaretColumn(head);
            if (update.docChanged) onDraftChange?.(editingRow.node, nextText);
            if (update.docChanged && !composingRef.current && applyInputMarkdownShortcut(update.view)) return;
            const before = nextText.slice(0, head);
            const after = nextText.slice(head);
            const coords = update.view.coordsAtPos(head);
            // 行頭または空白直後の `/文字列` を汎用スラッシュコマンドのトリガーにする（URL の // は非対象）。
            const slashMatch = before.match(/(^|\s)\/((?:field\s+ai)|[^\s/]*)$/i);
            // 新規入力中の [[ だけ補完対象にする。既存参照/ファイルリンク([[node:.. / [[file:.. )は
            // : / | を含むため発火させない（＝サジェストが消えない/全パスが並ぶ不具合の防止）。
            const refMatch = (slashMatch || after.startsWith("]]")) ? null : before.match(/\[\[([^\]\n:/|]*)$/);
            const tagMatch = slashMatch || refMatch ? null : before.match(/(^|\s)#([\p{L}\p{N}_-]*)$/u);
            const taskMatch = slashMatch || refMatch || tagMatch ? null : before.match(/(^|\s)@task(?:\s+([^\n]*))?$/iu);
            const userMatch = slashMatch || refMatch || tagMatch || taskMatch ? null : before.match(/(^|\s)@([\p{L}\p{N}_@._-]*)$/u);
            if (slashMatch && coords) {
              const query = slashMatch[2] ?? "";
              setSlashCommand({ nodeId: editingRow.node.id, query, from: head - query.length - 1, to: head, x: coords.left, y: coords.bottom + 8 });
              setSlashIndex(0);
              updateInlineSuggestion(null);
            } else if (refMatch && coords) {
              setSlashCommand(null);
              updateInlineSuggestion({ kind: "ref", nodeId: editingRow.node.id, query: refMatch[1] ?? "", from: head - refMatch[0].length, to: head, x: coords.left, y: coords.bottom + 8 });
            } else if (tagMatch && coords) {
              setSlashCommand(null);
              // 原子的なSupertagへ変換する際、入力トリガー直前の区切り空白も消す。
              // これを残すとタイトル末尾に不可視の空白が保存される。
              updateInlineSuggestion({ kind: "tag", nodeId: editingRow.node.id, query: tagMatch[2] ?? "", from: head - (tagMatch[2]?.length ?? 0) - 1 - (tagMatch[1]?.length ?? 0), to: head, x: coords.left, y: coords.bottom + 8 });
              setInlineIndex(0);
            } else if (taskMatch && coords) {
              setSlashCommand(null);
              updateInlineSuggestion({
                kind: "task",
                nodeId: editingRow.node.id,
                query: taskMatch[2] ?? "",
                from: head - taskMatch[0].length + (taskMatch[1]?.length ?? 0),
                to: head,
                x: coords.left,
                y: coords.bottom + 8,
              });
              setInlineIndex(0);
            } else if (userMatch && coords) {
              // @ は ClickUp風の万能メンション: Docsページ/タスク/ユーザーを何でも紐づけ
              setSlashCommand(null);
              updateInlineSuggestion({ kind: "mention", nodeId: editingRow.node.id, query: userMatch[2] ?? "", from: head - (userMatch[2]?.length ?? 0) - 1, to: head, x: coords.left, y: coords.bottom + 8 });
            } else {
              setSlashCommand(null);
              updateInlineSuggestion(null);
            }
          }),
        ],
      }),
    });
    editorViewRef.current = view;
    view.focus();
    return () => {
      view.destroy();
      if (editorThemeCompartmentRef.current === themeCompartment) editorThemeCompartmentRef.current = null;
      if (editorViewRef.current === view) editorViewRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingRow?.node.id, outlineVirtualized]);

  const insertMarkdownBlocks = useCallback(async (anchorRow: OutlineEditorRow, blocks: MarkdownBlock[]) => {
    let afterNodeId: string | null = anchorRow.node.id;
    const stack: Array<{ depth: number; node: DocsNode }> = [{ depth: anchorRow.depth, node: anchorRow.node }];
    for (const block of blocks) {
      while (stack.length > 0 && (stack.at(-1)?.depth ?? 0) >= anchorRow.depth + 1 + block.depth) stack.pop();
      const parent = stack.at(-1)?.node ?? anchorRow.node;
      const created = await onCreateNode({
        parentId: parent.id,
        afterNodeId,
        title: block.title,
        kind: block.kind,
        checked: block.checked,
      });
      stack.push({ depth: anchorRow.depth + 1 + block.depth, node: created });
      afterNodeId = created.id;
    }
  }, [onCreateNode]);

  const replaceAll = async () => {
    if (replaceUpdates.length === 0) return;
    if (!replaceArmed) {
      setReplaceArmed(true);
      return;
    }
    await onReplaceTitles(replaceUpdates);
    setReplaceArmed(false);
  };

  const applyUrlChoice = async (mode: "link" | "bookmark" | "plain") => {
    if (!urlChoice) return;
    const row = rowsRef.current.find((item) => item.node.id === urlChoice.nodeId);
    if (!row) return;
    const fallbackLabel = hostLabelForUrl(urlChoice.url);
    const sourceTitle = editingNodeId === row.node.id ? draftRef.current : row.node.title;
    const replaceUrl = (value: string, label: string) =>
      `${value.slice(0, urlChoice.from)}[${label}](${urlChoice.url})${value.slice(urlChoice.to)}`;
    const synchronousLink = replaceUrl(sourceTitle, fallbackLabel);
    const next =
      mode === "link"
        ? synchronousLink
        : mode === "bookmark"
          ? `${sourceTitle.slice(0, urlChoice.from)}${fallbackLabel} ${urlChoice.url}${sourceTitle.slice(urlChoice.to)}`
          : sourceTitle;
    if (editingNodeId === row.node.id && editorViewRef.current) {
      editorViewRef.current.dispatch({
        changes: { from: 0, to: editorViewRef.current.state.doc.length, insert: next },
        selection: EditorSelection.cursor(Math.min(next.length, urlChoice.from + next.length - sourceTitle.length)),
      });
      draftRef.current = next;
      setDraft(next);
    }
    await onCommitTitle(row.node, next);
    setUrlChoice(null);
    if (mode === "plain") return;
    const preview = await fetch("/api/docs/link-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: urlChoice.url }),
    }).then((response) => response.ok ? response.json() as Promise<{ title: string; description?: string; domain: string; favicon?: string }> : null).catch(() => null);
    const label = preview?.title || fallbackLabel;
    const titled = mode === "link" ? replaceUrl(sourceTitle, label) : next;
    if (label !== fallbackLabel || mode === "bookmark") {
      if (editingNodeId === row.node.id && editorViewRef.current && mode === "link") {
        editorViewRef.current.dispatch({
          changes: { from: 0, to: editorViewRef.current.state.doc.length, insert: titled },
          selection: EditorSelection.cursor(Math.min(titled.length, urlChoice.from + titled.length - sourceTitle.length)),
        });
        draftRef.current = titled;
        setDraft(titled);
      }
      await onCommitTitle(row.node, titled, mode === "bookmark" ? { body_json: { ...row.node.body_json, bookmark: { url: urlChoice.url, title: label, description: preview?.description ?? "", domain: preview?.domain ?? fallbackLabel, favicon: preview?.favicon ?? "" } } } : undefined);
    }
  };

  const applyInlineSuggestion = async (suggestion: InlineSuggestion, value: string, tag?: DocsSupertag) => {
    const view = editorViewRef.current;
    if (!view || editingNodeId !== suggestion.nodeId) return;
    caretColumnRef.current = suggestion.from + value.length;
    view.dispatch({
      changes: { from: suggestion.from, to: suggestion.to, insert: value },
      selection: EditorSelection.cursor(suggestion.from + value.length),
    });
    updateInlineSuggestion(null);
    if (tag) {
      const row = rowsRef.current.find((item) => item.node.id === suggestion.nodeId);
      if (row) {
        pendingTagFocusRef.current = { nodeId: row.node.id, tagId: tag.id };
        await onApplyTag(row.node, tag);
      }
    }
  };

  const handleCopy = (event: ReactClipboardEvent<HTMLDivElement>) => {
    const selected = rows.filter((row) => selectedNodeIds.has(row.node.id));
    if (selected.length <= 1) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", serializeBlocksToIndentedMarkdown(selected));
  };

  const handleStaticKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>, row: OutlineEditorRow) => {
    if (event.nativeEvent.isComposing) return;
    const target = event.target as HTMLElement;
    const taskId = target.closest<HTMLElement>("[data-docs-task-id]")?.dataset.docsTaskId;
    if (taskId && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      onOpenTask?.(taskId);
      return;
    }
    if (target.closest(".cm-editor, [data-docs-field-control], [data-docs-supertag-chip], [data-docs-attachment-control]")) return;
    if (event.key === "Enter") {
      event.preventDefault();
      focusNode(row.node.id, row.node.title.length);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (focusFirstDetail(row.node.id)) return;
      focusAdjacentNode(row.node.id, 1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!focusAdjacentNode(row.node.id, -1)) onNavigateToDocumentTitle?.();
      return;
    }
    if (event.key === "ArrowRight" && row.tags.length > 0) {
      event.preventDefault();
      focusFirstTag(row.node.id);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      setSearchState((current) => ({ ...current, scope: "page" }));
      setSearchMode("find");
    }
  }, [focusAdjacentNode, focusFirstDetail, focusFirstTag, focusNode, onNavigateToDocumentTitle, onOpenTask]);

  const submitMoveToPage = useCallback(async (page: MovePageCandidate) => {
    const current = moveDialog;
    if (!current || !onMoveToPage) return;
    setMoveDialog({ ...current, loading: true, error: null });
    try {
      await onMoveToPage(current.row.node, page);
      setMoveDialog(null);
    } catch (error) {
      setMoveDialog((latest) => latest ? {
        ...latest,
        loading: false,
        error: error instanceof Error ? error.message : "ノードを移動できませんでした",
      } : latest);
    }
  }, [moveDialog, onMoveToPage]);

  return (
    <div
      ref={editorRootRef}
      className={cn("docs-outline-block-editor min-w-0", className)}
      data-testid="docs-block-editor"
      data-docs-editor-node-id={documentRow?.node.id}
      tabIndex={0}
      aria-label="Docsエディタ"
      onCopy={handleCopy}
    >
      {searchMode ? (
        <div
          className="fixed left-1/2 top-16 z-50 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-wrap items-end gap-2 rounded-md border bg-popover p-2 text-xs shadow-lg"
          role="dialog"
          aria-label={searchMode === "replace" ? "検索置換" : "検索"}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setSearchMode(null);
              return;
            }
            if (event.key === "Enter" && searchMode === "find") {
              event.preventDefault();
              setCurrentSearchIndex((current) => {
                if (searchHits.length === 0) return 0;
                return event.shiftKey ? (current - 1 + searchHits.length) % searchHits.length : (current + 1) % searchHits.length;
              });
            }
          }}
        >
          <label className="grid gap-1">
            <span className="text-muted-foreground">検索</span>
            <input
              ref={searchInputRef}
              className="h-8 w-56 rounded border bg-background px-2"
              value={searchState.find}
              onChange={(event) => setSearchState((current) => ({ ...current, find: event.target.value }))}
              onKeyDown={(event) => {
                if (event.key !== "Enter" || searchMode !== "find") return;
                event.preventDefault();
                event.stopPropagation();
                setCurrentSearchIndex((current) => {
                  if (searchHits.length === 0) return 0;
                  return event.shiftKey ? (current - 1 + searchHits.length) % searchHits.length : (current + 1) % searchHits.length;
                });
              }}
            />
          </label>
          {searchMode === "replace" ? (
            <label className="grid gap-1">
              <span className="text-muted-foreground">置換</span>
              <input
                className="h-8 w-56 rounded border bg-background px-2"
                value={searchState.replace}
                onChange={(event) => setSearchState((current) => ({ ...current, replace: event.target.value }))}
              />
            </label>
          ) : null}
          {searchMode === "replace" ? (
            <label className="grid gap-1">
              <span className="text-muted-foreground">範囲</span>
              <select
                className="h-8 rounded border bg-background px-2"
                value={searchState.scope}
                onChange={(event) => setSearchState((current) => ({ ...current, scope: event.target.value as SearchReplaceState["scope"] }))}
              >
                <option value="page">現在ページ</option>
                <option value="workspace">ワークスペース全体</option>
              </select>
            </label>
          ) : null}
          <div className="h-8 rounded border bg-muted px-2 leading-8 text-muted-foreground">
            {searchHits.length > 0 ? `${Math.min(currentSearchIndex + 1, searchHits.length)} / ${searchHits.length}` : "0件"}
          </div>
          {searchMode === "find" ? (
            <div className="flex h-8 gap-1">
              <Button type="button" size="sm" variant="secondary" onClick={() => setCurrentSearchIndex((current) => searchHits.length ? (current - 1 + searchHits.length) % searchHits.length : 0)}>前へ</Button>
              <Button type="button" size="sm" variant="secondary" onClick={() => setCurrentSearchIndex((current) => searchHits.length ? (current + 1) % searchHits.length : 0)}>次へ</Button>
            </div>
          ) : (
            <Button type="button" size="sm" disabled={replaceUpdates.length === 0} onClick={() => void replaceAll()}>
              {replaceArmed ? `${replaceUpdates.length}件を置換` : `${replaceUpdates.length}件を確認`}
            </Button>
          )}
          <Button type="button" size="sm" variant="ghost" onClick={() => setSearchMode(null)}>閉じる</Button>
        </div>
      ) : null}
      {false ? (
        <div className="mb-3 grid gap-2 rounded-md border bg-background p-3 text-xs md:grid-cols-[1fr_1fr_auto_auto]">
          <label className="grid gap-1">
            <span className="text-muted-foreground">検索</span>
            <input className="h-8 rounded border bg-background px-2" value={searchState.find} onChange={(event) => setSearchState((current) => ({ ...current, find: event.target.value }))} />
          </label>
          <label className="grid gap-1">
            <span className="text-muted-foreground">置換</span>
            <input className="h-8 rounded border bg-background px-2" value={searchState.replace} onChange={(event) => setSearchState((current) => ({ ...current, replace: event.target.value }))} />
          </label>
          <select className="mt-5 h-8 rounded border bg-background px-2" value={searchState.scope} onChange={(event) => setSearchState((current) => ({ ...current, scope: event.target.value as SearchReplaceState["scope"] }))}>
            <option value="page">現在ページ</option>
            <option value="workspace">ワークスペース全体</option>
          </select>
          <div className="mt-5 flex gap-1">
            <Button type="button" size="sm" onClick={() => void replaceAll()}>一括置換</Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setSearchOpen(false)}>閉じる</Button>
          </div>
        </div>
      ) : null}
      {documentRow ? (() => {
        const valuesByFieldId = new Map((documentRow.fieldValues ?? []).map((value) => [value.field_id, value]));
        const visibleFields = (documentRow.fields ?? [])
          .filter((field) => !field.system_key?.startsWith("task_"))
          .map((field) => ({ field, value: fieldValueToDraft(valuesByFieldId.get(field.id)) }))
          .filter(({ value }) => value !== "");
        const verbatimContent = typeof documentRow.node.body_json?.verbatim_content === "string"
          ? documentRow.node.body_json.verbatim_content
          : null;
        const bookmark = documentRow.node.body_json?.bookmark;
        return (
          <div data-testid="docs-document-details" className="mb-2 space-y-1 pl-7">
            {visibleFields.length > 0 ? (
              <div className="space-y-1" data-testid="docs-document-fields">
                {visibleFields.map(({ field, value }, fieldIndex) => (
                  <div key={field.id} className="grid grid-cols-[minmax(8rem,0.35fr)_minmax(12rem,1fr)] items-start gap-2 text-xs">
                    <span className="truncate px-1 py-2 text-muted-foreground">&gt;{field.name}</span>
                    <FieldControl
                      field={field}
                      value={value}
                      nodes={nodes}
                      projects={projects}
                      currentNodeId={documentRow.node.id}
                      onChange={() => {}}
                      onCommit={(next) => void onSaveField(documentRow.node, field, next)}
                      onNavigatePrevious={() => {
                        const fields = Array.from(editorRootRef.current?.querySelectorAll<HTMLElement>("[data-testid='docs-document-fields'] [data-docs-field-control]") ?? []);
                        if (fieldIndex > 0 && fields[fieldIndex - 1]) fields[fieldIndex - 1].focus();
                        else document.querySelector<HTMLElement>("[data-docs-page-title]")?.focus();
                      }}
                      onNavigateNext={() => {
                        const fields = Array.from(editorRootRef.current?.querySelectorAll<HTMLElement>("[data-testid='docs-document-fields'] [data-docs-field-control]") ?? []);
                        if (fields[fieldIndex + 1]) fields[fieldIndex + 1].focus();
                        else if (rows[0]) focusNode(rows[0].node.id, 0);
                      }}
                      onEscape={() => document.querySelector<HTMLElement>("[data-docs-page-title]")?.focus()}
                    />
                  </div>
                ))}
              </div>
            ) : null}
            {verbatimContent !== null ? <VerbatimContentBlock content={verbatimContent} /> : null}
            {bookmark && typeof bookmark === "object" ? (
              <div className="max-w-xl rounded-md border bg-muted/20 p-3 text-xs">
                <div className="font-medium">{String((bookmark as Record<string, unknown>).title ?? "Bookmark")}</div>
                <div className="mt-1 text-muted-foreground">{String((bookmark as Record<string, unknown>).domain ?? "")}</div>
              </div>
            ) : null}
          </div>
        );
      })() : null}
      {rows.length === 0 && emptyParentId ? (
        <input
          autoFocus
          aria-label="最初のノード"
          className="ml-7 h-8 w-[calc(100%-1.75rem)] border-0 bg-transparent px-1 text-sm outline-none placeholder:text-muted-foreground"
          value={emptyDraft}
          placeholder="入力を開始…"
          onChange={(event) => setEmptyDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Escape") {
              event.preventDefault();
              setEmptyDraft("");
              return;
            }
            if (event.key !== "Enter" || !emptyDraft.trim()) return;
            event.preventDefault();
            void Promise.resolve(onCreateNode({
              parentId: emptyParentId,
              afterNodeId: null,
              title: emptyDraft,
              kind: "paragraph",
            })).then((created) => {
              setEmptyDraft("");
              focusNode(created.id, created.title.length, created.title);
            });
          }}
        />
      ) : null}
      <div ref={outlineScrollRef} className="space-y-0.5">
        <div
          className={outlineVirtualized ? "relative w-full" : "contents"}
          style={outlineVirtualized ? { height: `${rowVirtualizer.getTotalSize()}px` } : undefined}
        >
        {(outlineVirtualized
          ? rowVirtualizer.getVirtualItems()
          : rows.map((row, index) => ({ index, key: row.node.id, start: 0 })))
          .map((virtualRow) => {
          const index = virtualRow.index;
          const row = rows[index];
          if (!row) return null;
          const wrapVirtualRow = (content: ReactNode) => outlineVirtualized ? (
            <div
              key={virtualRow.key}
              ref={rowVirtualizer.measureElement}
              data-index={virtualRow.index}
              className="absolute left-0 top-0 w-full"
              style={{ transform: `translateY(${virtualRow.start - rowVirtualizer.options.scrollMargin}px)` }}
            >
              {content}
            </div>
          ) : content;
          const kind = docsBlockKind(row.node);
          const selected = selectedNodeIds.has(row.node.id);
          const collapsed = isCollapsed(row.node.id);
          // 折りたたむと子が rows から消えるため、collapsed のノードは実子有無を別途判定して
          // シェブロンを維持する（再展開不能バグの防止）。nodeHasChildren 未指定なら collapsed=展開可能とみなす。
          const childState =
            hasChildren(row, rows, index) ||
            (nodeHasChildren ? nodeHasChildren(row.node.id) : collapsed);
          const nodeLoading = isNodeLoading?.(row.node.id) ?? false;
          const taskBinding = row.taskBinding ?? null;
          const fieldsExpanded = !collapsed;
          const activeSearchOccurrence = currentSearchHit?.nodeId === row.node.id ? currentSearchHit.occurrence : null;
          const memoInputs = [
            row,
            index,
            selected,
            collapsed,
            childState,
            nodeLoading,
            taskBinding,
            fieldsExpanded,
            activeSearchOccurrence,
            currentSearchHit,
            dragNodeId,
            dropTarget,
            editingNodeId,
            menuNodeId,
            contextMenuPosition,
            searchMode,
            searchState.find,
            nodes,
            projects,
            supertags,
            suggestions,
            renderBelowRow,
            onSelectNode,
            onOpenNode,
            onOpenTask,
            onMoveNode,
            onToggleCollapsed,
            onToggleCheckbox,
            onSaveField,
            onDeleteAttachment,
            onMoveToPage,
            onSuggestionStatus,
            onArchiveNode,
            onDuplicateNode,
            onCommitTitle,
            onApplyTag,
            focusNode,
            openMoveDialog,
            copyAttachment,
            handleStaticKeyDown,
          ] as const;
          const cached = rowRenderCacheRef.current.get(row.node.id);
          if (cached && cached.inputs.length === memoInputs.length && cached.inputs.every((value, inputIndex) => value === memoInputs[inputIndex])) {
            return wrapVirtualRow(cached.element);
          }
          const fieldValuesById = new Map((row.fieldValues ?? []).map((value) => [value.field_id, value]));
          // タスク状態は専用のタスクチップで表示するため、同じ値をフィールド要約へ重ねて表示しない。
          const aiFieldSuggestions = suggestions
            .filter((suggestion) => suggestion.node_id === row.node.id && suggestion.status === "proposed")
            .flatMap((suggestion) => {
              const payloadFields = Array.isArray(suggestion.payload_json.fields) ? suggestion.payload_json.fields : [];
              return payloadFields.flatMap((item) => {
                if (!item || typeof item !== "object") return [];
                const record = item as Record<string, unknown>;
                const name = String(record.name ?? record.field ?? "").trim().toLowerCase();
                const value = String(record.value ?? "").trim();
                const field = row.fields?.find((candidate) => candidate.name.toLowerCase() === name || candidate.system_key?.toLowerCase() === name);
                return field && value ? [{ suggestion, field, value }] : [];
              });
            });
          const visibleFields = (row.fields ?? [])
            .filter((field) => !field.system_key?.startsWith("task_"))
            .filter((field) => !(field.name === "画像" && (row.attachments?.length ?? 0) > 0))
            .filter((field) => {
              const currentValue = fieldValueToDraft(fieldValuesById.get(field.id));
              return Boolean(currentValue) || aiFieldSuggestions.some((item) => item.field.id === field.id);
            });
          const visibleTags = taskBinding ? row.tags.filter((tag) => tag.system_key !== "task") : row.tags;
          const belowRowContent = renderBelowRow ? renderBelowRow(row, index) : null;
          const menuAtPointer = contextMenuPosition?.nodeId === row.node.id ? contextMenuPosition : null;
          const rowMenu = menuNodeId === row.node.id ? (
            <MenuMnemonicSurface
              ref={menuSurfaceRef}
              data-docs-row-menu
              className={cn(
                "z-50 min-w-44 rounded-md border bg-popover p-1 text-xs shadow-lg",
                menuAtPointer ? "fixed" : "absolute right-0 top-7",
              )}
              style={menuAtPointer ? { left: menuAtPointer.x, top: menuAtPointer.y } : undefined}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  event.stopPropagation();
                  setMenuNodeId(null);
                  setContextMenuPosition(null);
                }
              }}
            >
              <MenuButton icon={Plus} label="複製" mnemonic="C" onClick={() => { setMenuNodeId(null); setContextMenuPosition(null); void onDuplicateNode(row.node); }} />
              <MenuButton icon={Trash2} label="削除" mnemonic="D" onClick={() => { setMenuNodeId(null); setContextMenuPosition(null); void onArchiveNode(row.node); }} />
              <MenuButton icon={MoveRight} label="別ページへ移動" mnemonic="M" onClick={() => openMoveDialog(row)} />
              <MenuButton icon={CheckSquare} label="タスク化" mnemonic="T" onClick={() => { setMenuNodeId(null); setContextMenuPosition(null); void onCommitTitle(row.node, row.node.title, { body_json: { ...row.node.body_json, ...blockJsonForKind("checkbox") }, display_props: { ...row.node.display_props, show_checkbox: true } }); }} />
              <MenuButton icon={Tag} label="先頭タグを付与" mnemonic="G" onClick={() => {
                setMenuNodeId(null);
                setContextMenuPosition(null);
                if (row.tags[0]) void onApplyTag(row.node, row.tags[0]);
              }} />
              <MenuButton icon={ExternalLink} label="右パネルで開く" mnemonic="O" onClick={() => { setMenuNodeId(null); setContextMenuPosition(null); onOpenNode(row.node.id); }} />
            </MenuMnemonicSurface>
          ) : null;
          const element = (
             <Fragment key={`${row.node.id}:${row.node.parent_id ?? "root"}:${row.node.sort_order}`}>
            <div
              data-docs-node-id={row.node.id}
              data-docs-block-id={row.node.id}
              data-block-kind={kind}
              className={cn(
                "group relative rounded px-1 py-0",
                selected && "bg-primary/10",
                currentSearchHit?.nodeId === row.node.id && "ring-1 ring-primary/40",
                dragNodeId === row.node.id && "opacity-50",
                dropTarget?.nodeId === row.node.id && dropTarget.intent === "inside" && "ring-1 ring-primary/70 bg-primary/10",
                dropTarget?.nodeId === row.node.id && dropTarget.intent === "before" && "border-t border-primary",
                dropTarget?.nodeId === row.node.id && dropTarget.intent === "after" && "border-b border-primary",
              )}
              style={{ marginLeft: row.depth * 24 }}
              tabIndex={0}
              onKeyDown={(event) => handleStaticKeyDown(event, row)}
              onClick={(event) => {
                const taskId = (event.target as HTMLElement).closest<HTMLElement>("[data-docs-task-id]")?.dataset.docsTaskId;
                if (taskId) {
                  event.preventDefault();
                  event.stopPropagation();
                  onOpenTask?.(taskId);
                  return;
                }
                if ((event.target as HTMLElement).closest(".cm-editor, [data-docs-field-control], [data-docs-supertag-chip], [data-docs-attachment-control], button, a")) return;
                onSelectNode(row.node.id);
                focusNode(row.node.id, getCaretColumnFromPoint(event, row.node.title.length));
              }}
              onContextMenu={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onSelectNode(row.node.id);
                setContextMenuPosition({ nodeId: row.node.id, x: event.clientX, y: event.clientY });
                setMenuNodeId(row.node.id);
              }}
              onDoubleClick={() => {
                if (taskBinding?.id) {
                  onOpenTask?.(taskBinding.id);
                } else {
                  onOpenNode(row.node.id);
                }
              }}
              onDragOver={(event) => {
                if (!dragNodeId || dragNodeId === row.node.id) return;
                const intent = outlineDropIntentFromPointer(event);
                if (!outlineDropMove(rowsRef.current, dragNodeId, row.node.id, intent)) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                setDropTarget({ nodeId: row.node.id, intent });
              }}
              onDragLeave={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTarget(null);
              }}
              onDrop={(event) => {
                event.preventDefault();
                const intent = dropTarget?.nodeId === row.node.id ? dropTarget.intent : outlineDropIntentFromPointer(event);
                const move = dragNodeId ? outlineDropMove(rowsRef.current, dragNodeId, row.node.id, intent) : null;
                if (move) void onMoveNode(move);
                setDragNodeId(null);
                setDropTarget(null);
              }}
            >
              <div className={cn(
                "grid min-w-0 grid-cols-[44px_1fr_28px] items-start gap-1",
                kind === "heading_1" ? "mt-8" : kind === "heading_2" ? "mt-6" : kind === "heading_3" ? "mt-4" : null,
              )}>
                <div className={cn("flex items-center", kind === "heading_1" ? "mt-3" : kind === "heading_2" ? "mt-1.5" : "mt-1")}>
                  <button
                    type="button"
                    draggable
                    data-docs-drag-handle
                    className="grid size-5 cursor-grab place-items-center rounded text-muted-foreground opacity-0 hover:bg-accent active:cursor-grabbing group-hover:opacity-70 focus-visible:opacity-100"
                    title="ドラッグして並べ替え・階層移動"
                    aria-label={`${row.node.title}をドラッグして移動`}
                    onClick={(event) => event.stopPropagation()}
                    onDragStart={(event) => {
                      event.stopPropagation();
                      event.dataTransfer.effectAllowed = "move";
                      event.dataTransfer.setData("text/plain", row.node.id);
                      setDragNodeId(row.node.id);
                    }}
                    onDragEnd={() => {
                      setDragNodeId(null);
                      setDropTarget(null);
                    }}
                  >
                    <GripVertical className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    className="grid size-5 place-items-center rounded text-muted-foreground opacity-70 hover:bg-accent group-hover:opacity-100"
                    title={childState ? collapsed ? "展開" : "折りたたみ" : "ノード"}
                    tabIndex={childState ? 0 : -1}
                    onMouseDown={(event) => {
                      if (childState) event.preventDefault();
                    }}
                    onClick={(event) => {
                      event.stopPropagation();
                      if (childState) onToggleCollapsed(row.node.id);
                    }}
                  >
                    {nodeLoading
                      ? <LoaderCircle className="size-3.5 animate-spin" />
                      : childState
                        ? collapsed ? <ChevronRight className="size-4" /> : <ChevronDown className="size-4" />
                        : <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />}
                  </button>
                </div>
                <div className="min-w-0">
                  <div className={cn("min-h-7 cursor-text overflow-hidden whitespace-nowrap", docsRowBlockClass(kind))}>
                    <span className="inline-flex max-w-full min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap align-middle">
                      {kind === "checkbox" || row.node.display_props?.show_checkbox === true ? (
                        <button
                          type="button"
                          className="inline-grid size-4 shrink-0 place-items-center rounded border align-middle"
                          onClick={(event) => {
                            event.stopPropagation();
                            void onToggleCheckbox(row.node);
                          }}
                        >
                          {row.checked ? <Check className="size-3" /> : null}
                        </button>
                      ) : null}
                      {row.node.node_type === "search" ? (
                        <span className="inline-flex shrink-0 items-center gap-1 rounded border border-emerald-500/40 px-1.5 py-0.5 text-[11px] font-medium leading-4 text-emerald-600 dark:text-emerald-300">
                          <ListFilter className="size-3" />
                          Live query
                        </span>
                      ) : null}
                      {editingNodeId === row.node.id ? (
                        <span className="inline-block min-w-[1ch] max-w-full align-baseline">
                          <span ref={editorHostRef} />
                        </span>
                      ) : (
                        <span
                          data-docs-title-display
                          className="min-w-0 truncate"
                          dangerouslySetInnerHTML={{ __html: renderSearchHighlightedTitle(row.node.title, searchMode ? searchState.find : "", activeSearchOccurrence) }}
                        />
                      )}
                      {visibleTags.map((tag, tagIndex) => (
                        <DocsSupertagChip
                          key={tag.id}
                          tag={tag}
                          onOpen={onOpenTag ? () => onOpenTag(tag) : undefined}
                          onRemove={() => {
                            focusNode(row.node.id, row.node.title.length);
                            void onRemoveTag?.(row.node, tag);
                          }}
                          onNavigate={(direction) => {
                            if (direction === "text" || direction === "previous" && tagIndex === 0) {
                              focusNode(row.node.id, row.node.title.length);
                              return;
                            }
                            const chips = Array.from(
                              editorRootRef.current?.querySelectorAll<HTMLButtonElement>(
                                `[data-docs-block-id="${row.node.id}"] [data-docs-supertag-chip]`,
                              ) ?? [],
                            );
                            const target = direction === "previous" ? chips[tagIndex - 1] : chips[tagIndex + 1];
                            if (target) {
                              target.focus();
                              return;
                            }
                            if (focusFirstDetail(row.node.id)) return;
                            focusAdjacentNode(row.node.id, 1);
                          }}
                        />
                      ))}
                    </span>
                    {taskBinding ? (
                      <button
                        type="button"
                        className={cn(
                          "ml-2 inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] hover:text-foreground",
                          taskBinding.status === "done"
                            ? "border-emerald-500/40 text-emerald-600 dark:text-emerald-300"
                            : taskBinding.status === "doing"
                              ? "border-amber-500/40 text-amber-600 dark:text-amber-300"
                              : "border-sky-500/40 text-sky-600 dark:text-sky-300",
                        )}
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenTask?.(taskBinding.id);
                        }}
                      >
                        <CheckSquare className="size-3" />
                        {taskBinding.status ?? "task"}
                      </button>
                    ) : null}
                    {row.node.body_json?.bookmark && typeof row.node.body_json.bookmark === "object" ? (
                      <div className="mt-2 max-w-xl rounded-md border bg-muted/20 p-3 text-xs">
                        <div className="font-medium">{String((row.node.body_json.bookmark as Record<string, unknown>).title ?? "Bookmark")}</div>
                        <div className="mt-1 text-muted-foreground">{String((row.node.body_json.bookmark as Record<string, unknown>).domain ?? "")}</div>
                      </div>
                    ) : null}
                  </div>
                  {typeof row.node.body_json?.verbatim_content === "string" ? (
                    <VerbatimContentBlock content={row.node.body_json.verbatim_content} />
                  ) : null}
                  {fieldsExpanded && visibleFields.length > 0 ? (
                    <div
                      className="grid gap-0 border-l border-border/60 pl-4"
                      data-testid="docs-block-fields"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {visibleFields.map((field, fieldIndex) => {
                        const suggestion = aiFieldSuggestions.find((item) => item.field.id === field.id);
                        const currentValue = fieldValueToDraft(fieldValuesById.get(field.id));
                        return (
                          <div key={field.id} className="grid min-h-7 min-w-0 grid-cols-[minmax(7rem,auto)_minmax(10rem,1fr)] items-center gap-2 py-0">
                            <span className="inline-flex h-7 items-center gap-1 text-xs leading-7 text-muted-foreground">
                              <span aria-hidden="true">&gt;</span>
                              <span>{field.name}</span>
                            </span>
                             <FieldControl
                              field={field}
                              value={currentValue}
                              nodes={nodes}
                              projects={projects}
                              currentNodeId={row.node.id}
                               onChange={() => {}}
                               onCommit={(value) => void onSaveField(row.node, field, value)}
                               onNavigatePrevious={() => {
                                 const fields = fieldControlsForNode(row.node.id);
                                 if (fieldIndex > 0 && fields[fieldIndex - 1]) fields[fieldIndex - 1].focus();
                                 else focusNode(row.node.id, row.node.title.length);
                               }}
                               onNavigateNext={() => {
                                 const fields = fieldControlsForNode(row.node.id);
                                 if (fields[fieldIndex + 1]) fields[fieldIndex + 1].focus();
                                 else if (focusFirstAttachment(row.node.id)) return;
                                 else focusAdjacentNode(row.node.id, 1);
                               }}
                               onEscape={() => focusNode(row.node.id, row.node.title.length)}
                             />
                            {suggestion && !currentValue ? (
                              <button
                                type="button"
                                data-docs-ai-field-suggestion
                                className="col-start-2 rounded border border-dashed px-2 py-1 text-left text-xs text-muted-foreground hover:bg-accent hover:text-foreground focus:bg-accent"
                                onClick={async () => {
                                  await onSaveField(row.node, field, suggestion.value);
                                  await onSuggestionStatus?.(suggestion.suggestion.id, "accepted");
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Escape") {
                                    event.preventDefault();
                                    void onSuggestionStatus?.(suggestion.suggestion.id, "rejected");
                                    focusNode(row.node.id);
                                    return;
                                  }
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                    event.currentTarget.click();
                                    return;
                                  }
                                  if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
                                  event.preventDefault();
                                  const items = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-docs-ai-field-suggestion]"));
                                  const index = items.indexOf(event.currentTarget);
                                  const next = event.key === "ArrowUp" ? items[index - 1] : items[index + 1];
                                  next?.focus();
                                }}
                              >
                                AI候補: {suggestion.value}
                              </button>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                  {fieldsExpanded && (row.attachments?.length ?? 0) > 0 ? (
                    <div className="grid gap-1 border-l border-border/60 py-1 pl-4" data-testid="docs-block-attachments">
                      {row.attachments?.map((attachment, attachmentIndex) => (
                        <button
                          key={attachment.id}
                          type="button"
                          data-docs-attachment-control
                          data-docs-attachment-id={attachment.id}
                          className="block w-fit max-w-full rounded text-left outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                          aria-label={`${attachment.file_name}。Ctrl+Cでコピー、Deleteで削除`}
                          onClick={(event) => event.stopPropagation()}
                          onKeyDown={(event) => {
                            event.stopPropagation();
                            const controls = attachmentControlsForNode(row.node.id);
                            if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
                              event.preventDefault();
                              const previous = controls[attachmentIndex - 1];
                              if (previous) previous.focus();
                              else if (!focusLastField(row.node.id)) focusNode(row.node.id, row.node.title.length);
                              return;
                            }
                            if (event.key === "ArrowDown" || event.key === "ArrowRight") {
                              event.preventDefault();
                              const next = controls[attachmentIndex + 1];
                              if (next) next.focus();
                              else focusAdjacentNode(row.node.id, 1);
                              return;
                            }
                            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c") {
                              event.preventDefault();
                              void copyAttachment(attachment)
                                .then(() => toast.success("添付ファイルをコピーしました"))
                                .catch(() => toast.error("添付ファイルをコピーできませんでした"));
                              return;
                            }
                            if (event.key === "Backspace" || event.key === "Delete") {
                              event.preventDefault();
                              void Promise.resolve(onDeleteAttachment?.(attachment))
                                .then(() => {
                                  requestAnimationFrame(() => {
                                    const remaining = attachmentControlsForNode(row.node.id);
                                    const target = remaining[Math.min(attachmentIndex, Math.max(0, remaining.length - 1))];
                                    if (target) target.focus();
                                    else if (!focusLastField(row.node.id)) focusNode(row.node.id, row.node.title.length);
                                  });
                                })
                                .catch(() => toast.error("添付ファイルを削除できませんでした"));
                              return;
                            }
                            if (event.key === "Enter") {
                              event.preventDefault();
                              window.open(`/api/docs/attachments/${attachment.id}`, "_blank", "noopener,noreferrer");
                            }
                          }}
                        >
                          {attachment.mime_type?.startsWith("image/") ? (
                            <Image
                              src={`/api/docs/attachments/${attachment.id}`}
                              alt={attachment.file_name}
                              width={480}
                              height={320}
                              unoptimized
                              className="max-h-64 w-auto max-w-full rounded border object-contain"
                            />
                          ) : (
                            <span className="inline-flex h-7 items-center gap-1 text-xs text-primary underline underline-offset-2">
                              <FileText className="size-3.5" />
                              {attachment.file_name}
                            </span>
                          )}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="relative">
                  <button
                    type="button"
                    className="grid size-7 place-items-center rounded opacity-0 hover:bg-accent group-hover:opacity-100"
                    onClick={(event) => {
                      event.stopPropagation();
                      setContextMenuPosition(null);
                      setMenuNodeId(menuNodeId === row.node.id ? null : row.node.id);
                    }}
                  >
                    <MoreVertical className="size-4" />
                  </button>
                  {rowMenu && menuAtPointer && typeof document !== "undefined"
                    ? createPortal(rowMenu, document.body)
                    : rowMenu}
                </div>
              </div>
            </div>
            {belowRowContent != null ? (
              <div style={{ marginLeft: (row.depth + 1) * 24 }}>{belowRowContent}</div>
            ) : null}
            </Fragment>
          );
          rowRenderCacheRef.current.set(row.node.id, { inputs: memoInputs, element });
          return wrapVirtualRow(element);
        })}
        </div>
      </div>
      {hasMoreRows && onLoadMoreRows ? (
        <button
          type="button"
          className="ml-7 mt-1 h-8 rounded px-2 text-left text-xs text-muted-foreground outline-none hover:bg-accent focus:bg-accent focus:ring-1 focus:ring-ring"
          onClick={() => void onLoadMoreRows()}
        >
          続きを読み込む
        </button>
      ) : null}
      {moveDialog ? (
        <div
          className="fixed inset-0 z-[70] grid place-items-start bg-background/45 pt-[16vh] backdrop-blur-[1px]"
          role="dialog"
          aria-modal="true"
          aria-label="別ページへ移動"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setMoveDialog(null);
          }}
        >
          <div className="w-[min(36rem,calc(100vw-2rem))] rounded-lg border bg-popover p-2 shadow-2xl">
            <div className="px-2 pb-2 text-xs text-muted-foreground">
              「{moveDialog.row.node.title}」の移動先ページ
            </div>
            <input
              ref={moveInputRef}
              value={moveDialog.query}
              autoFocus
              placeholder="ページを検索"
              aria-label="移動先ページを検索"
              className="h-9 w-full rounded border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-primary"
              onChange={(event) => setMoveDialog((current) => current ? { ...current, query: event.target.value, loading: true, error: null } : current)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  setMoveDialog(null);
                  return;
                }
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  setMoveDialog((current) => {
                    if (!current || current.items.length === 0) return current;
                    const delta = event.key === "ArrowDown" ? 1 : -1;
                    return { ...current, activeIndex: (current.activeIndex + delta + current.items.length) % current.items.length };
                  });
                  return;
                }
                if (event.key === "Enter") {
                  event.preventDefault();
                  const target = moveDialog.items[Math.min(moveDialog.activeIndex, Math.max(0, moveDialog.items.length - 1))];
                  if (target) void submitMoveToPage(target);
                }
              }}
            />
            <div className="mt-1 grid max-h-72 gap-0.5 overflow-auto" role="listbox" aria-label="移動先ページ">
              {moveDialog.loading ? <div className="px-3 py-2 text-xs text-muted-foreground">検索中…</div> : null}
              {!moveDialog.loading && moveDialog.items.length === 0 ? <div className="px-3 py-2 text-xs text-muted-foreground">該当するページがありません</div> : null}
              {moveDialog.items.map((page, index) => (
                <button
                  key={page.id}
                  type="button"
                  role="option"
                  aria-selected={index === moveDialog.activeIndex}
                  className={cn("rounded px-3 py-2 text-left text-sm", index === moveDialog.activeIndex ? "bg-accent" : "hover:bg-accent/60")}
                  onMouseEnter={() => setMoveDialog((current) => current ? { ...current, activeIndex: index } : current)}
                  onClick={() => void submitMoveToPage(page)}
                >
                  <span className="block truncate">{page.title}</span>
                  {page.breadcrumb.length > 0 ? <span className="block truncate text-[11px] text-muted-foreground">{page.breadcrumb.join(" / ")}</span> : null}
                </button>
              ))}
            </div>
            {moveDialog.error ? <div className="px-2 pt-2 text-xs text-destructive">{moveDialog.error}</div> : null}
          </div>
        </div>
      ) : null}
      {urlChoice ? (
        <div
          data-docs-inline-popup
          className="fixed z-50 flex items-center gap-1 rounded-md border bg-popover p-1 text-xs shadow-lg"
          style={{ left: urlChoice.x, top: urlChoice.y }}
          onMouseDown={(event) => event.preventDefault()}
        >
          <span className="px-2 text-muted-foreground">URL貼り付け</span>
          <Button type="button" size="sm" variant="ghost" onClick={() => void applyUrlChoice("link")}>タイトル付きリンク</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => void applyUrlChoice("bookmark")}>ブックマークカード</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => void applyUrlChoice("plain")}>プレーン</Button>
        </div>
      ) : null}
      {inlineSuggestion ? (
        <div
          data-docs-inline-popup
          className="fixed z-50 grid max-h-60 min-w-56 gap-1 overflow-auto rounded-md border bg-popover p-1 text-xs shadow-lg"
          style={{ left: inlineSuggestion.x, top: inlineSuggestion.y }}
          role="listbox"
          aria-label="インライン候補"
          onMouseDown={(event) => event.preventDefault()}
        >
          {(inlineSuggestion.kind === "ref"
              ? nodes
                 .filter((node) => !node.archived_at && node.title.trim().length > 0)
                 .map((node) => {
                   const query = inlineSuggestion.query.toLowerCase();
                   const baseTitle = node.title;
                  const titleHit = (node.title || "").toLowerCase().includes(query);
                  const aliasHit = (node.aliases ?? []).find((alias) => alias.toLowerCase().includes(query));
                  if (!titleHit && !aliasHit) return null;
                  // title でヒットしない alias 一致は「タイトル（エイリアス名）」で併記する。
                  const label = !titleHit && aliasHit ? `${baseTitle}（${aliasHit}）` : baseTitle;
                  return { id: node.id, label, value: `[[node:${node.id}|${baseTitle}]]` };
                })
                .filter((item): item is { id: string; label: string; value: string } => item !== null)
                .slice(0, 8)
            : inlineSuggestion.kind === "user"
              ? users
                  .filter((user) => `${user.display_name ?? ""} ${user.username}`.toLowerCase().includes(inlineSuggestion.query.toLowerCase()))
                  .slice(0, 8)
                  .map((user) => {
                    const label = user.display_name || user.username;
                    return { id: user.id, label, value: `[[user:${user.id}|${label}]] ` };
                  })
            : inlineSuggestion.kind === "mention"
               ? [
                   ...nodes
                     .filter((node) => !node.archived_at && node.title.trim().length > 0)
                     .map((node) => {
                       const query = inlineSuggestion.query.toLowerCase();
                       const baseTitle = node.title;
                      const titleHit = (node.title || "").toLowerCase().includes(query);
                      const aliasHit = (node.aliases ?? []).find((alias) => alias.toLowerCase().includes(query));
                      if (!titleHit && !aliasHit) return null;
                      const label = !titleHit && aliasHit ? `📄 ${baseTitle}（${aliasHit}）` : `📄 ${baseTitle}`;
                      return { id: `n:${node.id}`, label, value: `[[node:${node.id}|${baseTitle}]] ` };
                    })
                    .filter((item): item is { id: string; label: string; value: string } => item !== null)
                    .slice(0, 6),
                  ...users
                    .filter((user) => `${user.display_name ?? ""} ${user.username}`.toLowerCase().includes(inlineSuggestion.query.toLowerCase()))
                    .slice(0, 4)
                    .map((user) => {
                      const label = user.display_name || user.username;
                      return { id: `u:${user.id}`, label: `👤 ${label}`, value: `[[user:${user.id}|${label}]] ` };
                    }),
                ]
              : inlineSuggestion.kind === "task"
                ? taskCandidates.map((task) => ({
                    id: task.id,
                    label: `☑ ${task.title}`,
                    value: `[[task:${task.id}|${task.title}]] `,
                  }))
              : []
          ).map((item, index) => (
            <button
              type="button"
              key={item.id}
              role="option"
              aria-selected={index === inlineIndex}
              className={cn("rounded px-2 py-1.5 text-left hover:bg-accent", index === inlineIndex && "bg-accent")}
              onMouseEnter={() => setInlineIndex(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                void applyInlineSuggestion(inlineSuggestion, item.value);
              }}
            >
              {item.label}
            </button>
          ))}
          {inlineSuggestion.kind === "field" ? (() => {
            const row = rows.find((item) => item.node.id === inlineSuggestion.nodeId);
            const candidates = row
              ? (fieldCandidatesForRow?.(row) ?? row.fields ?? [])
                  .filter((field) => field.name.toLowerCase().includes(inlineSuggestion.query.toLowerCase()))
                  .slice(0, 8)
              : [];
            return (
              <>
                {candidates.map((field, index) => (
                  <button
                    type="button"
                    key={field.id}
                    role="option"
                    aria-selected={index === inlineIndex}
                    className={cn("rounded px-2 py-1.5 text-left hover:bg-accent", index === inlineIndex && "bg-accent")}
                    onMouseEnter={() => setInlineIndex(index)}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      const value = `${field.name}: `;
                      const view = editorViewRef.current;
                      fieldCommandRef.current = {
                        nodeId: inlineSuggestion.nodeId,
                        fieldName: field.name,
                        prefix: (view?.state.sliceDoc(0, inlineSuggestion.from) ?? "") + value,
                      };
                      void applyInlineSuggestion(inlineSuggestion, value);
                    }}
                  >
                    {field.name}
                  </button>
                ))}
                {candidates.length === 0 && row && inlineSuggestion.query.trim() && onCreateFieldCandidate ? (
                  <button
                    type="button"
                    className="rounded px-2 py-1.5 text-left hover:bg-accent"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      void Promise.resolve(onCreateFieldCandidate(row, inlineSuggestion.query.trim())).then((created) => {
                        if (!created) return;
                        const fieldName = inlineSuggestion.query.trim();
                        const value = `${fieldName}: `;
                        const view = editorViewRef.current;
                        fieldCommandRef.current = {
                          nodeId: inlineSuggestion.nodeId,
                          fieldName,
                          prefix: (view?.state.sliceDoc(0, inlineSuggestion.from) ?? "") + value,
                        };
                        void applyInlineSuggestion(inlineSuggestion, value);
                      });
                    }}
                  >
                    Field「{inlineSuggestion.query.trim()}」を作成
                  </button>
                ) : null}
              </>
            );
          })() : null}
          {inlineSuggestion.kind === "tag" ? (
            (Array.from(new Map(supertags.map((tag) => [tag.id, tag])).values())
              .filter((tag) => tag.name.toLowerCase().includes(inlineSuggestion.query.toLowerCase()))
              .slice(0, 8)
            ).map((tag, index) => (
              <button
                type="button"
                key={tag.id}
                role="option"
                aria-selected={index === inlineIndex}
                className={cn("rounded px-2 py-1.5 text-left hover:bg-accent", index === inlineIndex && "bg-accent")}
                onMouseEnter={() => setInlineIndex(index)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  void applyInlineSuggestion(inlineSuggestion, "", tag);
                }}
              >
                #{tag.name}
              </button>
            ))
          ) : null}
        </div>
      ) : null}
      {slashCommand ? (() => {
        const commands = filterSlashCommands(slashCommand.query);
        const activeIndex = commands.length > 0 ? Math.min(slashIndex, commands.length - 1) : -1;
        return (
          <div
            ref={slashMenuRef}
            data-docs-inline-popup
            className="fixed z-50 grid max-h-72 min-w-52 gap-0.5 overflow-auto rounded-md border bg-popover p-1 text-xs shadow-lg"
            style={{ left: slashCommand.x, top: slashCommand.y }}
            role="listbox"
            onMouseDown={(event) => event.preventDefault()}
          >
            {commands.length === 0 ? (
              <div className="px-2 py-1.5 text-muted-foreground">該当なし</div>
            ) : (
              commands.map((command, index) => (
                <button
                  type="button"
                  key={command.id}
                  role="option"
                  aria-selected={index === activeIndex}
                  className={cn(
                    "rounded px-2 py-1.5 text-left hover:bg-accent",
                    index === activeIndex && "bg-accent",
                  )}
                  onMouseEnter={() => setSlashIndex(index)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    executeSlashCommand(command.id);
                  }}
                >
                  {command.label}
                </button>
              ))
            )}
          </div>
        );
      })() : null}
      {/*
        検索置換
      </Button>
      */}
    </div>
  );
}

function MenuButton({ icon: Icon, label, mnemonic, onClick }: { icon: LucideIcon; label: string; mnemonic: string; onClick: () => void }) {
  return (
    <MenuMnemonicButton type="button" mnemonic={mnemonic} className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent" onClick={onClick}>
      <Icon className="size-3.5" />
      {label}
    </MenuMnemonicButton>
  );
}
