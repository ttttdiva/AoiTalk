import type { ExplorerBookmark } from "@/lib/explorer-api";
import {
  isExplorerBookmarkFolder,
  isBookmarkFolderPath,
} from "@/lib/explorer-bookmark-tree";
import {
  bookmarkOwnerFromItem,
  bookmarkOwnerFromScope,
  type ExplorerBookmarkOwner,
  type BookmarkOwnerLike,
} from "@/lib/explorer-bookmark-ownership";

export type ExecuteExplorerBookmarkOptions = {
  closeEditor: () => void;
  navigate: (path: string) => void;
  focusFilesRoot: () => void;
  /**
   * Explicit endpoint provenance for this row.  Personal rows must never
   * invoke ProjectContext selection because absolute/external paths do not
   * have a same-Space Project owner.
   */
  owner?: ExplorerBookmarkOwner | BookmarkOwnerLike | null;
  /**
   * Select a same-Space target Project through ProjectContext and wait until
   * its canonical root is loaded before navigating into the target path.
   */
  selectProjectForPath?: (path: string) => Promise<boolean>;
};

/**
 * Canonical Files bookmark execution: close editor, navigate, focus canvas.
 * Folders and path-less entries are no-ops.
 */
export async function executeExplorerBookmark(
  item: ExplorerBookmark,
  options: ExecuteExplorerBookmarkOptions,
): Promise<void> {
  if (isExplorerBookmarkFolder(item)) return;
  // Check a trimmed view for malformed/path-less rows but preserve the exact
  // persisted target string for navigation.  In particular, do not rewrite
  // absolute paths or separators while opening a bookmark.
  const rawPath = typeof item.path === "string" ? item.path : "";
  const path = rawPath.trim();
  if (!path || isBookmarkFolderPath(path)) return;
  options.closeEditor();

  // The owner is explicit for newly-projected rows.  For legacy callers that
  // do not provide it, infer only a server-projected `space_id`; otherwise a
  // row is personal and must bypass Project selection.  This keeps the
  // helper safe by default while retaining compatibility with old API rows.
  let owner: ExplorerBookmarkOwner;
  if (options.owner != null) {
    owner = bookmarkOwnerFromScope(options.owner);
  } else {
    owner = bookmarkOwnerFromItem(
      item as ExplorerBookmark & { owner?: unknown; space_id?: string | null },
    );
  }
  if (
    owner.scope === "shared" &&
    options.selectProjectForPath &&
    !(await options.selectProjectForPath(path))
  ) {
    return;
  }
  options.navigate(rawPath);
  options.focusFilesRoot();
}
