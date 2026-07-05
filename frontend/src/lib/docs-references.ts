const UUID_PATTERN =
  "[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";

const EXPLICIT_DOCS_REF_REGEX = new RegExp(
  `(?:@docs:|aoitalk:\\/\\/docs\\/|\\[\\[node:)(${UUID_PATTERN})`,
  "gi",
);
const HASHTAG_REGEX = /(^|\s)#([\p{L}\p{N}_-]{1,80})/gu;

export type DocsReferenceHints = {
  docsIds: string[];
};

export function extractDocsReferenceHints(text: string): DocsReferenceHints {
  const docsIds = new Set<string>();

  for (const match of text.matchAll(EXPLICIT_DOCS_REF_REGEX)) {
    const id = match[1]?.toLowerCase();
    if (id) docsIds.add(id);
  }

  return {
    docsIds: Array.from(docsIds),
  };
}

export function createDocsNodeWikilink(nodeId: string, label: string): string {
  const cleanLabel = label.replace(/\]/g, "").trim() || "Untitled";
  return `[[node:${nodeId}|${cleanLabel}]]`;
}

export function extractDocsHashtagNames(text: string): string[] {
  const names = new Set<string>();
  for (const match of text.matchAll(HASHTAG_REGEX)) {
    const name = match[2]?.trim();
    if (name) names.add(name);
  }
  return Array.from(names);
}
