import type { ExplorerBookmark } from "@/lib/explorer-api";
import { normalizeMenuMnemonic } from "@/lib/menu-mnemonics";

export const BOOKMARK_FOLDER_PATH_PREFIX = "aoitalk-bookmark-folder:";

export type ExplorerBookmarkTreeNode = {
  item: ExplorerBookmark;
  children: ExplorerBookmarkTreeNode[];
};

export function isBookmarkFolderPath(path: string): boolean {
  return path.startsWith(BOOKMARK_FOLDER_PATH_PREFIX);
}

export function isExplorerBookmarkFolder(item: ExplorerBookmark): boolean {
  return item.kind === "folder" || isBookmarkFolderPath(item.path);
}

export function bookmarkNameMnemonic(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return null;
  return normalizeMenuMnemonic(Array.from(trimmed)[0]);
}

function bookmarkSortOrder(item: ExplorerBookmark): number {
  return item.sort_order ?? Number.MAX_SAFE_INTEGER;
}

function sortBookmarkNodes(nodes: ExplorerBookmarkTreeNode[]): ExplorerBookmarkTreeNode[] {
  return [...nodes].sort(
    (a, b) => bookmarkSortOrder(a.item) - bookmarkSortOrder(b.item),
  );
}

/**
 * Build a bookmark tree from a flat list. Orphaned children attach to root.
 * Cycles and missing parents do not cause infinite loops.
 */
export function buildExplorerBookmarkTree(
  bookmarks: ExplorerBookmark[],
): ExplorerBookmarkTreeNode[] {
  const byId = new Map<string, ExplorerBookmark>();
  for (const item of bookmarks) {
    if (item.id) byId.set(item.id, item);
  }

  const childrenByParent = new Map<string | null, ExplorerBookmark[]>();
  const pushChild = (parentId: string | null, item: ExplorerBookmark) => {
    const bucket = childrenByParent.get(parentId);
    if (bucket) bucket.push(item);
    else childrenByParent.set(parentId, [item]);
  };

  for (const item of bookmarks) {
    const parentId = item.parent_id ?? null;
    if (parentId && !byId.has(parentId)) {
      pushChild(null, item);
      continue;
    }
    pushChild(parentId, item);
  }

  const visiting = new Set<string>();
  const buildLevel = (parentId: string | null): ExplorerBookmarkTreeNode[] => {
    const items = childrenByParent.get(parentId) ?? [];
    const nodes: ExplorerBookmarkTreeNode[] = [];
    for (const item of items) {
      const id = item.id;
      if (id && visiting.has(id)) continue;
      if (id) visiting.add(id);
      const children = id ? buildLevel(id) : [];
      if (id) visiting.delete(id);
      nodes.push({ item, children: sortBookmarkNodes(children) });
    }
    return sortBookmarkNodes(nodes);
  };

  return buildLevel(null);
}

export function flattenExplorerBookmarkTree(
  nodes: ExplorerBookmarkTreeNode[],
  expandedFolderIds: ReadonlySet<string>,
  depth = 0,
): Array<{ node: ExplorerBookmarkTreeNode; depth: number }> {
  const rows: Array<{ node: ExplorerBookmarkTreeNode; depth: number }> = [];
  for (const node of nodes) {
    rows.push({ node, depth });
    const folderId = node.item.id ?? node.item.path;
    if (
      isExplorerBookmarkFolder(node.item) &&
      expandedFolderIds.has(folderId) &&
      node.children.length > 0
    ) {
      rows.push(
        ...flattenExplorerBookmarkTree(node.children, expandedFolderIds, depth + 1),
      );
    }
  }
  return rows;
}

export function collectDefaultExpandedFolderIds(
  bookmarks: ExplorerBookmark[],
): Set<string> {
  const ids = new Set<string>();
  for (const item of bookmarks) {
    if (isExplorerBookmarkFolder(item)) {
      ids.add(item.id ?? item.path);
    }
  }
  return ids;
}

export function isBookmarkDescendantOf(
  bookmarks: ExplorerBookmark[],
  descendantId: string,
  ancestorId: string,
): boolean {
  if (descendantId === ancestorId) return true;
  const byId = new Map<string, ExplorerBookmark>();
  for (const item of bookmarks) {
    if (item.id) byId.set(item.id, item);
  }
  let current = byId.get(descendantId);
  const visited = new Set<string>();
  while (current?.parent_id) {
    if (current.parent_id === ancestorId) return true;
    if (visited.has(current.parent_id)) break;
    visited.add(current.parent_id);
    current = byId.get(current.parent_id);
  }
  return false;
}

/** Count all descendants of a folder bookmark. Does not include the folder itself. */
export function countBookmarkDescendants(
  bookmarks: ExplorerBookmark[],
  ancestorId: string,
): number {
  return bookmarks.filter(
    (entry) =>
      entry.id &&
      entry.id !== ancestorId &&
      isBookmarkDescendantOf(bookmarks, entry.id, ancestorId),
  ).length;
}
