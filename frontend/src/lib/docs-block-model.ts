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

export function mergeBlockTitles(previous: string, next: string) {
  if (!previous) return next;
  if (!next) return previous;
  return `${previous}${next}`;
}

export function parseIndentedMarkdownBlocks(text: string): MarkdownBlock[] {
  return text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((rawLine) => {
      const indent = rawLine.match(/^\s*/)?.[0] ?? "";
      const depth = Math.floor(indent.replace(/\t/g, "  ").length / 2);
      const withoutIndent = rawLine.trimStart().replace(/^[-*]\s+/, "");
      const shortcut = titleForMarkdownShortcut(withoutIndent);
      return {
        title: shortcut.title.trim(),
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
    .map(({ depth, node }) => {
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
