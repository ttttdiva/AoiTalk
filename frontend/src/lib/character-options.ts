export type CharacterOption = {
  slug: string;
  name: string;
};

type CharacterPayload = {
  character_options?: unknown;
  characters?: unknown;
  current?: unknown;
};

export function normalizeCharacterOptions(
  payload: CharacterPayload,
): CharacterOption[] {
  const source = Array.isArray(payload.character_options)
    ? payload.character_options
    : [];
  const seen = new Set<string>();
  const options: CharacterOption[] = [];

  for (const value of source) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      continue;
    }
    const record = value as Record<string, unknown>;
    const slug = typeof record.slug === "string" ? record.slug.trim() : "";
    const name = typeof record.name === "string" ? record.name.trim() : "";
    if (!slug || !name || seen.has(slug)) continue;
    seen.add(slug);
    options.push({ slug, name });
  }

  if (options.length > 0) return options;
  if (!Array.isArray(payload.characters)) return [];
  for (const value of payload.characters) {
    if (typeof value !== "string") continue;
    const name = value.trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    options.push({ slug: name, name });
  }
  return options;
}

export function resolveCurrentCharacterSlug(
  options: readonly CharacterOption[],
  current: unknown,
): string {
  const value = typeof current === "string" ? current.trim() : "";
  if (!value) return "";
  const canonicalValue =
    value === "project_management_assistant" &&
    options.some((option) => option.slug === "project_manager")
      ? "project_manager"
      : value;
  return (
    options.find((option) => option.slug === canonicalValue)?.slug ??
    options.find((option) => option.name === canonicalValue)?.slug ??
    canonicalValue
  );
}

export function characterOptionLabel(
  option: CharacterOption,
  options: readonly CharacterOption[],
): string {
  const duplicateName = options.some(
    (candidate) =>
      candidate.slug !== option.slug && candidate.name === option.name,
  );
  return duplicateName ? `${option.name} (${option.slug})` : option.name;
}
