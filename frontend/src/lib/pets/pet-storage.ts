import { normalizePreferences, parseManifest, type StoredPet } from "./codex-pet";
const DB_NAME = "aoitalk-codex-pets-v1";
const STORE = "pets";

async function database(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") throw new Error("このブラウザではペットを保存できません（IndexedDBが無効です）。");
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    let settled = false;
    request.onupgradeneeded = () => { request.result.createObjectStore(STORE); };
    request.onsuccess = () => {
      if (settled) { request.result.close(); return; }
      settled = true;
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
    request.onerror = () => { settled = true; reject(new Error("ペット保存領域を開けません。ブラウザの保存設定を確認してください。")); };
    request.onblocked = () => { settled = true; reject(new Error("ペット保存領域が使用中です。ほかのAoiTalkタブを閉じて再試行してください。")); };
  });
}
export async function loadPet(userId: string): Promise<StoredPet | null> {
  const db = await database();
  try {
    const value = await new Promise<unknown>((resolve, reject) => {
      const transaction = db.transaction(STORE, "readonly");
      const request = transaction.objectStore(STORE).get(userId);
      transaction.oncomplete = () => resolve(request.result);
      transaction.onabort = () => reject(transaction.error ?? new Error("ペットを読み込めません。"));
    });
    if (value === undefined) return null;
    if (!value || typeof value !== "object") throw new Error("保存済みペットの形式が不正です。再インポートしてください。");
    const stored = value as StoredPet;
    if (stored.version !== 1 || !(stored.image instanceof Blob) || stored.image.size > 12 * 1024 * 1024) throw new Error("保存済みペットの形式が不正です。再インポートしてください。");
    return { version: 1, image: stored.image, manifest: parseManifest(JSON.stringify(stored.manifest)), preferences: normalizePreferences(stored.preferences) };
  } finally { db.close(); }
}
/** Resolve on transaction completion, not merely request success (quota errors). */
export async function savePet(userId: string, pet: StoredPet | null): Promise<void> {
  const db = await database();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE, "readwrite");
      const store = transaction.objectStore(STORE);
      if (pet) store.put(pet, userId); else store.delete(userId);
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(new Error("ペットを保存できませんでした。保存容量・ブラウザ設定を確認してください。"));
    });
  } finally { db.close(); }
}
