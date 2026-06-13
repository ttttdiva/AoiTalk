"use client";

import { useState, useCallback } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import type { ExplorerDirectory, ExplorerFile } from "@/lib/explorer-api";
import { explorerMove } from "@/lib/explorer-api";
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

type SortKey = "name" | "size" | "date";
type SortDir = "asc" | "desc";

interface FileListProps {
  onFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
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
      className="cursor-pointer px-2 py-1 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
      onClick={() => onToggle(columnKey)}
    >
      <span className="inline-flex items-center gap-0.5">
        {label}
        {activeSortKey === columnKey && <ArrowUpDown className="size-3" />}
      </span>
    </th>
  );
}

export function FileList({ onFileClick, onContextMenu }: FileListProps) {
  const { browseData, navigate, selectedItems, toggleSelect, refresh } =
    useExplorer();
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  const toggleSort = useCallback(
    (key: SortKey) => {
      if (sortKey === key) {
        setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
      } else {
        setSortKey(key);
        setSortDir("asc");
      }
    },
    [sortKey],
  );

  const handleClick = (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
    isDir: boolean,
  ) => {
    if (e.ctrlKey || e.metaKey) {
      toggleSelect(item.path);
      return;
    }
    if (isDir) {
      navigate(item.path);
    } else {
      onFileClick(item as ExplorerFile);
    }
  };

  const handleDragStart = useCallback(
    (e: React.DragEvent, item: ExplorerDirectory | ExplorerFile) => {
      const paths = selectedItems.has(item.path)
        ? Array.from(selectedItems)
        : [item.path];
      e.dataTransfer.setData(DND_MIME, JSON.stringify(paths));
      e.dataTransfer.effectAllowed = "move";
    },
    [selectedItems],
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

      for (const src of paths) {
        try {
          await explorerMove(src, destPath);
        } catch {
          break;
        }
      }
      refresh();
    },
    [refresh],
  );

  if (!browseData) return null;

  const sortedDirs = [...browseData.directories].sort((a, b) => {
    const mul = sortDir === "asc" ? 1 : -1;
    if (sortKey === "name") return mul * a.name.localeCompare(b.name);
    if (sortKey === "date") {
      return mul * (a.modified_at || "").localeCompare(b.modified_at || "");
    }
    return 0;
  });

  const sortedFiles = [...browseData.files].sort((a, b) => {
    const mul = sortDir === "asc" ? 1 : -1;
    if (sortKey === "name") return mul * a.name.localeCompare(b.name);
    if (sortKey === "size") return mul * ((a.size || 0) - (b.size || 0));
    if (sortKey === "date") {
      return mul * (a.modified_at || "").localeCompare(b.modified_at || "");
    }
    return 0;
  });

  return (
    <div className="overflow-auto p-1">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b">
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
            <th className="px-2 py-1 text-left text-xs font-medium text-muted-foreground">
              種別
            </th>
          </tr>
        </thead>
        <tbody>
          {sortedDirs.map((dir) => (
            <tr
              key={dir.path}
              draggable
              className={cn(
                "cursor-pointer hover:bg-muted",
                selectedItems.has(dir.path) && "bg-accent",
                dropTarget === dir.path &&
                  "ring-2 ring-blue-400 bg-blue-500/10",
              )}
              onClick={(e) => handleClick(e, dir, true)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, dir);
              }}
              onDragStart={(e) => handleDragStart(e, dir)}
              onDragOver={(e) => handleDragOverFolder(e, dir.path)}
              onDragLeave={handleDragLeaveFolder}
              onDrop={(e) => handleDropOnFolder(e, dir.path)}
            >
              <td className="px-1">
                <Folder className="size-4 text-blue-500" />
              </td>
              <td className="px-2 py-1">{dir.name}</td>
              <td className="px-2 py-1 text-muted-foreground">
                {dir.item_count != null ? `${dir.item_count}件` : "-"}
              </td>
              <td className="px-2 py-1 text-muted-foreground">
                {dir.modified_at
                  ? new Date(dir.modified_at).toLocaleDateString("ja-JP")
                  : "-"}
              </td>
              <td className="px-2 py-1 text-muted-foreground">フォルダ</td>
            </tr>
          ))}
          {sortedFiles.map((file) => (
            <tr
              key={file.path}
              draggable={!isRecordTableFile(file)}
              className={cn(
                "cursor-pointer hover:bg-muted",
                selectedItems.has(file.path) && "bg-accent",
              )}
              onClick={(e) => handleClick(e, file, false)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, file);
              }}
              onDragStart={(e) => {
                if (isRecordTableFile(file)) return;
                handleDragStart(e, file);
              }}
            >
              <td className="px-1">{fileTypeIcon(file)}</td>
              <td className="px-2 py-1">{file.name}</td>
              <td className="px-2 py-1 text-muted-foreground">
                {isRecordTableFile(file)
                  ? `${file.row_count ?? 0}行`
                  : formatSize(file.size)}
              </td>
              <td className="px-2 py-1 text-muted-foreground">
                {file.modified_at
                  ? new Date(file.modified_at).toLocaleDateString("ja-JP")
                  : "-"}
              </td>
              <td className="px-2 py-1 text-muted-foreground">
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
