export interface NodeBox { key: string; x: number; y: number; width: number; height: number }
export interface Rect { x: number; y: number; width: number; height: number }

function finite(value: number, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new RangeError(`${name} must be finite`);
  return value;
}

function nonNegative(value: number, name: string): number {
  if (finite(value, name) < 0) throw new RangeError(`${name} must be nonnegative`);
  return value;
}

function checkNode(node: NodeBox): void {
  finite(node.x, `${node.key}.x`);
  finite(node.y, `${node.key}.y`);
  nonNegative(node.width, `${node.key}.width`);
  nonNegative(node.height, `${node.key}.height`);
}

export function boundsForGroup(
  memberKeys: readonly string[],
  nodes: readonly NodeBox[],
  padding = 24,
  titleHeight = 24,
): Rect | null {
  nonNegative(padding, "padding");
  nonNegative(titleHeight, "titleHeight");
  const members = new Set(memberKeys);
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (const node of nodes) {
    if (!members.has(node.key)) continue;
    checkNode(node);
    left = Math.min(left, node.x);
    top = Math.min(top, node.y);
    right = Math.max(right, node.x + node.width);
    bottom = Math.max(bottom, node.y + node.height);
  }
  if (left === Infinity) return null;
  const x = left - padding;
  const y = top - padding - titleHeight;
  return { x, y, width: right + padding - x, height: bottom + padding - y };
}

export function moveGroupMembers(
  memberKeys: readonly string[],
  nodes: readonly NodeBox[],
  dx: number,
  dy: number,
): NodeBox[] {
  finite(dx, "dx");
  finite(dy, "dy");
  const members = new Set(memberKeys);
  for (const node of nodes) if (members.has(node.key)) checkNode(node);
  return nodes.map((node) => (members.has(node.key) ? { ...node, x: node.x + dx, y: node.y + dy } : node));
}