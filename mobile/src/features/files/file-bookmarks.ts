import type { FilesBookmark, FilesScope, FilesSource } from "../../lib/files-types";

export function normalizeFileBookmarkPath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
}

/** Filter presentation only. Space-wide shared collections remain unchanged. */
export function visibleFileBookmarks(
  bookmarks: readonly FilesBookmark[],
  location: {
    source: FilesSource;
    scope: FilesScope;
    rootPath: string;
    projectId?: string | null;
  },
): FilesBookmark[] {
  const root = normalizeFileBookmarkPath(location.rootPath);
  // The admin aggregate root is not a project: never dump every project's
  // bookmarks into that view. User and Local views also need an explicit root.
  if (!root || (location.source === "server" && location.scope === "workspace" && !location.projectId)) {
    return [];
  }
  const seen = new Set<string>();
  return bookmarks.filter((bookmark) => {
    if (!bookmark.path || bookmark.kind === "folder") return false;
    const path = normalizeFileBookmarkPath(bookmark.path);
    if (path !== root && !path.startsWith(`${root}/`)) return false;
    if (seen.has(path)) return false;
    seen.add(path);
    return true;
  });
}

export function fileBookmarkRelativePath(path: string, rootPath: string): string {
  const normalized = normalizeFileBookmarkPath(path);
  const root = normalizeFileBookmarkPath(rootPath);
  if (normalized === root) return "/";
  return root && normalized.startsWith(`${root}/`)
    ? normalized.slice(root.length)
    : normalized;
}
