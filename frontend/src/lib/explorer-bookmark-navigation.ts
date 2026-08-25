import type { ExplorerBookmark } from "@/lib/explorer-api";
import {
  isExplorerBookmarkFolder,
  isBookmarkFolderPath,
} from "@/lib/explorer-bookmark-tree";

export type ExecuteExplorerBookmarkOptions = {
  closeEditor: () => void;
  navigate: (path: string) => void;
  focusFilesRoot: () => void;
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
  const path = item.path?.trim();
  if (!path || isBookmarkFolderPath(path)) return;
  options.closeEditor();
  if (options.selectProjectForPath && !(await options.selectProjectForPath(path))) {
    return;
  }
  options.navigate(path);
  options.focusFilesRoot();
}
