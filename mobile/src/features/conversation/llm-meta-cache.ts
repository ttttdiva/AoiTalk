import { chatApi, type LlmModeResponse } from "../../lib/chat-api";
import { getCachedToken, getTokenAuthScope } from "../../lib/auth";
import type { LlmModelCatalogResponse } from "../../types/api";
import type { SkillSlashCommand } from "./chat-commands";

/**
 * セッション入室毎に再取得していた LLM メタ情報
 * (getLlmMode / getLlmModelCatalog / listSkillSlashCommands) の
 * モジュールレベル・メモリ TTL キャッシュ。
 *
 * - TTL は 10 分。永続化はしない（サーバーモデルはオフラインでは使えないため）。
 * - 認証スコープが変化したら全キャッシュを破棄する。
 */
const TTL_MS = 10 * 60 * 1000;

type CacheEntry<T> = { scope: string; expiresAt: number; value: T };

let llmModeCache: CacheEntry<LlmModeResponse> | null = null;
let catalogCache: CacheEntry<LlmModelCatalogResponse> | null = null;
const skillCommandsCache = new Map<string, CacheEntry<SkillSlashCommand[]>>();
const llmModeFlights = new Map<string, Promise<LlmModeResponse>>();
const catalogFlights = new Map<string, Promise<LlmModelCatalogResponse>>();
const skillCommandFlights = new Map<string, Promise<SkillSlashCommand[]>>();
let lastScope: string | null = null;
let cacheGeneration = 0;

function currentScope(explicitScope?: string): string {
  return explicitScope || getTokenAuthScope(getCachedToken() ?? null);
}

/** 認証スコープが変わっていたら全キャッシュを破棄して現在スコープを記録する。 */
function ensureScope(scope: string): void {
  if (lastScope !== scope) {
    clearLlmMetaCache();
    lastScope = scope;
  }
}

function isFresh<T>(entry: CacheEntry<T> | null | undefined, scope: string): entry is CacheEntry<T> {
  return Boolean(entry && entry.scope === scope && entry.expiresAt > Date.now());
}

export async function getCachedLlmMode(explicitScope?: string): Promise<LlmModeResponse> {
  const scope = currentScope(explicitScope);
  ensureScope(scope);
  if (isFresh(llmModeCache, scope)) return llmModeCache.value;
  const existing = llmModeFlights.get(scope);
  if (existing) return existing;
  const generation = cacheGeneration;
  const flight = chatApi.getLlmMode().then((value) => {
    if (lastScope === scope && cacheGeneration === generation) {
      llmModeCache = { scope, expiresAt: Date.now() + TTL_MS, value };
    }
    return value;
  });
  llmModeFlights.set(scope, flight);
  void flight.then(
    () => {
      if (llmModeFlights.get(scope) === flight) llmModeFlights.delete(scope);
    },
    () => {
      if (llmModeFlights.get(scope) === flight) llmModeFlights.delete(scope);
    },
  );
  return flight;
}

export async function getCachedLlmModelCatalog(
  explicitScope?: string,
): Promise<LlmModelCatalogResponse> {
  const scope = currentScope(explicitScope);
  ensureScope(scope);
  if (isFresh(catalogCache, scope)) return catalogCache.value;
  const existing = catalogFlights.get(scope);
  if (existing) return existing;
  const generation = cacheGeneration;
  const flight = chatApi.getLlmModelCatalog().then((value) => {
    if (lastScope === scope && cacheGeneration === generation) {
      catalogCache = { scope, expiresAt: Date.now() + TTL_MS, value };
    }
    return value;
  });
  catalogFlights.set(scope, flight);
  void flight.then(
    () => {
      if (catalogFlights.get(scope) === flight) catalogFlights.delete(scope);
    },
    () => {
      if (catalogFlights.get(scope) === flight) catalogFlights.delete(scope);
    },
  );
  return flight;
}

export async function getCachedSkillSlashCommands(
  projectId?: string | null,
  explicitScope?: string,
): Promise<SkillSlashCommand[]> {
  const scope = currentScope(explicitScope);
  ensureScope(scope);
  const key = `${scope}:${projectId ?? ""}`;
  const entry = skillCommandsCache.get(key);
  if (isFresh(entry, scope)) return entry.value;
  const existing = skillCommandFlights.get(key);
  if (existing) return existing;
  const generation = cacheGeneration;
  const flight = chatApi.listSkillSlashCommands(projectId).then((value) => {
    if (lastScope === scope && cacheGeneration === generation) {
      skillCommandsCache.set(key, { scope, expiresAt: Date.now() + TTL_MS, value });
    }
    return value;
  });
  skillCommandFlights.set(key, flight);
  void flight.then(
    () => {
      if (skillCommandFlights.get(key) === flight) skillCommandFlights.delete(key);
    },
    () => {
      if (skillCommandFlights.get(key) === flight) skillCommandFlights.delete(key);
    },
  );
  return flight;
}

/**
 * LLM mode の最新値をキャッシュへ書き込む。
 * setLlmMode / WebSocket 由来のモード変更を反映し、次回入室で古い値を返さないようにする。
 */
export function primeLlmMode(value: LlmModeResponse, explicitScope?: string): void {
  const scope = currentScope(explicitScope);
  ensureScope(scope);
  llmModeCache = { scope, expiresAt: Date.now() + TTL_MS, value };
}

export function clearLlmMetaCache(): void {
  cacheGeneration += 1;
  llmModeCache = null;
  catalogCache = null;
  skillCommandsCache.clear();
  llmModeFlights.clear();
  catalogFlights.clear();
  skillCommandFlights.clear();
}
