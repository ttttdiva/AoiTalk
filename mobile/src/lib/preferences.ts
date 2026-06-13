import * as SecureStore from 'expo-secure-store';
import { STORAGE_KEYS } from '../constants/config';

export async function getDefaultCharacterName(): Promise<string> {
  return (await SecureStore.getItemAsync(STORAGE_KEYS.DEFAULT_CHARACTER_NAME)) || 'default';
}

export async function saveDefaultCharacterName(value: string): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.DEFAULT_CHARACTER_NAME, value.trim() || 'default');
}
