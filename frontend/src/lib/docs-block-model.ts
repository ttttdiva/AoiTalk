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
