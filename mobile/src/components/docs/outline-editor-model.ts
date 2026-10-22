import type { DocsNode } from "../../types/api";
import { compareDocsOutlineOrder } from "../../lib/docs-sibling-order";

export type OutlineRow = {
  node: DocsNode;
  depth: number;
  siblingIndex: number;
  hasChildren: boolean;
};

export function sortOutlineNodes(nodes: DocsNode[]): DocsNode[] {
  return [...nodes].sort(compareDocsOutlineOrder);
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
  expandedIds: ReadonlySet<string>,
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
      if (hasChildren && expandedIds.has(node.id)) visit(node.id, depth + 1);
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
  if (!nextSibling) {
    const appended = currentOrder + 1;
    return Number.isFinite(appended) && appended > currentOrder ? appended : null;
  }
  const nextOrder = nextSibling.sort_order ?? currentOrder + 1;
  const midpoint = currentOrder + (nextOrder - currentOrder) / 2;
  return Number.isFinite(midpoint) && midpoint > currentOrder && midpoint < nextOrder
    ? midpoint
    : null;
}

/** Merge a complete saved snapshot with rows still waiting for local creation. */
export function placePendingOutlineRows(
  nodes: DocsNode[],
  pending: readonly { node: DocsNode; afterId: string }[],
): DocsNode[] {
  const pendingIds = new Set(pending.map((entry) => entry.node.id));
  let result = nodes.filter((node) => !pendingIds.has(node.id));
  for (const entry of pending) {
    const current = nodes.find((node) => node.id === entry.node.id) ?? entry.node;
    let anchor = result.find((node) => node.id === entry.afterId);
    if (!anchor) { result.push(current); continue; }
    let siblings = sortOutlineNodes(result.filter((node) => node.parent_id === anchor!.parent_id && !node.archived_at));
    let index = siblings.findIndex((node) => node.id === anchor!.id);
    let order = calculateInsertSortOrder(anchor, siblings[index + 1] ?? null);
    if (order === null) {
      const orders = new Map(siblings.map((node, i) => [node.id, (i + 1) * 1024]));
      result = result.map((node) => orders.has(node.id) ? { ...node, sort_order: orders.get(node.id)! } : node);
      anchor = result.find((node) => node.id === entry.afterId)!;
      siblings = sortOutlineNodes(result.filter((node) => node.parent_id === anchor!.parent_id && !node.archived_at));
      index = siblings.findIndex((node) => node.id === anchor!.id);
      order = calculateInsertSortOrder(anchor, siblings[index + 1] ?? null)!;
    }
    result.push({ ...current, sort_order: order });
  }
  return result;
}
