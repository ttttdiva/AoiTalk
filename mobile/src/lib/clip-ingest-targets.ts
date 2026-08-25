/**
 * クリップ取り込み先（サーバー `user_settings.clip_ingest.targets`）のローカルキャッシュ。
 *
 * サーバー未到達時にモバイルLLMだけで取り込みを完結させるため、オンライン時に
 * 取得した取り込み先設定を SQLite へ保存しておく。読み取りは同期 SQLite で行い、
 * 取得できない場合は最後に保存したキャッシュを返す（`files-location-cache` と同じ流儀）。
 *
 * キャッシュ行は認証スコープ（`auth:<user_id>` / 'anonymous'）ごとに分ける。
 * 別ユーザーの取り込み先を読み書きしないためで、logout・スコープ変更時は
 * `clearCachedClipIngestTargets()` で全スコープ分を削除する。
 */

import { taskApi } from "./task-api";
import { getToken, getTokenAuthScope } from "./auth";
import type { ClipIngestTargetSetting } from "../types/api";

/** 正規化済みの取り込み先。node_id と node_system_key の少なくとも一方を持つ。 */
export interface ClipIngestTarget {
  nodeId: string;
  nodeSystemKey: string;
  label: string;
  breadcrumb: string[];
  routingHint: string;
  fallback: boolean;
  /**
   * `fallback` is kept backwards compatible with the server's "first fallback
   * wins" representation.  This bit remembers that the setting explicitly
   * marked the row as a fallback as well, so a second fallback row cannot leak
   * into the normal router candidate list.
   */
  fallbackConfigured?: boolean;
}

type SqliteLike = {
  runSync: (sql: string, params?: unknown[]) => unknown;
  getFirstSync: (sql: string, params?: unknown[]) => unknown;
  execSync: (sql: string) => unknown;
};

function cleanText(value: unknown, limit: number): string {
  return String(value ?? "").trim().slice(0, limit);
}

const FILM_ROOT_SYSTEM_KEY = "foam_source_grounded_v1:root.Film";

function isAllowedTarget(
  breadcrumb: string[],
  nodeSystemKey: string,
): boolean {
  return (
    breadcrumb[0] !== "Film" &&
    nodeSystemKey !== FILM_ROOT_SYSTEM_KEY
  );
}

/** Stable identifier used by the local router.  Empty/malformed cache rows
 * deliberately have no identifier and must never become LLM candidates. */
export function getClipIngestTargetId(target: Partial<ClipIngestTarget>): string {
  const nodeId = cleanText(target.nodeId, 100);
  if (nodeId) return nodeId;
  const nodeSystemKey = cleanText(target.nodeSystemKey, 500);
  return nodeSystemKey ? `key:${nodeSystemKey}` : "";
}

export function isClipIngestFallbackTarget(
  target: Partial<ClipIngestTarget>,
): boolean {
  return target.fallback === true || target.fallbackConfigured === true;
}

/** サーバー設定 JSON を正規化する。enabled=false と壊れた行は除外する。 */
export function parseClipIngestTargets(
  raw: ClipIngestTargetSetting[] | null | undefined,
): ClipIngestTarget[] {
  if (!Array.isArray(raw)) return [];
  const targets: ClipIngestTarget[] = [];
  const seen = new Set<string>();
  let fallbackCount = 0;
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    // サーバー（`clip_ingest_service._load_targets`）は `enabled is True` だけを
    // 採用する。未指定・null・truthyな別値はいずれも無効として扱う。
    if (item.enabled !== true) continue;
    const nodeId = cleanText(item.node_id, 100);
    const nodeSystemKey = cleanText(item.node_system_key, 500);
    if (!nodeId && !nodeSystemKey) continue;
    const breadcrumb = Array.isArray(item.breadcrumb)
      ? item.breadcrumb
          .map((value) => cleanText(value, 200))
          .filter((value) => value.length > 0)
      : [];
    if (!isAllowedTarget(breadcrumb, nodeSystemKey)) continue;
    const dedupeKey = nodeId || `key:${nodeSystemKey}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    // fallback はサーバー仕様上1件まで。2件目以降も明示fallbackとして記録し、
    // ローカルrouterの通常候補へ漏らさない。
    const explicitlyFallback = item.fallback === true;
    const fallback = explicitlyFallback && fallbackCount === 0;
    if (explicitlyFallback) fallbackCount += 1;
    const normalized: ClipIngestTarget = {
      nodeId,
      nodeSystemKey,
      label: cleanText(item.label, 200),
      breadcrumb,
      routingHint: cleanText(item.routing_hint, 1000),
      fallback,
    };
    // 2件目以降の明示fallbackだけ、既存の公開shapeを壊さずに記録する。
    // 先頭は fallback=true 自体で判別できる。
    if (explicitlyFallback && !fallback) normalized.fallbackConfigured = true;
    targets.push(normalized);
  }
  return targets;
}

// SQLite は遅延 require する（テスト・非RN環境での import 時失敗を避ける）。
function getSqliteOrNull(): SqliteLike | null {
  try {
    const { getSqlite } = require("../db/client") as {
      getSqlite: () => SqliteLike;
    };
    const { ensureSchema } = require("../db/migrate") as {
      ensureSchema: () => void;
    };
    const db = getSqlite();
    ensureSchema();
    return db;
  } catch {
    return null;
  }
}

/** 現在の認証スコープ。キャッシュ行のキーに使う。 */
export async function getClipIngestCacheScope(): Promise<string> {
  return getTokenAuthScope(await getToken());
}

export function readCachedClipIngestTargets(
  authScope: string,
): ClipIngestTarget[] | null {
  const db = getSqliteOrNull();
  if (!db) return null;
  try {
    const row = db.getFirstSync(
      `SELECT targets_json FROM clip_ingest_target_cache WHERE cache_key = ?;`,
      [authScope],
    ) as { targets_json?: string | null } | null | undefined;
    if (!row?.targets_json) return null;
    const parsed = JSON.parse(row.targets_json) as ClipIngestTarget[];
    return Array.isArray(parsed)
      ? parsed
          .filter(
            (target) =>
              target &&
              Array.isArray(target.breadcrumb) &&
              getClipIngestTargetId(target) &&
              isAllowedTarget(target.breadcrumb, target.nodeSystemKey),
          )
          .map((target) => {
            const fallbackConfigured = target.fallbackConfigured === true;
            const rest = { ...target };
            delete rest.fallbackConfigured;
            return {
              ...rest,
              nodeId: cleanText(target.nodeId, 100),
              nodeSystemKey: cleanText(target.nodeSystemKey, 500),
              label: cleanText(target.label, 200),
              breadcrumb: target.breadcrumb
                .map((value) => cleanText(value, 200))
                .filter(Boolean),
              routingHint: cleanText(target.routingHint, 1000),
              fallback: target.fallback === true,
              ...(fallbackConfigured ? { fallbackConfigured: true } : {}),
            };
          })
      : null;
  } catch {
    return null;
  }
}

export function writeCachedClipIngestTargets(
  authScope: string,
  targets: ClipIngestTarget[],
): void {
  const db = getSqliteOrNull();
  if (!db) return;
  try {
    db.runSync(
      `INSERT INTO clip_ingest_target_cache (cache_key, targets_json, cached_at)
       VALUES (?, ?, ?)
       ON CONFLICT(cache_key) DO UPDATE SET
         targets_json = excluded.targets_json,
         cached_at = excluded.cached_at;`,
      [authScope, JSON.stringify(targets), new Date().toISOString()],
    );
  } catch {
    // キャッシュ更新はベストエフォート。失敗しても取り込み自体は継続する。
  }
}

// 取り込み先設定はほとんど変わらないので、毎同期の GET を避けて TTL で間引く。
// llm-meta-cache と同じ 10 分。authScope 単位に持ち、ユーザーが変われば取り直す。
const REFRESH_TTL_MS = 10 * 60 * 1000;
const refreshedAt = new Map<string, number>();

/** logout・認証スコープ変更時に全スコープ分のキャッシュを捨てる。 */
export function clearCachedClipIngestTargets(): void {
  // キャッシュを捨てたら TTL も捨てる。次の同期で必ず取り直させる。
  refreshedAt.clear();
  const db = getSqliteOrNull();
  if (!db) return;
  try {
    db.execSync(`DELETE FROM clip_ingest_target_cache;`);
  } catch {
    // no-op
  }
}

/** サーバーから取り込み先設定を取得してキャッシュを更新する。失敗時は null。 */
export async function refreshClipIngestTargets(): Promise<
  ClipIngestTarget[] | null
> {
  try {
    const authScope = await getClipIngestCacheScope();
    const settings = await taskApi.getUserSettings();
    const targets = parseClipIngestTargets(settings.clip_ingest?.targets);
    writeCachedClipIngestTargets(authScope, targets);
    // 取得できたときだけ TTL を進める（失敗時は次の同期で再試行する）。
    refreshedAt.set(authScope, Date.now());
    return targets;
  } catch {
    return null;
  }
}

/**
 * TTL を過ぎている場合だけ取り込み先設定を取り直す。同期のたびに
 * `GET /api/users/me/settings` を叩かないための間引き。
 */
export async function refreshClipIngestTargetsIfStale(): Promise<void> {
  const authScope = await getClipIngestCacheScope();
  const lastAt = refreshedAt.get(authScope) ?? 0;
  if (Date.now() - lastAt < REFRESH_TTL_MS) return;
  await refreshClipIngestTargets();
}

/**
 * 取り込み先を解決する。サーバーから取れればそれを使い、
 * 取れなければ現在の認証スコープで最後に保存したキャッシュを返す。
 * どちらも無ければ空配列。
 */
export async function loadClipIngestTargets(options?: {
  allowRemote?: boolean;
}): Promise<ClipIngestTarget[]> {
  if (options?.allowRemote !== false) {
    const remote = await refreshClipIngestTargets();
    if (remote) return remote;
  }
  return readCachedClipIngestTargets(await getClipIngestCacheScope()) ?? [];
}
