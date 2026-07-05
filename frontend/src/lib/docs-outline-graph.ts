export type KnowledgeDisplayNodeLink = {
  id: string;
  parentId: string | null;
};

export type KnowledgeDisplayPlacementLink = {
  nodeId: string;
  parentNodeId: string;
};

export function collectKnowledgeDisplayDescendantIds(
  nodes: KnowledgeDisplayNodeLink[],
  placements: KnowledgeDisplayPlacementLink[],
  nodeId: string,
): string[] {
  const childrenByParent = new Map<string, string[]>();
  const append = (parentId: string | null | undefined, childId: string) => {
    if (!parentId) return;
    const children = childrenByParent.get(parentId) ?? [];
    children.push(childId);
    childrenByParent.set(parentId, children);
  };

  for (const node of nodes) append(node.parentId, node.id);
  for (const placement of placements) append(placement.parentNodeId, placement.nodeId);

  const descendants: string[] = [];
  const seen = new Set([nodeId]);
  const queue = [...(childrenByParent.get(nodeId) ?? [])];
  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || seen.has(current)) continue;
    seen.add(current);
    descendants.push(current);
    queue.push(...(childrenByParent.get(current) ?? []));
  }
  return descendants;
}
