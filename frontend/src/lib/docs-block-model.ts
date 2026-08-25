export type SortableNode = {
  id: string;
  sort_order: number;
  created_at?: string | null;
};

export function compareNodesByPosition<T extends SortableNode>(a: T, b: T): number {
  if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
  const aCreated = a.created_at ? Date.parse(a.created_at) : 0;
  const bCreated = b.created_at ? Date.parse(b.created_at) : 0;
  if (aCreated !== bCreated) return aCreated - bCreated;
  return a.id.localeCompare(b.id);
}

export function sortNodesByPosition<T extends SortableNode>(nodes: T[]): T[] {
  return [...nodes].sort(compareNodesByPosition);
}

export function midpointSortOrder(previous?: number | null, next?: number | null): number {
  if (previous === undefined || previous === null) return (next ?? 2) - 1;
  if (next === undefined || next === null) return previous + 1;
  return previous + (next - previous) / 2;
}

export function needsSortRebalance(sortedOrders: number[]): boolean {
  for (let index = 1; index < sortedOrders.length; index += 1) {
    if (Math.abs(sortedOrders[index] - sortedOrders[index - 1]) < 0.000001) return true;
  }
  return false;
}

export type DocsBlockKind =
  | "paragraph"
  | "heading_1"
  | "heading_2"
  | "heading_3"
  | "checkbox"
  | "quote"
  | "search";

/**
 * A block whose body is independent from the outline title.  These are used
 * for imported Markdown/code (including multiline ClipIngest content).  The
 * title remains the human-facing label/body_text mirror while `content` is
 * the editable raw payload.
 */
export type DocsContentBlockType = "markdown" | "code";

export type DocsTypedContentBlock = {
  block_type: DocsContentBlockType;
  content: string;
  label: string;
  clip_ingest?: Record<string, unknown>;
};

export type DocsBlockSnapshot = {
  id: string;
  parent_id: string | null;
  title: string;
  description?: string | null;
  body_text?: string | null;
  body_json?: Record<string, unknown> | null;
  node_type?: string | null;
  sort_order: number;
};

export type MarkdownBlock = {
  title: string;
  depth: number;
  kind: DocsBlockKind;
  checked?: boolean;
  /** A deliberately pasted/created empty paragraph, not an arbitrary blank node. */
  blank?: boolean;
};

export type BlockHistoryPatch =
  | { type: "update"; id: string; before: DocsBlockSnapshot; after: DocsBlockSnapshot }
  | { type: "create"; node: DocsBlockSnapshot }
  | { type: "archive"; node: DocsBlockSnapshot };

export type BlockHistoryEntry = {
  label: string;
  patches: BlockHistoryPatch[];
};

function blockJsonRecord(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

/**
 * Return whether a node is the canonical persisted representation of an
 * explicitly-created empty outline paragraph.
 *
 * Empty titles are intentionally not accepted on their own.  The node type,
 * doc-block format, paragraph block type, and explicit marker must all agree
 * so that legacy/malformed blank KnowledgeNodes remain rejected and hidden.
 */
export function isExplicitBlankParagraph(
  title: string | null | undefined,
  bodyJson: unknown,
  nodeType: string | null | undefined,
): boolean {
  if (title !== "" || nodeType !== "node") return false;
  const body = blockJsonRecord(bodyJson);
  return body.format === "doc_block"
    && body.block_type === "paragraph"
    && body.blank === true;
}

/**
 * Build the canonical body metadata for an explicitly empty paragraph.
 * Existing metadata is retained, but the block identity and marker are
 * normalized so callers cannot accidentally persist another block kind.
 */
export function blankParagraphBodyJson(existingBodyJson: unknown): Record<string, unknown> {
  return {
    ...blockJsonRecord(existingBodyJson),
    format: "doc_block",
    block_type: "paragraph",
    blank: true,
  };
}

/**
 * Remove only the explicit blank marker while retaining all other body
 * metadata.  This is used when a blank paragraph receives non-empty text.
 */
export function clearBlankParagraphMarker(existingBodyJson: unknown): Record<string, unknown> {
  const next = { ...blockJsonRecord(existingBodyJson) };
  delete next.blank;
  return next;
}

export function normalizeDocsBlockContent(value: string) {
  return value.replace(/\r\n?/g, "\n");
}

/**
 * Return the content that should be shown for a typed Docs block.
 *
 * Typed block content remains lossless in the node body.  This helper only
 * changes the read-only projection for code blocks whose entire meaningful
 * body is wrapped in one complete Markdown-style fence pair.
 */
export function docsTypedContentDisplayContent(
  block: Pick<DocsTypedContentBlock, "block_type" | "content">,
): string {
  const content = normalizeDocsBlockContent(block.content);
  if (block.block_type !== "code") return content;

  const lines = content.split("\n");
  let first = 0;
  while (first < lines.length && (lines[first] ?? "").trim() === "") first += 1;

  let last = lines.length - 1;
  while (last > first && (lines[last] ?? "").trim() === "") last -= 1;

  if (first >= last) return content;

  const opening = (lines[first] ?? "").match(/^[ \t]{0,3}(`{3,}|~{3,})(.*)$/);
  if (!opening) return content;

  const fence = opening[1] ?? "";
  const fenceChar = fence[0];
  if (!fenceChar) return content;

  const closing = (lines[last] ?? "").match(/^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$/);
  const closingFence = closing?.[1] ?? "";
  if (
    !closingFence ||
    closingFence[0] !== fenceChar ||
    closingFence.length < fence.length
  ) {
    return content;
  }

  return lines.slice(first + 1, last).join("\n");
}

/**
 * Read the editable typed-content contract from a node body.  Legacy
 * `verbatim_*` keys intentionally are not accepted here: they are migration
 * input only and must never become a readonly visible-content path again.
 */
export function docsTypedContentBlock(
  bodyJson: Record<string, unknown> | null | undefined,
  fallbackLabel = "本文",
): DocsTypedContentBlock | null {
  const body = blockJsonRecord(bodyJson);
  if (body.format !== "doc_block") return null;
  const blockType = body.block_type;
  if (blockType !== "markdown" && blockType !== "code") return null;
  if (typeof body.content !== "string") return null;
  const label = typeof body.label === "string" && body.label.trim()
    ? body.label.trim()
    : fallbackLabel.trim() || "本文";
  const provenance = body.clip_ingest;
  return {
    block_type: blockType,
    content: normalizeDocsBlockContent(body.content),
    label,
    ...(provenance && typeof provenance === "object" && !Array.isArray(provenance)
      ? { clip_ingest: provenance as Record<string, unknown> }
      : {}),
  };
}

export function docsTypedContentBodyJson(
  blockType: DocsContentBlockType,
  content: string,
  label: string,
  extra: Record<string, unknown> = {},
) {
  const safeExtra = Object.fromEntries(
    Object.entries(extra).filter(([key]) => key !== "verbatim_blocks" && key !== "verbatim_content"),
  );
  return {
    ...safeExtra,
    format: "doc_block",
    block_type: blockType,
    content: normalizeDocsBlockContent(content),
    label: label.trim() || "本文",
  } satisfies Record<string, unknown>;
}

export function docsBlockKind(node: Pick<DocsBlockSnapshot, "body_json" | "node_type">): DocsBlockKind {
  if (node.node_type === "search") return "search";
  const bodyJson = blockJsonRecord(node.body_json);
  const rawKind = bodyJson.block_type ?? bodyJson.kind ?? bodyJson.type;
  if (
    rawKind === "heading_1" ||
    rawKind === "heading_2" ||
    rawKind === "heading_3" ||
    rawKind === "checkbox" ||
    rawKind === "quote"
  ) {
    return rawKind;
  }
  return "paragraph";
}

export function blockJsonForKind(kind: DocsBlockKind, checked = false): Record<string, unknown> {
  return {
    format: "doc_block",
    block_type: kind === "search" ? "paragraph" : kind,
    ...(kind === "checkbox" ? { checked } : {}),
  };
}

export function titleForMarkdownShortcut(input: string): {
  title: string;
  kind: DocsBlockKind;
  checked?: boolean;
  matched: boolean;
} {
  const value = input.replace(/\r\n?/g, "\n").split("\n")[0] ?? "";
  const heading = value.match(/^(#{1,3})\s+(.+)$/);
  if (heading) {
    return {
      title: heading[2].trim(),
      kind: `heading_${heading[1].length}` as DocsBlockKind,
      matched: true,
    };
  }
  const checkbox = value.match(/^\[( |x|X)?\]\s+(.+)$/);
  if (checkbox) {
    return {
      title: checkbox[2].trim(),
      kind: "checkbox",
      checked: checkbox[1]?.toLowerCase() === "x",
      matched: true,
    };
  }
  const quote = value.match(/^>\s+(.+)$/);
  if (quote) return { title: quote[1].trim(), kind: "quote", matched: true };
  return { title: value, kind: "paragraph", matched: false };
}

/**
 * 入力中に変換できる、本文をまだ持たない Markdown prefix を判定する。
 * 完成形の判定(titleForMarkdownShortcut)とは別にして、`#tag` のような
 * 空白なしの入力や IME 中の未確定文字を誤変換しない境界を保つ。
 */
export function markdownShortcutPrefixForTitle(input: string): {
  kind: DocsBlockKind;
  checked?: boolean;
  prefixLength: number;
} | null {
  const value = input.replace(/\r\n?/g, "\n").split("\n")[0] ?? "";
  const heading = value.match(/^(#{1,3})\s+$/);
  if (heading) {
    return {
      kind: `heading_${heading[1].length}` as DocsBlockKind,
      prefixLength: heading[0].length,
    };
  }
  const checkbox = value.match(/^\[( |x|X)?\]\s+$/);
  if (checkbox) {
    return {
      kind: "checkbox",
      checked: checkbox[1]?.toLowerCase() === "x",
      prefixLength: checkbox[0].length,
    };
  }
  const quote = value.match(/^>\s+$/);
  if (quote) return { kind: "quote", prefixLength: quote[0].length };
  return null;
}

export function markdownShortcutPatchForTitle(
  input: string,
  currentKind: DocsBlockKind,
): { title: string; kind: DocsBlockKind; checked?: boolean } | null {
  const shortcut = titleForMarkdownShortcut(input);
  if (!shortcut.matched) return null;
  if (shortcut.kind === currentKind && shortcut.kind !== "checkbox") return { title: shortcut.title, kind: shortcut.kind };
  return { title: shortcut.title, kind: shortcut.kind, checked: shortcut.checked };
}

export function splitBlockTitle(title: string, cursor: number) {
  const safeCursor = Math.max(0, Math.min(cursor, title.length));
  return {
    before: title.slice(0, safeCursor),
    after: title.slice(safeCursor),
  };
}

/**
 * 空白だけの入力は、明示的な空paragraphメタデータがない限り Docs の
 * 保存対象となる KnowledgeNode ではない。trim() をここへ集約し、呼び出し側が
 * 空文字だけを特別扱いしないようにする。
 */
export function hasMeaningfulBlockTitle(title: string | null | undefined): title is string {
  return typeof title === "string" && title.trim().length > 0;
}

export function mergeBlockTitles(previous: string, next: string) {
  if (!previous) return next;
  if (!next) return previous;
  return `${previous}${next}`;
}

export function parseIndentedMarkdownBlocks(text: string): MarkdownBlock[] {
  return text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((rawLine): MarkdownBlock => {
      const indent = rawLine.match(/^\s*/)?.[0] ?? "";
      const depth = Math.floor(indent.replace(/\t/g, "  ").length / 2);
      const withoutIndent = rawLine.trimStart().replace(/^[-*]\s+/, "");
      const shortcut = titleForMarkdownShortcut(withoutIndent);
      const title = shortcut.title.trim();
      // 明示的な空行は通常の paragraph block として順序・深さを保持する。
      // body metadata が付かない限り legacy blank node を有効化することはない。
      if (!hasMeaningfulBlockTitle(title)) {
        return { title: "", depth, kind: "paragraph", blank: true };
      }
      return {
        title,
        depth,
        kind: shortcut.kind,
        checked: shortcut.checked,
      };
    });
}

export function serializeBlocksToIndentedMarkdown(
  rows: Array<{ depth: number; node: Pick<DocsBlockSnapshot, "title" | "body_json" | "node_type"> }>,
) {
  return rows
    .filter(({ node }) =>
      hasMeaningfulBlockTitle(node.title)
      || isExplicitBlankParagraph(node.title, node.body_json, node.node_type),
    )
    .map(({ depth, node }) => {
      if (isExplicitBlankParagraph(node.title, node.body_json, node.node_type)) {
        return "  ".repeat(depth);
      }
      const kind = docsBlockKind(node);
      const prefix =
        kind === "heading_1"
          ? "# "
          : kind === "heading_2"
            ? "## "
            : kind === "heading_3"
              ? "### "
              : kind === "checkbox"
                ? "[] "
                : "";
      return `${"  ".repeat(depth)}${prefix}${node.title}`;
    })
    .join("\n");
}

export function invertHistoryEntry(entry: BlockHistoryEntry): BlockHistoryEntry {
  return {
    label: `undo:${entry.label}`,
    patches: [...entry.patches].reverse().map((patch) => {
      if (patch.type === "update") return { type: "update", id: patch.id, before: patch.after, after: patch.before };
      if (patch.type === "create") return { type: "archive", node: patch.node };
      return { type: "create", node: patch.node };
    }),
  };
}
