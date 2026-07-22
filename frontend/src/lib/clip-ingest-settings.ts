export interface ClipIngestTarget {
  node_id: string;
  node_system_key?: string;
  label: string;
  breadcrumb: string[];
  routing_hint: string;
  enabled: boolean;
  fallback: boolean;
}

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeClipIngestTargets(value: unknown): ClipIngestTarget[] {
  if (!Array.isArray(value)) return [];

  const seen = new Set<string>();
  let fallbackSeen = false;
  const targets: ClipIngestTarget[] = [];

  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    const nodeId = cleanString(raw.node_id);
    if (!nodeId || seen.has(nodeId)) continue;
    seen.add(nodeId);

    const requestedFallback = raw.fallback === true;
    const fallback = requestedFallback && !fallbackSeen;
    if (fallback) fallbackSeen = true;
    const nodeSystemKey = cleanString(raw.node_system_key);
    targets.push({
      node_id: nodeId,
      ...(nodeSystemKey ? { node_system_key: nodeSystemKey } : {}),
      label: cleanString(raw.label) || "Untitled",
      breadcrumb: Array.isArray(raw.breadcrumb)
        ? raw.breadcrumb.map(cleanString).filter(Boolean)
        : [],
      routing_hint: cleanString(raw.routing_hint),
      enabled: raw.enabled !== false,
      fallback,
    });
  }
  return targets;
}

export function parseClipIngestTargets(settings: unknown): ClipIngestTarget[] {
  if (!settings || typeof settings !== "object") return [];
  const clipIngest = (settings as Record<string, unknown>).clip_ingest;
  if (!clipIngest || typeof clipIngest !== "object") return [];
  return normalizeClipIngestTargets(
    (clipIngest as Record<string, unknown>).targets,
  );
}

export function selectClipIngestFallback(
  targets: ClipIngestTarget[],
  nodeId: string | null,
): ClipIngestTarget[] {
  return targets.map((target) => ({
    ...target,
    fallback: nodeId !== null && target.node_id === nodeId,
  }));
}
