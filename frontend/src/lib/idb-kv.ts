"use client";

// 低帯域環境向けの永続キャッシュ（SWR キャッシュ／チャット・Docs スナップショット）で使う、
// 依存追加なしの軽量 IndexedDB キー・バリューストア。
// - IndexedDB 非対応・失敗時は全メソッドが安全にフォールバック（no-op / 空）する。
// - 例外は握りつぶし、キャッシュ不調でアプリ本体を止めない方針。

const STORE_NAME = "kv";

function openDb(dbName: string): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    if (typeof indexedDB === "undefined") {
      resolve(null);
      return;
    }
    let settled = false;
    const settle = (value: IDBDatabase | null) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    try {
      const req = indexedDB.open(dbName, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME);
        }
      };
      req.onsuccess = () => settle(req.result);
      req.onerror = () => settle(null);
      req.onblocked = () => settle(null);
    } catch {
      settle(null);
    }
  });
}

export class IdbKvStore {
  private readonly dbName: string;
  private dbPromise: Promise<IDBDatabase | null> | null = null;

  constructor(dbName: string) {
    this.dbName = dbName;
  }

  private getDb(): Promise<IDBDatabase | null> {
    if (!this.dbPromise) this.dbPromise = openDb(this.dbName);
    return this.dbPromise;
  }

  async entries(): Promise<Array<[string, unknown]>> {
    const db = await this.getDb();
    if (!db) return [];
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(STORE_NAME, "readonly");
        const store = tx.objectStore(STORE_NAME);
        const keysReq = store.getAllKeys();
        const valuesReq = store.getAll();
        tx.oncomplete = () => {
          const keys = (keysReq.result ?? []) as IDBValidKey[];
          const values = (valuesReq.result ?? []) as unknown[];
          const out: Array<[string, unknown]> = [];
          keys.forEach((key, index) => out.push([String(key), values[index]]));
          resolve(out);
        };
        tx.onerror = () => resolve([]);
        tx.onabort = () => resolve([]);
      } catch {
        resolve([]);
      }
    });
  }

  async get<T>(key: string): Promise<T | undefined> {
    const db = await this.getDb();
    if (!db) return undefined;
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(STORE_NAME, "readonly");
        const req = tx.objectStore(STORE_NAME).get(key);
        req.onsuccess = () => resolve(req.result as T | undefined);
        req.onerror = () => resolve(undefined);
      } catch {
        resolve(undefined);
      }
    });
  }

  async set(key: string, value: unknown): Promise<void> {
    await this.bulkWrite([[key, value]], []);
  }

  async delete(key: string): Promise<void> {
    await this.bulkWrite([], [key]);
  }

  // set / delete をまとめて 1 トランザクションで書く（debounce フラッシュ用）。
  async bulkWrite(
    sets: Array<[string, unknown]>,
    deletes: string[],
  ): Promise<void> {
    if (sets.length === 0 && deletes.length === 0) return;
    const db = await this.getDb();
    if (!db) return;
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(STORE_NAME, "readwrite");
        const store = tx.objectStore(STORE_NAME);
        for (const [key, value] of sets) store.put(value, key);
        for (const key of deletes) store.delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => resolve();
        tx.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  }

  async clear(): Promise<void> {
    const db = await this.getDb();
    if (!db) return;
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(STORE_NAME, "readwrite");
        tx.objectStore(STORE_NAME).clear();
        tx.oncomplete = () => resolve();
        tx.onerror = () => resolve();
        tx.onabort = () => resolve();
      } catch {
        resolve();
      }
    });
  }
}
