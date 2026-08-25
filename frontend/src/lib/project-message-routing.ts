export type MessageRoutableProject = {
  id: string;
  name: string;
  slug?: string;
  aliases?: string[];
  metadata?: Record<string, unknown>;
};

const RESERVED_PRODUCT_WORDS = new Set([
  "inbox",
  "docs",
  "workspace",
  "ワークスペース",
  "案件情報",
]);

function normalize(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase();
}

function isDefaultInboxProject(project: MessageRoutableProject): boolean {
  return (
    project.metadata?.isInboxDefault === true ||
    normalize(project.slug ?? "").startsWith("inbox-project-")
  );
}

function isDistinctiveName(value: string): boolean {
  const normalized = normalize(value);
  if (!normalized || RESERVED_PRODUCT_WORDS.has(normalized)) return false;
  const compact = normalized.replace(/[\s_-]/g, "");
  return compact.length >= 3;
}

function containsExplicitName(content: string, name: string): boolean {
  const normalizedName = normalize(name);
  if (!isDistinctiveName(normalizedName)) return false;

  if (/^[a-z0-9][a-z0-9._/-]*$/i.test(normalizedName)) {
    const escaped = normalizedName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(
      `(^|[^a-z0-9_])${escaped}(?=$|[^a-z0-9_])`,
      "i",
    ).test(content);
  }
  const escaped = normalizedName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(
    `${escaped}(?=$|の件|について|に関して|案件|プロジェクト|[はをとで、。\\s])`,
    "i",
  ).test(content);
}

export function resolveProjectIdFromMessage(
  content: string,
  projects: MessageRoutableProject[],
): string | null {
  const normalizedContent = normalize(content);
  if (!normalizedContent) return null;

  const matches = projects.filter((project) => {
    if (isDefaultInboxProject(project)) return false;
    const names = [project.name, project.slug, ...(project.aliases ?? [])].filter(
      (item): item is string => Boolean(item),
    );
    return names.some((name) => containsExplicitName(normalizedContent, name));
  });
  return matches.length === 1 ? matches[0].id : null;
}

export function resolveMessageProjectId(args: {
  content: string;
  projects: MessageRoutableProject[];
  sessionProjectId?: string | null;
  fallbackProjectId?: string | null;
}): string | undefined {
  if (args.sessionProjectId) return args.sessionProjectId;
  return (
    resolveProjectIdFromMessage(args.content, args.projects) ??
    args.fallbackProjectId ??
    undefined
  );
}
