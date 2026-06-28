export const GENERATION_PROFILE_STORAGE_KEY = "aoitalk-generation-profile";

export const GENERATION_PROFILE_VALUES = [
  "chat",
  "assisted_work",
  "autonomous_work",
  "review",
] as const;

export type GenerationProfile = (typeof GENERATION_PROFILE_VALUES)[number];

export const DEFAULT_GENERATION_PROFILE: GenerationProfile = "chat";

const VALID_GENERATION_PROFILES = new Set<string>(GENERATION_PROFILE_VALUES);

type GenerationProfileStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function normalizeGenerationProfile(
  value: unknown,
): GenerationProfile | null {
  if (typeof value !== "string") return null;
  return VALID_GENERATION_PROFILES.has(value)
    ? (value as GenerationProfile)
    : null;
}

export function loadStoredGenerationProfile(
  storage: GenerationProfileStorage | null | undefined,
): GenerationProfile {
  if (!storage) return DEFAULT_GENERATION_PROFILE;
  const stored = normalizeGenerationProfile(
    storage.getItem(GENERATION_PROFILE_STORAGE_KEY),
  );
  if (!stored) {
    storage.removeItem(GENERATION_PROFILE_STORAGE_KEY);
  }
  return stored ?? DEFAULT_GENERATION_PROFILE;
}

export function saveStoredGenerationProfile(
  storage: GenerationProfileStorage | null | undefined,
  profile: GenerationProfile,
) {
  if (!storage) return;
  storage.setItem(GENERATION_PROFILE_STORAGE_KEY, profile);
}

export function getSettingsGenerationProfile(
  settings: Record<string, unknown>,
): GenerationProfile | null {
  const chat = settings.chat;
  if (typeof chat !== "object" || chat === null || Array.isArray(chat)) {
    return null;
  }
  return normalizeGenerationProfile(
    (chat as Record<string, unknown>).generation_profile,
  );
}
