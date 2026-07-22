import type { DocsNode } from "../../types/api";

export type OutlineRow = {
  node: DocsNode;
  depth: number;
  siblingIndex: number;
  hasChildren: boolean;
};

export function sortOutlineNodes(nodes: DocsNode[]): DocsNode[] {
  return [...nodes].sort((a, b) => {
    const order = (a.sort_order ?? 0) - (b.sort_order ?? 0);
    if (order !== 0) return order;
    return (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id);
  });
}

export function buildChildrenMap(nodes: DocsNode[]): Map<string, DocsNode[]> {
  const result = new Map<string, DocsNode[]>();
  for (const node of nodes) {
    if (!node.parent_id) continue;
    const current = result.get(node.parent_id) ?? [];
    current.push(node);
    result.set(node.parent_id, current);
  }
  for (const [parentId, children] of result) {
    result.set(parentId, sortOutlineNodes(children));
  }
  return result;
}

export function flattenVisibleOutline(
  nodes: DocsNode[],
  rootNodeId: string,
  collapsedIds: ReadonlySet<string>,
  showArchived = false,
): OutlineRow[] {
  const visibleNodes = showArchived ? nodes : nodes.filter((node) => !node.archived_at);
  const childrenMap = buildChildrenMap(visibleNodes);
  const rows: OutlineRow[] = [];
  const visited = new Set<string>();

  const visit = (parentId: string, depth: number) => {
    const children = childrenMap.get(parentId) ?? [];
    children.forEach((node, siblingIndex) => {
      if (visited.has(node.id)) return;
      visited.add(node.id);
      const hasChildren = (childrenMap.get(node.id)?.length ?? 0) > 0;
      rows.push({ node, depth, siblingIndex, hasChildren });
      if (hasChildren && !collapsedIds.has(node.id)) visit(node.id, depth + 1);
    });
  };

  visit(rootNodeId, 0);
  return rows;
}

export function calculateInsertSortOrder(
  current: DocsNode,
  nextSibling: DocsNode | null,
): number | null {
  const currentOrder = current.sort_order ?? 0;
  if (!nextSibling) return currentOrder + 1;
  const nextOrder = nextSibling.sort_order ?? currentOrder + 1;
  const midpoint = currentOrder + (nextOrder - currentOrder) / 2;
  return Number.isFinite(midpoint) && midpoint > currentOrder && midpoint < nextOrder
    ? midpoint
    : null;
}
