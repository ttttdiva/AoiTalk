/**
 * 認証トークン管理（expo-secure-store）
 */

import * as SecureStore from "expo-secure-store";
import { STORAGE_KEYS } from "../constants/config";
import type { UserInfo } from "../types/api";
import { normalizeApiUrl } from "./api-url";

export type AuthMode = "signed_out" | "anonymous" | "authenticated";

/** トークンを安全に保存 */
export async function saveToken(token: string): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.ACCESS_TOKEN, token);
}

/** トークンを取得 */
export async function getToken(): Promise<string | null> {
  return SecureStore.getItemAsync(STORAGE_KEYS.ACCESS_TOKEN);
}

/** トークンを削除（ログアウト時） */
export async function removeToken(): Promise<void> {
  await SecureStore.deleteItemAsync(STORAGE_KEYS.ACCESS_TOKEN);
}

/** 認証モードを保存 */
export async function saveAuthMode(mode: AuthMode): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.AUTH_MODE, mode);
}

/** 認証モードを取得 */
export async function getAuthMode(): Promise<AuthMode | null> {
  const value = await SecureStore.getItemAsync(STORAGE_KEYS.AUTH_MODE);
  if (
    value === "signed_out" ||
    value === "anonymous" ||
    value === "authenticated"
  ) {
    return value;
  }
  return null;
}

/** 認証モードを削除 */
export async function removeAuthMode(): Promise<void> {
  await SecureStore.deleteItemAsync(STORAGE_KEYS.AUTH_MODE);
}

/** 保存済みトークンの有無 */
export async function hasStoredToken(): Promise<boolean> {
  return Boolean(await getToken());
}

/** API URLを保存 */
export async function saveApiUrl(url: string): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.API_URL, normalizeApiUrl(url));
}

/** API URLを取得 */
export async function getApiUrl(): Promise<string | null> {
  return SecureStore.getItemAsync(STORAGE_KEYS.API_URL);
}

/** 選択中プロジェクトIDを保存 */
export async function saveSelectedProjectId(id: string): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.SELECTED_PROJECT_ID, id);
}

/** 選択中プロジェクトIDを取得 */
export async function getSelectedProjectId(): Promise<string | null> {
  return SecureStore.getItemAsync(STORAGE_KEYS.SELECTED_PROJECT_ID);
}

export async function saveSelectedSpaceId(id: string): Promise<void> {
  await SecureStore.setItemAsync(STORAGE_KEYS.SELECTED_SPACE_ID, id);
}

export async function getSelectedSpaceId(): Promise<string | null> {
  return SecureStore.getItemAsync(STORAGE_KEYS.SELECTED_SPACE_ID);
}

/** JWTペイロードをデコード（署名検証なし、ローカル表示・同期スコープ用） */
export function decodeTokenPayload(
  token: string,
  options: { ignoreExpiration?: boolean } = {},
): UserInfo | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = JSON.parse(atob(parts[1]));
    if (
      !options.ignoreExpiration &&
      typeof payload.exp === "number" &&
      payload.exp * 1000 <= Date.now()
    ) {
      return null;
    }
    return {
      user_id: payload.user_id,
      username: payload.username,
      role: payload.role,
    };
  } catch {
    return null;
  }
}
