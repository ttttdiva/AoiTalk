"use client";

import { useState, useCallback, useMemo, type ReactNode } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import type { ExplorerDirectory, ExplorerFile } from "@/lib/explorer-api";
import { useFilerOperations } from "@/hooks/use-filer-operations";
import {
  sortExplorerDirectories,
  sortExplorerFiles,
  type SortKey,
} from "@/lib/explorer-sort";
import {
  Folder,
  Image as ImageIcon,
  Film,
  Music,
  FileIcon,
  ArrowUpDown,
  Table2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { isRecordTableFile } from "@/lib/record-tables-api";
import { formatExplorerDateTime } from "@/lib/explorer-format";

function formatSize(bytes?: number): string {
  if (bytes == null) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function fileTypeIcon(file: ExplorerFile) {
  if (isRecordTableFile(file)) {
    return <Table2 className="size-4 text-emerald-500" />;
  }
  const ext = file.extension?.toLowerCase() || "";
  if (["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"].includes(ext)) {
    return <ImageIcon className="size-4 text-green-500" />;
  }
  if (["mp4", "webm", "mov", "avi", "mkv"].includes(ext)) {
    return <Film className="size-4 text-purple-500" />;
  }
  if (["mp3", "wav", "ogg", "flac", "aac"].includes(ext)) {
    return <Music className="size-4 text-orange-500" />;
  }
  return <FileIcon className="size-4 text-muted-foreground" />;
}

const DND_MIME = "application/x-explorer-paths";

interface FileListProps {
  onFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
  directories?: ExplorerDirectory[];
  files?: ExplorerFile[];
  headerAddon?: ReactNode;
}

interface SortHeaderProps {
  label: string;
  columnKey: SortKey;
  activeSortKey: SortKey;
  onToggle: (key: SortKey) => void;
}

function SortHeader({
  label,
  columnKey,
  activeSortKey,
  onToggle,
}: SortHeaderProps) {
  return (
    <th
      className="cursor-pointer px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground"
      onClick={() => onToggle(columnKey)}
    >
      <span className="inline-flex items-center gap-0.5">
        {label}
        {activeSortKey === columnKey && <ArrowUpDown className="size-3" />}
      </span>
    </th>
  );
}

export function FileList({
  onFileClick,
  onContextMenu,
  directories,
  files,
  headerAddon,
}: FileListProps) {
  const {
    browseData,
    navigate,
    selectedItems,
    focusedItemPath,
    clipboard,
    selectItem,
    toggleSelect,
    selectRange,
    refresh,
    sortKey,
    sortDir,
    setSort,
    isHydrusMode,
    capabilities,
  } = useExplorer();
  const { transfer } = useFilerOperations({ capabilities, refresh });
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const displayedPathSet = useMemo(
    () =>
      new Set([
        ...(directories ?? browseData?.directories ?? []).map((item) => item.path),
        ...(files ?? browseData?.files ?? []).map((item) => item.path),
      ]),
    [browseData?.directories, browseData?.files, directories, files],
  );

  const toggleSort = useCallback(
    (key: SortKey) => {
      if (sortKey === key) {
        setSort(key, sortDir === "asc" ? "desc" : "asc");
      } else {
        setSort(key, "asc");
      }
    },
    [sortKey, sortDir, setSort],
  );

  const handleClick = (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => {
    if (e.shiftKey) {
      selectRange(item.path, orderedPaths, e.ctrlKey || e.metaKey);
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      toggleSelect(item.path);
      return;
    }
    selectItem(item.path);
  };

  const handleDoubleClick = (
    item: ExplorerDirectory | ExplorerFile,
    isDir: boolean,
  ) => {
    if (isDir) {
      navigate(item.path);
    } else {
      onFileClick(item as ExplorerFile);
    }
  };

  const handleDragStart = useCallback(
    (e: React.DragEvent, item: ExplorerDirectory | ExplorerFile) => {
      const paths = selectedItems.has(item.path)
        ? Array.from(selectedItems).filter((path) => displayedPathSet.has(path))
        : [item.path];
      e.dataTransfer.setData(DND_MIME, JSON.stringify(paths));
      // Folder targets still request `move`; the bookmark/launcher rail
      // requests `copy` and never mutates the source.  Advertise both so the
      // browser can render the target-selected operation correctly.
      e.dataTransfer.effectAllowed = "copyMove";
    },
    [displayedPathSet, selectedItems],
  );

  const handleDragOverFolder = useCallback(
    (e: React.DragEvent, dirPath: string) => {
      if (!e.dataTransfer.types.includes(DND_MIME)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      setDropTarget(dirPath);
    },
    [],
  );

  const handleDragLeaveFolder = useCallback(() => {
    setDropTarget(null);
  }, []);

  const handleDropOnFolder = useCallback(
    async (e: React.DragEvent, destPath: string) => {
      e.preventDefault();
      e.stopPropagation();
      setDropTarget(null);

      const raw = e.dataTransfer.getData(DND_MIME);
      if (!raw) return;
      const paths: string[] = JSON.parse(raw);
      if (paths.includes(destPath)) return;

      // 実行・Undo登録は filer-operations 側へ集約
      await transfer({ paths, destDir: destPath, operation: "move" });
    },
    [transfer],
  );

  if (!browseData) return null;

  // Hydrus は検索時に Hydrus 側で全件ソート済みのため、1ページ分だけを
  // フロント側で並べ替えると全体の順序が壊れる。API 応答順のまま描画する。
  const sourceDirectories = directories ?? browseData.directories;
  const sourceFiles = files ?? browseData.files;
  const sortedDirs = isHydrusMode
    ? sourceDirectories
    : sortExplorerDirectories(sourceDirectories, sortKey, sortDir);
  const sortedFiles = isHydrusMode
    ? sourceFiles
    : sortExplorerFiles(sourceFiles, sortKey, sortDir);
  const orderedPaths = [
    ...sortedDirs.map((dir) => dir.path),
    ...sortedFiles.map((file) => file.path),
  ];

  return (
    <div className="overflow-auto rounded-md border border-border bg-card/20">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border bg-muted/25">
            <th className="w-6 px-1" />
            <SortHeader
              label="名前"
              columnKey="name"
              activeSortKey={sortKey}
              onToggle={toggleSort}
            />
            <SortHeader
              label="サイズ"
              columnKey="size"
              activeSortKey={sortKey}
              onToggle={toggleSort}
            />
            <SortHeader
              label="更新日"
              columnKey="date"
              activeSortKey={sortKey}
              onToggle={toggleSort}
            />
            <th className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              種別
            </th>
          </tr>
          {headerAddon && (
            <tr className="border-b border-border">
              <th colSpan={5} className="p-1 text-left font-normal">
                {headerAddon}
              </th>
            </tr>
          )}
        </thead>
        <tbody>
          {sortedDirs.map((dir) => (
            <tr
              key={dir.path}
              data-explorer-item-path={dir.path}
              draggable
              className={cn(
                "cursor-pointer border-b border-border/70 transition-colors hover:bg-muted/45",
                selectedItems.has(dir.path) && "bg-primary/5",
                focusedItemPath === dir.path && "outline outline-1 outline-primary/45 outline-offset-[-1px]",
                clipboard?.operation === "cut" &&
                  clipboard.paths.includes(dir.path) &&
                  "opacity-50",
                dropTarget === dir.path && "ring-1 ring-primary bg-primary/10",
              )}
              onClick={(e) => handleClick(e, dir)}
              onDoubleClick={() => handleDoubleClick(dir, true)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!selectedItems.has(dir.path)) selectItem(dir.path);
                onContextMenu(e, dir);
              }}
              onDragStart={(e) => handleDragStart(e, dir)}
              onDragOver={(e) => handleDragOverFolder(e, dir.path)}
              onDragLeave={handleDragLeaveFolder}
              onDrop={(e) => handleDropOnFolder(e, dir.path)}
            >
              <td className="w-10 px-3 py-2">
                <Folder className="size-4 text-tertiary" />
              </td>
              <td className="max-w-[34rem] px-3 py-2 font-medium">{dir.name}</td>
              <td className="px-3 py-2 text-muted-foreground">
                {dir.item_count != null ? `${dir.item_count}件` : "-"}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                {formatExplorerDateTime(dir.modified_at)}
              </td>
              <td className="px-3 py-2 text-muted-foreground">フォルダ</td>
            </tr>
          ))}
          {sortedFiles.map((file) => (
            <tr
              key={file.path}
              data-explorer-item-path={file.path}
              draggable={!isRecordTableFile(file)}
              className={cn(
                "cursor-pointer border-b border-border/70 transition-colors hover:bg-muted/45",
                selectedItems.has(file.path) && "bg-primary/5",
                focusedItemPath === file.path && "outline outline-1 outline-primary/45 outline-offset-[-1px]",
                clipboard?.operation === "cut" &&
                  clipboard.paths.includes(file.path) &&
                  "opacity-50",
              )}
              onClick={(e) => handleClick(e, file)}
              onDoubleClick={() => handleDoubleClick(file, false)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!selectedItems.has(file.path)) selectItem(file.path);
                onContextMenu(e, file);
              }}
              onDragStart={(e) => {
                if (isRecordTableFile(file)) return;
                handleDragStart(e, file);
              }}
            >
              <td className="w-10 px-3 py-2">{fileTypeIcon(file)}</td>
              <td className="max-w-[34rem] px-3 py-2 font-medium">{file.name}</td>
              <td className="px-3 py-2 text-muted-foreground">
                {isRecordTableFile(file)
                  ? `${file.row_count ?? 0}行`
                  : formatSize(file.size)}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                {formatExplorerDateTime(file.modified_at)}
              </td>
              <td className="px-3 py-2 text-muted-foreground">
                {isRecordTableFile(file)
                  ? "DBテーブル"
                  : file.extension?.toUpperCase() || "ファイル"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
