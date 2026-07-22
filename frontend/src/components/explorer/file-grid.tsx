"use client";

/* eslint-disable @next/next/no-img-element */

import { useState, useEffect, useCallback, useRef } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import type { ExplorerDirectory, ExplorerFile } from "@/lib/explorer-api";
import { explorerMove } from "@/lib/explorer-api";
import {
  getFileServeUrl,
  getImageThumbnailUrl,
  getVideoThumbnailUrl,
} from "@/lib/explorer-serve-url";
import { Folder, Film, Music, FileIcon, Play, Table2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { isRecordTableFile } from "@/lib/record-tables-api";
import {
  sortExplorerDirectories,
  sortExplorerFiles,
} from "@/lib/explorer-sort";

// ファイルタイプ判定
const IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"];
const VIDEO_EXTS = ["mp4", "webm", "mov", "avi", "mkv"];
const AUDIO_EXTS = ["mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma"];

function getExt(file: ExplorerFile): string {
  const raw = (
    file.extension ||
    file.name.split(".").pop() ||
    ""
  ).toLowerCase();
  return raw.startsWith(".") ? raw.slice(1) : raw;
}
function isImageFile(file: ExplorerFile): boolean {
  return IMAGE_EXTS.includes(getExt(file));
}
function isVideoFile(file: ExplorerFile): boolean {
  return VIDEO_EXTS.includes(getExt(file));
}

// サムネイルサイズ設定
const THUMB_SIZES = [80, 100, 130, 160, 200, 250, 320];
const DEFAULT_THUMB_INDEX = 3; // 160px

function fileTypeIcon(file: ExplorerFile, size: number) {
  const iconSize = Math.min(size * 0.4, 48);
  if (isRecordTableFile(file)) {
    return (
      <Table2
        style={{ width: iconSize, height: iconSize }}
        className="text-emerald-500"
      />
    );
  }
  const ext = getExt(file);
  if (VIDEO_EXTS.includes(ext))
    return (
      <Film
        style={{ width: iconSize, height: iconSize }}
        className="text-purple-500"
      />
    );
  if (AUDIO_EXTS.includes(ext))
    return (
      <Music
        style={{ width: iconSize, height: iconSize }}
        className="text-orange-500"
      />
    );
  return (
    <FileIcon
      style={{ width: iconSize, height: iconSize }}
      className="text-muted-foreground"
    />
  );
}

// 画像サムネイルコンポーネント
function ImageThumbnail({ file, size }: { file: ExplorerFile; size: number }) {
  const [loaded, setLoaded] = useState(false);
  const [thumbError, setThumbError] = useState(false);
  const [originalError, setOriginalError] = useState(false);

  // フォールバック: サムネAPI が失敗したら原本URLで再試行
  const src = thumbError
    ? getFileServeUrl(file.path)
    : getImageThumbnailUrl(file.path, Math.max(120, Math.round(size * 1.5)));

  if (originalError) {
    return (
      <div
        className="flex flex-col items-center justify-center bg-muted/30 rounded text-center p-1 aspect-square w-full"
        style={{ maxWidth: size, maxHeight: size }}
        title={file.path}
      >
        <FileIcon className="size-6 text-muted-foreground" />
        <span className="mt-1 text-[9px] text-muted-foreground line-clamp-2 break-all">
          画像読込失敗
        </span>
      </div>
    );
  }

  return (
    <div
      className="relative overflow-hidden rounded bg-muted/20 flex items-center justify-center aspect-square w-full"
      style={{ maxWidth: size, maxHeight: size }}
    >
      {!loaded && (
        <div className="absolute inset-0 animate-pulse bg-muted/40" />
      )}
      <img
        src={src}
        alt={file.name}
        loading="lazy"
        className={cn(
          "object-cover transition-opacity duration-200 w-full h-full",
          loaded ? "opacity-100" : "opacity-0",
        )}
        onLoad={() => setLoaded(true)}
        onError={() => {
          if (!thumbError) {
            // サムネAPI 失敗 → 原本で再試行
            setThumbError(true);
          } else {
            setOriginalError(true);
          }
        }}
        draggable={false}
      />
    </div>
  );
}

// フォルダサムネイル（代表画像が指定されている場合のみ使用）
function FolderThumbnail({
  previewPath,
  size,
}: {
  previewPath: string;
  size: number;
}) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  if (error) {
    // サムネ取得失敗時は通常のフォルダアイコンにフォールバック
    return (
      <div
        className="flex items-center justify-center w-full bg-muted/20 rounded"
        style={{ maxWidth: size, aspectRatio: "1 / 1" }}
      >
        <Folder
          style={{
            width: Math.min(size * 0.5, 48),
            height: Math.min(size * 0.5, 48),
          }}
          className="text-blue-500"
        />
      </div>
    );
  }

  return (
    <div
      className="relative overflow-hidden rounded bg-muted/20 flex items-center justify-center aspect-square w-full"
      style={{ maxWidth: size, maxHeight: size }}
    >
      {!loaded && (
        <div className="absolute inset-0 animate-pulse bg-muted/40" />
      )}
      <img
        src={getImageThumbnailUrl(
          previewPath,
          Math.max(120, Math.round(size * 1.5)),
        )}
        alt="folder preview"
        loading="lazy"
        className={cn(
          "object-cover transition-opacity duration-200 w-full h-full",
          loaded ? "opacity-100" : "opacity-0",
        )}
        onLoad={() => setLoaded(true)}
        onError={() => setError(true)}
        draggable={false}
      />
      {/* フォルダであることを示すバッジ */}
      <div className="absolute bottom-0.5 left-0.5 rounded-sm bg-black/55 px-1 py-0.5">
        <Folder className="size-3 text-white" />
      </div>
    </div>
  );
}

// 動画サムネイルコンポーネント
function VideoThumbnail({ file, size }: { file: ExplorerFile; size: number }) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  return (
    <div
      className="relative overflow-hidden rounded bg-muted/20 flex items-center justify-center aspect-square w-full"
      style={{ maxWidth: size, maxHeight: size }}
    >
      {!error ? (
        <>
          {!loaded && (
            <div className="absolute inset-0 animate-pulse bg-muted/40" />
          )}
          <img
            src={getVideoThumbnailUrl(file.path)}
            alt={file.name}
            loading="lazy"
            className={cn(
              "object-cover transition-opacity duration-200 w-full h-full",
              loaded ? "opacity-100" : "opacity-0",
            )}
            onLoad={() => setLoaded(true)}
            onError={() => setError(true)}
            draggable={false}
          />
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="rounded-full bg-black/50 p-1.5">
              <Play className="size-4 text-white fill-white" />
            </div>
          </div>
        </>
      ) : (
        <Film
          style={{ width: size * 0.4, height: size * 0.4 }}
          className="text-purple-500"
        />
      )}
    </div>
  );
}

interface FileGridProps {
  onFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
}

// D&D用MIMEタイプ
const DND_MIME = "application/x-explorer-paths";

export function FileGrid({ onFileClick, onContextMenu }: FileGridProps) {
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
    isHydrusMode,
  } = useExplorer();
  const containerRef = useRef<HTMLDivElement>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  // サムネイルサイズ（localStorage保存）
  const [thumbIndex, setThumbIndex] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_THUMB_INDEX;
    const saved = localStorage.getItem("filer-thumb-size");
    const idx = saved ? parseInt(saved, 10) : DEFAULT_THUMB_INDEX;
    return idx >= 0 && idx < THUMB_SIZES.length ? idx : DEFAULT_THUMB_INDEX;
  });

  const thumbSize = THUMB_SIZES[thumbIndex];

  const changeThumbSize = useCallback((delta: number) => {
    setThumbIndex((prev) => {
      const next = Math.max(0, Math.min(THUMB_SIZES.length - 1, prev + delta));
      localStorage.setItem("filer-thumb-size", String(next));
      return next;
    });
  }, []);

  // Ctrl+マウスホイールでサイズ変更
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      if (e.ctrlKey) {
        e.preventDefault();
        changeThumbSize(e.deltaY < 0 ? 1 : -1);
      }
    };
    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, [changeThumbSize]);

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

  // --- D&D ハンドラ ---
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
      // 自分自身へのドロップは無視
      if (paths.includes(destPath)) return;

      for (const src of paths) {
        try {
          await explorerMove(src, destPath);
        } catch {
          // エラー時は中断
          break;
        }
      }
      refresh();
    },
    [refresh],
  );

  // セルの幅 = サムネイルサイズ + パディング。
  // 狭い画面では `min(...)` 側で 1セル幅 ≒ コンテナ幅/3 まで縮むため、最低3列が保証される
  const cellWidth = thumbSize + 16;
  const minColumns = 3;
  const gridTemplate = `repeat(auto-fill, minmax(min(${cellWidth}px, calc((100% - ${(minColumns - 1) * 4}px) / ${minColumns})), 1fr))`;

  if (!browseData) return null;

  // Hydrus は検索時に Hydrus 側で全件ソート済みのため、1ページ分だけを
  // フロント側で並べ替えると全体の順序が壊れる。API 応答順のまま描画する。
  const sortedDirs = isHydrusMode
    ? browseData.directories
    : sortExplorerDirectories(browseData.directories, sortKey, sortDir);
  const sortedFiles = isHydrusMode
    ? browseData.files
    : sortExplorerFiles(browseData.files, sortKey, sortDir);
  const orderedPaths = [
    ...sortedDirs.map((dir) => dir.path),
    ...sortedFiles.map((file) => file.path),
  ];

  return (
    <div ref={containerRef}>
      {/* サイズインジケーター */}
      <div className="flex items-center justify-end gap-2 px-2 pb-1 text-[10px] text-muted-foreground">
        <span>Ctrl+ホイールでサイズ変更</span>
        <span>{thumbSize}px</span>
      </div>

      <div
        data-explorer-grid="true"
        className="grid gap-1 p-2"
        style={{
          gridTemplateColumns: gridTemplate,
        }}
      >
        {/* フォルダ */}
        {sortedDirs.map((dir) => (
          <div
            key={dir.path}
            data-explorer-item-path={dir.path}
            draggable
            className={cn(
              "flex cursor-pointer flex-col items-center gap-1 rounded-lg p-2 text-center transition-opacity hover:bg-muted",
              selectedItems.has(dir.path) && "bg-accent",
              focusedItemPath === dir.path && "ring-2 ring-primary/45",
              clipboard?.operation === "cut" &&
                clipboard.paths.includes(dir.path) &&
                "opacity-50",
              dropTarget === dir.path && "ring-2 ring-blue-400 bg-blue-500/10",
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
            {dir.preview_path ? (
              <FolderThumbnail
                previewPath={dir.preview_path}
                size={thumbSize}
              />
            ) : (
              <div
                className="flex items-center justify-center w-full"
                style={{
                  maxWidth: thumbSize,
                  aspectRatio: "5 / 3",
                }}
              >
                <Folder
                  style={{
                    width: Math.min(thumbSize * 0.5, 48),
                    height: Math.min(thumbSize * 0.5, 48),
                  }}
                  className="text-blue-500"
                />
              </div>
            )}
            <span className="w-full truncate text-xs">{dir.name}</span>
            {dir.item_count != null && (
              <span className="text-[10px] text-muted-foreground">
                {dir.item_count}件
              </span>
            )}
          </div>
        ))}

        {/* ファイル */}
        {sortedFiles.map((file) => (
          <div
            key={file.path}
            data-explorer-item-path={file.path}
            draggable={!isRecordTableFile(file)}
            className={cn(
              "flex cursor-pointer flex-col items-center gap-1 rounded-lg p-2 text-center transition-opacity hover:bg-muted",
              selectedItems.has(file.path) && "bg-accent",
              focusedItemPath === file.path && "ring-2 ring-primary/45",
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
            {isRecordTableFile(file) ? (
              <div
                className="flex items-center justify-center w-full rounded-md border border-emerald-500/30 bg-emerald-500/10"
                style={{
                  maxWidth: thumbSize,
                  aspectRatio: "5 / 3",
                }}
              >
                {fileTypeIcon(file, thumbSize)}
              </div>
            ) : isImageFile(file) ? (
              <ImageThumbnail file={file} size={thumbSize} />
            ) : isVideoFile(file) ? (
              <VideoThumbnail file={file} size={thumbSize} />
            ) : (
              <div
                className="flex items-center justify-center w-full"
                style={{
                  maxWidth: thumbSize,
                  aspectRatio: "5 / 3",
                }}
              >
                {fileTypeIcon(file, thumbSize)}
              </div>
            )}
            <span className="w-full truncate text-xs" title={file.name}>
              {file.name}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
