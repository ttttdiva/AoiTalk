export type ProjectOverviewStatus =
  | "empty"
  | "pending"
  | "building"
  | "fresh"
  | "failed";

export type ProjectOverviewSectionKind =
  | "highlight"
  | "bullets"
  | "cards"
  | "timeline";

export type ProjectOverviewSectionEmphasis =
  | "normal"
  | "primary"
  | "warning"
  | "critical";

export type ProjectOverviewSectionDensity = "compact" | "normal";

export type ProjectOverviewGraphNodeKind =
  | "lead"
  | "member"
  | "stakeholder"
  | "system"
  | "other";

export type ProjectOverviewSection = {
  id?: string;
  kind: ProjectOverviewSectionKind;
  title: string;
  emphasis: ProjectOverviewSectionEmphasis;
  columns: 1 | 2;
  density: ProjectOverviewSectionDensity;
  memory_ids: string[];
};

export type ProjectOverviewGraphNode = {
  id: string;
  kind: ProjectOverviewGraphNodeKind;
  label: string;
  subtitle: string;
  memory_ids: string[];
};

export type ProjectOverviewGraphEdge = {
  id?: string;
  source: string;
  target: string;
  label: string;
  memory_ids: string[];
};

export type ProjectOverviewLayout = {
  schema_version: 1;
  sections: ProjectOverviewSection[];
  graph: {
    title?: string;
    nodes: ProjectOverviewGraphNode[];
    edges: ProjectOverviewGraphEdge[];
  };
};

export type ProjectOverviewMemoryRef = {
  id: string;
  title: string | null;
  memory_type: string;
  content: string;
  importance: number;
  confidence: number;
  is_pinned: boolean;
  updated_at: string | null;
};

export type ProjectOverviewResponse = {
  project_id: string;
  status: ProjectOverviewStatus;
  layout: ProjectOverviewLayout;
  layoutValid: boolean;
  source_digest: string | null;
  generated_at: string | null;
  generation_version: number;
  error_message: string | null;
  memory_refs: Record<string, ProjectOverviewMemoryRef>;
};

const SECTION_KINDS = new Set<ProjectOverviewSectionKind>([
  "highlight",
  "bullets",
  "cards",
  "timeline",
]);
const SECTION_EMPHASIS = new Set<ProjectOverviewSectionEmphasis>([
  "normal",
  "primary",
  "warning",
  "critical",
]);
const SECTION_DENSITIES = new Set<ProjectOverviewSectionDensity>([
  "compact",
  "normal",
]);
const GRAPH_NODE_KINDS = new Set<ProjectOverviewGraphNodeKind>([
  "lead",
  "member",
  "stakeholder",
  "system",
  "other",
]);
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FORBIDDEN_LAYOUT_TEXT_RE =
  /(?:<\s*\/?\s*[a-z][^>]*>|\b(?:javascript|data)\s*:|\b(?:mermaid|classname|style|css|html|script)\b|#[0-9a-f]{3,8}\b|(?:rgb|hsl)a?\s*\()/i;

const EMPTY_LAYOUT: ProjectOverviewLayout = {
  schema_version: 1,
  sections: [],
  graph: {
    nodes: [],
    edges: [],
  },
};

function recordOf(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): boolean {
  const allowedSet = new Set(allowed);
  return Object.keys(value).every((key) => allowedSet.has(key));
}

function layoutText(
  value: unknown,
  {
    maxLength,
    required = false,
  }: {
    maxLength: number;
    required?: boolean;
  },
): string | null {
  if (value == null || value === "") return required ? null : "";
  if (typeof value !== "string") return null;
  const clean = value
    .replace(/\0/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (required && !clean) return null;
  if (clean.length > maxLength) return null;
  if (/[\u0000-\u001f\u007f]/.test(clean)) return null;
  if (FORBIDDEN_LAYOUT_TEXT_RE.test(clean)) return null;
  return clean;
}

function graphIdentifier(
  value: unknown,
  required = true,
): string | null {
  if (value == null || value === "") return required ? null : "";
  if (typeof value !== "string") return null;
  const clean = value.replace(/\0/g, "").trim();
  if (!clean) return required ? null : "";
  if (clean.length > 120) return null;
  if (/[\u0000-\u001f\u007f]/.test(clean)) return null;
  return clean;
}

function memoryIds(value: unknown): string[] | null {
  if (value == null || value === "") return [];
  if (!Array.isArray(value) || value.length > 12) return null;
  const result: string[] = [];
  const seen = new Set<string>();
  for (const raw of value) {
    if (typeof raw !== "string" || !UUID_RE.test(raw)) return null;
    const normalized = raw.toLowerCase();
    if (seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

export function emptyProjectOverviewLayout(): ProjectOverviewLayout {
  return {
    schema_version: 1,
    sections: [],
    graph: { nodes: [], edges: [] },
  };
}

export function projectOverviewLayoutHasContent(
  layout: ProjectOverviewLayout,
): boolean {
  return layout.sections.length > 0 || layout.graph.nodes.length > 0;
}

export function parseProjectOverviewLayout(
  value: unknown,
): ProjectOverviewLayout | null {
  const root = recordOf(value);
  if (!root) return null;
  if (!hasOnlyKeys(root, ["schema_version", "sections", "graph"])) return null;
  if (root.schema_version !== 1) return null;
  if (!Array.isArray(root.sections) || root.sections.length > 8) return null;

  const sections: ProjectOverviewSection[] = [];
  const sectionIds = new Set<string>();
  for (const rawSection of root.sections) {
    const section = recordOf(rawSection);
    if (!section) return null;
    if (
      !hasOnlyKeys(section, [
        "id",
        "kind",
        "title",
        "emphasis",
        "columns",
        "density",
        "memory_ids",
      ])
    ) {
      return null;
    }

    const id = graphIdentifier(section.id, false);
    if (id == null || (id && sectionIds.has(id))) return null;
    if (id) sectionIds.add(id);

    const kind = section.kind;
    const emphasis = section.emphasis ?? "normal";
    const density = section.density ?? "normal";
    const columns = section.columns ?? 1;
    const title = layoutText(section.title, {
      maxLength: 80,
      required: true,
    });
    const refs = memoryIds(section.memory_ids);

    if (
      typeof kind !== "string" ||
      !SECTION_KINDS.has(kind as ProjectOverviewSectionKind) ||
      typeof emphasis !== "string" ||
      !SECTION_EMPHASIS.has(
        emphasis as ProjectOverviewSectionEmphasis,
      ) ||
      typeof density !== "string" ||
      !SECTION_DENSITIES.has(density as ProjectOverviewSectionDensity) ||
      (columns !== 1 && columns !== 2) ||
      title == null ||
      refs == null
    ) {
      return null;
    }

    sections.push({
      ...(id ? { id } : {}),
      kind: kind as ProjectOverviewSectionKind,
      title,
      emphasis: emphasis as ProjectOverviewSectionEmphasis,
      columns,
      density: density as ProjectOverviewSectionDensity,
      memory_ids: refs,
    });
  }

  const rawGraph = root.graph == null ? {} : recordOf(root.graph);
  if (!rawGraph) return null;
  if (!hasOnlyKeys(rawGraph, ["title", "nodes", "edges"])) return null;
  const graphTitle = layoutText(rawGraph.title, { maxLength: 80 });
  if (graphTitle == null) return null;

  const rawNodes = rawGraph.nodes ?? [];
  const rawEdges = rawGraph.edges ?? [];
  if (
    !Array.isArray(rawNodes) ||
    !Array.isArray(rawEdges) ||
    rawNodes.length > 24 ||
    rawEdges.length > 48
  ) {
    return null;
  }

  const nodes: ProjectOverviewGraphNode[] = [];
  const nodeIds = new Set<string>();
  const nodeMemoryIds = new Map<string, Set<string>>();

  for (const rawNode of rawNodes) {
    const node = recordOf(rawNode);
    if (!node) return null;
    if (
      !hasOnlyKeys(node, [
        "id",
        "kind",
        "label",
        "subtitle",
        "memory_ids",
      ])
    ) {
      return null;
    }
    const id = graphIdentifier(node.id);
    const kind = node.kind;
    const label = layoutText(node.label, {
      maxLength: 80,
      required: true,
    });
    const subtitle = layoutText(node.subtitle, { maxLength: 120 });
    const refs = memoryIds(node.memory_ids);
    if (
      id == null ||
      nodeIds.has(id) ||
      typeof kind !== "string" ||
      !GRAPH_NODE_KINDS.has(kind as ProjectOverviewGraphNodeKind) ||
      label == null ||
      subtitle == null ||
      refs == null ||
      refs.length === 0
    ) {
      return null;
    }
    nodeIds.add(id);
    nodeMemoryIds.set(id, new Set(refs));
    nodes.push({
      id,
      kind: kind as ProjectOverviewGraphNodeKind,
      label,
      subtitle,
      memory_ids: refs,
    });
  }

  const edges: ProjectOverviewGraphEdge[] = [];
  const edgeIds = new Set<string>();
  for (const rawEdge of rawEdges) {
    const edge = recordOf(rawEdge);
    if (!edge) return null;
    if (
      !hasOnlyKeys(edge, [
        "id",
        "source",
        "target",
        "label",
        "memory_ids",
      ])
    ) {
      return null;
    }
    const id = graphIdentifier(edge.id, false);
    const source = graphIdentifier(edge.source);
    const target = graphIdentifier(edge.target);
    const label = layoutText(edge.label, { maxLength: 80 });
    const refs = memoryIds(edge.memory_ids);
    if (
      id == null ||
      source == null ||
      target == null ||
      label == null ||
      refs == null ||
      refs.length === 0 ||
      !nodeIds.has(source) ||
      !nodeIds.has(target)
    ) {
      return null;
    }
    if (id && edgeIds.has(id)) return null;
    if (id) edgeIds.add(id);

    const sourceRefs = nodeMemoryIds.get(source) ?? new Set<string>();
    const targetRefs = nodeMemoryIds.get(target) ?? new Set<string>();
    if (
      !refs.some(
        (memoryId) =>
          sourceRefs.has(memoryId) && targetRefs.has(memoryId),
      )
    ) {
      return null;
    }

    edges.push({
      ...(id ? { id } : {}),
      source,
      target,
      label,
      memory_ids: refs,
    });
  }

  return {
    schema_version: 1,
    sections,
    graph: {
      ...(graphTitle ? { title: graphTitle } : {}),
      nodes,
      edges,
    },
  };
}

function displayText(
  value: unknown,
  maxLength: number,
): string {
  if (typeof value !== "string") return "";
  return value
    .replace(/\0/g, "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .slice(0, maxLength);
}

function normalizeMemoryRefs(
  value: unknown,
): Record<string, ProjectOverviewMemoryRef> {
  const raw = recordOf(value);
  if (!raw) return {};
  const result: Record<string, ProjectOverviewMemoryRef> = {};
  for (const [key, candidate] of Object.entries(raw)) {
    const item = recordOf(candidate);
    if (!item) continue;
    const id =
      typeof item.id === "string" && UUID_RE.test(item.id)
        ? item.id.toLowerCase()
        : UUID_RE.test(key)
          ? key.toLowerCase()
          : null;
    if (!id) continue;
    result[id] = {
      id,
      title:
        typeof item.title === "string"
          ? displayText(item.title, 200) || null
          : null,
      memory_type:
        displayText(item.memory_type, 32) || "fact",
      content: displayText(item.content, 2000),
      importance: Number.isFinite(Number(item.importance))
        ? Math.max(0, Math.min(10, Number(item.importance)))
        : 0,
      confidence: Number.isFinite(Number(item.confidence))
        ? Math.max(0, Math.min(1, Number(item.confidence)))
        : 0,
      is_pinned: item.is_pinned === true,
      updated_at:
        typeof item.updated_at === "string"
          ? displayText(item.updated_at, 80)
          : null,
    };
  }
  return result;
}

function normalizeStatus(value: unknown): ProjectOverviewStatus {
  switch (String(value ?? "").trim().toLowerCase()) {
    case "empty":
      return "empty";
    case "pending":
      return "pending";
    case "building":
    case "running":
      return "building";
    case "fresh":
      return "fresh";
    case "failed":
      return "failed";
    default:
      return "failed";
  }
}

export function normalizeProjectOverviewResponse(
  value: unknown,
): ProjectOverviewResponse {
  const root = recordOf(value);
  if (!root) {
    return {
      project_id: "",
      status: "failed",
      layout: emptyProjectOverviewLayout(),
      layoutValid: false,
      source_digest: null,
      generated_at: null,
      generation_version: 1,
      error_message: "overview_response_invalid",
      memory_refs: {},
    };
  }

  const parsedLayout = parseProjectOverviewLayout(root.layout);
  const layout = parsedLayout ?? emptyProjectOverviewLayout();
  const layoutValid = parsedLayout != null;
  const memory_refs = normalizeMemoryRefs(root.memory_refs);
  const generatedAt =
    typeof root.generated_at === "string"
      ? displayText(root.generated_at, 80)
      : null;
  const rawVersion = Number(root.generation_version);

  return {
    project_id:
      typeof root.project_id === "string"
        ? displayText(root.project_id, 120)
        : "",
    status: layoutValid ? normalizeStatus(root.status) : "failed",
    layout,
    layoutValid,
    source_digest:
      typeof root.source_digest === "string"
        ? displayText(root.source_digest, 256)
        : null,
    generated_at: generatedAt,
    generation_version:
      Number.isInteger(rawVersion) && rawVersion > 0
        ? rawVersion
        : 1,
    error_message: layoutValid
      ? typeof root.error_message === "string"
        ? displayText(root.error_message, 128)
        : null
      : "overview_layout_invalid",
    memory_refs,
  };
}

export function projectOverviewDisplayStatus(
  value: ProjectOverviewResponse | null,
  refreshing: boolean,
): ProjectOverviewStatus {
  if (refreshing) return "pending";
  if (!value) return "empty";
  if (
    value.status === "pending" &&
    !value.generated_at &&
    !projectOverviewLayoutHasContent(value.layout)
  ) {
    return "empty";
  }
  return value.status;
}

export const EMPTY_PROJECT_OVERVIEW_LAYOUT = EMPTY_LAYOUT;
