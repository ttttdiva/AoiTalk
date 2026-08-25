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

const FILM_ROOT_SYSTEM_KEY = "foam_source_grounded_v1:root.Film";

export function isAllowedClipIngestTarget(value: {
  breadcrumb?: unknown;
  node_system_key?: unknown;
  system_key?: unknown;
}): boolean {
  const breadcrumb = Array.isArray(value.breadcrumb)
    ? value.breadcrumb.map(cleanString).filter(Boolean)
    : [];
  const systemKey = cleanString(value.node_system_key ?? value.system_key);
  return breadcrumb[0] !== "Film" && systemKey !== FILM_ROOT_SYSTEM_KEY;
}

export function normalizeClipIngestTargets(value: unknown): ClipIngestTarget[] {
  if (!Array.isArray(value)) return [];

  const seen = new Set<string>();
  let fallbackSeen = false;
  const targets: ClipIngestTarget[] = [];

  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    if (!isAllowedClipIngestTarget(raw)) continue;
    const nodeId = cleanString(raw.node_id);
    if (!nodeId || seen.has(nodeId)) continue;
    seen.add(nodeId);

    const enabled = raw.enabled !== false;
    const requestedFallback = raw.fallback === true && enabled;
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
      enabled,
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
  const validNodeId = nodeId !== null
    && targets.some((target) => target.node_id === nodeId && target.enabled)
    ? nodeId
    : null;
  return targets.map((target) => ({
    ...target,
    fallback: validNodeId !== null && target.node_id === validNodeId,
  }));
}

export function clipIngestFallbackId(
  targets: ClipIngestTarget[],
): string | null {
  return targets.find((target) => target.enabled && target.fallback)?.node_id ?? null;
}

export function setClipIngestTargetEnabled(
  targets: ClipIngestTarget[],
  nodeId: string,
  enabled: boolean,
): ClipIngestTarget[] {
  return targets.map((target) => target.node_id === nodeId
    ? { ...target, enabled, fallback: enabled ? target.fallback : false }
    : target);
}

export function removeClipIngestTarget(
  targets: ClipIngestTarget[],
  nodeId: string,
): ClipIngestTarget[] {
  return targets.filter((target) => target.node_id !== nodeId);
}

export function formatClipIngestBreadcrumb(target: Pick<
  ClipIngestTarget,
  "breadcrumb" | "label"
>): string {
  const parts = target.breadcrumb.map(cleanString).filter(Boolean);
  const label = cleanString(target.label);
  const last = parts.at(-1);
  if (label && (!last || last.toLocaleLowerCase() !== label.toLocaleLowerCase())) {
    parts.push(label);
  }
  return parts.join(" / ") || label || "Docs";
}
