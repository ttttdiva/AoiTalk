import AsyncStorage from "@react-native-async-storage/async-storage";
import { getToken, getTokenAuthScope } from "./auth";
import {
  fetchApi,
  getBaseUrl,
  isApiConnectionError,
  isApiHttpError,
} from "./api-client";
import { normalizeApiUrl } from "./api-url";
import type { ManagedCharacter } from "../types/api";

const CHARACTER_CACHE_PREFIX = "aoitalk_character_profiles_v2";

/**
 * サーバー未接続・初回起動でも通常チャットを開始できる最低限の定義。
 * このslugはサーバーの初期DBでも必ずseedされる。
 */
export const OFFLINE_DEFAULT_CHARACTER: ManagedCharacter = {
  id: "offline-project-manager",
  name: "案件管理アシスタント",
  slug: "project_manager",
  character_type: "assistant",
  description: "案件・タスク・予定の整理を支援する標準キャラクターです。",
  is_enabled: true,
};

type CharacterCache = {
  cached_at: string;
  characters: ManagedCharacter[];
};

export class CharacterSlugRequiredError extends Error {
  constructor() {
    super("有効なキャラクターslugを指定してください。");
    this.name = "CharacterSlugRequiredError";
  }
}

export function requireCharacterSlug(value: string | null | undefined): string {
  const slug = String(value ?? "").trim();
  if (!slug) throw new CharacterSlugRequiredError();
  return slug;
}

function isManagedCharacter(value: unknown): value is ManagedCharacter {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.name === "string" &&
    typeof record.slug === "string" &&
    Boolean(record.slug.trim())
  );
}

async function characterCacheKey(enabledOnly: boolean): Promise<string> {
  const [baseUrl, token] = await Promise.all([getBaseUrl(), getToken()]);
  const normalizedBaseUrl = normalizeApiUrl(baseUrl).toLowerCase();
  const authScope = getTokenAuthScope(token);
  const mode = enabledOnly ? "enabled" : "all";
  return `${CHARACTER_CACHE_PREFIX}:${encodeURIComponent(normalizedBaseUrl)}:${encodeURIComponent(authScope)}:${mode}`;
}

async function cacheCharacters(
  cacheKey: string,
  characters: ManagedCharacter[],
): Promise<void> {
  const payload: CharacterCache = {
    cached_at: new Date().toISOString(),
    characters,
  };
  await AsyncStorage.setItem(cacheKey, JSON.stringify(payload)).catch(
    () => undefined,
  );
}

async function readCachedCharacters(
  cacheKey: string,
): Promise<ManagedCharacter[]> {
  const raw = await AsyncStorage.getItem(cacheKey).catch(() => null);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as Partial<CharacterCache>;
    return Array.isArray(parsed.characters)
      ? parsed.characters.filter(isManagedCharacter)
      : [];
  } catch {
    return [];
  }
}

async function readOfflineCharacters(
  enabledOnly: boolean,
  savedSlug?: string | null,
): Promise<ManagedCharacter[]> {
  let cached: ManagedCharacter[] = [];
  try {
    // full cacheを読むことで、enabled_only取得だけを先に行った端末でも
    // ローカル一覧の組み立ては常に同じになる。
    cached = await readCachedCharacters(
      await characterCacheKey(false),
    );
  } catch {
    // SecureStore/AsyncStorageの読み取り失敗でも同梱定義は返す。
  }

  const bySlug = new Map<string, ManagedCharacter>([
    [OFFLINE_DEFAULT_CHARACTER.slug, OFFLINE_DEFAULT_CHARACTER],
  ]);
  for (const character of cached) {
    bySlug.set(character.slug, character);
  }

  const normalizedSavedSlug = String(savedSlug ?? "").trim();
  if (normalizedSavedSlug && !bySlug.has(normalizedSavedSlug)) {
    // 詳細は取得できなくても、ユーザーが端末に保存した選択値は
    // オフライン中も表示・再選択できるようにする。
    bySlug.set(normalizedSavedSlug, {
      id: `offline-saved-${normalizedSavedSlug}`,
      name: normalizedSavedSlug,
      slug: normalizedSavedSlug,
      character_type: "assistant",
      description: "端末に保存されたキャラクターです。",
      is_enabled: true,
    });
  }

  const characters = [...bySlug.values()];
  return enabledOnly
    ? characters.filter((character) => character.is_enabled !== false)
    : characters;
}

/**
 * キャラクター一覧は、サーバー側の一時障害中でも最後に取得した一覧で
 * 選択を続けられるようにする。4xx は認証・権限・契約エラーの可能性が
 * あるため、キャッシュで隠さない。
 */
function canUseCharacterCache(error: unknown): boolean {
  if (isApiConnectionError(error)) return true;
  return isApiHttpError(error) && error.status >= 500 && error.status < 600;
}

export const characterApi = {
  async list(enabledOnly = false): Promise<ManagedCharacter[]> {
    const suffix = enabledOnly ? "?enabled_only=true" : "";
    const cacheKey = await characterCacheKey(enabledOnly);
    try {
      const data = await fetchApi<{
        success: boolean;
        characters: ManagedCharacter[];
      }>(`/api/characters/manage${suffix}`);
      const characters = data.characters.filter(isManagedCharacter);
      await cacheCharacters(cacheKey, characters);
      return characters;
    } catch (error) {
      if (!canUseCharacterCache(error)) throw error;
      const cached = await readCachedCharacters(cacheKey);
      if (!cached.length) throw error;
      return enabledOnly
        ? cached.filter((character) => character.is_enabled !== false)
        : cached;
    }
  },

  /** 認証済みの接続復旧時に、全プロフィールのキャッシュを更新する。 */
  async refreshCache(): Promise<ManagedCharacter[]> {
    if (!(await getToken())) return [];
    return this.list(false);
  },

  async getCachedList(enabledOnly = false): Promise<ManagedCharacter[]> {
    const cached = await readCachedCharacters(
      await characterCacheKey(enabledOnly),
    );
    return enabledOnly
      ? cached.filter((character) => character.is_enabled !== false)
      : cached;
  },

  /** 通信を発生させず、同梱定義・取得済みcache・保存済みslugから一覧を作る。 */
  async getOfflineList(
    enabledOnly = false,
    savedSlug?: string | null,
  ): Promise<ManagedCharacter[]> {
    return readOfflineCharacters(enabledOnly, savedSlug);
  },

  async getBySlug(slug: string): Promise<ManagedCharacter | null> {
    const normalized = requireCharacterSlug(slug);
    const characters = await this.list();
    return (
      characters.find(
        (character) =>
          character.slug === normalized && character.is_enabled !== false,
      ) ?? null
    );
  },

  async toggle(characterId: string): Promise<ManagedCharacter> {
    const data = await fetchApi<{
      success: boolean;
      character: ManagedCharacter;
    }>(`/api/characters/manage/${characterId}/toggle`, {
      method: "POST",
    });
    for (const enabledOnly of [false, true]) {
      const cacheKey = await characterCacheKey(enabledOnly);
      const cached = await readCachedCharacters(cacheKey);
      if (!cached.length) continue;
      const updated = cached.map((character) =>
        character.id === data.character.id ? data.character : character,
      );
      await cacheCharacters(
        cacheKey,
        enabledOnly
          ? updated.filter((character) => character.is_enabled !== false)
          : updated,
      );
    }
    return data.character;
  },
};
