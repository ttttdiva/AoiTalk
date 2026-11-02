import type { GraphDefinition, TeamGraph } from "./agent-team-graph-api";

// Compare JSON values without relying on object key order. Sparse arrays are not JSON values.
function equalJson(a: unknown, b: unknown): boolean {
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return a === b && (a === null || typeof a === "string" || typeof a === "boolean" || (typeof a === "number" && Number.isFinite(a)));
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!(i in a) || !(i in b) || !equalJson(a[i], b[i])) return false;
    return true;
  }
  if (Object.getPrototypeOf(a) !== Object.prototype || Object.getPrototypeOf(b) !== Object.prototype) return false;
  const x = a as Record<string, unknown>, y = b as Record<string, unknown>;
  const keys = Object.keys(x);
  return keys.length === Object.keys(y).length && keys.every(k => Object.hasOwn(y, k) && equalJson(x[k], y[k]));
}

// The server removes null execution kinds while normalizing legacy snapshots.
function normalizedDefinition(definition: GraphDefinition): GraphDefinition {
  return { ...definition, groups: definition.groups.map(group => {
    if (group.execution_kind !== null) return group;
    const normalized = { ...group };
    delete normalized.execution_kind;
    return normalized;
  }) };
}

export function classifyGraphReadback(base: TeamGraph, intended: { title: string; project_id: string | null; definition: GraphDefinition }, observed: TeamGraph): "confirmed" | "unchanged" | "conflict" {
  if (observed.id !== base.id) return "conflict";
  if (observed.version === base.version + 1 && observed.title === intended.title && observed.project_id === intended.project_id && equalJson(normalizedDefinition(observed.definition), normalizedDefinition(intended.definition))) return "confirmed";
  if (observed.version === base.version && observed.title === base.title && observed.project_id === base.project_id && equalJson(normalizedDefinition(observed.definition), normalizedDefinition(base.definition))) return "unchanged";
  return "conflict";
}

export async function boundedGraphRead(get: () => Promise<{ graph: TeamGraph }>, timeoutMs = 5000): Promise<TeamGraph> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return (await Promise.race([get(), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("保存状態の確認がタイムアウトしました。")), timeoutMs); })])).graph;
  } finally { if (timer !== undefined) clearTimeout(timer); }
}
