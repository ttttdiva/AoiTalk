import {
  filesApi,
  type FilesEntry,
  type FilesScope,
  type FilesSource,
} from "./files-api";
import { isApiConnectionError, isApiTimeoutError } from "./api-client";

export type FilesListResult = {
  currentPath: string;
  parentPath: string | null;
  canGoUp: boolean;
  isAdminMode: boolean;
  items: FilesEntry[];
};

export type FilesLocation = {
  source: FilesSource;
  scope: FilesScope;
  authScope: string;
  path?: string;
  projectId?: string | null;
};

export type FilesLocationLoad = {
  requestKey: string;
  resolvedKey: string;
  result: FilesListResult;
  fromCache: boolean;
  // clear()（認証遷移など）をまたいだ古いflightの結果。現在locationへ適用しない。
  superseded?: boolean;
  // オフラインで SQLite キャッシュから返した結果。表示はするが書き込みは無効化する。
  stale?: boolean;
  // stale 時の最終同期時刻（ISO8601）。バナー表示に使う。
  cachedAt?: string | null;
};

/**
 * The transport state relevant to a Files server listing.
 *
 * `online` is deliberately not used as a server gate: a LAN AoiTalk server
 * is usable while Android reports that the internet is unreachable.  The
 * cache only needs `connected` to decide whether a request can be attempted;
 * `serverKnownUnreachable` is a short-lived optimization supplied by the
 * network store after a read-only probe/request failed.
 */
export type FilesNetworkState = {
  connected: boolean;
  online?: boolean;
  serverKnownUnreachable?: boolean;
  serverReachable?: boolean;
  serverCheckedAt?: number | null;
};

/** A server listing was skipped because the device has no physical network. */
export class FilesNetworkUnavailableError extends Error {
  readonly code = "FILES_NETWORK_UNAVAILABLE";

  constructor() {
    super("ネットワークに接続されていないためサーバーファイルを取得できません");
    this.name = "FilesNetworkUnavailableError";
  }
}

export function isFilesNetworkUnavailableError(
  error: unknown,
): error is FilesNetworkUnavailableError {
  return (
    error instanceof FilesNetworkUnavailableError ||
    (error instanceof Error &&
      (error as Error & { code?: unknown }).code ===
        "FILES_NETWORK_UNAVAILABLE")
  );
}

type ListFiles = (
  source: FilesSource,
  path?: string,
  scope?: FilesScope,
) => Promise<FilesListResult>;

type NetworkStateProvider = () => FilesNetworkState | boolean;

// サーバー一覧のオフライン永続化レイヤー。SQLite 実装（createSqliteFilesCache）
// を注入するが、テストではメモリ実装へ差し替えられるよう抽象化する。
export type FilesCachePersistence = {
  read(
    cacheKey: string,
  ): { result: FilesListResult; cachedAt: string } | undefined;
  write(cacheKey: string, location: FilesLocation, result: FilesListResult): void;
  clearAll(): void;
};

export function filesLocationKey(location: FilesLocation): string {
  return JSON.stringify([
    location.source,
    location.scope,
    location.authScope,
    location.path ?? "",
    location.projectId ?? "",
  ]);
}

export function isFilesLoadCurrent(
  activeKey: string | null,
  load: Pick<FilesLocationLoad, "requestKey" | "resolvedKey" | "superseded">,
): boolean {
  if (load.superseded) return false;
  return activeKey === load.requestKey || activeKey === load.resolvedKey;
}

export class FilesLocationCache {
  private readonly cache = new Map<string, FilesListResult>();
  private readonly inFlight = new Map<string, Promise<FilesLocationLoad>>();
  private generation = 0;

  constructor(
    private readonly listFiles: ListFiles,
    // サーバー一覧の永続キャッシュ。未指定ならメモリのみで従来動作。
    private readonly persistence?: FilesCachePersistence,
    // 旧来の boolean callback も受け付ける。新しい呼び出し側は
    // connected と serverKnownUnreachable を分離した state を返す。
    private readonly getNetworkState: NetworkStateProvider = () => ({
      connected: true,
    }),
  ) {}

  private networkState(): FilesNetworkState & { forceStale: boolean } {
    const state = this.getNetworkState();
    // Existing tests/callers used `true` to mean "serve the stale cache". Keep
    // that compatibility while allowing the real network store to distinguish
    // a disconnected device from a connected-but-internetless LAN.
    if (typeof state === "boolean") {
      return {
        connected: true,
        serverKnownUnreachable: state,
        forceStale: state,
      };
    }
    const recentServerFailure =
      state.serverReachable === false &&
      state.serverCheckedAt !== null &&
      state.serverCheckedAt !== undefined &&
      Date.now() - state.serverCheckedAt <= 30_000;
    const forceStale =
      state.serverKnownUnreachable === true || recentServerFailure;
    return {
      connected: state.connected !== false,
      online: state.online,
      serverKnownUnreachable: forceStale,
      serverReachable: state.serverReachable,
      serverCheckedAt: state.serverCheckedAt,
      forceStale,
    };
  }

  private readCached(
    location: FilesLocation,
  ): { result: FilesListResult; cachedAt: string | null } | undefined {
    const key = filesLocationKey(location);
    // Prefer SQLite over the in-memory value for an offline result.  The
    // persisted row is the durable, auth/scope-isolated snapshot intended for
    // this exact location and survives a process restart.
    const persisted = this.persistence?.read(key);
    if (persisted) {
      return { result: persisted.result, cachedAt: persisted.cachedAt };
    }
    const memory = this.cache.get(key);
    return memory ? { result: memory, cachedAt: null } : undefined;
  }

  private staleLoad(
    requestKey: string,
    location: FilesLocation,
    cached: { result: FilesListResult; cachedAt: string | null },
    generation: number,
  ): FilesLocationLoad {
    const resolvedKey = filesLocationKey({
      ...location,
      path: cached.result.currentPath,
    });
    return {
      requestKey,
      resolvedKey,
      result: cached.result,
      fromCache: true,
      stale: true,
      cachedAt: cached.cachedAt,
      // A fallback may resolve after clear()/invalidate().  Keep this marker
      // even though the fallback itself is read-only so UI cannot apply an old
      // auth/location result to the new screen.
      superseded: generation !== this.generation,
    };
  }

  peek(location: FilesLocation): FilesListResult | undefined {
    const key = filesLocationKey(location);
    const memory = this.cache.get(key);
    if (memory) return memory;
    // メモリMiss時はサーバーソースのみ SQLite へフォールバックする。
    if (location.source === "server") {
      return this.persistence?.read(key)?.result;
    }
    return undefined;
  }

  load(
    location: FilesLocation,
    options: { revalidate?: boolean } = {},
  ): Promise<FilesLocationLoad> {
    const requestKey = filesLocationKey(location);
    const generation = this.generation;
    const network = this.networkState();
    const cached = this.cache.get(requestKey);
    if (
      cached &&
      !options.revalidate &&
      (location.source !== "server" ||
        (network.connected && !network.forceStale))
    ) {
      return Promise.resolve({
        requestKey,
        resolvedKey: requestKey,
        result: cached,
        fromCache: true,
      });
    }

    // Never start a server request when the device has no physical network.
    // A memory snapshot is still safe to show read-only, but a miss is a
    // typed failure rather than an unnecessary request that can hang.
    if (location.source === "server" && !network.connected) {
      const cached = this.readCached(location);
      if (cached) {
        return Promise.resolve(
          this.staleLoad(requestKey, location, cached, generation),
        );
      }
      return Promise.reject(new FilesNetworkUnavailableError());
    }

    // A recent server probe/request failure can serve the durable snapshot
    // immediately.  If no snapshot exists, continue with one request so an
    // empty Files screen can still recover after a transient failure.
    if (
      location.source === "server" &&
      network.forceStale
    ) {
      const cached = this.readCached(location);
      if (cached) {
        return Promise.resolve(
          this.staleLoad(requestKey, location, cached, generation),
        );
      }
    }

    const running = this.inFlight.get(requestKey);
    if (running) return running;

    const flight = this.listFiles(
      location.source,
      location.path || undefined,
      location.scope,
    )
      .then((result) => {
        const resolvedLocation = { ...location, path: result.currentPath };
        const resolvedKey = filesLocationKey(resolvedLocation);
        const superseded = generation !== this.generation;
        if (!superseded) {
          this.cache.set(requestKey, result);
          this.cache.set(resolvedKey, result);
          // サーバー一覧のみ SQLite へ永続化する（ローカルは実体が端末にあるため不要）。
          if (location.source === "server") {
            this.persistence?.write(resolvedKey, resolvedLocation, result);
            if (resolvedKey !== requestKey) {
              this.persistence?.write(requestKey, location, result);
            }
          }
        }
        return {
          requestKey,
          resolvedKey,
          result,
          fromCache: false,
          superseded,
        };
      })
      .catch((error: unknown) => {
        // Directory listing is read-only.  A transport failure or GET timeout
        // may safely fall back to the same scoped durable snapshot.  HTTP,
        // authentication, and server/application responses must propagate so
        // they are not mislabeled as offline and hidden from the user.
        if (
          location.source === "server" &&
          isFilesListTransportError(error)
        ) {
          const cached = this.readCached(location);
          if (cached) {
            return this.staleLoad(requestKey, location, cached, generation);
          }
        }
        throw error;
      })
      .finally(() => {
        if (this.inFlight.get(requestKey) === flight) {
          this.inFlight.delete(requestKey);
        }
      });
    this.inFlight.set(requestKey, flight);
    return flight;
  }

  invalidate(location: FilesLocation): void {
    const key = filesLocationKey(location);
    this.cache.delete(key);
    // A mutation may complete while a pre-mutation list request is still in
    // flight.  Drop that flight and advance the generation so its stale
    // result cannot repopulate the cache or win the subsequent revalidation.
    this.inFlight.delete(key);
    this.generation += 1;
  }

  clear(): void {
    this.generation += 1;
    this.cache.clear();
    this.inFlight.clear();
    // 認証遷移では安全側に全 auth_scope の永続キャッシュを削除する。
    this.persistence?.clearAll();
  }
}

/**
 * Errors that prove no useful server response was received for a read-only
 * listing.  In particular, `ApiTimeoutError` is included here even though it
 * is intentionally excluded from mutation reachability bookkeeping: retrying
 * the mutation would be unsafe, while serving a stale GET snapshot is safe.
 */
export function isFilesListTransportError(error: unknown): boolean {
  return isApiTimeoutError(error) || isApiConnectionError(error);
}

// ---- SQLite 永続キャッシュ実装 ----

// filer_dir_cache の 1 行分。expo-sqlite が返す生の列名に対応する。
type FilerDirCacheRow = {
  current_path?: string | null;
  parent_path?: string | null;
  can_go_up?: number | null;
  is_admin_mode?: number | null;
  items_json?: string | null;
  cached_at?: string | null;
};

type SqliteLike = {
  runSync: (sql: string, params?: unknown[]) => unknown;
  getFirstSync: (sql: string, params?: unknown[]) => unknown;
  execSync: (sql: string) => unknown;
};

const FILER_DIR_CACHE_LIMIT = 300;

export function createSqliteFilesCache(deps?: {
  getDb?: () => SqliteLike;
  ensure?: () => void;
}): FilesCachePersistence {
  const getDb =
    deps?.getDb ??
    (() => {
       
      const { getSqlite } = require("../db/client") as {
        getSqlite: () => SqliteLike;
      };
      return getSqlite();
    });
  const ensure =
    deps?.ensure ??
    (() => {
       
      const { ensureSchema } = require("../db/migrate") as {
        ensureSchema: () => void;
      };
      ensureSchema();
    });

  let ensured = false;
  const prepare = (): SqliteLike | null => {
    try {
      const db = getDb();
      if (!ensured) {
        ensure();
        ensured = true;
      }
      return db;
    } catch {
      return null;
    }
  };

  return {
    read(cacheKey) {
      const db = prepare();
      if (!db) return undefined;
      try {
        const row = db.getFirstSync(
          `SELECT current_path, parent_path, can_go_up, is_admin_mode, items_json, cached_at
             FROM filer_dir_cache WHERE cache_key = ?;`,
          [cacheKey],
        ) as FilerDirCacheRow | null | undefined;
        if (!row) return undefined;
        const items = JSON.parse(row.items_json ?? "[]") as FilesEntry[];
        return {
          result: {
            currentPath: row.current_path ?? "",
            parentPath: row.parent_path ?? null,
            canGoUp: row.can_go_up === 1,
            isAdminMode: row.is_admin_mode === 1,
            items,
          },
          cachedAt: row.cached_at ?? "",
        };
      } catch {
        return undefined;
      }
    },

    write(cacheKey, location, result) {
      const db = prepare();
      if (!db) return;
      try {
        const cachedAt = new Date().toISOString();
        db.runSync(
          `INSERT INTO filer_dir_cache
             (cache_key, source, scope, auth_scope, project_id, path,
              current_path, parent_path, can_go_up, is_admin_mode, items_json, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cache_key) DO UPDATE SET
             source = excluded.source,
             scope = excluded.scope,
             auth_scope = excluded.auth_scope,
             project_id = excluded.project_id,
             path = excluded.path,
             current_path = excluded.current_path,
             parent_path = excluded.parent_path,
             can_go_up = excluded.can_go_up,
             is_admin_mode = excluded.is_admin_mode,
             items_json = excluded.items_json,
             cached_at = excluded.cached_at;`,
          [
            cacheKey,
            location.source,
            location.scope,
            location.authScope,
            location.projectId ?? "",
            location.path ?? "",
            result.currentPath,
            result.parentPath,
            result.canGoUp ? 1 : 0,
            result.isAdminMode ? 1 : 0,
            JSON.stringify(result.items ?? []),
            cachedAt,
          ],
        );
        // 上限超過分を cached_at 昇順（古い順）に削除する。
        const countRow = db.getFirstSync(
          `SELECT COUNT(*) AS n FROM filer_dir_cache;`,
        ) as { n?: number } | undefined;
        const total = countRow?.n ?? 0;
        if (total > FILER_DIR_CACHE_LIMIT) {
          db.runSync(
            `DELETE FROM filer_dir_cache WHERE cache_key IN (
               SELECT cache_key FROM filer_dir_cache
               ORDER BY cached_at ASC LIMIT ?
             );`,
            [total - FILER_DIR_CACHE_LIMIT],
          );
        }
      } catch {
        // 永続化はベストエフォート。失敗してもメモリキャッシュで動作を継続する。
      }
    },

    clearAll() {
      const db = prepare();
      if (!db) return;
      try {
        db.execSync(`DELETE FROM filer_dir_cache;`);
      } catch {
        // no-op
      }
    },
  };
}

// オフライン判定は network store を遅延参照する（テスト・非RN環境での import 時失敗を避ける）。
function detectSingletonNetworkState(): FilesNetworkState {
  try {
    const net = require("../stores/network") as {
      isServerKnownUnreachable: () => boolean;
      useNetworkStore: {
        getState: () => {
          connected?: boolean;
          online: boolean;
        };
      };
    };
    const state = net.useNetworkStore.getState();
    return {
      // Older persisted/test stores may not expose connected.  Treat an
      // omitted value as connected so internet reachability never becomes a
      // LAN server hard gate.
      connected: state.connected !== false,
      online: state.online,
      serverKnownUnreachable: net.isServerKnownUnreachable(),
    };
  } catch {
    return { connected: true };
  }
}

export const filesLocationCache = new FilesLocationCache(
  (source, path, scope) => filesApi.list(source, path, scope),
  createSqliteFilesCache(),
  detectSingletonNetworkState,
);
