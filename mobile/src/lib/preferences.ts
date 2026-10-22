import * as SecureStore from 'expo-secure-store';
import { STORAGE_KEYS } from '../constants/config';

export async function getChatAvatarEnabled(): Promise<boolean> {
  return (await SecureStore.getItemAsync(STORAGE_KEYS.CHAT_AVATAR_ENABLED)) === 'true';
}

export async function saveChatAvatarEnabled(enabled: boolean): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.CHAT_AVATAR_ENABLED, String(enabled));
}

/**
 * Read the device-only chat background preference.  A missing or malformed
 * value intentionally resolves to the product default (enabled); an explicit
 * `false` is preserved across launches.
 */
export async function getChatWaveBackgroundEnabled(): Promise<boolean> {
  const stored = await SecureStore.getItemAsync(
    STORAGE_KEYS.CHAT_WAVE_BACKGROUND_ENABLED,
  );
  if (stored === "false") return false;
  return true;
}

export async function saveChatWaveBackgroundEnabled(enabled: boolean): Promise<void> {
  await SecureStore.setItemAsync(
    STORAGE_KEYS.CHAT_WAVE_BACKGROUND_ENABLED,
    String(enabled),
  );
}

function chatIntentPreferenceKey(scope: string): string {
  // SecureStore keys allow only alphanumerics, '.', '-' and '_'. Encode the
  // complete server/account scope without collisions or credential material.
  const suffix = Array.from(scope).map((value) => value.codePointAt(0)!.toString(16).padStart(6, '0')).join('');
  return `${STORAGE_KEYS.CHAT_INTENT_CONFIRMATION_ENABLED}.${suffix}`;
}

export async function getChatIntentConfirmationEnabled(scope: string): Promise<boolean> {
  return (await SecureStore.getItemAsync(chatIntentPreferenceKey(scope))) === 'true';
}

export async function saveChatIntentConfirmationEnabled(scope: string, enabled: boolean): Promise<void> {
  await SecureStore.setItemAsync(chatIntentPreferenceKey(scope), String(enabled));
}

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
