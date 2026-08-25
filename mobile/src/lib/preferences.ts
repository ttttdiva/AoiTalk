import * as SecureStore from 'expo-secure-store';
import { STORAGE_KEYS } from '../constants/config';

export async function getCurrentCharacterSlug(): Promise<string | null> {
  const value = await SecureStore.getItemAsync(STORAGE_KEYS.CURRENT_CHARACTER_SLUG);
  const normalized = value?.trim();
  return normalized || null;
}

export async function saveCurrentCharacterSlug(value: string): Promise<void> {
  const normalized = value.trim();
  if (!normalized) {
    throw new Error("現在のキャラクターを選択してください。");
  }
  await SecureStore.setItemAsync(
    STORAGE_KEYS.CURRENT_CHARACTER_SLUG,
    normalized,
  );
}
