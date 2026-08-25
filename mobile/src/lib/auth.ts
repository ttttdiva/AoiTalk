/**
 * 認証トークン管理（expo-secure-store）
 */

import * as SecureStore from "expo-secure-store";
import { STORAGE_KEYS } from "../constants/config";
import type { UserInfo } from "../types/api";
import { normalizeApiUrl } from "./api-url";

export type AuthMode = "signed_out" | "anonymous" | "authenticated";

let cachedToken: string | null | undefined;
let tokenLoadPromise: Promise<string | null> | null = null;
let tokenRevision = 0;
let tokenMutationQueue: Promise<void> = Promise.resolve();

function enqueueTokenMutation<T>(mutation: () => Promise<T>): Promise<T> {
  const result = tokenMutationQueue.then(mutation, mutation);
  tokenMutationQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

/** トークンを安全に保存 */
export function saveToken(token: string): Promise<void> {
  return enqueueTokenMutation(async () => {
    await SecureStore.setItemAsync(STORAGE_KEYS.ACCESS_TOKEN, token);
    tokenRevision += 1;
    cachedToken = token;
  });
}

/** トークンを取得 */
export function getToken(): Promise<string | null> {
  if (cachedToken !== undefined) return Promise.resolve(cachedToken);
  if (tokenLoadPromise) return tokenLoadPromise;

  const revision = tokenRevision;
  tokenLoadPromise = SecureStore.getItemAsync(STORAGE_KEYS.ACCESS_TOKEN)
    .then((token) => {
      if (revision !== tokenRevision) return cachedToken ?? null;
      cachedToken = token;
      return token;
    })
    .finally(() => {
      tokenLoadPromise = null;
    });
  return tokenLoadPromise;
}

/** SecureStoreを待たずに、既に解決済みの認証トークンを参照する。 */
export function getCachedToken(): string | null | undefined {
  return cachedToken;
}

/** refresh等の長い処理を開始する時点のtokenと世代を原子的に読む。 */
export async function getTokenSnapshot(): Promise<{
  token: string | null;
  revision: number;
}> {
  await tokenMutationQueue;
  const token = await getToken();
  return { token, revision: tokenRevision };
}

/** 認証世代が変わっていない場合だけrefresh結果を保存する。 */
export function saveTokenIfRevision(
  token: string,
  expectedRevision: number,
): Promise<boolean> {
  return enqueueTokenMutation(async () => {
    if (tokenRevision !== expectedRevision) return false;
    await SecureStore.setItemAsync(STORAGE_KEYS.ACCESS_TOKEN, token);
    tokenRevision += 1;
    cachedToken = token;
    return true;
  });
}

/** トークンを削除（ログアウト時） */
export function removeToken(): Promise<void> {
  return enqueueTokenMutation(async () => {
    await SecureStore.deleteItemAsync(STORAGE_KEYS.ACCESS_TOKEN);
    tokenRevision += 1;
    cachedToken = null;
  });
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

function sha256Hex(value: string): string {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b,
    0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
    0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7,
    0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152,
    0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
    0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
    0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f,
    0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const bytes: number[] = [];
  for (let index = 0; index < value.length; index += 1) {
    let codePoint = value.charCodeAt(index);
    if (
      codePoint >= 0xd800 &&
      codePoint <= 0xdbff &&
      index + 1 < value.length
    ) {
      const low = value.charCodeAt(index + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        codePoint = 0x10000 + ((codePoint - 0xd800) << 10) + (low - 0xdc00);
        index += 1;
      }
    }
    if (codePoint < 0x80) {
      bytes.push(codePoint);
    } else if (codePoint < 0x800) {
      bytes.push(0xc0 | (codePoint >>> 6), 0x80 | (codePoint & 0x3f));
    } else if (codePoint < 0x10000) {
      bytes.push(
        0xe0 | (codePoint >>> 12),
        0x80 | ((codePoint >>> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    } else {
      bytes.push(
        0xf0 | (codePoint >>> 18),
        0x80 | ((codePoint >>> 12) & 0x3f),
        0x80 | ((codePoint >>> 6) & 0x3f),
        0x80 | (codePoint & 0x3f),
      );
    }
  }

  const bitLength = bytes.length * 8;
  bytes.push(0x80);
  while (bytes.length % 64 !== 56) bytes.push(0);
  const highLength = Math.floor(bitLength / 0x100000000);
  const lowLength = bitLength >>> 0;
  for (let shift = 24; shift >= 0; shift -= 8) {
    bytes.push((highLength >>> shift) & 0xff);
  }
  for (let shift = 24; shift >= 0; shift -= 8) {
    bytes.push((lowLength >>> shift) & 0xff);
  }

  const state = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f,
    0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const rotateRight = (word: number, amount: number) =>
    (word >>> amount) | (word << (32 - amount));
  const words = new Uint32Array(64);
  for (let offset = 0; offset < bytes.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      const position = offset + index * 4;
      words[index] =
        ((bytes[position] << 24) |
          (bytes[position + 1] << 16) |
          (bytes[position + 2] << 8) |
          bytes[position + 3]) >>>
        0;
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 =
        rotateRight(words[index - 15], 7) ^
        rotateRight(words[index - 15], 18) ^
        (words[index - 15] >>> 3);
      const s1 =
        rotateRight(words[index - 2], 17) ^
        rotateRight(words[index - 2], 19) ^
        (words[index - 2] >>> 10);
      words[index] =
        (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }

    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const sum1 =
        rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + sum1 + choice + constants[index] + words[index]) >>> 0;
      const sum0 =
        rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    state[0] = (state[0] + a) >>> 0;
    state[1] = (state[1] + b) >>> 0;
    state[2] = (state[2] + c) >>> 0;
    state[3] = (state[3] + d) >>> 0;
    state[4] = (state[4] + e) >>> 0;
    state[5] = (state[5] + f) >>> 0;
    state[6] = (state[6] + g) >>> 0;
    state[7] = (state[7] + h) >>> 0;
  }
  return state.map((word) => word.toString(16).padStart(8, "0")).join("");
}

/** tokenからユーザー単位のcache/sync scopeを得る。 */
export function getTokenAuthScope(token: string | null | undefined): string {
  if (!token) return "anonymous";
  const user = decodeTokenPayload(token, { ignoreExpiration: true });
  if (user?.user_id) return `auth:${user.user_id}`;

  // Opaque tokens have no decodable account id. Use a non-reversible,
  // fixed-width fingerprint so accounts remain isolated without exposing the token.
  return `auth:opaque:${sha256Hex(token)}`;
}
