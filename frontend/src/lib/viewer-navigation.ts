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
