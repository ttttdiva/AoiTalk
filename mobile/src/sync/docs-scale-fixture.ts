/**
 * Synthetic Docs data-shape helpers used by the scale harness.
 *
 * The defaults intentionally describe the production-sized shape without
 * allocating that many JavaScript objects.  Callers consume the iterators in
 * bounded pages so a scale test can exercise the same hierarchy/edge ratios as
 * production while retaining only one page at a time.
 */

export const DOCS_PRODUCTION_SHAPE = Object.freeze({
  rootNodes: 160_000,
  edges: 108_000,
});

export type DocsScaleFixtureConfig = {
  /** Fraction of the production shape to materialize (0 < scale <= 1). */
  scale?: number;
  /** Maximum number of rows retained by the page iterator. */
  pageSize?: number;
  /** Number of children represented under each synthetic root. */
  childrenPerRoot?: number;
};

export type DocsScaleNode = {
  id: string;
  parentId: string | null;
  title: string;
};

export type DocsScaleEdge = {
  id: string;
  sourceNodeId: string;
  targetNodeId: string;
};

export type DocsScaleFixture = {
  nodeCount: number;
  edgeCount: number;
  pageSize: number;
  nodes: () => IterableIterator<DocsScaleNode>;
  edges: () => IterableIterator<DocsScaleEdge>;
  nodePages: () => IterableIterator<DocsScaleNode[]>;
  edgePages: () => IterableIterator<DocsScaleEdge[]>;
};

function scaledCount(value: number, scale: number): number {
  return Math.max(1, Math.round(value * scale));
}

function normalizeScale(value: number | undefined): number {
  if (value == null || !Number.isFinite(value)) return 0.01;
  return Math.min(1, Math.max(Number.EPSILON, value));
}

function normalizePageSize(value: number | undefined): number {
  if (value == null || !Number.isFinite(value)) return 200;
  return Math.max(1, Math.floor(value));
}

function* page<T>(rows: Iterable<T>, pageSize: number): IterableIterator<T[]> {
  let current: T[] = [];
  for (const row of rows) {
    current.push(row);
    if (current.length < pageSize) continue;
    yield current;
    current = [];
  }
  if (current.length) yield current;
}

export function createDocsScaleFixture(
  config: DocsScaleFixtureConfig = {},
): DocsScaleFixture {
  const scale = normalizeScale(config.scale);
  const pageSize = normalizePageSize(config.pageSize);
  const childrenPerRoot = Math.max(
    1,
    Math.floor(config.childrenPerRoot ?? 4),
  );
  const nodeCount = scaledCount(DOCS_PRODUCTION_SHAPE.rootNodes, scale);
  const edgeCount = scaledCount(DOCS_PRODUCTION_SHAPE.edges, scale);

  function* nodes(): IterableIterator<DocsScaleNode> {
    for (let index = 0; index < nodeCount; index += 1) {
      const rootIndex = Math.floor(index / childrenPerRoot);
      yield {
        id: `scale-node-${index}`,
        parentId: index === 0 ? null : `scale-node-${rootIndex * childrenPerRoot}`,
        title: `Synthetic Docs node ${index}`,
      };
    }
  }

  function* edges(): IterableIterator<DocsScaleEdge> {
    for (let index = 0; index < edgeCount; index += 1) {
      const source = index % nodeCount;
      const target = (source + 1) % nodeCount;
      yield {
        id: `scale-edge-${index}`,
        sourceNodeId: `scale-node-${source}`,
        targetNodeId: `scale-node-${target}`,
      };
    }
  }

  return {
    nodeCount,
    edgeCount,
    pageSize,
    nodes,
    edges,
    nodePages: () => page(nodes(), pageSize),
    edgePages: () => page(edges(), pageSize),
  };
}
