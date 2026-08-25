"use client";

import { IdbKvStore } from "@/lib/idb-kv";

// 低帯域環境向けの永続キャッシュ基盤。
// - SWR キャッシュ（データ系キーのみ）を IndexedDB に永続化し、起動時ハイドレート→即描画を実現する。
// - チャットのメッセージ／Docs bootstrap も同じ DB に別名で保存し、ログアウト時に一括破棄する。

const LEGACY_DB_NAME = "aoitalk-cache";
const DB_NAME_PREFIX = "aoitalk-cache";
// SWR キャッシュ由来のエントリは IndexedDB 上で衝突を避けるためこの接頭辞を付ける。
const SWR_ENTRY_PREFIX = "swr::";

// 永続化対象の SWR キー（データ系のみ）。通知・ヘルス等の揮発データは永続化しない。
const SWR_PERSIST_PREFIXES = ["tasks-page/"];

// チャット／Docs スナップショットの直書きキー接頭辞（ログアウト clear の対象）。
export const CHAT_MESSAGES_CACHE_PREFIX = "chat/messages/";
export const DOCS_BOOTSTRAP_CACHE_PREFIX = "docs/bootstrap/";

let activeUserScope = "anon";
let store: IdbKvStore | null = null;
let legacyCleanupStarted = false;
let cacheEpoch = 0;
let snapshotWritesBlocked = false;

function databaseNameForScope(scope: string): string {
  // user id は UUID を想定するが、将来別形式になっても DB 名として安全な形にする。
  return `${DB_NAME_PREFIX}::${encodeURIComponent(scope)}`;
}

/**
 * 永続キャッシュを現在ユーザー専用の IndexedDB に切り替える。
 *
 * Provider の render 中、子コンポーネントがキャッシュを読む前に同期的に呼ぶ。
 * ユーザーごとに物理 DB を分離することで、認証確認の非同期処理を待たずとも
 * 別ユーザーの Docs / Tasks / Chat を読み出さない。
 */
export function configurePersistentCacheUser(userId: string | null): void {
  const nextScope = userId ?? "anon";
  if (nextScope === activeUserScope && store) return;
  cacheEpoch += 1;
  snapshotWritesBlocked = false;
  activeUserScope = nextScope;
  store = new IdbKvStore(databaseNameForScope(activeUserScope));
}

export function getPersistentStore(): IdbKvStore {
  if (!store) {
    store = new IdbKvStore(databaseNameForScope(activeUserScope));
  }
  return store;
}

function shouldPersistSwrKey(key: unknown): key is string {
  return (
    typeof key === "string" &&
    SWR_PERSIST_PREFIXES.some((prefix) => key.startsWith(prefix))
  );
}

// ─── ログアウト／ユーザー切替時のキャッシュ破棄 ───

export async function clearPersistentCache(): Promise<void> {
  // logout中に開始済みのDocs/Chat書込みがclear後に復活しないよう無効化する。
  snapshotWritesBlocked = true;
  cacheEpoch += 1;
  try {
    await getPersistentStore().clear();
  } catch {
    // キャッシュ破棄失敗はアプリ動作に影響させない。
  }
}

/**
 * 旧版のユーザー非分離 DB を一度だけ破棄する。
 *
 * 旧 DB は以後読み込まないため情報露出は起こらないが、端末上に平文キャッシュを
 * 残さないため best-effort で消去する。
 */
export async function discardLegacyPersistentCache(): Promise<void> {
  if (legacyCleanupStarted) return;
  legacyCleanupStarted = true;
  try {
    await new IdbKvStore(LEGACY_DB_NAME).clear();
  } catch {
    // 旧キャッシュは読み込まないため、削除失敗でも現在ユーザーの表示には影響しない。
  }
}

// ─── チャット／Docs スナップショットの読み書き ───

export async function readCachedSnapshot<T>(key: string): Promise<T | undefined> {
  try {
    return await getPersistentStore().get<T>(key);
  } catch {
    return undefined;
  }
}

export async function writeCachedSnapshot(
  key: string,
  value: unknown,
): Promise<void> {
  if (snapshotWritesBlocked) return;
  const targetStore = getPersistentStore();
  const writeEpoch = cacheEpoch;
  try {
    await targetStore.set(key, value);
    if (snapshotWritesBlocked || writeEpoch !== cacheEpoch) {
      // ユーザー切替/logoutと競合した旧書込みだけを書込み先DBから除去する。
      await targetStore.delete(key);
    }
  } catch {
    // 保存失敗は無視（次回取得で再構築される）。
  }
}

// ─── SWR 永続キャッシュ実装 ───

export type SwrState = {
  data?: unknown;
  error?: unknown;
  isValidating?: boolean;
  isLoading?: boolean;
  [key: string]: unknown;
};

const activePersistentCaches = new Set<PersistentSwrCache>();

export async function discardPendingPersistentWrites(): Promise<void> {
  await Promise.all(
    Array.from(activePersistentCaches, (cache) => cache.dispose()),
  );
}

/**
 * SWR の cache provider 実装。内部 Map をラップし、データ系キーの更新を
 * debounce して IndexedDB へ書き出す。永続化するのは data のみ（error/各種フラグは揮発）。
 */
export class PersistentSwrCache {
  private readonly map: Map<string, SwrState>;
  private readonly persistent: IdbKvStore;
  private readonly pendingSets = new Map<string, SwrState>();
  private readonly pendingDeletes = new Set<string>();
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private flushPromise: Promise<void> | null = null;
  private disposed = false;
  private readonly flushDelayMs: number;

  constructor(
    initial: Map<string, SwrState>,
    persistent: IdbKvStore,
    flushDelayMs = 1500,
  ) {
    this.map = initial;
    this.persistent = persistent;
    this.flushDelayMs = flushDelayMs;
    activePersistentCaches.add(this);
  }

  keys(): IterableIterator<string> {
    return this.map.keys();
  }

  get(key: string): SwrState | undefined {
    return this.map.get(key);
  }

  set(key: string, value: SwrState): void {
    this.map.set(key, value);
    if (!this.disposed && shouldPersistSwrKey(key)) {
      this.pendingDeletes.delete(key);
      this.pendingSets.set(key, value);
      this.scheduleFlush();
    }
  }

  delete(key: string): void {
    this.map.delete(key);
    if (!this.disposed && shouldPersistSwrKey(key)) {
      this.pendingSets.delete(key);
      this.pendingDeletes.add(key);
      this.scheduleFlush();
    }
  }

  private scheduleFlush(): void {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      const pending = this.flush();
      this.flushPromise = pending;
      void pending.finally(() => {
        if (this.flushPromise === pending) this.flushPromise = null;
      });
    }, this.flushDelayMs);
  }

  private async flush(): Promise<void> {
    if (this.disposed) return;
    const sets: Array<[string, unknown]> = [];
    for (const [key, value] of this.pendingSets) {
      if (value && typeof value === "object" && value.data !== undefined) {
        // data のみを保存（エラー・validating 等の揮発状態は永続化しない）。
        sets.push([`${SWR_ENTRY_PREFIX}${key}`, { data: value.data }]);
      } else {
        this.pendingDeletes.add(key);
      }
    }
    const deletes = Array.from(this.pendingDeletes).map(
      (key) => `${SWR_ENTRY_PREFIX}${key}`,
    );
    this.pendingSets.clear();
    this.pendingDeletes.clear();
    await this.persistent.bulkWrite(sets, deletes);
  }

  async dispose(): Promise<void> {
    if (!this.disposed) {
      this.disposed = true;
      if (this.flushTimer) {
        clearTimeout(this.flushTimer);
        this.flushTimer = null;
      }
      this.pendingSets.clear();
      this.pendingDeletes.clear();
      activePersistentCaches.delete(this);
    }
    await this.flushPromise;
  }
}

/**
 * IndexedDB から SWR キャッシュエントリを読み出し、Map に復元する。
 * SWR provider に渡す前段のハイドレート処理。
 */
export async function hydrateSwrCacheMap(
  target: Map<string, SwrState>,
): Promise<void> {
  try {
    const entries = await getPersistentStore().entries();
    for (const [key, value] of entries) {
      if (!key.startsWith(SWR_ENTRY_PREFIX)) continue;
      if (!value || typeof value !== "object") continue;
      const swrKey = key.slice(SWR_ENTRY_PREFIX.length);
      target.set(swrKey, value as SwrState);
    }
  } catch {
    // ハイドレート失敗時は空キャッシュで続行（従来挙動）。
  }
}
