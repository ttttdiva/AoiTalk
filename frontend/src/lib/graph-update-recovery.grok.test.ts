// AgentTeam Graph save/readback regression: public synthetic contract test.
// Request nonce: b0f21472163843249ce22d958350bfd6
import { describe, expect, it } from 'vitest';
import { classifyGraphReadback } from './graph-update-recovery';

type ExecutionKind = 'media_image' | 'media_video' | 'software_code' | null;

interface GraphNode {
  key: string;
  agent_id: string;
  revision_id?: string | null;
  role: string;
  x: number;
  y: number;
  [extra: string]: unknown;
}

interface GraphEdge {
  key: string;
  source: string;
  target: string;
  kind: string;
  output_key?: string | null;
  [extra: string]: unknown;
}

interface GraphGroup {
  key: string;
  title: string;
  purpose: string;
  node_keys: string[];
  leader_key: string;
  execution_kind?: ExecutionKind;
  [extra: string]: unknown;
}

interface Graph {
  id: string;
  version: number;
  title: string;
  project_id: string | null;
  definition: {
    schema_version: 1;
    root_key: string;
    nodes: GraphNode[];
    edges: GraphEdge[];
    groups: GraphGroup[];
    [extra: string]: unknown;
  };
}

type Verdict = 'confirmed' | 'unchanged' | 'conflict';
type Arg = Parameters<typeof classifyGraphReadback>[0];

// The only cast: synthetic graphs (and deliberately malformed JSON values) cross into the function here.
function classify(base: unknown, intended: unknown, observed: unknown): Verdict {
  return classifyGraphReadback(base as Arg, intended as Parameters<typeof classifyGraphReadback>[1], observed as Arg) as Verdict;
}

function makeBase(): Graph {
  return {
    id: 'graph-0001',
    version: 7,
    title: 'Synthetic team',
    project_id: 'project-a',
    definition: {
      schema_version: 1,
      root_key: 'lead',
      nodes: [
        { key: 'lead', agent_id: 'agent-lead', revision_id: null, role: 'leader', x: 0, y: 0 },
        { key: 'writer', agent_id: 'agent-writer', revision_id: 'rev-w1', role: 'worker', x: 200, y: 0 },
        { key: 'artist', agent_id: 'agent-artist', revision_id: null, role: 'worker', x: 200, y: 160 },
      ],
      edges: [
        { key: 'e1', source: 'lead', target: 'writer', kind: 'delegate', output_key: null },
        { key: 'e2', source: 'lead', target: 'artist', kind: 'delegate', output_key: 'image' },
      ],
      groups: [
        {
          key: 'g1',
          title: 'Content',
          purpose: 'Write and illustrate',
          node_keys: ['writer', 'artist'],
          leader_key: 'writer',
          execution_kind: 'media_image',
        },
      ],
    },
  };
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function edit(graph: Graph, mutate: (g: Graph) => void): Graph {
  const copy = clone(graph);
  mutate(copy);
  return copy;
}

function saved(intended: Graph, mutate: (g: Graph) => void = () => {}): Graph {
  return edit(intended, (g) => {
    g.version = intended.version + 1;
    mutate(g);
  });
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const key of Reflect.ownKeys(value as object)) {
      deepFreeze((value as Record<PropertyKey, unknown>)[key]);
    }
    Object.freeze(value);
  }
  return value;
}

function reorderKeys<T>(value: T): T {
  if (Array.isArray(value)) return value.map(reorderKeys) as T;
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as object).reverse()) {
      out[key] = reorderKeys((value as Record<string, unknown>)[key]);
    }
    return out as T;
  }
  return value;
}

function withoutExecutionKind(graph: Graph): Graph {
  return edit(graph, (g) => {
    for (const group of g.definition.groups) delete group.execution_kind;
  });
}

const BASE = makeBase();
// A real change: rename, move the artist, pin the lead to a revision.
const CHANGED = edit(BASE, (g) => {
  g.title = 'Synthetic team v2';
  g.definition.nodes[2].x = 260;
  g.definition.nodes[2].y = 180;
  g.definition.nodes[0].revision_id = 'rev-l3';
});
const LEGACY_BASE = withoutExecutionKind(BASE);
const LEGACY_CHANGED = withoutExecutionKind(CHANGED);

interface Case {
  name: string;
  base: Graph;
  intended: Graph;
  observed: Graph;
  expected: Verdict;
}

const cases: Case[] = [
  {
    name: 'successful changed save reads back intended at version+1',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED),
    expected: 'confirmed',
  },
  {
    name: 'object key order is ignored at every level',
    base: BASE,
    intended: CHANGED,
    observed: reorderKeys(saved(CHANGED)),
    expected: 'confirmed',
  },
  {
    name: 'readback equal to base at same version is unchanged, never confirmed (no automatic resend implied)',
    base: BASE,
    intended: CHANGED,
    observed: clone(BASE),
    expected: 'unchanged',
  },
  {
    name: 'unchanged also ignores key order',
    base: BASE,
    intended: CHANGED,
    observed: reorderKeys(clone(BASE)),
    expected: 'unchanged',
  },
  {
    name: 'different graph id is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.id = 'graph-0002';
    }),
    expected: 'conflict',
  },
  {
    name: 'intended content at the base version (version not bumped) is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: edit(CHANGED, (g) => {
      g.version = BASE.version;
    }),
    expected: 'conflict',
  },
  {
    name: 'skipped version (base+2) is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.version = BASE.version + 2;
    }),
    expected: 'conflict',
  },
  {
    name: 'base content at a bumped version is a conflict, not unchanged',
    base: BASE,
    intended: CHANGED,
    observed: edit(BASE, (g) => {
      g.version = BASE.version + 1;
    }),
    expected: 'conflict',
  },
  {
    name: 'conflicting external edit (title differs from intended) is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.title = 'Edited elsewhere';
    }),
    expected: 'conflict',
  },
  {
    name: 'node array order matters',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.nodes.reverse();
    }),
    expected: 'conflict',
  },
  {
    name: 'group node_keys order matters',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.groups[0].node_keys.reverse();
    }),
    expected: 'conflict',
  },
  {
    name: 'node pin lost (revision_id null instead of pinned) is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.nodes[0].revision_id = null;
    }),
    expected: 'conflict',
  },
  {
    name: 'revision_id omitted vs null is not normalized',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      delete g.definition.nodes[2].revision_id;
    }),
    expected: 'conflict',
  },
  {
    name: 'model/agent not copied (agent_id still old) is a conflict',
    base: BASE,
    intended: edit(BASE, (g) => {
      g.definition.nodes[1].agent_id = 'agent-writer-2';
    }),
    observed: saved(BASE),
    expected: 'conflict',
  },
  {
    name: 'move-only save is confirmed',
    base: BASE,
    intended: edit(BASE, (g) => {
      g.definition.nodes[1].x = 240;
      g.definition.nodes[1].y = -40;
    }),
    observed: saved(
      edit(BASE, (g) => {
        g.definition.nodes[1].x = 240;
        g.definition.nodes[1].y = -40;
      }),
    ),
    expected: 'confirmed',
  },
  {
    name: 'wrong position after move is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.nodes[2].y = 181;
    }),
    expected: 'conflict',
  },
  {
    name: 'edge reconnect not applied is a conflict',
    base: BASE,
    intended: edit(BASE, (g) => {
      g.definition.edges[1].source = 'writer';
    }),
    observed: saved(BASE),
    expected: 'conflict',
  },
  {
    name: 'edge output_key difference is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.edges[1].output_key = 'images';
    }),
    expected: 'conflict',
  },
  {
    name: 'edge output_key omitted vs null is not normalized',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      delete g.definition.edges[0].output_key;
    }),
    expected: 'conflict',
  },
  {
    name: 'node role difference is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.nodes[2].role = 'reviewer';
    }),
    expected: 'conflict',
  },
  {
    name: 'root_key difference is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.root_key = 'writer';
    }),
    expected: 'conflict',
  },
  {
    name: 'explicit group execution_kind preserved is confirmed',
    base: BASE,
    intended: edit(CHANGED, (g) => {
      g.definition.groups[0].execution_kind = 'media_video';
    }),
    observed: saved(
      edit(CHANGED, (g) => {
        g.definition.groups[0].execution_kind = 'media_video';
      }),
    ),
    expected: 'confirmed',
  },
  {
    name: 'legacy: intended omits execution_kind, server returns null, is confirmed',
    base: LEGACY_BASE,
    intended: LEGACY_CHANGED,
    observed: saved(LEGACY_CHANGED, (g) => {
      g.definition.groups[0].execution_kind = null;
    }),
    expected: 'confirmed',
  },
  {
    name: 'legacy: base omits execution_kind, server returns null at same version, is unchanged',
    base: LEGACY_BASE,
    intended: LEGACY_CHANGED,
    observed: edit(LEGACY_BASE, (g) => {
      g.definition.groups[0].execution_kind = null;
    }),
    expected: 'unchanged',
  },
  {
    name: 'explicit execution_kind lost to null is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.groups[0].execution_kind = null;
    }),
    expected: 'conflict',
  },
  {
    name: 'explicit execution_kind lost by omission is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: withoutExecutionKind(saved(CHANGED)),
    expected: 'conflict',
  },
  {
    name: 'explicit execution_kind changed to another kind is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.groups[0].execution_kind = 'software_code';
    }),
    expected: 'conflict',
  },
  {
    name: 'unknown future execution_kind is not treated as legacy/generic',
    base: LEGACY_BASE,
    intended: LEGACY_CHANGED,
    observed: saved(LEGACY_CHANGED, (g) => {
      (g.definition.groups[0] as Record<string, unknown>).execution_kind = 'media_audio';
    }),
    expected: 'conflict',
  },
  {
    name: 'project target mismatch is a conflict',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.project_id = 'project-b';
    }),
    expected: 'conflict',
  },
  {
    name: 'project null vs set is a conflict',
    base: BASE,
    intended: edit(CHANGED, (g) => {
      g.project_id = null;
    }),
    observed: saved(CHANGED),
    expected: 'conflict',
  },
  {
    name: 'unknown definition field difference remains different',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.nodes[0].color = 'red';
    }),
    expected: 'conflict',
  },
  {
    name: 'unknown group field difference remains different, even if null',
    base: BASE,
    intended: CHANGED,
    observed: saved(CHANGED, (g) => {
      g.definition.groups[0].budget = null;
    }),
    expected: 'conflict',
  },
];

describe('classifyGraphReadback (synthetic contract, nonce b0f21472163843249ce22d958350bfd6)', () => {
  it.each(cases)('$name', ({ base, intended, observed, expected }) => {
    const snapshots = [clone(base), clone(intended), clone(observed)];
    const frozen = [deepFreeze(clone(base)), deepFreeze(clone(intended)), deepFreeze(clone(observed))];

    const verdict = classify(frozen[0], frozen[1], frozen[2]);

    expect(verdict).toBe(expected);
    expect(frozen).toEqual(snapshots);
  });

  const nonJson: Array<{ name: string; observed: () => unknown }> = [
    {
      name: 'NaN coordinate',
      observed: () =>
        saved(CHANGED, (g) => {
          g.definition.nodes[2].x = Number.NaN;
        }),
    },
    {
      name: 'Infinity coordinate',
      observed: () =>
        saved(CHANGED, (g) => {
          g.definition.nodes[2].y = Number.POSITIVE_INFINITY;
        }),
    },
    {
      name: 'non-plain object (Date) in place of a string',
      observed: () => {
        const g = saved(CHANGED) as unknown as Record<string, unknown>;
        g.title = new Date(0);
        return g;
      },
    },
    {
      name: 'class instance as a node',
      observed: () => {
        class FakeNode {
          constructor(public key: string) {}
        }
        const g = saved(CHANGED);
        const copy = { ...g, definition: { ...g.definition, nodes: g.definition.nodes.map((n) => Object.assign(new FakeNode(n.key), n)) } };
        return copy;
      },
    },
    {
      name: 'sparse nodes array',
      observed: () => {
        const g = saved(CHANGED);
        const sparse: GraphNode[] = new Array(g.definition.nodes.length);
        g.definition.nodes.forEach((n, i) => {
          if (i !== 1) sparse[i] = n;
        });
        g.definition.nodes = sparse;
        return g;
      },
    },
    {
      name: 'undefined-valued unknown field (must not compare as stringify-equal to absent)',
      observed: () =>
        saved(CHANGED, (g) => {
          g.definition.nodes[0].note = undefined;
        }),
    },
  ];

  it.each(nonJson)('rejects or does not match non-JSON readback: $name', ({ observed }) => {
    const base = deepFreeze(clone(BASE));
    const intended = deepFreeze(clone(CHANGED));
    const value = observed();
    deepFreeze(value);

    let verdict: Verdict | 'threw';
    try {
      verdict = classify(base, intended, value);
    } catch {
      verdict = 'threw';
    }

    expect(['conflict', 'threw']).toContain(verdict);
    expect(base).toEqual(BASE);
    expect(intended).toEqual(CHANGED);
  });

  it('NaN on both intended and readback still does not confirm', () => {
    const intended = edit(CHANGED, (g) => {
      g.definition.nodes[2].x = Number.NaN;
    });
    const observed = saved(intended);

    let verdict: Verdict | 'threw';
    try {
      verdict = classify(deepFreeze(clone(BASE)), deepFreeze(intended), deepFreeze(observed));
    } catch {
      verdict = 'threw';
    }

    expect(['conflict', 'threw']).toContain(verdict);
  });
});
