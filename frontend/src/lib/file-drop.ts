"use client";

import { explorerDownloadUrl, explorerInfo, explorerList } from "@/lib/explorer-api";

export interface DroppedExplorerFile {
  file: File;
  relativePath?: string;
}

function isFileEntry(entry: FileSystemEntry): entry is FileSystemFileEntry {
  return entry.isFile;
}

function isDirectoryEntry(
  entry: FileSystemEntry,
): entry is FileSystemDirectoryEntry {
  return entry.isDirectory;
}

function getFileFromEntry(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    entry.file(resolve, reject);
  });
}

async function readAllDirectoryEntries(
  reader: FileSystemDirectoryReader,
): Promise<FileSystemEntry[]> {
  const entries: FileSystemEntry[] = [];

  while (true) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (batch.length === 0) break;
    entries.push(...batch);
  }

  return entries;
}

function normalizeEntryPath(entry: FileSystemEntry, fallback: string) {
  return (entry.fullPath || fallback).replace(/^\/+/, "");
}

function getEntryFromItem(item: DataTransferItem): FileSystemEntry | null {
  if (item.kind !== "file") return null;
  if (typeof item.webkitGetAsEntry !== "function") return null;
  return item.webkitGetAsEntry();
}

const EXPLORER_PATHS_MIME = "application/x-explorer-paths";
const MAX_EXPLORER_PATH_DEPTH = 64;
const MAX_FILE_ENTRY_DEPTH = 64;

function pathName(path: string): string {
  return path.replaceAll("\\", "/").split("/").filter(Boolean).pop() || "source";
}

async function downloadExplorerFile(path: string, relativePath: string): Promise<DroppedExplorerFile> {
  const response = await fetch(explorerDownloadUrl(path), {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`Explorerのファイルを読み込めませんでした: ${path}`);
  }
  const blob = await response.blob();
  const fileName = pathName(path);
  return {
    file: new File([blob], fileName, { type: blob.type || "application/octet-stream" }),
    relativePath: relativePath || fileName,
  };
}

function explorerVisitKey(path: string): string {
  return path.replaceAll("\\", "/").replace(/\/+$/, "").toLocaleLowerCase();
}

async function collectExplorerPath(
  path: string,
  relativeRoot: string,
  visited: Set<string>,
  depth = 0,
): Promise<DroppedExplorerFile[]> {
  if (depth > MAX_EXPLORER_PATH_DEPTH) {
    throw new Error("Explorerのフォルダ階層が深すぎるため取り込めません");
  }
  const visitKey = explorerVisitKey(path);
  if (visited.has(visitKey)) return [];
  visited.add(visitKey);
  const listing = await explorerList(path);
  const files = await Promise.all(
    (listing.files || []).map((file) => {
      const childPath = file.path || `${path.replace(/\/$/, "")}/${file.name}`;
      const relativePath = `${relativeRoot}/${file.name}`.replace(/^\/+/, "");
      return downloadExplorerFile(childPath, relativePath);
    }),
  );
  const nested = await Promise.all(
    (listing.directories || []).map((directory) => {
      const childPath = directory.path || `${path.replace(/\/$/, "")}/${directory.name}`;
      return collectExplorerPath(
        childPath,
        `${relativeRoot}/${directory.name}`.replace(/^\/+/, ""),
        visited,
        depth + 1,
      );
    }),
  );
  return [...files, ...nested.flat()];
}

async function getDroppedExplorerWorkspaceFiles(dataTransfer: DataTransfer): Promise<DroppedExplorerFile[] | null> {
  if (!Array.from(dataTransfer.types).includes(EXPLORER_PATHS_MIME)) return null;
  const raw = dataTransfer.getData(EXPLORER_PATHS_MIME);
  if (!raw) throw new Error("Explorerからドロップされたパスを読み取れませんでした");
  let paths: unknown;
  try {
    paths = JSON.parse(raw);
  } catch {
    throw new Error("Explorerからドロップされたパスの形式が不正です");
  }
  if (!Array.isArray(paths) || !paths.every((path) => typeof path === "string" && path.trim())) {
    throw new Error("Explorerからドロップされたパスの形式が不正です");
  }
  const selected = Array.from(new Set(paths as string[]));
  const visited = new Set<string>();
  const result = await Promise.all(selected.map(async (path) => {
    const info = await explorerInfo(path);
    if (!info.is_directory) {
      return [await downloadExplorerFile(path, pathName(path))];
    }
    return collectExplorerPath(path, pathName(path), visited);
  }));
  return result.flat();
}

async function collectEntryFiles(
  entry: FileSystemEntry,
  fallbackPath = entry.name,
  visited = new Set<string>(),
  depth = 0,
): Promise<DroppedExplorerFile[]> {
  if (depth > MAX_FILE_ENTRY_DEPTH) {
    throw new Error("ドロップされたフォルダ階層が深すぎるため取り込めません");
  }
  const relativePath = normalizeEntryPath(entry, fallbackPath);
  const visitKey = (entry.fullPath || relativePath).replaceAll("\\", "/").replace(/\/+$/, "").toLocaleLowerCase();
  if (visited.has(visitKey)) return [];
  visited.add(visitKey);

  if (isFileEntry(entry)) {
    const file = await getFileFromEntry(entry);
    return [{ file, relativePath }];
  }

  if (!isDirectoryEntry(entry)) return [];

  const reader = entry.createReader();
  const children = await readAllDirectoryEntries(reader);
  const nested = await Promise.all(
    children.map((child) =>
      collectEntryFiles(child, `${relativePath}/${child.name}`, visited, depth + 1),
    ),
  );

  return nested.flat();
}

export async function getDroppedExplorerFiles(
  dataTransfer: DataTransfer,
): Promise<DroppedExplorerFile[]> {
  const workspaceFiles = await getDroppedExplorerWorkspaceFiles(dataTransfer);
  if (workspaceFiles) return workspaceFiles;
  const droppedItems = Array.from(dataTransfer.items).map((item) => ({
    entry: getEntryFromItem(item),
    file: item.kind === "file" ? item.getAsFile() : null,
  }));
  const entries = droppedItems
    .map((item) => item.entry)
    .filter((entry): entry is FileSystemEntry => entry != null);

  if (entries.length > 0) {
    const nested = await Promise.allSettled(
      entries.map((entry) => collectEntryFiles(entry)),
    );
    const entryFiles = nested.flatMap((result) =>
      result.status === "fulfilled" ? result.value : [],
    );
    const fallbackFiles = droppedItems
      .filter((item) => item.entry == null && item.file != null)
      .map((item) => item.file as File)
      .map((file) => ({
        file,
        relativePath: file.webkitRelativePath || file.name,
      }));
    return [...entryFiles, ...fallbackFiles];
  }

  return Array.from(dataTransfer.files).map((file) => ({
    file,
    relativePath: file.webkitRelativePath || file.name,
  }));
}
