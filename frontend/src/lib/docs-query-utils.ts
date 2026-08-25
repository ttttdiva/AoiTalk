export type DocsTagFilterResolution = {
  tagFilters: Array<{ tagId: string; includeDescendants: boolean }>;
  unresolvedTagConditions: string[];
};

function tagConditionLabel(clause: Record<string, unknown>) {
  if (typeof clause.tag === "string") return clause.tag;
  if (typeof clause.tag_system_key === "string") return clause.tag_system_key;
  if (typeof clause.supertag_system_key === "string") return clause.supertag_system_key;
  if (typeof clause.tag_name === "string") return clause.tag_name;
  return "";
}

export function resolveDocsTagFilters(options: {
  explicitSupertagIds: string[];
  clauses: Record<string, unknown>[];
  tagIdSet: Set<string>;
  tagIdBySystemKey: Map<string, string>;
  tagIdByName: Map<string, string>;
}): DocsTagFilterResolution {
  const bySystemKey = (value: string) =>
    options.tagIdBySystemKey.get(value) ?? options.tagIdBySystemKey.get(value.toLowerCase());
  const byName = (value: string) =>
    options.tagIdByName.get(value.toLowerCase()) ?? options.tagIdByName.get(value);
  const tagFilters = options.explicitSupertagIds.map((tagId) => ({
    tagId,
    includeDescendants: true,
  }));
  const unresolvedTagConditions: string[] = [];

  for (const clause of options.clauses) {
    const hasTagCondition =
      typeof clause.tag === "string" ||
      typeof clause.tag_system_key === "string" ||
      typeof clause.supertag_system_key === "string" ||
      typeof clause.tag_name === "string";
    if (!hasTagCondition) continue;

    const tagId =
      typeof clause.tag === "string"
        ? options.tagIdSet.has(clause.tag)
          ? clause.tag
          : bySystemKey(clause.tag) ?? byName(clause.tag)
        : typeof clause.tag_system_key === "string"
          ? bySystemKey(clause.tag_system_key)
          : typeof clause.supertag_system_key === "string"
            ? bySystemKey(clause.supertag_system_key)
            : typeof clause.tag_name === "string"
              ? byName(clause.tag_name)
              : null;
    if (typeof tagId === "string" && tagId) {
      tagFilters.push({
        tagId,
        includeDescendants: clause.include_descendants !== false,
      });
    } else {
      unresolvedTagConditions.push(tagConditionLabel(clause));
    }
  }

  return { tagFilters, unresolvedTagConditions };
}
