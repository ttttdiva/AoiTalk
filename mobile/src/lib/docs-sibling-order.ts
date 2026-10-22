import type { DocsNode } from "../types/api";

/** Stable visible outline order, including legacy/equal numeric positions. */
export function compareDocsOutlineOrder(a: DocsNode, b: DocsNode): number {
  const order = (a.sort_order ?? 0) - (b.sort_order ?? 0);
  if (order !== 0) return order;
  return (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id);
}
