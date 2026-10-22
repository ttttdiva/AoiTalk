import type { ExplorerFile, FileInfo } from "@/lib/explorer-api";

/**
 * Build the minimal Files file shape used when explorerInfo succeeds but the
 * richer directory listing is unavailable.  The bookmark label is display
 * data; the persisted explorer metadata determines the file extension.
 */
export function buildExplorerFileFallback(
  path: string,
  info: Pick<FileInfo, "name" | "type" | "extension">,
  displayName?: string,
): ExplorerFile {
  const authoritativeName =
    info.name?.trim() ||
    path.split(/[\\/|]/).filter(Boolean).pop() ||
    path;
  const fallbackName = displayName?.trim() || authoritativeName;
  const baseName = authoritativeName.split(/[\\/|]/).filter(Boolean).pop() || authoritativeName;
  const dotIndex = baseName.lastIndexOf(".");
  const inferredSuffix = dotIndex > 0 ? baseName.slice(dotIndex) : "";
  const extension = info.extension?.trim().toLowerCase() || inferredSuffix.toLowerCase();

  return {
    name: fallbackName,
    path,
    type: info.type || "application/octet-stream",
    extension,
  };
}
