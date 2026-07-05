import type { DocsNode, DocsNodePlacement } from "@/components/docs/types";

export type OutlineLine = {
  nodeId: string | null;
  placementId?: string | null;
  depth: number;
  text: string;
  pending?: boolean;
};

export type SerializedOutline = {
  text: string;
  lineMap: OutlineLine[];
};

export type OutlineOperation =
  | { type: "patch_title"; nodeId: string; title: string }
  | {
      type: "create_node";
      pendingLine: number;
      parentId: string | null;
      afterSiblingId: string | null;
      title: string;
      depth: number;
    }
  | { type: "archive_node"; nodeId: string; line: number; title: string }
  | {
      type: "move_node";
      nodeId: string;
      parentId: string | null;
      afterSiblingId: string | null;
      depth: number;
    }
  | { type: "restore_node"; nodeId: string; line: number; title: string };

export type Tombstone = {
  nodeId: string;
  title: string;
  parentId: string | null;
  afterSiblingId: string | null;
  depth: number;
};

type ReconcileOptions = {
  before: SerializedOutline;
  afterText: string;
  tombstones?: Tombstone[];
};

function normalizeLines(text: string) {
  return text.replace(/\r\n?/g, "\n").split("\n");
}

function lineDepth(line: string) {
  const match = line.match(/^\t*/);
  return match ? match[0].length : 0;
}

function lineTitle(line: string) {
  return line.replace(/^\t*/, "");
}

function nodeChildrenByParent(nodes: DocsNode[]) {
  const children = new Map<string | null, DocsNode[]>();
  for (const node of nodes) {
    if (node.archived_at) continue;
    const key = node.parent_id ?? null;
    const list = children.get(key) ?? [];
    list.push(node);
    children.set(key, list);
  }
  for (const list of children.values()) {
    list.sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0) || (a.created_at ?? "").localeCompare(b.created_at ?? ""));
  }
  return children;
}

export function serializeOutline(nodes: DocsNode[], rootId: string | null = null): SerializedOutline {
  const children = nodeChildrenByParent(nodes);
  const lines: OutlineLine[] = [];
  const visit = (parentId: string | null, depth: number) => {
    for (const node of children.get(parentId) ?? []) {
      lines.push({ nodeId: node.id, depth, text: node.title });
      visit(node.id, depth + 1);
    }
  };
  visit(rootId, 0);
  return {
    text: lines.map((line) => `${"\t".repeat(line.depth)}${line.text}`).join("\n"),
    lineMap: lines,
  };
}

export function serializePlacementOutline(
  nodes: DocsNode[],
  placements: DocsNodePlacement[],
  parentNodeId: string,
): SerializedOutline {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const placementLines = placements
    .filter((placement) => placement.parent_node_id === parentNodeId)
    .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  const lineMap = placementLines.flatMap((placement): OutlineLine[] => {
    const node = nodesById.get(placement.node_id);
    if (!node || node.archived_at) return [];
    return [{ nodeId: node.id, placementId: placement.id, depth: 0, text: node.title }];
  });
  return {
    text: lineMap.map((line) => line.text).join("\n"),
    lineMap,
  };
}

function parentForLine(lineMap: OutlineLine[], lineIndex: number, depth: number) {
  if (depth <= 0) return null;
  for (let index = lineIndex - 1; index >= 0; index -= 1) {
    const line = lineMap[index];
    if (line && line.depth === depth - 1) return line.nodeId;
  }
  return null;
}

function previousSiblingForLine(lineMap: OutlineLine[], lineIndex: number, depth: number) {
  for (let index = lineIndex - 1; index >= 0; index -= 1) {
    const line = lineMap[index];
    if (!line) continue;
    if (line.depth < depth) return null;
    if (line.depth === depth) return line.nodeId;
  }
  return null;
}

function tombstoneForLine(tombstones: Tombstone[], title: string, parentId: string | null, afterSiblingId: string | null, depth: number) {
  return tombstones.find(
    (item) =>
      item.title === title &&
      item.parentId === parentId &&
      item.afterSiblingId === afterSiblingId &&
      item.depth === depth,
  );
}

function sameLine(line: OutlineLine | undefined, rawLine: string | undefined) {
  if (!line || rawLine === undefined) return false;
  return line.depth === lineDepth(rawLine) && line.text === lineTitle(rawLine);
}

export function reconcileOutlineText({ before, afterText, tombstones = [] }: ReconcileOptions): OutlineOperation[] {
  const afterLines = normalizeLines(afterText);
  const operations: OutlineOperation[] = [];
  const projectedLineMap: OutlineLine[] = [];
  let beforeIndex = 0;

  for (let afterIndex = 0; afterIndex < afterLines.length; afterIndex += 1) {
    const rawAfter = afterLines[afterIndex];
    if (rawAfter === undefined) continue;

    const depth = lineDepth(rawAfter);
    const title = lineTitle(rawAfter);
    while (
      before.lineMap[beforeIndex]?.nodeId &&
      sameLine(before.lineMap[beforeIndex + 1], rawAfter)
    ) {
      const deleted = before.lineMap[beforeIndex];
      if (deleted?.nodeId) {
        operations.push({ type: "archive_node", nodeId: deleted.nodeId, line: beforeIndex, title: deleted.text });
      }
      beforeIndex += 1;
    }

    const beforeLine = before.lineMap[beforeIndex];
    const afterNext = afterLines[afterIndex + 1];
    const parentId = parentForLine(projectedLineMap, projectedLineMap.length, depth);
    const afterSiblingId = previousSiblingForLine(projectedLineMap, projectedLineMap.length, depth);

    if (beforeLine?.nodeId && sameLine(beforeLine, afterNext)) {
      const tombstone = tombstoneForLine(tombstones, title, parentId, afterSiblingId, depth);
      if (tombstone) {
        operations.push({ type: "restore_node", nodeId: tombstone.nodeId, line: afterIndex, title });
        projectedLineMap.push({ nodeId: tombstone.nodeId, depth, text: title });
      } else if (title.trim()) {
        operations.push({
          type: "create_node",
          pendingLine: afterIndex,
          parentId,
          afterSiblingId,
          title,
          depth,
        });
        projectedLineMap.push({ nodeId: null, depth, text: title, pending: true });
      }
      continue;
    }

    if (beforeLine?.nodeId) {
      if (beforeLine.depth !== depth) {
        operations.push({
          type: "move_node",
          nodeId: beforeLine.nodeId,
          parentId,
          afterSiblingId,
          depth,
        });
      }
      if (beforeLine.text !== title) {
        operations.push({ type: "patch_title", nodeId: beforeLine.nodeId, title });
      }
      projectedLineMap.push({ nodeId: beforeLine.nodeId, placementId: beforeLine.placementId, depth, text: title });
      beforeIndex += 1;
      continue;
    }

    const tombstone = tombstoneForLine(tombstones, title, parentId, afterSiblingId, depth);
    if (tombstone) {
      operations.push({ type: "restore_node", nodeId: tombstone.nodeId, line: afterIndex, title });
      projectedLineMap.push({ nodeId: tombstone.nodeId, depth, text: title });
    } else if (title.trim()) {
      operations.push({
        type: "create_node",
        pendingLine: afterIndex,
        parentId,
        afterSiblingId,
        title,
        depth,
      });
      projectedLineMap.push({ nodeId: null, depth, text: title, pending: true });
    }
  }

  for (let index = beforeIndex; index < before.lineMap.length; index += 1) {
    const line = before.lineMap[index];
    if (!line?.nodeId) continue;
    operations.push({ type: "archive_node", nodeId: line.nodeId, line: index, title: line.text });
  }

  return operations;
}
