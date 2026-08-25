/**
 * Web Storage は privacy 設定や sandboxed document では、プロパティ取得時または
 * 各操作時に SecurityError を投げることがある。永続化不能をアプリ処理の失敗へ
 * 波及させないため、ブラウザ判定を含めてこの境界内で吸収する。
 */
type BrowserStorageName = "localStorage" | "sessionStorage";

function getBrowserStorage(name: BrowserStorageName): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window[name];
  } catch {
    return null;
  }
}

function safeStorageGetItem(
  name: BrowserStorageName,
  key: string,
): string | null {
  try {
    return getBrowserStorage(name)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function safeStorageSetItem(
  name: BrowserStorageName,
  key: string,
  value: string,
): boolean {
  try {
    const storage = getBrowserStorage(name);
    if (!storage) return false;
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function safeStorageRemoveItem(
  name: BrowserStorageName,
  key: string,
): boolean {
  try {
    const storage = getBrowserStorage(name);
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

export function safeLocalStorageGetItem(key: string): string | null {
  return safeStorageGetItem("localStorage", key);
}

export function safeLocalStorageSetItem(key: string, value: string): boolean {
  return safeStorageSetItem("localStorage", key, value);
}

export function safeLocalStorageRemoveItem(key: string): boolean {
  return safeStorageRemoveItem("localStorage", key);
}

type SafeStorageFacade = {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => boolean;
  removeItem: (key: string) => boolean;
};

/** Storageを受け取る既存のserializer向け例外安全facade。 */
export const safeLocalStorage: SafeStorageFacade = {
  getItem: safeLocalStorageGetItem,
  setItem: safeLocalStorageSetItem,
  removeItem: safeLocalStorageRemoveItem,
};

/** sessionStorageを受け取る既存serializer向け例外安全facade。 */
export const safeSessionStorage: SafeStorageFacade = {
  getItem: (key) => safeStorageGetItem("sessionStorage", key),
  setItem: (key, value) =>
    safeStorageSetItem("sessionStorage", key, value),
  removeItem: (key) => safeStorageRemoveItem("sessionStorage", key),
};
