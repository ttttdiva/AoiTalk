import AsyncStorage from "@react-native-async-storage/async-storage";
import type { FilesBookmark } from "../../lib/files-types";
import { normalizeFileBookmarkPath } from "./file-bookmarks";

const key = (owner: string) => `aoitalk.files.local-bookmarks.v1:${encodeURIComponent(owner)}`;

/** Device paths must never be submitted to the server's personal collection. */
export async function loadLocalFileBookmarks(owner: string): Promise<FilesBookmark[]> {
  const raw = await AsyncStorage.getItem(key(owner));
  if (!raw) return [];
  let parsed: unknown;
  try { parsed = JSON.parse(raw); } catch { return []; }
  if (!Array.isArray(parsed)) return [];
  return parsed.filter((item): item is FilesBookmark =>
    Boolean(item && typeof item === "object" && typeof item.path === "string" &&
      item.path.startsWith("file://") && typeof item.name === "string"),
  );
}

export async function setLocalFileBookmark(
  owner: string, bookmark: FilesBookmark, enabled: boolean,
): Promise<void> {
  if (!bookmark.path.startsWith("file://")) throw new Error("Local bookmark requires a device file URI");
  const path = normalizeFileBookmarkPath(bookmark.path);
  const entries = (await loadLocalFileBookmarks(owner)).filter((item) =>
    normalizeFileBookmarkPath(item.path) !== path,
  );
  if (enabled) entries.push(bookmark);
  await AsyncStorage.setItem(key(owner), JSON.stringify(entries));
}
