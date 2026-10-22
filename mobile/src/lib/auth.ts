/**
 * 認証トークン管理（expo-secure-store）
 */

import * as SecureStore from "expo-secure-store";
import { STORAGE_KEYS } from "../constants/config";
import type { UserInfo } from "../types/api";
import { normalizeApiUrl } from "./api-url";

export type AuthMode = "signed_out" | "anonymous" | "authenticated";

/** Non-secret actor metadata used to recover a re-authentication state. */
export type AuthActor = Pick<UserInfo, "user_id" | "username" | "role">;
export type StoredAuthActor = AuthActor & { serverFingerprint: string };

const AUTH_ACTOR_METADATA_KEY = "aoitalk_auth_actor_v1";

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

function normalizeAuthActor(value: unknown): AuthActor | null {
  if (!value || typeof value !== "object") return null;
  const actor = value as Partial<Record<keyof AuthActor, unknown>>;
  if (
    typeof actor.user_id !== "string"
    || !actor.user_id.trim()
    || typeof actor.username !== "string"
    || !actor.username.trim()
    || typeof actor.role !== "string"
    || !actor.role.trim()
  ) {
    return null;
  }
  return {
    user_id: actor.user_id,
    username: actor.username,
    role: actor.role,
  };
}

/** Persist only the account identity needed to recover after token removal. */
export async function saveAuthActor(
  actor: AuthActor,
  serverFingerprint: string,
): Promise<void> {
  const normalized = normalizeAuthActor(actor);
  const normalizedServer = serverFingerprint.trim();
  if (!normalized || !normalizedServer) throw new Error("認証アカウント情報が不正です");
  await SecureStore.setItemAsync(
    AUTH_ACTOR_METADATA_KEY,
    JSON.stringify({ ...normalized, serverFingerprint: normalizedServer }),
  );
}

export type AuthActorReadResult = {
  readable: boolean;
  actor: StoredAuthActor | null;
};

/** Read actor metadata without exposing credentials; malformed data is absent. */
export async function readAuthActor(): Promise<AuthActorReadResult> {
  try {
    const raw = await SecureStore.getItemAsync(AUTH_ACTOR_METADATA_KEY);
    if (!raw) return { readable: true, actor: null };
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      const actor = normalizeAuthActor(parsed);
      const serverFingerprint = typeof parsed.serverFingerprint === "string"
        ? parsed.serverFingerprint.trim()
        : "";
      return {
        readable: true,
        actor: actor && serverFingerprint
          ? { ...actor, serverFingerprint }
          : null,
      };
    } catch {
      return { readable: true, actor: null };
    }
  } catch {
    return { readable: false, actor: null };
  }
}

/** Remove actor metadata when the user explicitly signs out or switches guest. */
export async function removeAuthActor(): Promise<void> {
  await SecureStore.deleteItemAsync(AUTH_ACTOR_METADATA_KEY);
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

/** Public, non-secret pin for one durable AoiTalk server identity. */
export type ServerIdentityPin = {
  serverId: string;
  publicKey: string;
  fingerprints: string[];
  pinnedAt: string;
};

export type ServerIdentityPinsReadResult = {
  readable: boolean;
  pins: ServerIdentityPin[];
};

const SERVER_IDENTITY_PINS_KEY = "aoitalk_server_identity_pins_v1";
const SERVER_IDENTITY_PINS_VERSION = 1;
const SERVER_ID_RE = /^[0-9a-f]{64}$/;
const BASE64URL_RE = /^[A-Za-z0-9_-]+$/;
let identityPinMutationQueue: Promise<void> = Promise.resolve();

function enqueueIdentityPinMutation<T>(mutation: () => Promise<T>): Promise<T> {
  const result = identityPinMutationQueue.then(mutation, mutation);
  identityPinMutationQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

function decodeIdentityPublicKey(value: string): Uint8Array | null {
  if (!BASE64URL_RE.test(value)) return null;
  try {
    const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
    const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    if (binary.length !== 32) return null;
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function normalizeIdentityPin(value: unknown): ServerIdentityPin | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<ServerIdentityPin>;
  if (
    typeof raw.serverId !== "string"
    || !SERVER_ID_RE.test(raw.serverId.trim().toLowerCase())
    || typeof raw.publicKey !== "string"
    || !BASE64URL_RE.test(raw.publicKey)
    || typeof raw.pinnedAt !== "string"
    || !raw.pinnedAt.trim()
    || !Array.isArray(raw.fingerprints)
  ) return null;
  const serverId = raw.serverId.trim().toLowerCase();
  const publicKey = raw.publicKey.trim();
  const publicKeyBytes = decodeIdentityPublicKey(publicKey);
  if (!publicKeyBytes || sha256Hex(publicKeyBytes) !== serverId) return null;
  const fingerprints = [...new Set(raw.fingerprints.filter(
    (fingerprint): fingerprint is string =>
      typeof fingerprint === "string" && Boolean(fingerprint.trim()),
  ).map((fingerprint) => fingerprint.trim()))];
  if (!fingerprints.length) return null;
  return {
    serverId,
    publicKey,
    fingerprints,
    pinnedAt: raw.pinnedAt.trim(),
  };
}

function parseIdentityPins(raw: string | null): ServerIdentityPin[] | null {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (parsed.version !== SERVER_IDENTITY_PINS_VERSION || !Array.isArray(parsed.pins)) {
      return null;
    }
    const pins = parsed.pins.map(normalizeIdentityPin);
    return pins.every((pin): pin is ServerIdentityPin => pin !== null)
      ? pins
      : null;
  } catch {
    return null;
  }
}

/** Read all server identity pins without exposing any credential material. */
export async function readServerIdentityPins(): Promise<ServerIdentityPinsReadResult> {
  try {
    const parsed = parseIdentityPins(await SecureStore.getItemAsync(SERVER_IDENTITY_PINS_KEY));
    return parsed ? { readable: true, pins: parsed } : { readable: false, pins: [] };
  } catch {
    return { readable: false, pins: [] };
  }
}

export async function getServerIdentityPin(
  serverId: string,
): Promise<ServerIdentityPin | null> {
  const normalized = serverId.trim().toLowerCase();
  if (!SERVER_ID_RE.test(normalized)) return null;
  const result = await readServerIdentityPins();
  if (!result.readable) return null;
  return result.pins.find((pin) => pin.serverId === normalized) ?? null;
}

export async function getServerIdentityPinForFingerprint(
  fingerprint: string,
): Promise<ServerIdentityPin | null> {
  const normalized = fingerprint.trim();
  if (!normalized) return null;
  const result = await readServerIdentityPins();
  if (!result.readable) return null;
  return result.pins.find((pin) => pin.fingerprints.includes(normalized)) ?? null;
}

/**
 * Pin a proof only after the caller has verified its nonce signature and
 * explicitly authorized this primary connection.  A URL fingerprint already
 * bound to another public key is rejected and never rebound silently.
 */
export function saveServerIdentityPin(
  pin: Pick<ServerIdentityPin, "serverId" | "publicKey" | "fingerprints">,
): Promise<void> {
  return enqueueIdentityPinMutation(async () => {
    const serverId = pin.serverId.trim().toLowerCase();
    const publicKey = pin.publicKey.trim();
    const fingerprints = [...new Set(pin.fingerprints.map((value) => value.trim()).filter(Boolean))];
    const publicKeyBytes = decodeIdentityPublicKey(publicKey);
    if (
      !SERVER_ID_RE.test(serverId)
      || !BASE64URL_RE.test(publicKey)
      || !publicKeyBytes
      || sha256Hex(publicKeyBytes) !== serverId
      || !fingerprints.length
    ) throw new Error("サーバー識別情報が不正です");

    const current = await readServerIdentityPins();
    if (!current.readable) throw new Error("サーバー識別情報を読み取れません");
    const conflicting = current.pins.find((item) =>
      item.fingerprints.some((fingerprint) => fingerprints.includes(fingerprint))
      && item.serverId !== serverId,
    );
    if (conflicting) throw new Error("接続先URLは別のサーバーへpin済みです");
    const existing = current.pins.find((item) => item.serverId === serverId);
    const merged: ServerIdentityPin = {
      serverId,
      publicKey: existing?.publicKey ?? publicKey,
      fingerprints: [...new Set([...(existing?.fingerprints ?? []), ...fingerprints])],
      pinnedAt: existing?.pinnedAt ?? new Date().toISOString(),
    };
    if (existing && existing.publicKey !== publicKey) {
      throw new Error("サーバー公開鍵がpin済みの値と一致しません");
    }
    const next = [
      ...current.pins.filter((item) => item.serverId !== serverId),
      merged,
    ];
    await SecureStore.setItemAsync(
      SERVER_IDENTITY_PINS_KEY,
      JSON.stringify({ version: SERVER_IDENTITY_PINS_VERSION, pins: next }),
    );
  });
}

/** JWTペイロードをデコード（署名検証なし、ローカル表示・同期スコープ用） */
export function decodeTokenPayload(
  token: string,
  options: { ignoreExpiration?: boolean } = {},
): UserInfo | null {
  const inspection = inspectTokenPayload(token);
  if (!inspection.structurallyValid) return null;
  if (inspection.expired && !options.ignoreExpiration) return null;
  return inspection.user;
}

export type TokenPayloadInspection = {
  user: UserInfo | null;
  structurallyValid: boolean;
  expired: boolean;
};

/** Inspect token shape/expiry without exposing the token or accepting malformed actors. */
export function inspectTokenPayload(token: string): TokenPayloadInspection {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) {
      return { user: null, structurallyValid: false, expired: false };
    }
    const payload = JSON.parse(atob(parts[1]));
    const user = normalizeAuthActor(payload);
    const expired = typeof payload.exp === "number" && payload.exp * 1000 <= Date.now();
    return { user, structurallyValid: Boolean(user), expired };
  } catch {
    return { user: null, structurallyValid: false, expired: false };
  }
}

export function sha256Hex(value: string | Uint8Array): string {
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
  const bytes: number[] = value instanceof Uint8Array ? Array.from(value) : [];
  for (let index = 0; typeof value === "string" && index < value.length; index += 1) {
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
