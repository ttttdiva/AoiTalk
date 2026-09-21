import type { ExplorerFile } from "./explorer-api";

export function isViewerFile(file: ExplorerFile): boolean {
  const type = file.type || "";
  return type.startsWith("image") || type.startsWith("video");
}

export function viewerFiles(files: ExplorerFile[]): ExplorerFile[] {
  return files.filter(isViewerFile);
}

export function boundaryViewerFile(
  files: ExplorerFile[],
  direction: -1 | 1,
): ExplorerFile | null {
  const items = viewerFiles(files);
  if (items.length === 0) return null;
  return direction === 1 ? items[0] : items.at(-1)!;
}

/**
 * Return the viewer-compatible file adjacent to a path.
 *
 * Paths are the viewer identity (rather than array indexes) because the
 * displayed list can be replaced asynchronously (for example by an HF
 * search) while an arrow key event is still being handled.  Keeping this
 * lookup path based lets callers maintain a synchronous cursor and avoids a
 * stale React render skipping or repeating an item during rapid navigation.
 */
export function adjacentViewerFile(
  files: ExplorerFile[],
  currentPath: string,
  direction: -1 | 1,
): ExplorerFile | null {
  const items = viewerFiles(files);
  const index = items.findIndex((item) => item.path === currentPath);
  if (index < 0) return null;
  return items[index + direction] ?? null;
}

export function preloadViewerFiles(
  files: ExplorerFile[],
  currentPath: string,
  radius: number,
): ExplorerFile[] {
  const items = viewerFiles(files);
  const index = items.findIndex((item) => item.path === currentPath);
  if (index < 0 || radius < 1) return [];
  const result: ExplorerFile[] = [];
  for (let distance = 1; distance <= radius; distance++) {
    const next = items[index + distance];
    const previous = items[index - distance];
    if (next) result.push(next);
    if (previous) result.push(previous);
  }
  return result;
}
