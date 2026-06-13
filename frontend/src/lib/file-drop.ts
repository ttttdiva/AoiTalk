"use client";

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

async function collectEntryFiles(
  entry: FileSystemEntry,
  fallbackPath = entry.name,
): Promise<DroppedExplorerFile[]> {
  const relativePath = normalizeEntryPath(entry, fallbackPath);

  if (isFileEntry(entry)) {
    const file = await getFileFromEntry(entry);
    return [{ file, relativePath }];
  }

  if (!isDirectoryEntry(entry)) return [];

  const reader = entry.createReader();
  const children = await readAllDirectoryEntries(reader);
  const nested = await Promise.all(
    children.map((child) =>
      collectEntryFiles(child, `${relativePath}/${child.name}`),
    ),
  );

  return nested.flat();
}

export async function getDroppedExplorerFiles(
  dataTransfer: DataTransfer,
): Promise<DroppedExplorerFile[]> {
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
