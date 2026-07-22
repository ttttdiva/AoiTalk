import { createHash } from "node:crypto";

export const FOAM_CURATION_SCHEMA_VERSION = "foam-curation/v2" as const;

export type FoamDisposition =
  | "keep"
  | "rewrite"
  | "merge"
  | "reference"
  | "archive"
  | "discard";

export type FoamSourceInventoryEntry = {
  file: string;
  sha256: string;
};

export type FoamCuratedSource = FoamSourceInventoryEntry & {
  disposition: FoamDisposition;
  reason: string;
};

export type FoamCuratedNode = {
  semantic_key: string;
  title: string;
  parent_key: string | null;
  block_type: string;
  aliases?: string[];
  supertags: string[];
  source_refs: Array<{ file: string }>;
};

export type FoamLegacyResolution = {
  legacy_node_id: string;
  action: "incorporated" | "preserved_copy" | "discarded_with_reason";
  target_keys: string[];
  reason: string;
};

export type FoamCurationManifest = {
  schema_version: typeof FOAM_CURATION_SCHEMA_VERSION;
  migration_key: string;
  source_root: string;
  workspace: { id: string; owner_id: string };
  sources: FoamCuratedSource[];
  nodes: FoamCuratedNode[];
  legacy_resolutions: FoamLegacyResolution[];
};

export type FoamManifestValidation = {
  source_count: number;
  node_count: number;
  root_count: number;
  supertags: string[];
  manifest_sha256: string;
};

const SEMANTIC_KEY = /^[a-z0-9][a-z0-9._/-]*$/;
const MIGRATION_KEY = /^[a-z0-9][a-z0-9_:-]*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const FORBIDDEN_TITLE = [
  /Foam外または未解決/i,
  /\[\[[^\]]*\]\]/,
  /^\s*(?:#{1,6}|[-*+] |\d+[.)] )/,
  /(?:password|passwd|パスワード|api[_ -]?key|access[_ -]?token)\s*[:=]/i,
  /(?:teams\.microsoft\.com|meet\.google\.com|zoom\.us|webex\.com)/i,
  /\.onion\b/i,
];

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function foamManifestSha256(manifest: FoamCurationManifest): string {
  return createHash("sha256").update(stableJson(manifest)).digest("hex");
}

export function validateFoamCurationManifest(
  manifest: FoamCurationManifest,
  inventory: FoamSourceInventoryEntry[],
): FoamManifestValidation {
  const errors: string[] = [];
  if (manifest.schema_version !== FOAM_CURATION_SCHEMA_VERSION) {
    errors.push(`schema_version must be ${FOAM_CURATION_SCHEMA_VERSION}`);
  }
  if (!MIGRATION_KEY.test(manifest.migration_key) || manifest.migration_key.endsWith("_v1")) {
    errors.push("migration_key must identify the curated version, not v1");
  }
  if (!UUID.test(manifest.workspace?.id ?? "")) errors.push("workspace.id must be a UUID");
  if (!UUID.test(manifest.workspace?.owner_id ?? "")) errors.push("workspace.owner_id must be a UUID");

  const inventoryByFile = new Map(inventory.map((entry) => [entry.file, entry]));
  const sourceByFile = new Map<string, FoamCuratedSource>();
  for (const source of manifest.sources) {
    if (sourceByFile.has(source.file)) errors.push(`duplicate source: ${source.file}`);
    sourceByFile.set(source.file, source);
    const actual = inventoryByFile.get(source.file);
    if (!actual) errors.push(`unknown source: ${source.file}`);
    else if (actual.sha256 !== source.sha256) errors.push(`source hash mismatch: ${source.file}`);
    if (!source.reason.trim()) errors.push(`source reason is empty: ${source.file}`);
  }
  for (const item of inventory) {
    if (!sourceByFile.has(item.file)) errors.push(`missing source: ${item.file}`);
  }
  if (sourceByFile.size !== inventoryByFile.size) {
    errors.push(`source coverage mismatch: manifest=${sourceByFile.size} inventory=${inventoryByFile.size}`);
  }

  const nodeByKey = new Map<string, FoamCuratedNode>();
  const normalizedTitles = new Map<string, string>();
  for (const node of manifest.nodes) {
    if (!SEMANTIC_KEY.test(node.semantic_key)) errors.push(`invalid semantic_key: ${node.semantic_key}`);
    if (nodeByKey.has(node.semantic_key)) errors.push(`duplicate semantic_key: ${node.semantic_key}`);
    nodeByKey.set(node.semantic_key, node);
    const title = node.title.trim();
    if (!title || title !== node.title || title.length > 240 || /[\r\n]/.test(title)) {
      errors.push(`invalid title: ${node.semantic_key}`);
    }
    if (FORBIDDEN_TITLE.some((pattern) => pattern.test(title))) {
      errors.push(`unsafe or uncurated title: ${node.semantic_key}`);
    }
    const canonical = title.normalize("NFKC").toLocaleLowerCase("ja-JP");
    const duplicate = normalizedTitles.get(canonical);
    if (duplicate) errors.push(`duplicate canonical title: ${duplicate}, ${node.semantic_key}`);
    else normalizedTitles.set(canonical, node.semantic_key);
    if (!node.supertags.length) errors.push(`node has no supertag: ${node.semantic_key}`);
    if (!node.source_refs.length) errors.push(`node has no source_refs: ${node.semantic_key}`);
    for (const ref of node.source_refs) {
      if (!sourceByFile.has(ref.file)) errors.push(`node references unknown source: ${node.semantic_key} -> ${ref.file}`);
    }
  }
  for (const node of manifest.nodes) {
    if (node.parent_key && !nodeByKey.has(node.parent_key)) {
      errors.push(`missing parent: ${node.semantic_key} -> ${node.parent_key}`);
    }
    const visited = new Set<string>([node.semantic_key]);
    let parentKey = node.parent_key;
    while (parentKey) {
      if (visited.has(parentKey)) {
        errors.push(`hierarchy cycle: ${node.semantic_key}`);
        break;
      }
      visited.add(parentKey);
      parentKey = nodeByKey.get(parentKey)?.parent_key ?? null;
    }
  }

  const sourceNodeCounts = new Map<string, number>();
  for (const node of manifest.nodes) {
    for (const ref of node.source_refs) {
      sourceNodeCounts.set(ref.file, (sourceNodeCounts.get(ref.file) ?? 0) + 1);
    }
  }
  for (const source of manifest.sources) {
    if (["keep", "rewrite", "merge"].includes(source.disposition) && !sourceNodeCounts.has(source.file)) {
      errors.push(`retained source has no curated node: ${source.file}`);
    }
  }

  const resolutionIds = new Set<string>();
  for (const resolution of manifest.legacy_resolutions) {
    if (resolutionIds.has(resolution.legacy_node_id)) {
      errors.push(`duplicate legacy resolution: ${resolution.legacy_node_id}`);
    }
    resolutionIds.add(resolution.legacy_node_id);
    if (!resolution.reason.trim()) errors.push(`legacy resolution reason is empty: ${resolution.legacy_node_id}`);
    for (const key of resolution.target_keys) {
      if (!nodeByKey.has(key)) errors.push(`legacy resolution target is missing: ${resolution.legacy_node_id} -> ${key}`);
    }
  }

  if (errors.length) {
    throw new Error(`Foam curation manifest validation failed (${errors.length})\n- ${errors.join("\n- ")}`);
  }
  return {
    source_count: manifest.sources.length,
    node_count: manifest.nodes.length,
    root_count: manifest.nodes.filter((node) => !node.parent_key).length,
    supertags: [...new Set(manifest.nodes.flatMap((node) => node.supertags))].sort(),
    manifest_sha256: foamManifestSha256(manifest),
  };
}
