import type { ManagedCharacter } from "../../types/api";
import { characterApi } from "../../lib/character-api";
import {
  getCurrentCharacterSlug,
  saveCurrentCharacterSlug,
} from "../../lib/preferences";
import { conversationsRepo } from "../../repositories/conversations";

export const CHARACTER_SELECTION_REQUIRED_MESSAGE =
  "利用可能な「現在のキャラクター」を設定から選択してください。";

export class CharacterSelectionRequiredError extends Error {
  constructor() {
    super(CHARACTER_SELECTION_REQUIRED_MESSAGE);
    this.name = "CharacterSelectionRequiredError";
  }
}

export function isCharacterEnabled(character: ManagedCharacter): boolean {
  return character.is_enabled !== false;
}

export function findSelectedCharacter(
  characters: readonly ManagedCharacter[],
  slug: string | null,
): ManagedCharacter | null {
  if (!slug) return null;
  return (
    characters.find(
      (character) =>
        character.slug === slug && isCharacterEnabled(character),
    ) ?? null
  );
}

export function resolveCurrentCharacterSlug(
  characters: readonly ManagedCharacter[],
  savedSlug: string | null,
): string {
  const selected = findSelectedCharacter(characters, savedSlug);
  if (selected) return selected.slug;

  const fallback = characters.find(isCharacterEnabled);
  if (!fallback) {
    throw new CharacterSelectionRequiredError();
  }
  return fallback.slug;
}

export async function getResolvedCurrentCharacterSlug(): Promise<string> {
  const savedSlug = await getCurrentCharacterSlug();

  let characters: ManagedCharacter[] | null = null;
  try {
    characters = await characterApi.list();
  } catch {
    // Offline/anonymous operation can continue with an explicit saved slug.
  }

  if (characters?.length) {
    const resolvedSlug = resolveCurrentCharacterSlug(characters, savedSlug);
    if (resolvedSlug !== savedSlug) {
      await saveCurrentCharacterSlug(resolvedSlug);
    }
    return resolvedSlug;
  }

  const offlineCharacters = await characterApi
    .getOfflineList(false, savedSlug)
    .catch(() => []);
  if (offlineCharacters.length) {
    const resolvedSlug = resolveCurrentCharacterSlug(
      offlineCharacters,
      savedSlug,
    );
    if (resolvedSlug !== savedSlug) {
      await saveCurrentCharacterSlug(resolvedSlug);
    }
    return resolvedSlug;
  }

  if (savedSlug) return savedSlug;
  throw new CharacterSelectionRequiredError();
}

/**
 * 新規チャットを開くためのローカル先行解決。
 *
 * 保存済みの選択値は端末側で既に確定しているため、ここでcharacter APIを
 * 待たない。未選択時だけ、ネットワークを使わない端末内一覧を参照する。
 */
export async function getLocalCurrentCharacterSlug(): Promise<string> {
  const savedSlug = await getCurrentCharacterSlug();
  if (savedSlug) return savedSlug;

  const offlineCharacters = await characterApi
    .getOfflineList(false, savedSlug)
    .catch(() => []);
  if (offlineCharacters.length > 0) {
    const resolvedSlug = resolveCurrentCharacterSlug(
      offlineCharacters,
      savedSlug,
    );
    await saveCurrentCharacterSlug(resolvedSlug);
    return resolvedSlug;
  }

  throw new CharacterSelectionRequiredError();
}

export async function createCurrentCharacterSession(
  projectId?: string | null,
  options?: { localFirst?: boolean },
) {
  if (options?.localFirst) {
    const characterSlug = await getLocalCurrentCharacterSlug();
    return conversationsRepo.createLocalSession(characterSlug, projectId);
  }

  const characterSlug = await getResolvedCurrentCharacterSlug();
  return conversationsRepo.createSession(characterSlug, projectId);
}
