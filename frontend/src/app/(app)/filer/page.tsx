"use client";

/* eslint-disable @next/next/no-img-element */

import {
  useState,
  useEffect,
  useLayoutEffect,
  useCallback,
  useRef,
  Suspense,
  useMemo,
} from "react";
import { useSearchParams } from "next/navigation";
import { useAudioPlayer } from "@/contexts/audio-player-context";
import {
  ExplorerProvider,
  FILER_TAB_LABELS,
  type FilerTab,
  useExplorer,
} from "@/contexts/explorer-context";
import { useSnippets } from "@/contexts/snippets-context";
import { ExplorerToolbar } from "@/components/explorer/explorer-toolbar";
import { FileGrid } from "@/components/explorer/file-grid";
import { FileList } from "@/components/explorer/file-list";
import { FileContextMenu } from "@/components/explorer/file-context-menu";
import { NewFolderDialog } from "@/components/explorer/new-folder-dialog";
import { NewItemNameInput } from "@/components/explorer/new-item-name-input";
import { RenameDialog } from "@/components/explorer/rename-dialog";
import { UploadZone } from "@/components/explorer/upload-zone";
import { FilePreviewPanel } from "@/components/explorer/file-preview-panel";
import {
  HydrusSearchBar,
  type HydrusPagingController,
} from "@/components/hf-browser/hydrus-search-bar";
import { HfReferenceDialog } from "@/components/hf-browser/hf-reference-dialog";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  ChevronRight,
  Home,
  ArrowUp,
  X,
  Search,
  FileIcon,
  Folder,
  Play,
  Info,
} from "lucide-react";
import type {
  ExplorerDirectory,
  ExplorerFile,
  SearchResult,
} from "@/lib/explorer-api";
import {
  ExplorerUploadError,
  explorerArchive,
  explorerDownloadPaths,
  explorerErrorMessage,
  explorerExtract,
  explorerFullContent,
  explorerInfo,
  explorerList,
  explorerUpload,
  explorerSearch,
} from "@/lib/explorer-api";
import { uploadFailureToastOptions } from "@/lib/upload-failure";
import {
  canRedoFiler,
  canUndoFiler,
  useFilerOperations,
} from "@/hooks/use-filer-operations";
import { toFilerDeleteTarget } from "@/lib/explorer/filer-operations";
import { type FilerSearchItem } from "@/lib/migemo-lite";
import { useIncrementalSearch } from "@/hooks/use-incremental-search";
import {
  isRecordTableFile,
  RECORD_TABLE_EXTENSION,
  RECORD_TABLE_TYPE,
} from "@/lib/record-tables-api";
import { getFileServeUrl, getImageThumbnailUrl } from "@/lib/explorer-serve-url";
import { cn } from "@/lib/utils";
import { HF_PREFIX, isHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath, parseHydrusFileId } from "@/lib/hydrus/virtual-path";
import { hydrusGetMetadata, type HydrusFileMetadata } from "@/lib/hf-api";
import { replaceFilerName } from "@/lib/explorer/filer-search";
import { resolveFilerCreationShortcut } from "@/lib/explorer/filer-creation-shortcuts";
import { FilesBookmarkLauncherSidebar } from "@/components/explorer/files-bookmark-launcher-sidebar";
import { useWorkspaceShellRegistration } from "@/components/layout/shell-context";
import { FILES_DOWNLOAD_PATH_EVENT, FILES_OPEN_PATH_EVENT } from "@/lib/files-sidebar-events";
import dynamic from "next/dynamic";
import { toast } from "sonner";
import {
  boundaryViewerFile,
  preloadViewerFiles,
  viewerFiles,
} from "@/lib/viewer-navigation";
import {
  DOUBLE_ESCAPE_RESET_EVENT,
  EMPTY_DOUBLE_ESCAPE_STATE,
  resetDoubleEscapeState,
  transitionDoubleEscapeKey,
  type DoubleEscapeState,
} from "@/lib/double-escape";

const DocumentEditor = dynamic(
  () =>
    import("@/components/editor/document-editor").then((m) => ({
      default: m.DocumentEditor,
    })),
  {
    ssr: false,
    loading: () => (
      <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
        エディタ読み込み中...
      </div>
    ),
  },
);

const InlineChatPanel = dynamic(
  () =>
    import("@/components/editor/inline-chat-panel").then((m) => ({
      default: m.InlineChatPanel,
    })),
  { ssr: false },
);

// ─── ファイル配信URL ───
const getFilerFileUrl = getFileServeUrl;
function isImage(type: string) {
  return type.startsWith("image");
}
function isVideo(type: string) {
  return type.startsWith("video");
}
function isAudio(type: string) {
  return type.startsWith("audio");
}
type ExplorerItem = ExplorerDirectory | ExplorerFile;
const FILER_TAB_ORDER: readonly FilerTab[] = [
  "workspace",
  "user",
  "hf",
  "hydrus",
];
const SUPPORTED_ARCHIVE_SUFFIXES = [
  ".tar.bz2",
  ".tar.gz",
  ".tar.xz",
  ".tbz2",
  ".tgz",
  ".txz",
  ".7z",
  ".tar",
  ".zip",
  ".bz2",
  ".gz",
  ".xz",
] as const;

function isSupportedArchiveName(name: string) {
  const lowerName = name.toLowerCase();
  return SUPPORTED_ARCHIVE_SUFFIXES.some((suffix) =>
    lowerName.endsWith(suffix),
  );
}

function isExplorerDirectory(item: ExplorerItem): item is ExplorerDirectory {
  return !("type" in item);
}

/**
 * The Files editor and its persistent bookmark/launcher sidebar are siblings
 * in the shared shell, so the sidebar is intentionally outside the canvas
 * root.  Treat only those Files-marked surfaces as one keyboard workspace;
 * unrelated shell/header focus still resets the double-Escape sequence.  The
 * canvas keeps the singular `data-shell-workspace` marker; the shell slot uses
 * the existing `data-workspace` marker instead.
 */
function isFilesWorkspaceTarget(
  target: EventTarget | null,
  canvasRoot: HTMLElement | null,
): boolean {
  if (!(target instanceof Node)) return false;
  if (canvasRoot?.contains(target)) return true;
  return target instanceof Element && Boolean(
    target.closest(
      '[data-shell-workspace="files"], [data-workspace="files"], [data-shell-region="files-bookmark-launcher-sidebar"], [data-files-bookmark-quick-launcher]',
    ),
  );
}

/**
 * Alt+A のクイックランチャーが開いている間は、そちらのニーモニック入力と
 * ショートカットを優先し、Files 側のインクリメンタルサーチは抑止する。
 */
function isBookmarkQuickLauncherOpen(): boolean {
  return Boolean(
    document.querySelector(
      '[data-files-bookmark-quick-launcher][data-open], [data-files-bookmark-quick-launcher][data-slot="dropdown-menu-content"][data-open]',
    ),
  );
}

function searchResultToDirectory(item: SearchResult): ExplorerDirectory {
  return {
    name: item.name,
    path: item.path,
    item_count: item.item_count ?? undefined,
    modified_at: item.modified_at,
  };
}

function searchResultToFile(item: SearchResult): ExplorerFile {
  return {
    name: item.name,
    path: item.path,
    type: item.type || "binary",
    extension: item.extension || "",
    size: item.size_bytes,
    modified_at: item.modified_at,
  };
}

// ─── ファイルビューア（画像/動画全画面モーダル） ───
export function FileViewer({
  file,
  files,
  onClose,
  onNavigate,
  onBoundaryNavigate,
  onAdjacentFiles,
  returnFocusRef,
}: {
  file: ExplorerFile;
  files: ExplorerFile[];
  onClose: () => void;
  onNavigate: (file: ExplorerFile) => void;
  onBoundaryNavigate?: (direction: -1 | 1) => Promise<ExplorerFile | null>;
  onAdjacentFiles?: (direction: -1 | 1) => Promise<ExplorerFile[]>;
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const initialFocusRef = useRef<HTMLButtonElement>(null);
  const touchStartRef = useRef<{ x: number; y: number; t: number } | null>(
    null,
  );
  const viewableFiles = useMemo(() => viewerFiles(files), [files]);
  const currentIndex = viewableFiles.findIndex((f) => f.path === file.path);
  const isImageFile = isImage(file.type || "");
  const navigatingRef = useRef(false);
  const navigationGenerationRef = useRef(0);
  const viewerMountedRef = useRef(true);
  const preloadedImagesRef = useRef(new Map<string, HTMLImageElement>());

  const handleClose = useCallback(() => {
    onClose();
    window.setTimeout(() => {
      returnFocusRef?.current?.focus({ preventScroll: true });
    }, 0);
  }, [onClose, returnFocusRef]);

  useEffect(() => {
    viewerMountedRef.current = true;
    return () => {
      viewerMountedRef.current = false;
      navigationGenerationRef.current += 1;
    };
  }, []);

  const goPrev = useCallback(async () => {
    if (navigatingRef.current) return;
    if (currentIndex > 0) {
      onNavigate(viewableFiles[currentIndex - 1]);
      return;
    }
    if (!onBoundaryNavigate) return;
    navigatingRef.current = true;
    const generation = ++navigationGenerationRef.current;
    try {
      const target = await onBoundaryNavigate(-1);
      if (target && viewerMountedRef.current && generation === navigationGenerationRef.current) {
        onNavigate(target);
      }
    } catch (error) {
      if (viewerMountedRef.current) {
        toast.error(error instanceof Error ? error.message : "前のページを読み込めませんでした");
      }
    } finally {
      if (generation === navigationGenerationRef.current) navigatingRef.current = false;
    }
  }, [currentIndex, onBoundaryNavigate, onNavigate, viewableFiles]);

  const goNext = useCallback(async () => {
    if (navigatingRef.current) return;
    if (currentIndex < viewableFiles.length - 1) {
      onNavigate(viewableFiles[currentIndex + 1]);
      return;
    }
    if (!onBoundaryNavigate) return;
    navigatingRef.current = true;
    const generation = ++navigationGenerationRef.current;
    try {
      const target = await onBoundaryNavigate(1);
      if (target && viewerMountedRef.current && generation === navigationGenerationRef.current) {
        onNavigate(target);
      }
    } catch (error) {
      if (viewerMountedRef.current) {
        toast.error(error instanceof Error ? error.message : "次のページを読み込めませんでした");
      }
    } finally {
      if (generation === navigationGenerationRef.current) navigatingRef.current = false;
    }
  }, [currentIndex, onBoundaryNavigate, onNavigate, viewableFiles]);

  // 前後の画像をviewer存続中のブラウザキャッシュへ載せる（動画は容量的に除外）
  useEffect(() => {
    if (currentIndex < 0) return;
    let cancelled = false;
    const PRELOAD_RADIUS = 3;
    const targets = preloadViewerFiles(files, file.path, PRELOAD_RADIUS);
    const preload = (items: ExplorerFile[]) => {
      if (cancelled) return;
      for (const item of items) {
        if (!isImage(item.type || "")) continue;
        const url = getFilerFileUrl(item.path);
        if (preloadedImagesRef.current.has(url)) continue;
        while (preloadedImagesRef.current.size >= 12) {
          const oldest = preloadedImagesRef.current.keys().next().value as string | undefined;
          if (!oldest) break;
          preloadedImagesRef.current.get(oldest)!.src = "";
          preloadedImagesRef.current.delete(oldest);
        }
        const img = new window.Image();
        img.decoding = "async";
        img.src = url;
        preloadedImagesRef.current.set(url, img);
      }
    };
    preload(targets);
    if (onAdjacentFiles && currentIndex < PRELOAD_RADIUS) {
      void onAdjacentFiles(-1)
        .then((items) => preload(items.slice(-PRELOAD_RADIUS)))
        .catch(() => undefined);
    }
    if (
      onAdjacentFiles &&
      viewableFiles.length - 1 - currentIndex < PRELOAD_RADIUS
    ) {
      void onAdjacentFiles(1)
        .then((items) => preload(items.slice(0, PRELOAD_RADIUS)))
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [currentIndex, file.path, files, onAdjacentFiles, viewableFiles]);

  useEffect(
    () => () => {
      for (const image of preloadedImagesRef.current.values()) image.src = "";
      preloadedImagesRef.current.clear();
    },
    [],
  );

  // スワイプでページ送り（画像表示時のみ。動画はネイティブコントロール優先）
  const handleTouchStart = useCallback(
    (e: React.TouchEvent) => {
      if (!isImageFile) return;
      if (e.touches.length !== 1) {
        touchStartRef.current = null;
        return;
      }
      const t = e.touches[0];
      touchStartRef.current = { x: t.clientX, y: t.clientY, t: Date.now() };
    },
    [isImageFile],
  );

  const handleTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      if (!isImageFile) return;
      const start = touchStartRef.current;
      touchStartRef.current = null;
      if (!start) return;
      const end = e.changedTouches[0];
      if (!end) return;
      const dx = end.clientX - start.x;
      const dy = end.clientY - start.y;
      const dt = Date.now() - start.t;
      // 水平方向に 50px 以上、縦より明確に水平、1秒以内
      if (dt > 1000) return;
      if (Math.abs(dx) < 50) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.3) return;
      if (dx > 0) void goPrev();
      else void goNext();
    },
    [isImageFile, goPrev, goNext],
  );

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      // video要素自体がフォーカスされている場合はネイティブ処理に任せる（二重発火防止）
      if (e.target === videoRef.current) return;
      if (e.key === "ArrowLeft") {
        void goPrev();
        return;
      }
      if (e.key === "ArrowRight") {
        void goNext();
        return;
      }
      const video = videoRef.current;
      if (video) {
        if (e.key === " " || e.key === "k" || e.key === "K") {
          e.preventDefault();
          if (video.paused) {
            void video.play();
          } else {
            video.pause();
          }
        }
        if (e.key === "j" || e.key === "J") video.currentTime -= 10;
        if (e.key === "l" || e.key === "L") video.currentTime += 10;
        if (e.key === "f" || e.key === "F") {
          if (document.fullscreenElement) {
            void document.exitFullscreen();
          } else {
            void video.requestFullscreen();
          }
        }
        if (e.key === "m" || e.key === "M") video.muted = !video.muted;
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [goPrev, goNext]);

  const [fileInfo, setFileInfo] = useState<{
    size_bytes?: number;
    created_at?: string;
    modified_at?: string;
  } | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);
  const [hydrusMetadata, setHydrusMetadata] = useState<HydrusFileMetadata | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFileInfo(null);
    setHydrusMetadata(null);
    if (isHydrusPath(file.path)) {
      const fileId = parseHydrusFileId(file.path);
      if (fileId == null) return () => { cancelled = true; };
      hydrusGetMetadata([fileId], false)
        .then((result) => {
          if (!cancelled && result.metadata?.[0]?.file_id === fileId) {
            setHydrusMetadata(result.metadata[0]);
          }
        })
        .catch(() => {
          if (!cancelled) setHydrusMetadata(null);
        });
      return () => { cancelled = true; };
    }
    if (isHfPath(file.path)) return () => { cancelled = true; };
    explorerInfo(file.path)
      .then((info) => {
        if (!cancelled) setFileInfo(info);
      })
      .catch(() => {
        if (!cancelled) setFileInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [file.path]);

  useEffect(() => {
    setImageError(null);
  }, [file.path]);

  const handleOverlayClick = (event: React.MouseEvent) => {
    if (event.target === event.currentTarget) handleClose();
  };

  const extLabel = file.extension?.replace(/^\./, "").toUpperCase() || "MEDIA";
  const metadataRows = [
    ["Size", hydrusMetadata?.size ?? fileInfo?.size_bytes ?? file.size],
    ["Created", fileInfo?.created_at],
    ["Modified", hydrusMetadata?.time_modified ? new Date(hydrusMetadata.time_modified * 1000).toISOString() : fileInfo?.modified_at ?? file.modified_at],
    ["Path", file.path],
    ["MIME", hydrusMetadata?.mime],
    ["Resolution", hydrusMetadata?.width && hydrusMetadata?.height ? `${hydrusMetadata.width} × ${hydrusMetadata.height}` : undefined],
    ["Duration", typeof hydrusMetadata?.duration === "number" ? `${Math.round(hydrusMetadata.duration)} s` : undefined],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) handleClose();
      }}
    >
      <DialogContent
        showCloseButton={false}
        initialFocus={initialFocusRef}
        finalFocus={returnFocusRef}
        aria-modal="true"
        data-shell-region="files-viewer"
        className="fixed inset-0 z-[100] flex h-dvh w-screen max-w-none translate-x-0 translate-y-0 gap-0 rounded-none border-0 bg-black p-0 text-foreground shadow-none sm:max-w-none"
        onClick={handleOverlayClick}
        onTouchStart={handleTouchStart}
        onTouchEnd={handleTouchEnd}
      >
      <div className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-start justify-between p-4">
          <div className="pointer-events-auto flex min-w-0 items-center gap-3 rounded-md border border-border bg-card/90 px-4 py-2 backdrop-blur">
            <DialogTitle className="min-w-0 truncate text-[16px] font-medium">{file.name}</DialogTitle>
            <span className="shrink-0 rounded border border-border bg-muted px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{extLabel}</span>
            {viewableFiles.length > 1 && <span className="shrink-0 text-xs text-muted-foreground">{currentIndex + 1} / {viewableFiles.length}</span>}
          </div>
          <Button ref={initialFocusRef} variant="ghost" size="icon" className="pointer-events-auto text-muted-foreground hover:bg-muted hover:text-foreground" onClick={handleClose} aria-label="閉じる">
            <X className="size-5" />
          </Button>
        </div>

        <div className="group relative flex min-h-0 flex-1 items-center justify-center">
          <Button
            variant="ghost"
            size="icon"
            className="absolute left-4 z-20 rounded-full border border-border bg-card/80 text-foreground opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100"
            onClick={(event) => { event.stopPropagation(); void goPrev(); }}
            aria-label="前のファイル"
          >
            <ChevronRight className="size-5 rotate-180" />
          </Button>
          <div
            className="flex h-full w-full items-center justify-center overflow-auto p-4 sm:p-8"
            onClick={handleOverlayClick}
          >
            {isImage(file.type || "") && !imageError && (
              <div
                className="flex h-full w-full items-center justify-center overflow-auto"
                onClick={handleOverlayClick}
              >
                <img
                  key={file.path}
                  src={getFilerFileUrl(file.path)}
                  alt={file.name}
                  className="max-h-full max-w-full select-none object-contain"
                  draggable={false}
                  onError={() => setImageError("画像を表示できませんでした（404 / 認証 / パス解決失敗の可能性）")}
                />
              </div>
            )}
            {isImage(file.type || "") && imageError && (
              <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-border bg-card/80 px-8 py-10 text-center text-muted-foreground">
                <FileIcon className="size-10 opacity-60" />
                <span className="text-sm">画像を表示できませんでした</span>
                <span className="max-w-[60vw] truncate text-xs">{file.path}</span>
                <span className="text-[11px] opacity-70">{imageError}</span>
              </div>
            )}
            {isVideo(file.type || "") && (
              <video
                ref={videoRef}
                src={getFilerFileUrl(file.path)}
                controls
                autoPlay
                className="max-h-full max-w-full object-contain"
                onClick={(event) => event.stopPropagation()}
              >
                <track kind="captions" />
              </video>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="absolute right-4 z-20 rounded-full border border-border bg-card/80 text-foreground opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100"
            onClick={(event) => { event.stopPropagation(); void goNext(); }}
            aria-label="次のファイル"
          >
            <ChevronRight className="size-5" />
          </Button>
        </div>

        <div className="absolute inset-x-0 bottom-4 z-20 flex flex-col items-center gap-3 px-4">
          {viewableFiles.length > 1 && (
            <div className="flex max-w-full items-center gap-3 overflow-x-auto rounded-xl border border-border bg-card/90 px-4 py-3 backdrop-blur">
              {viewableFiles.map((item) => {
                const active = item.path === file.path;
                const itemIsImage = isImage(item.type || "");
                return (
                  <button key={item.path} type="button" className={cn("relative size-16 shrink-0 overflow-hidden rounded-md border border-border bg-muted/40 opacity-60 transition-opacity hover:opacity-100", active && "border-2 border-primary opacity-100 ring-2 ring-primary/20")} onClick={() => onNavigate(item)} title={item.name}>
                    {itemIsImage ? <img src={getImageThumbnailUrl(item.path, 128)} alt="" className="size-full object-cover" loading="lazy" /> : <span className="flex size-full items-center justify-center text-muted-foreground">{isVideo(item.type || "") ? <Play className="size-5" /> : <FileIcon className="size-5" />}</span>}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <aside className="hidden w-[300px] shrink-0 flex-col border-l border-border bg-card md:flex">
        <div className="flex h-14 items-center justify-between border-b border-border px-4">
          <div className="flex items-center gap-2"><Info className="size-4 text-muted-foreground" /><span className="text-[16px] font-semibold">File Properties</span></div>
          <Button variant="ghost" size="icon-xs" className="text-muted-foreground hover:bg-muted hover:text-foreground" onClick={handleClose} aria-label="閉じる"><X className="size-4" /></Button>
        </div>
        <div className="flex-1 space-y-6 overflow-auto p-4">
          <section className="space-y-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">General</h3>
            <div className="space-y-3 text-[13px]">
              <div className="flex flex-col gap-1"><span className="text-muted-foreground">Name</span><span className="break-all font-medium">{file.name}</span></div>
              <div className="grid grid-cols-2 gap-3 border-t border-border pt-3"><div className="flex flex-col gap-1"><span className="text-muted-foreground">Size</span><span className="font-mono text-xs">{typeof fileInfo?.size_bytes === "number" ? formatBytes(fileInfo.size_bytes) : typeof file.size === "number" ? formatBytes(file.size) : "-"}</span></div><div className="flex flex-col gap-1 text-right"><span className="text-muted-foreground">Format</span><span className="font-mono text-xs">{extLabel}</span></div></div>
            </div>
          </section>
          <section className="space-y-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">File metadata</h3>
            <div className="space-y-3 rounded-md border border-border bg-muted/30 p-3 text-xs">
              {metadataRows.map(([label, value]) => <div key={label} className="flex items-start justify-between gap-3"><span className="text-muted-foreground">{label}</span><span className={cn("max-w-[11rem] break-all text-right", label === "Path" && "font-mono text-[10px] text-muted-foreground")}>{label === "Size" && typeof value === "number" ? formatBytes(value) : (label === "Modified" || label === "Created") && typeof value === "string" ? new Date(value).toLocaleString() : String(value)}</span></div>)}
            </div>
          </section>
        </div>
      </aside>
      </DialogContent>
    </Dialog>
  );
}

function formatBytes(bytes?: number): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

// 絶対パス判定（ブックマーク権限フィルタ用）
// ─── ファイルエクスプローラメインコンテンツ ───
// テキストファイル拡張子
const TEXT_EXTS = new Set([
  ".txt",
  ".md",
  ".markdown",
  ".json",
  ".yaml",
  ".yml",
  ".xml",
  ".csv",
  ".log",
  ".py",
  ".js",
  ".ts",
  ".jsx",
  ".tsx",
  ".html",
  ".css",
  ".sql",
  ".ini",
  ".cfg",
  ".bat",
  ".cmd",
  ".sh",
  ".ps1",
  ".vbs",
]);

// ─── エディタスプリットビュー ───
function EditorPane({
  editingFile,
  closeEditor,
  handleFileClick,
  onContextMenu,
  onBackgroundContextMenu,
}: {
  editingFile: { path: string; name: string; extension: string };
  closeEditor: () => void;
  handleFileClick: (file: ExplorerFile) => void;
  onContextMenu: (
    e: React.MouseEvent,
    item: ExplorerDirectory | ExplorerFile,
  ) => void;
  onBackgroundContextMenu: (e: React.MouseEvent) => void;
}) {
  const { browseData, navigate, currentPath, goUp, refresh, filerTab } = useExplorer();
  const { snippets } = useSnippets();
  const [editorState, setEditorState] = useState<{
    path: string | null;
    content: string | null;
    error: string | null;
  }>({ path: null, content: null, error: null });
  const [showChat, setShowChat] = useState(false);
  const [selectedText, setSelectedText] = useState("");
  const defaultEditorLoadError = "Failed to load file";
  /*

  // ファイル内容をロード
  useEffect(() => {
    let cancelled = false;
    explorerFullContent(editingFile.path).then((res) => {
      if (cancelled) return;
      setEditorState({
        path: editingFile.path,
        content: res.success ? res.content : null,
        error: res.success
          ? null
          : (res.error || "繝輔ぃ繧､繝ｫ縺ｮ隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆"),
      });
      return;
      if (res.success) {
        if (cancelled) return;
        setEditorState({
          path: editingFile.path,
          content: res.content,
          error: null,
        });
      } else {
        setLoadError(res.error || "ファイルの読み込みに失敗しました");
      }
    });
  }, [editingFile.path]);
  */

  useEffect(() => {
    let cancelled = false;
    explorerFullContent(editingFile.path).then((res) => {
      if (cancelled) return;
      setEditorState({
        path: editingFile.path,
        content: res.success ? res.content : null,
        error: res.success ? null : res.error || defaultEditorLoadError,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [defaultEditorLoadError, editingFile.path]);

  const isLoading = editorState.path !== editingFile.path;
  const editorContent = isLoading ? null : editorState.content;
  const loadError = isLoading ? null : editorState.error;

  return (
    <div className="flex h-full min-h-0" data-shell-region="files-editor">
      {/* 左: ファイルリスト（コンパクト） */}
      <div
        className="hidden w-[240px] shrink-0 flex-col overflow-auto border-r border-border bg-card md:flex"
        onContextMenu={onBackgroundContextMenu}
      >
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border px-4">
          <span className="text-[16px] font-semibold">Files</span>
          <div className="flex items-center gap-1 text-muted-foreground">
            <Button variant="ghost" size="icon-xs" className="text-muted-foreground hover:bg-muted" onClick={() => void refresh()} title="更新"><Search className="size-3.5" /></Button>
          </div>
        </div>
        <div className="border-b border-border px-4 py-3">
          <div
            className="truncate text-[11px] font-semibold uppercase tracking-wider text-muted-foreground"
            title={currentPath}
          >
            {FILER_TAB_LABELS[filerTab]} / {currentPath.split("/").pop() || "ルート"}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          {browseData?.can_go_up && (
            <button
              className="mb-1 flex h-7 w-full items-center gap-1.5 rounded px-2 text-xs text-muted-foreground hover:bg-muted"
              onClick={goUp}
            >
              <ArrowUp className="size-3" />
              上へ
            </button>
          )}
          {browseData?.directories.map((dir) => (
            <button
              key={dir.path}
              className="flex h-7 w-full items-center gap-1.5 truncate rounded px-2 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground"
              onClick={() => navigate(dir.path)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, dir);
              }}
              title={dir.name}
            >
              <><span className="text-muted-foreground">▸</span><Folder className="size-3.5 shrink-0 text-tertiary" />{dir.name}</>
            </button>
          ))}
          {browseData?.files.map((file) => (
            <button
              key={file.path}
              className={`flex h-7 w-full items-center gap-1.5 truncate rounded px-2 text-[13px] hover:bg-muted ${
                file.path === editingFile.path
                  ? "border-l-2 border-primary bg-muted font-medium text-foreground"
                  : ""
              }`}
              onClick={() => handleFileClick(file)}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onContextMenu(e, file);
              }}
              title={file.name}
            >
              <><FileIcon className="size-3.5 shrink-0 text-muted-foreground" />{file.name}</>
            </button>
          ))}
        </div>
      </div>

      {/* 右: エディタ + チャットパネル */}
      <div className="flex min-h-0 flex-1 min-w-0">
        <div className="min-h-0 flex-1 min-w-0">
          {loadError ? (
            <div className="flex h-full items-center justify-center text-sm text-destructive">
              {loadError}
            </div>
          ) : editorContent === null ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              読み込み中...
            </div>
          ) : (
            <DocumentEditor
              filePath={editingFile.path}
              initialContent={editorContent}
              extension={editingFile.extension}
              snippets={snippets}
              autoFocus
              onClose={closeEditor}
              onAskAI={(text) => {
                setSelectedText(text);
                setShowChat(true);
              }}
            />
          )}
        </div>

        {/* インラインチャットパネル */}
        {showChat && (
          <InlineChatPanel
            filePath={editingFile.path}
            selectedText={selectedText}
            onClose={() => setShowChat(false)}
          />
        )}
      </div>
    </div>
  );
}

// ─── 新規テキストファイルダイアログ ───
function NewTextFileDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (path: string, name: string) => void;
}) {
  const { currentPath, refresh } = useExplorer();
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);

  const handleCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const fileName = trimmed.includes(".") ? trimmed : trimmed + ".md";
    const filePath = currentPath ? `${currentPath}/${fileName}` : fileName;
    setLoading(true);
    try {
      const file = new File([""], fileName, { type: "text/plain" });
      await explorerUpload(currentPath, [file]);
      refresh();
      setName("");
      onOpenChange(false);
      onCreated(filePath, fileName);
    } catch (error) {
      if (error instanceof ExplorerUploadError) {
        toast.error(error.message, uploadFailureToastOptions(error.batchResult.failures));
      } else {
        toast.error(
          error instanceof Error ? error.message : "ファイルの作成に失敗しました",
        );
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新規テキストファイル</DialogTitle>
        </DialogHeader>
        <NewItemNameInput
          placeholder="ファイル名（例: notes.txt）"
          value={name}
          onValueChange={setName}
          onSubmit={handleCreate}
          autoFocus
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button onClick={handleCreate} disabled={!name.trim() || loading}>
            {loading ? "作成中..." : "作成"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ExplorerContent() {
  const explorerRootRef = useRef<HTMLDivElement>(null);
  const explorerScrollRef = useRef<HTMLDivElement>(null);
  const pendingIncrementalSearchRef = useRef("");
  const quickFilterInputRef = useRef<HTMLInputElement>(null);
  const fileSearchInputRef = useRef<HTMLInputElement>(null);
  const fileSearchReplaceInputRef = useRef<HTMLInputElement>(null);
  const isTextInput = useCallback((target: EventTarget | null) => {
    if (!(target instanceof HTMLElement)) return false;
    return (
      target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable
    );
  }, []);
  const activePathRef = useRef<string | null>(null);
  const searchParams = useSearchParams();
  const {
    currentPath,
    navigate,
    goBack,
    goForward,
    goUp,
    refresh,
    browseData,
    loading,
    error,
    viewMode,
    setViewMode,
    setSort,
    filerTab,
    setFilerTab,
    homeRootPath,
    contextRootPath,
    isAbsoluteFilerPath,
    isRemoteWorkspace,
    isHfMode,
    isHydrusMode,
    capabilities,
    setBrowseData,
    selectedItems,
    focusedItemPath,
    selectItem,
    toggleSelect,
    selectRange,
    selectAll,
    clearSelection,
    clipboard,
    setClipboard,
    editingFile,
    openEditor,
    closeEditor,
    hfCreatorMapping,
    hfSearchQuery,
    setHfSearchQuery,
    userId,
  } = useExplorer();
  const hydrusPagingRef = useRef<HydrusPagingController | null>(null);

  // Homeボタンと Ctrl+H のナビゲート先。
  const homeNavigate = useCallback(() => {
    if (filerTab === "hydrus") return;
    if (filerTab === "hf" || isHfMode || isHfPath(currentPath)) {
      navigate(HF_PREFIX);
    } else {
      navigate(homeRootPath || "");
    }
  }, [navigate, homeRootPath, currentPath, filerTab, isHfMode]);

  const cycleFilerTab = useCallback(
    (direction: -1 | 1) => {
      const currentIndex = FILER_TAB_ORDER.indexOf(filerTab);
      const nextIndex =
        (Math.max(0, currentIndex) + direction + FILER_TAB_ORDER.length) %
        FILER_TAB_ORDER.length;
      const nextTab = FILER_TAB_ORDER[nextIndex];

      if (nextTab) {
        setFilerTab(nextTab);
      }
    },
    [filerTab, setFilerTab],
  );

  // 削除・移動・Undo/Redo の実行は filer-operations 側へ集約。
  // Hydrus タブの表示反映は ExplorerProvider の prune/restore が担う。
  const { deleteTargets, transfer, undo, redo, renameBatch } = useFilerOperations({
    capabilities,
    refresh,
  });

  // Hydrus 検索エラー（検索結果そのものは context の browseData に流し込む）
  const [hydrusError, setHydrusError] = useState<string | null>(null);
  const [fileSearchQuery, setFileSearchQuery] = useState("");
  const [fileSearchReplaceQuery, setFileSearchReplaceQuery] = useState("");
  const [fileSearchRegex, setFileSearchRegex] = useState(false);
  const [fileSearchOpen, setFileSearchOpen] = useState(false);
  const [fileSearchActive, setFileSearchActive] = useState(false);
  const [fileSearchLoading, setFileSearchLoading] = useState(false);
  const [fileSearchError, setFileSearchError] = useState<string | null>(null);
  const [fileSearchCount, setFileSearchCount] = useState(0);
  const [fileSearchTruncated, setFileSearchTruncated] = useState(false);
  const [quickFilterOpen, setQuickFilterOpen] = useState(false);
  const [quickFilterQuery, setQuickFilterQuery] = useState("");
  // `openEditorFromFiler` is also used by the URL-open effect.  Keep the
  // transient search state in refs so that the callback does not change
  // identity whenever a filter is typed (and accidentally re-run that
  // effect).
  const fileSearchStateRef = useRef({ open: false, active: false });
  const refreshRef = useRef(refresh);
  useEffect(() => {
    fileSearchStateRef.current = {
      open: fileSearchOpen,
      active: fileSearchActive,
    };
  }, [fileSearchActive, fileSearchOpen]);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  const audioPlayer = useAudioPlayer();

  // ダイアログ状態
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newTextFileOpen, setNewTextFileOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [viewerFile, setViewerFile] = useState<ExplorerFile | null>(null);
  const viewerReturnFocusRef = useRef<HTMLElement | null>(null);
  const [previewFile, setPreviewFile] = useState<ExplorerFile | null>(null);
  const [hfReferenceOpen, setHfReferenceOpen] = useState(false);
  const [recordTableFile, setRecordTableFile] = useState<ExplorerFile | null>(
    null,
  );

  // Do not retain an A user's viewer/preview selection while the authenticated
  // principal changes (including logout).  The panel itself also gates late
  // metadata responses, but clearing these roots prevents one-frame leakage of
  // the previous file entirely.
  useEffect(() => {
    setViewerFile(null);
    setPreviewFile(null);
    setCtxItem(null);
    setCtxPos(null);
    setRenameTarget(null);
    setRecordTableFile(null);
    setHfReferenceOpen(false);
    setHydrusError(null);
    closeEditor();
  }, [closeEditor, userId]);

  const loadHydrusBoundary = useCallback(
    async (direction: -1 | 1): Promise<ExplorerFile | null> => {
      if (!isHydrusMode) return null;
      const controller = hydrusPagingRef.current;
      if (!controller) return null;
      let targetPage = controller.page + direction;
      let totalPages = controller.totalPages;
      while (targetPage >= 1 && targetPage <= totalPages) {
        const result = await controller.loadPage(targetPage);
        if (!result) return null;
        totalPages = result.totalPages;
        const candidates = viewerFiles(result.data.files).filter(
          (item) => item.path !== viewerFile?.path,
        );
        const target = boundaryViewerFile(candidates, direction);
        if (target) return target;
        targetPage += direction;
      }
      return null;
    },
    [isHydrusMode, viewerFile?.path],
  );

  const preloadHydrusAdjacent = useCallback(
    async (direction: -1 | 1): Promise<ExplorerFile[]> => {
      if (!isHydrusMode) return [];
      const controller = hydrusPagingRef.current;
      if (!controller) return [];
      const targetPage = controller.page + direction;
      if (targetPage < 1 || targetPage > controller.totalPages) return [];
      const result = await controller.prefetchPage(targetPage);
      if (!result) return [];
      return viewerFiles(result.data.files);
    },
    [isHydrusMode],
  );

  // コンテキストメニュー
  const [ctxItem, setCtxItem] = useState<
    ExplorerDirectory | ExplorerFile | null
  >(null);
  const [ctxPos, setCtxPos] = useState<{ x: number; y: number } | null>(null);
  const editorEscapeStateRef = useRef<DoubleEscapeState>(
    EMPTY_DOUBLE_ESCAPE_STATE,
  );
  // The shared shell boundary and the canvas React capture handler both see
  // key events for the main Files surface.  Mark each native event once so
  // the state machine is never advanced twice for the same Escape.
  const handledEditorEscapeEventsRef = useRef<WeakSet<Event>>(new WeakSet());

  // 音楽ファイルプレイリスト
  const audioFiles = useMemo(
    () =>
      (browseData?.files ?? [])
        .filter((f) => isAudio(f.type || ""))
        .map((f) => ({
          name: f.name,
          path: f.path,
          type: f.type || "audio",
          rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
          sourceKind: isAbsoluteFilerPath
            ? ("filer" as const)
            : ("explorer" as const),
        })),
    [browseData, contextRootPath, currentPath, isAbsoluteFilerPath],
  );
  const normalizedQuickFilter = quickFilterQuery.trim().toLowerCase();
  const visibleDirectories = useMemo(
    () =>
      (browseData?.directories ?? []).filter((directory) =>
        directory.name.toLowerCase().includes(normalizedQuickFilter),
      ),
    [browseData?.directories, normalizedQuickFilter],
  );
  const visibleFiles = useMemo(
    () =>
      (browseData?.files ?? []).filter((file) =>
        file.name.toLowerCase().includes(normalizedQuickFilter),
      ),
    [browseData?.files, normalizedQuickFilter],
  );
  const visibleItems = useMemo<ExplorerItem[]>(
    () => [...visibleDirectories, ...visibleFiles],
    [visibleDirectories, visibleFiles],
  );
  const itemByPath = useMemo(() => {
    return new Map(visibleItems.map((entry) => [entry.path, entry]));
  }, [visibleItems]);
  const selectedPaths = useMemo(
    () => Array.from(selectedItems).filter((path) => itemByPath.has(path)),
    [itemByPath, selectedItems],
  );
  const selectedRegularPaths = useMemo(
    () =>
      selectedPaths.filter((path) => {
        const item = itemByPath.get(path);
        return !item || !("type" in item) || !isRecordTableFile(item);
      }),
    [itemByPath, selectedPaths],
  );
  const selectedArchivePaths = useMemo(
    () =>
      selectedRegularPaths.filter((path) => {
        const item = itemByPath.get(path);
        if (!item || !("type" in item)) return false;
        return isSupportedArchiveName(item.name);
      }),
    [itemByPath, selectedRegularPaths],
  );
  const activePath =
    focusedItemPath && itemByPath.has(focusedItemPath)
      ? focusedItemPath
      : (selectedPaths[0] ?? null);
  const activeItem = activePath ? (itemByPath.get(activePath) ?? null) : null;
  const canUseFileShortcuts =
    !isRemoteWorkspace && !isHfMode && filerTab !== "hydrus";
  const canUseDownloadShortcut = !isHfMode && filerTab !== "hydrus";
  const canUseExplorerSearch = !isHfMode && filerTab !== "hydrus";
  const isExplorerInteractionBlocked =
    loading ||
    !!editingFile ||
    !!recordTableFile ||
    !!viewerFile ||
    !!previewFile ||
    !!renameTarget ||
    newFolderOpen ||
    newTextFileOpen ||
    !!ctxPos;
  const isEditorEscapeBlocked =
    !!recordTableFile ||
    !!viewerFile ||
    !!previewFile ||
    !!renameTarget ||
    newFolderOpen ||
    newTextFileOpen ||
    hfReferenceOpen ||
    quickFilterOpen ||
    !!ctxPos;

  const resetEditorEscape = useCallback(() => {
    editorEscapeStateRef.current = resetDoubleEscapeState();
  }, []);

  useEffect(() => {
    resetEditorEscape();
  }, [
    currentPath,
    editingFile?.path,
    filerTab,
    isEditorEscapeBlocked,
    resetEditorEscape,
  ]);

  useEffect(() => {
    window.addEventListener("blur", resetEditorEscape);
    return () => window.removeEventListener("blur", resetEditorEscape);
  }, [resetEditorEscape]);

  // Shell-owned transient panels consume their Escape before it reaches the
  // workspace.  Reset the armed state through a narrow event bridge so that
  // closing such a panel can never become the editor's second Escape.
  useEffect(() => {
    window.addEventListener(DOUBLE_ESCAPE_RESET_EVENT, resetEditorEscape);
    return () => window.removeEventListener(DOUBLE_ESCAPE_RESET_EVENT, resetEditorEscape);
  }, [resetEditorEscape]);

  const processEditorEscape = useCallback(
    (
      event: {
        key: string;
        repeat: boolean;
        defaultPrevented?: boolean;
        preventDefault: () => void;
        stopPropagation: () => void;
        stopImmediatePropagation?: () => void;
      },
      nativeEvent: Event,
    ) => {
      if (handledEditorEscapeEventsRef.current.has(nativeEvent)) return;
      handledEditorEscapeEventsRef.current.add(nativeEvent);
      if (!editingFile) {
        resetEditorEscape();
        return;
      }
      const next = transitionDoubleEscapeKey(
        editorEscapeStateRef.current,
        event,
        {
          blocked: isEditorEscapeBlocked,
          now: performance.now(),
        },
      );
      editorEscapeStateRef.current = next.state;
      if (!next.shouldClose) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      closeEditor();
    },
    [closeEditor, editingFile, isEditorEscapeBlocked, resetEditorEscape],
  );

  const handleEditorKeyDownCapture = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      processEditorEscape(event, event.nativeEvent);
    },
    [processEditorEscape],
  );

  // The sidebar is rendered beside the canvas, and the mobile Sheet renders a
  // Portal outside the shell subtree.  A document capture boundary covers
  // both while remaining scoped to Files-marked surfaces (never all Escapes).
  useEffect(() => {
    const handleFilesKeyDown = (event: KeyboardEvent) => {
      if (!isFilesWorkspaceTarget(event.target, explorerRootRef.current)) return;
      processEditorEscape(event, event);
    };
    document.addEventListener("keydown", handleFilesKeyDown, true);
    return () => document.removeEventListener("keydown", handleFilesKeyDown, true);
  }, [processEditorEscape]);

  // The canvas owns a React blur handler, but the persistent sidebar (and its
  // mobile Sheet/Portal copy) is rendered outside that subtree.  Observe
  // focusout at the document boundary without consuming it: moving between
  // any Files-marked surface keeps the armed first Escape, while leaving the
  // workspace resets it even when the canvas never receives a blur event.
  useEffect(() => {
    const handleDocumentFocusOut = (event: FocusEvent) => {
      if (!isFilesWorkspaceTarget(event.target, explorerRootRef.current)) return;
      if (event.relatedTarget && isFilesWorkspaceTarget(event.relatedTarget, explorerRootRef.current)) {
        return;
      }
      resetEditorEscape();
    };
    document.addEventListener("focusout", handleDocumentFocusOut, true);
    return () => document.removeEventListener("focusout", handleDocumentFocusOut, true);
  }, [resetEditorEscape]);

  const handleExplorerBlur = useCallback(
    (event: React.FocusEvent<HTMLDivElement>) => {
      const relatedTarget = event.relatedTarget;
      if (
        relatedTarget &&
        isFilesWorkspaceTarget(relatedTarget, event.currentTarget)
      ) {
        return;
      }
      resetEditorEscape();
    },
    [resetEditorEscape],
  );

  const resetFileSearchState = useCallback((clearQuery = false) => {
    if (clearQuery) setFileSearchQuery("");
    setFileSearchActive(false);
    setFileSearchError(null);
    setFileSearchCount(0);
    setFileSearchTruncated(false);
  }, []);

  const clearFileSearch = useCallback(() => {
    resetFileSearchState(true);
    clearSelection();
    void refresh();
  }, [clearSelection, refresh, resetFileSearchState]);

  const openFileSearch = useCallback(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
    setFileSearchOpen(true);
    window.requestAnimationFrame(() => {
      fileSearchInputRef.current?.focus();
      fileSearchInputRef.current?.select();
    });
  }, []);

  const closeFileSearch = useCallback(() => {
    setFileSearchOpen(false);
    setFileSearchReplaceQuery("");
    setFileSearchRegex(false);
    clearFileSearch();
    window.requestAnimationFrame(() => {
      explorerRootRef.current?.focus({ preventScroll: true });
    });
  }, [clearFileSearch]);

  const closeQuickFilter = useCallback(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
    window.requestAnimationFrame(() => {
      explorerRootRef.current?.focus({ preventScroll: true });
    });
  }, []);

  /**
   * Move from an explorer interaction into the editor as one state
   * transition.  Quick Filter, file search, preview and the context menu are
   * all transient surfaces owned by this workspace; leaving any of them
   * mounted while `editingFile` is set makes the shell quite correctly treat
   * Escape as belonging to that (now hidden) surface forever.  Clear those
   * surfaces before opening the editor, while leaving real dialogs/viewers
   * untouched so their higher Escape priority is not changed.
   */
  const openEditorFromFiler = useCallback(
    (file: { path: string; name: string; type?: string }) => {
      const hadFileSearch =
        fileSearchStateRef.current.open || fileSearchStateRef.current.active;

      setQuickFilterOpen(false);
      setQuickFilterQuery("");
      setFileSearchOpen(false);
      setFileSearchReplaceQuery("");
      setFileSearchRegex(false);
      resetFileSearchState(true);
      if (hadFileSearch) {
        // Search replaces browseData with a result set.  Restore the current
        // directory without the focus side effect of closeFileSearch(), since
        // the next focus owner is the CodeMirror editor.
        clearSelection();
        void refreshRef.current();
      }
      setPreviewFile(null);
      setCtxItem(null);
      setCtxPos(null);

      resetEditorEscape();
      openEditor(file);
    },
    [clearSelection, openEditor, resetEditorEscape, resetFileSearchState],
  );

  // URL ?open=path パラメータでファイルを自動オープン
  useEffect(() => {
    const openPath = searchParams.get("open");
    if (openPath) {
      const name = openPath.split("/").pop() || openPath;
      openEditorFromFiler({ path: openPath, name });
    }
    const recordProject = searchParams.get("recordProject");
    const recordTable = searchParams.get("recordTable");
    if (recordProject && recordTable) {
      const name = searchParams.get("recordName") || "DBテーブル.dbtable";
      const file: ExplorerFile = {
        name,
        path: `aoitalk-record-table:${recordProject}:${recordTable}`,
        type: "application/x-aoitalk-record-table",
        extension: ".dbtable",
        virtual_kind: "record_table",
        project_id: recordProject,
        record_table_id: recordTable,
      };
      window.setTimeout(() => setRecordTableFile(file), 0);
    }
  }, [openEditorFromFiler, searchParams]);

  const runFileSearch = useCallback(async () => {
    const query = fileSearchQuery.trim();
    if (!query) {
      clearFileSearch();
      return;
    }

    setFileSearchLoading(true);
    setFileSearchError(null);
    setFileSearchActive(false);
    setFileSearchTruncated(false);
    try {
      const data = await explorerSearch(query, currentPath, 200, {
        regex: fileSearchRegex,
      });
      const directories: ExplorerDirectory[] = [];
      const files: ExplorerFile[] = [];

      for (const item of data.results) {
        if (item.kind === "directory" || !item.type) {
          directories.push(searchResultToDirectory(item));
        } else {
          files.push(searchResultToFile(item));
        }
      }

      setBrowseData({
        success: true,
        current_path: currentPath,
        parent_path: browseData?.parent_path ?? null,
        can_go_up: browseData?.can_go_up ?? false,
        directories,
        files,
        total_items: directories.length + files.length,
        is_admin_mode: browseData?.is_admin_mode,
      });
      setFileSearchActive(true);
      setFileSearchCount(directories.length + files.length);
      setFileSearchTruncated(Boolean(data.truncated));
      clearSelection();
    } catch (error) {
      setFileSearchError(explorerErrorMessage(error));
    } finally {
      setFileSearchLoading(false);
    }
  }, [
    browseData?.can_go_up,
    browseData?.is_admin_mode,
    browseData?.parent_path,
    clearFileSearch,
    clearSelection,
    currentPath,
    fileSearchQuery,
    fileSearchRegex,
    setBrowseData,
  ]);

  const replaceFileSearch = useCallback(async () => {
    const query = fileSearchQuery.trim();
    if (!query || !fileSearchActive || !browseData || !capabilities.canRename) {
      return;
    }

    try {
      const items = [...browseData.directories, ...browseData.files]
        .map((item) => ({
          path: item.path,
          currentName: item.name,
          newName: replaceFilerName(
            item.name,
            query,
            fileSearchReplaceQuery,
            fileSearchRegex,
          ),
        }))
        .filter((item) => item.newName !== item.currentName);
      if (items.length === 0) {
        setFileSearchError("置換対象がありません");
        return;
      }

      setFileSearchError(null);
      const result = await renameBatch(items);
      if (result.renamed > 0) {
        await runFileSearch();
        if (fileSearchTruncated) {
          toast.warning("検索結果の上限200件までを置換しました");
        }
      }
    } catch (error) {
      setFileSearchError(
        error instanceof Error ? error.message : "置換に失敗しました",
      );
    }
  }, [
    browseData,
    capabilities.canRename,
    fileSearchActive,
    fileSearchQuery,
    fileSearchRegex,
    fileSearchReplaceQuery,
    fileSearchTruncated,
    renameBatch,
    runFileSearch,
  ]);

  useEffect(() => {
    resetFileSearchState();
    setFileSearchOpen(false);
    setFileSearchQuery("");
    setFileSearchReplaceQuery("");
    setFileSearchRegex(false);
  }, [currentPath, filerTab, resetFileSearchState]);

  // ファイルを開く
  const handleFileClick = useCallback(
    (file: ExplorerFile) => {
      if (isRecordTableFile(file)) {
        setRecordTableFile(file);
        closeEditor();
        setPreviewFile(null);
      } else if (isAudio(file.type || "")) {
        audioPlayer.play(
          {
            name: file.name,
            path: file.path,
            type: file.type || "audio",
            rootPath: isAbsoluteFilerPath ? currentPath : contextRootPath || "",
            sourceKind: isAbsoluteFilerPath ? "filer" : "explorer",
          },
          audioFiles,
        );
      } else if (isImage(file.type || "") || isVideo(file.type || "")) {
        const sourceItem = Array.from(
          document.querySelectorAll<HTMLElement>("[data-explorer-item-path]"),
        ).find((item) => item.dataset.explorerItemPath === file.path);
        if (sourceItem) {
          if (!sourceItem.hasAttribute("tabindex")) sourceItem.tabIndex = -1;
          viewerReturnFocusRef.current = sourceItem;
        } else if (document.activeElement instanceof HTMLElement) {
          viewerReturnFocusRef.current = document.activeElement;
        }
        setViewerFile(file);
      } else if (TEXT_EXTS.has(file.extension || "")) {
        // テキストファイルはエディタで開く
        openEditorFromFiler(file);
      } else {
        setPreviewFile(file);
      }
    },
    [
      audioPlayer,
      audioFiles,
      closeEditor,
      contextRootPath,
      currentPath,
      isAbsoluteFilerPath,
      openEditorFromFiler,
    ],
  );

  // Shell launchers dispatch into the canonical Files open/download handlers
  // so editor, media viewer, audio player, and download behavior stay aligned
  // with normal Files interactions.
  useEffect(() => {
    const parentPath = (path: string): string | null => {
      const normalized = path.replace(/[\\/]+$/, "");
      const index = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
      return index > 0 ? normalized.slice(0, index) : null;
    };
    const resolveCanonicalFile = async (path: string, displayName?: string): Promise<ExplorerFile | null> => {
      const direct = browseData?.files.find((candidate) => candidate.path === path);
      if (direct) return direct;
      // Record-table launchers use a canonical virtual path and may not be
      // present in a filesystem directory listing. Preserve the metadata
      // required by the existing record-table viewer instead of inferring an
      // opaque binary file.
      const recordPrefix = "aoitalk-record-table:";
      if (path.startsWith(recordPrefix)) {
        const [projectId, recordTableId] = path.slice(recordPrefix.length).split(":", 2);
        if (projectId && recordTableId) {
          return {
            name: displayName?.trim() || `${recordTableId}${RECORD_TABLE_EXTENSION}`,
            path,
            type: RECORD_TABLE_TYPE,
            extension: RECORD_TABLE_EXTENSION,
            virtual_kind: "record_table",
            project_id: projectId,
            record_table_id: recordTableId,
          };
        }
      }
      // A launcher can point outside the currently visible directory. Fetch
      // its parent so record-table metadata (`virtual_kind`, project/table IDs)
      // is preserved instead of falling through to an octet-stream preview.
      const candidates = [parentPath(path), currentPath].filter(
        (candidate, index, all): candidate is string => Boolean(candidate) && all.indexOf(candidate) === index,
      );
      for (const candidate of candidates) {
        try {
          const data = await explorerList(candidate);
          const exact = data.files.find((file) => file.path === path);
          if (exact) return exact;
        } catch {
          // Try the current directory fallback before reporting an error.
        }
      }
      return null;
    };
    const onOpen = async (event: Event) => {
      const detail = (event as CustomEvent<{ path?: unknown; name?: unknown }>).detail;
      const path = detail?.path;
      if (typeof path !== "string" || !path) return;
      const name = typeof detail.name === "string" ? detail.name : undefined;
      const item = await resolveCanonicalFile(path, name);
      if (item) {
        handleFileClick(item);
      } else {
        toast.error("ファイル情報を取得できないため開けませんでした");
      }
    };
    const onDownload = (event: Event) => {
      const path = (event as CustomEvent<{ path?: unknown }>).detail?.path;
      if (typeof path !== "string" || !path) return;
      void explorerDownloadPaths([path]).catch((error) => {
        toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
      });
    };
    window.addEventListener(FILES_OPEN_PATH_EVENT, onOpen);
    window.addEventListener(FILES_DOWNLOAD_PATH_EVENT, onDownload);
    return () => {
      window.removeEventListener(FILES_OPEN_PATH_EVENT, onOpen);
      window.removeEventListener(FILES_DOWNLOAD_PATH_EVENT, onDownload);
    };
  }, [browseData?.files, currentPath, handleFileClick]);

  // F7 / Shift+F7 と Ctrl/Cmd+N / Ctrl/Cmd+Shift+N（作成可能時のみ）
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const creationShortcut = resolveFilerCreationShortcut(e, {
        canCreate: capabilities.canCreate,
        creationDialogOpen: newFolderOpen || newTextFileOpen,
        inputFocused: isTextInput(e.target),
      });
      if (creationShortcut.matched) {
        // Ctrl/Cmd+N はブラウザの新規ウィンドウと競合するため、Files画面
        // 上では権限・フォーカス状態にかかわらず既定動作を抑止する。
        if (creationShortcut.preventDefault) e.preventDefault();
        if (creationShortcut.action === "text-file") {
          setNewTextFileOpen(true);
        } else if (creationShortcut.action === "folder") {
          setNewFolderOpen(true);
        }
        // 権限なし・入力中・作成モーダル表示中はpreventDefaultだけで終了。
        return;
      }
      if (!capabilities.canCreate || newFolderOpen || newTextFileOpen) return;
      // 入力フィールドがフォーカスされている場合は既存のF7操作を無効化。
      if (isTextInput(e.target)) return;
      if (e.code === "F7" && e.shiftKey) {
        e.preventDefault();
        setNewTextFileOpen(true);
      } else if (e.code === "F7" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        setNewFolderOpen(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [capabilities.canCreate, isTextInput, newFolderOpen, newTextFileOpen]);

  const copySelectedItems = useCallback(
    (operation: "copy" | "cut") => {
      if (selectedRegularPaths.length === 0) return;
      setClipboard({ paths: selectedRegularPaths, operation });
    },
    [selectedRegularPaths, setClipboard],
  );

  const pasteClipboardItems = useCallback(async () => {
    if (!clipboard) return;
    await transfer({
      paths: clipboard.paths,
      destDir: currentPath,
      operation: clipboard.operation === "cut" ? "move" : "copy",
      onTransferred: () => {
        if (clipboard.operation === "cut") setClipboard(null);
        clearSelection();
      },
    });
  }, [clearSelection, clipboard, currentPath, setClipboard, transfer]);

  const archiveSelectedItems = useCallback(async () => {
    let targetPaths = selectedRegularPaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      (!("type" in activeItem) || !isRecordTableFile(activeItem))
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseFileShortcuts || targetPaths.length === 0) return;
    try {
      const result = await explorerArchive(targetPaths, currentPath);
      clearSelection();
      await refresh();
      toast.success(`${result.archive_name}を作成しました`);
    } catch {
      toast.error("zip圧縮に失敗しました");
    }
  }, [
    canUseFileShortcuts,
    clearSelection,
    currentPath,
    refresh,
    activeItem,
    selectedRegularPaths,
  ]);

  const extractSelectedItems = useCallback(async () => {
    let targetPaths = selectedArchivePaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      "type" in activeItem &&
      isSupportedArchiveName(activeItem.name)
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseFileShortcuts || (!activeItem && selectedPaths.length === 0)) {
      return;
    }
    if (targetPaths.length === 0) {
      toast.error("対応している圧縮ファイルを選択してください");
      return;
    }
    try {
      const result = await explorerExtract(targetPaths, currentPath);
      clearSelection();
      await refresh();
      toast.success(`${result.extracted.length}件の圧縮ファイルを展開しました`);
    } catch {
      toast.error("圧縮ファイルの展開に失敗しました");
    }
  }, [
    canUseFileShortcuts,
    clearSelection,
    currentPath,
    refresh,
    activeItem,
    selectedPaths.length,
    selectedArchivePaths,
  ]);

  const downloadSelectedItems = useCallback(async () => {
    let targetPaths = selectedRegularPaths;
    if (
      targetPaths.length === 0 &&
      activeItem &&
      (!("type" in activeItem) || !isRecordTableFile(activeItem))
    ) {
      targetPaths = [activeItem.path];
    }
    if (!canUseDownloadShortcut || targetPaths.length === 0) return;
    try {
      await explorerDownloadPaths(targetPaths);
      toast.success(
        targetPaths.length === 1
          ? "ダウンロードを開始しました"
          : `${targetPaths.length}件をダウンロードします`,
      );
    } catch (error) {
      toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
    }
  }, [activeItem, canUseDownloadShortcut, selectedRegularPaths]);

  const deleteSelectedItems = useCallback(async () => {
    if (selectedPaths.length === 0) return;
    const targets = selectedPaths
      .map((path) => itemByPath.get(path))
      .filter((item): item is ExplorerItem => !!item)
      .map(toFilerDeleteTarget);
    await deleteTargets(targets, { onDeleted: clearSelection });
  }, [clearSelection, deleteTargets, itemByPath, selectedPaths]);

  const getRenderedItemPaths = useCallback(() => {
    const renderedPaths = Array.from(
      explorerRootRef.current?.querySelectorAll<HTMLElement>(
        "[data-explorer-item-path]",
      ) ?? [],
    )
      .map((item) => item.dataset.explorerItemPath)
      .filter((path): path is string => !!path && itemByPath.has(path));
    return renderedPaths.length > 0
      ? renderedPaths
      : visibleItems.map((item) => item.path);
  }, [itemByPath, visibleItems]);

  const focusRenderedItemPath = useCallback(
    (path: string) => {
      activePathRef.current = path;
      selectItem(path);
      window.requestAnimationFrame(() => {
        const element = Array.from(
          explorerRootRef.current?.querySelectorAll<HTMLElement>(
            "[data-explorer-item-path]",
          ) ?? [],
        ).find((item) => item.dataset.explorerItemPath === path);
        element?.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
    },
    [selectItem],
  );

  const focusItemByIndex = useCallback(
    (index: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const nextIndex = Math.max(0, Math.min(itemPaths.length - 1, index));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) focusRenderedItemPath(nextPath);
    },
    [focusRenderedItemPath, getRenderedItemPaths],
  );

  const focusItemByOffset = useCallback(
    (offset: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const currentIndex = activePath
        ? itemPaths.findIndex((path) => path === activePath)
        : -1;
      const nextIndex =
        currentIndex < 0
          ? offset < 0
            ? itemPaths.length - 1
            : 0
          : Math.max(0, Math.min(itemPaths.length - 1, currentIndex + offset));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) focusRenderedItemPath(nextPath);
    },
    [activePath, focusRenderedItemPath, getRenderedItemPaths],
  );

  const getVisibleItemPageSize = useCallback(() => {
    const scrollRoot = explorerScrollRef.current ?? explorerRootRef.current;
    if (!scrollRoot) return 10;
    const scrollRect = scrollRoot.getBoundingClientRect();
    const visibleCount = Array.from(
      explorerRootRef.current?.querySelectorAll<HTMLElement>(
        "[data-explorer-item-path]",
      ) ?? [],
    ).filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.bottom > scrollRect.top && rect.top < scrollRect.bottom;
    }).length;
    return Math.max(1, visibleCount || 10);
  }, []);

  const scrollRenderedItemIntoView = useCallback((path: string) => {
    window.requestAnimationFrame(() => {
      const element = Array.from(
        explorerRootRef.current?.querySelectorAll<HTMLElement>(
          "[data-explorer-item-path]",
        ) ?? [],
      ).find((item) => item.dataset.explorerItemPath === path);
      element?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }, []);

  const extendSelectionToPath = useCallback(
    (nextPath: string, itemPaths: string[]) => {
      activePathRef.current = nextPath;
      selectRange(nextPath, itemPaths);
      scrollRenderedItemIntoView(nextPath);
    },
    [scrollRenderedItemIntoView, selectRange],
  );

  const extendSelectionByOffset = useCallback(
    (offset: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const currentIndex = activePath
        ? itemPaths.findIndex((path) => path === activePath)
        : -1;
      const nextIndex =
        currentIndex < 0
          ? offset < 0
            ? itemPaths.length - 1
            : 0
          : Math.max(0, Math.min(itemPaths.length - 1, currentIndex + offset));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) extendSelectionToPath(nextPath, itemPaths);
    },
    [activePath, extendSelectionToPath, getRenderedItemPaths],
  );

  const extendSelectionToIndex = useCallback(
    (index: number) => {
      const itemPaths = getRenderedItemPaths();
      if (itemPaths.length === 0) return;
      const nextIndex = Math.max(0, Math.min(itemPaths.length - 1, index));
      const nextPath = itemPaths[nextIndex];
      if (nextPath) extendSelectionToPath(nextPath, itemPaths);
    },
    [extendSelectionToPath, getRenderedItemPaths],
  );

  const orderedSelectionForClipboard = useCallback(() => {
    const source =
      selectedPaths.length > 0
        ? selectedPaths
        : activePath
          ? [activePath]
          : [];
    if (source.length === 0) return [];
    const set = new Set(source);
    const ordered = getRenderedItemPaths().filter((path) => set.has(path));
    return ordered.length > 0 ? ordered : source;
  }, [activePath, getRenderedItemPaths, selectedPaths]);

  const copySelectedPathsText = useCallback(async () => {
    const paths = orderedSelectionForClipboard();
    if (paths.length === 0) return;
    try {
      await navigator.clipboard.writeText(paths.join("\n"));
      toast.success(
        paths.length > 1
          ? `${paths.length}件のパスをコピーしました`
          : "パスをコピーしました",
      );
    } catch {
      toast.error("クリップボードへのコピーに失敗しました");
    }
  }, [orderedSelectionForClipboard]);

  const copySelectedNamesText = useCallback(async () => {
    const paths = orderedSelectionForClipboard();
    if (paths.length === 0) return;
    const names = paths
      .map((path) => itemByPath.get(path)?.name)
      .filter((name): name is string => !!name);
    if (names.length === 0) return;
    try {
      await navigator.clipboard.writeText(names.join("\n"));
      toast.success(
        names.length > 1
          ? `${names.length}件のファイル名をコピーしました`
          : "ファイル名をコピーしました",
      );
    } catch {
      toast.error("クリップボードへのコピーに失敗しました");
    }
  }, [itemByPath, orderedSelectionForClipboard]);

  const openExplorerItem = useCallback(
    (item: ExplorerItem) => {
      if (isExplorerDirectory(item)) {
        resetFileSearchState();
        navigate(item.path);
      } else {
        handleFileClick(item);
      }
    },
    [handleFileClick, navigate, resetFileSearchState],
  );

  const getRenderedSearchItems = useCallback((): FilerSearchItem[] => {
    return getRenderedItemPaths()
      .map((path) => itemByPath.get(path))
      .filter((item): item is ExplorerItem => !!item)
      .map((item) => ({ path: item.path, name: item.name }));
  }, [getRenderedItemPaths, itemByPath]);

  const incrementalSearch = useIncrementalSearch({
    // ディレクトリ移動・タブ切替・再帰検索結果表示の切替で以前の検索条件を失効させる。
    getContextKey: () =>
      `${filerTab}|${currentPath}|${fileSearchActive ? "search" : "list"}`,
    getItems: getRenderedSearchItems,
    getActiveKey: () => activePathRef.current,
    focusMatch: (item) => focusRenderedItemPath(item.path),
  });

  useEffect(() => {
    activePathRef.current = activePath;
  }, [activePath]);

  useEffect(() => {
    if (loading || isExplorerInteractionBlocked || activePath) return;
    if (!browseData || visibleItems.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      const firstPath = getRenderedItemPaths()[0] ?? visibleItems[0]?.path;
      if (firstPath) focusRenderedItemPath(firstPath);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    activePath,
    browseData,
    focusRenderedItemPath,
    getRenderedItemPaths,
    isExplorerInteractionBlocked,
    loading,
    visibleItems,
  ]);

  useLayoutEffect(() => {
    incrementalSearch.reset();
    if (loading) return;

    const root = explorerRootRef.current;
    const activeElement = document.activeElement;
    const ownsKeyboardFocus =
      !activeElement ||
      activeElement === document.body ||
      !!root?.contains(activeElement);
    if (!ownsKeyboardFocus || isTextInput(activeElement)) return;
    root?.focus({ preventScroll: true });
  }, [currentPath, filerTab, incrementalSearch, isTextInput, loading]);

  useEffect(() => {
    setQuickFilterOpen(false);
    setQuickFilterQuery("");
  }, [currentPath, filerTab]);

  useEffect(() => {
    if (!quickFilterOpen) return;
    const frame = window.requestAnimationFrame(() => {
      quickFilterInputRef.current?.focus();
      quickFilterInputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [quickFilterOpen, viewMode]);

  useEffect(() => {
    if (!fileSearchOpen) return;
    const frame = window.requestAnimationFrame(() => {
      fileSearchInputRef.current?.focus();
      fileSearchInputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [fileSearchOpen, viewMode]);

  useLayoutEffect(() => {
    if (loading) return;
    const pendingInput = pendingIncrementalSearchRef.current;
    pendingIncrementalSearchRef.current = "";
    if (!pendingInput) return;

    const root = explorerRootRef.current;
    const activeElement = document.activeElement;
    if (
      (activeElement &&
        activeElement !== document.body &&
        !root?.contains(activeElement)) ||
      isTextInput(activeElement)
    ) {
      return;
    }
    incrementalSearch.handleCharacter(pendingInput);
  }, [
    currentPath,
    filerTab,
    incrementalSearch,
    isTextInput,
    loading,
  ]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const activeElement = document.activeElement;
      if (
        activeElement &&
        activeElement !== document.body &&
        !explorerRootRef.current?.contains(activeElement)
      ) {
        return;
      }
      const key = e.key.toLowerCase();
      const primaryModifier = e.ctrlKey || e.metaKey;
      const isQuickFilterShortcut =
        primaryModifier && !e.altKey && !e.shiftKey && key === "s";
      const isFileSearchShortcut =
        (!primaryModifier &&
          !e.altKey &&
          !e.shiftKey &&
          e.key === "F3") ||
        (primaryModifier && !e.altKey && !e.shiftKey && key === "f");
      // エディタ表示中の Ctrl+S は DocumentEditor の保存へ譲る。
      if (editingFile && isQuickFilterShortcut) return;
      if (isFileSearchShortcut) {
        if (!canUseExplorerSearch || isExplorerInteractionBlocked) return;
        e.preventDefault();
        openFileSearch();
        return;
      }
      if (isQuickFilterShortcut && quickFilterOpen) {
        e.preventDefault();
        closeQuickFilter();
        return;
      }
      if (
        loading &&
        (activeElement === document.body ||
          activeElement === explorerRootRef.current) &&
        !isTextInput(e.target) &&
        !primaryModifier &&
        !e.altKey &&
        e.key.length === 1 &&
        e.key !== ":" &&
        e.key !== ";" &&
        !e.isComposing
      ) {
        e.preventDefault();
        pendingIncrementalSearchRef.current += e.key;
        return;
      }
      if (isExplorerInteractionBlocked || isTextInput(e.target)) return;
      // Files source tabs: Ctrl+← / Ctrl+→.
      // Alt+←/→ は履歴移動、修飾なし←/→はファイルフォーカス移動のまま維持する。
      if (
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.shiftKey &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight")
      ) {
        e.preventDefault();
        cycleFilerTab(e.key === "ArrowRight" ? 1 : -1);
        return;
      }
      // Ctrl+J: 直近に成立した検索条件で次の一致へ進む。1秒の入力継続タイムアウトとは
      // 独立しており、検索条件が無い場合は preventDefault せずグローバルの Ctrl+J
      // （チャット入力欄フォーカス）へ譲る。
      if (
        primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        key === "j" &&
        !isBookmarkQuickLauncherOpen() &&
        incrementalSearch.hasActiveSearch()
      ) {
        e.preventDefault();
        incrementalSearch.focusNextMatch();
        return;
      }
      if (isQuickFilterShortcut) {
        e.preventDefault();
        if (fileSearchOpen) closeFileSearch();
        setQuickFilterOpen(true);
        window.requestAnimationFrame(() => {
          quickFilterInputRef.current?.focus();
          quickFilterInputRef.current?.select();
        });
        return;
      }
      const activePathForEvent =
        activePathRef.current ??
        activePath ??
        getRenderedItemPaths()[0] ??
        null;
      const activeItemForEvent = activePathForEvent
        ? (itemByPath.get(activePathForEvent) ?? null)
        : null;

      // 表示切替（`:` サムネイル / `;` リスト）。
      // インクリメンタル検索が単一文字キーを消費する前に処理する。
      // `:` は US 配列では Shift+`;` のため shiftKey は条件に含めない。
      if (!primaryModifier && !e.altKey && !e.isComposing) {
        if (e.key === ":") {
          e.preventDefault();
          setViewMode("grid");
          return;
        }
        if (e.key === ";") {
          e.preventDefault();
          setViewMode("list");
          return;
        }
      }

      // ソート切替（F8 名前昇順 / F9 更新日時降順）。
      // Hydrus タブでは検索時に Hydrus 側でインポート日時降順に並べており、
      // フロント側の並べ替えは行わない（FileGrid/FileList 側で抑止）。
      if (!primaryModifier && !e.altKey && !e.shiftKey) {
        if (e.key === "F8") {
          e.preventDefault();
          setSort("name", "asc");
          return;
        }
        if (e.key === "F9") {
          e.preventDefault();
          setSort("date", "desc");
          return;
        }
      }

      // Backspace は削除ではなく「戻る」。Alt+← と同じ扱い。
      if (!primaryModifier && !e.altKey && !e.shiftKey && e.key === "Backspace") {
        e.preventDefault();
        goBack();
        return;
      }

      // 現在のファイラータブのホームへ戻る。
      if (
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !e.shiftKey &&
        key === "h" &&
        filerTab !== "hydrus"
      ) {
        e.preventDefault();
        homeNavigate();
        return;
      }

      if (!primaryModifier && e.altKey && !e.shiftKey) {
        if (e.key === "ArrowLeft" || e.key === "Backspace") {
          e.preventDefault();
          goBack();
          return;
        }
        if (e.key === "ArrowRight") {
          e.preventDefault();
          goForward();
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          goUp();
          return;
        }
      }

      // Undo / Redo。スタックが空の時は preventDefault せず、
      // レイアウト側の TaskCompletionUndoProvider（Ctrl+Z）へ譲る。
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "z") {
        if (e.defaultPrevented || !canUndoFiler()) return;
        e.preventDefault();
        void undo();
        return;
      }
      if (
        primaryModifier &&
        !e.altKey &&
        ((!e.shiftKey && key === "y") || (e.shiftKey && key === "z"))
      ) {
        if (e.defaultPrevented || !canRedoFiler()) return;
        e.preventDefault();
        void redo();
        return;
      }
      if (primaryModifier && key === "a") {
        e.preventDefault();
        selectAll(visibleItems.map((item) => item.path));
        return;
      }
      if (primaryModifier && key === "c") {
        e.preventDefault();
        if (capabilities.canCopy) copySelectedItems("copy");
        return;
      }
      if (primaryModifier && key === "x") {
        e.preventDefault();
        if (capabilities.canMove) copySelectedItems("cut");
        return;
      }
      if (primaryModifier && key === "v") {
        e.preventDefault();
        void pasteClipboardItems();
        return;
      }
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "i") {
        e.preventDefault();
        void archiveSelectedItems();
        return;
      }
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "u") {
        e.preventDefault();
        void extractSelectedItems();
        return;
      }
      if (primaryModifier && !e.altKey && e.shiftKey && key === "l") {
        if (canUseDownloadShortcut) {
          e.preventDefault();
          void downloadSelectedItems();
        }
        return;
      }
      if (primaryModifier && !e.altKey && !e.shiftKey && key === "p") {
        e.preventDefault();
        void copySelectedPathsText();
        return;
      }
      if (primaryModifier && !e.altKey && e.shiftKey && key === "p") {
        e.preventDefault();
        void copySelectedNamesText();
        return;
      }
      if (
        primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        !e.isComposing &&
        (e.code === "Space" || e.key === " ")
      ) {
        e.preventDefault();
        if (activePathForEvent) toggleSelect(activePathForEvent);
        return;
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey) {
        let offset = 0;
        if (e.key === "ArrowLeft") offset = -1;
        if (e.key === "ArrowRight") offset = 1;
        if (e.key === "ArrowUp") offset = -1;
        if (e.key === "ArrowDown") offset = 1;
        if (e.key === "PageUp") offset = -getVisibleItemPageSize();
        if (e.key === "PageDown") offset = getVisibleItemPageSize();
        if (offset !== 0) {
          e.preventDefault();
          focusItemByOffset(offset);
          return;
        }
        if (e.key === "Home") {
          e.preventDefault();
          focusItemByIndex(0);
          return;
        }
        if (e.key === "End") {
          e.preventDefault();
          focusItemByIndex(Number.MAX_SAFE_INTEGER);
          return;
        }
      }
      if (!primaryModifier && !e.altKey && e.shiftKey) {
        let offset = 0;
        if (e.key === "ArrowUp") offset = -1;
        if (e.key === "ArrowDown") offset = 1;
        if (e.key === "PageUp") offset = -getVisibleItemPageSize();
        if (e.key === "PageDown") offset = getVisibleItemPageSize();
        if (offset !== 0) {
          e.preventDefault();
          extendSelectionByOffset(offset);
          return;
        }
        if (e.key === "Home") {
          e.preventDefault();
          extendSelectionToIndex(0);
          return;
        }
        if (e.key === "End") {
          e.preventDefault();
          extendSelectionToIndex(Number.MAX_SAFE_INTEGER);
          return;
        }
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey && e.key === "Enter") {
        if (activeItemForEvent) {
          e.preventDefault();
          openExplorerItem(activeItemForEvent);
        }
        return;
      }
      if (!primaryModifier && !e.altKey && !e.shiftKey && e.key === "F2") {
        if (capabilities.canRename && activeItemForEvent) {
          e.preventDefault();
          setRenameTarget(activeItemForEvent);
        }
        return;
      }
      // Delete は HF / Hydrus でも有効（HF は確認ポップアップ経由）
      if (
        capabilities.canDelete &&
        selectedPaths.length > 0 &&
        !primaryModifier &&
        !e.altKey &&
        !e.shiftKey &&
        e.key === "Delete"
      ) {
        e.preventDefault();
        void deleteSelectedItems();
        return;
      }
      if (
        !primaryModifier &&
        !e.altKey &&
        e.key.length === 1 &&
        !e.isComposing
      ) {
        if (isBookmarkQuickLauncherOpen()) return;
        e.preventDefault();
        incrementalSearch.handleCharacter(e.key);
      }
    };
    // capture フェーズで登録し、レイアウト側の TaskCompletionUndoProvider（Ctrl+Z）
    // より先に評価されるようにする。スタックが空なら preventDefault せず譲る。
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [
    canUseExplorerSearch,
    canUseDownloadShortcut,
    capabilities,
    archiveSelectedItems,
    copySelectedItems,
    deleteSelectedItems,
    downloadSelectedItems,
    editingFile,
    extractSelectedItems,
    filerTab,
    redo,
    undo,
    activePath,
    focusItemByIndex,
    focusItemByOffset,
    getVisibleItemPageSize,
    getRenderedItemPaths,
    goBack,
    goForward,
    goUp,
    homeNavigate,
    incrementalSearch,
    isTextInput,
    isExplorerInteractionBlocked,
    itemByPath,
    loading,
    openExplorerItem,
    pasteClipboardItems,
    selectAll,
    selectedPaths.length,
    setSort,
    setViewMode,
    toggleSelect,
    visibleItems,
    copySelectedPathsText,
    copySelectedNamesText,
    extendSelectionByOffset,
    extendSelectionToIndex,
    closeFileSearch,
    closeQuickFilter,
    cycleFilerTab,
    fileSearchOpen,
    openFileSearch,
    quickFilterOpen,
  ]);

  const fileSearchControl = fileSearchOpen ? (
    <div
      className="flex max-w-2xl flex-col gap-1 rounded-md bg-muted/50 px-2 py-1.5"
      data-testid="filer-search-panel"
    >
      <div className="flex items-center gap-2">
        <Search className="size-4 shrink-0 text-muted-foreground" />
        <Input
          ref={fileSearchInputRef}
          value={fileSearchQuery}
          onChange={(event) => setFileSearchQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              closeFileSearch();
              return;
            }
            if (event.key === "Enter") {
              event.preventDefault();
              void runFileSearch();
            }
          }}
          placeholder="ファイル名・フォルダ名を検索..."
          aria-label="ファイル名・フォルダ名の検索"
          disabled={fileSearchLoading}
          className="h-7 min-w-40 flex-1"
        />
        <label
          htmlFor="filer-search-regex"
          className="flex shrink-0 cursor-pointer items-center gap-1 text-[11px] text-muted-foreground"
        >
          <Checkbox
            id="filer-search-regex"
            checked={fileSearchRegex}
            onCheckedChange={(checked) => setFileSearchRegex(checked === true)}
            disabled={isRemoteWorkspace}
          />
          正規表現
        </label>
        {fileSearchLoading ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            検索中...
          </span>
        ) : fileSearchActive ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {fileSearchCount}
            {fileSearchTruncated ? "+" : ""}件
          </span>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={closeFileSearch}
          aria-label="ファイル検索を閉じる"
          title="検索を閉じる (Esc)"
        >
          <X className="size-3.5" />
        </Button>
      </div>
      <div className="flex items-center gap-2 pl-6">
        <Input
          ref={fileSearchReplaceInputRef}
          value={fileSearchReplaceQuery}
          onChange={(event) => setFileSearchReplaceQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              closeFileSearch();
              return;
            }
            if (event.key === "Enter") {
              event.preventDefault();
              void replaceFileSearch();
            }
          }}
          placeholder="置換後の文字列..."
          aria-label="置換後の文字列"
          className="h-7 min-w-40 flex-1"
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => void replaceFileSearch()}
          disabled={
            fileSearchLoading ||
            !fileSearchActive ||
            !fileSearchQuery.trim() ||
            !capabilities.canRename
          }
        >
          すべて置換
        </Button>
      </div>
      {fileSearchError && (
        <span className="pl-6 text-xs text-destructive">{fileSearchError}</span>
      )}
    </div>
  ) : null;

  const quickFilterControl = quickFilterOpen ? (
    <div className="flex items-center gap-2 rounded-md bg-muted/50 px-2 py-1">
      <Search className="size-4 shrink-0 text-muted-foreground" />
      <Input
        ref={quickFilterInputRef}
        value={quickFilterQuery}
        onChange={(event) => setQuickFilterQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          event.stopPropagation();
          closeQuickFilter();
        }}
        placeholder="ファイル名で絞り込み..."
        aria-label="ファイル名の即席フィルター"
        className="h-7"
      />
      <span className="shrink-0 text-[11px] text-muted-foreground">
        {visibleItems.length}/{browseData
          ? browseData.directories.length + browseData.files.length
          : 0}
        件
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        onClick={closeQuickFilter}
        aria-label="即席フィルターを閉じる"
        title="フィルターを閉じる (Esc)"
      >
        <X className="size-3.5" />
      </Button>
    </div>
  ) : null;

  // コンテキストメニュー（アイテム）
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, item: ExplorerDirectory | ExplorerFile) => {
      e.preventDefault();
      setCtxItem(item);
      setCtxPos({ x: e.clientX, y: e.clientY });
    },
    [],
  );

  // 背景右クリック（アイテムなし）
  const handleBackgroundContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setCtxItem(null);
    setCtxPos({ x: e.clientX, y: e.clientY });
  }, []);

  // パンくず: コンテキストルート以降の相対パスのみ表示
  const breadcrumbs = useMemo(() => {
    if (!currentPath) return [];
    // HF 仮想パスは "HF|<account>|<type>|<repoId>|<subPath>" なので
    // "HF" を先頭、リポ名（表示用に簡略化）、サブパスセグメントを並べる
    if (isHfMode) {
      if (currentPath === HF_PREFIX) return ["HF"];
      const parts = currentPath.split("|");
      // parts[0]="HF", parts[1]=accountId, parts[2]=repoType, parts[3]=repoId, parts[4..]=subPath
      if (parts.length < 4) return [currentPath];
      const repoLabel = parts[3]; // "owner/name"
      const subPath = parts.slice(4).join("|");
      const crumbs = ["HF", repoLabel];
      if (subPath) crumbs.push(...subPath.split("/").filter(Boolean));
      return crumbs;
    }
    // 絶対パス閲覧時はフルパス表示
    if (isAbsoluteFilerPath) {
      return currentPath.split(/[/\\]/).filter(Boolean);
    }
    // コンテキストルートを省略して相対パスのみ
    if (contextRootPath && currentPath.startsWith(contextRootPath)) {
      const relative = currentPath
        .slice(contextRootPath.length)
        .replace(/^[/\\]/, "");
      return relative ? relative.split(/[/\\]/).filter(Boolean) : [];
    }
    return currentPath.split(/[/\\]/).filter(Boolean);
  }, [currentPath, contextRootPath, isAbsoluteFilerPath, isHfMode]);

  return (
    <div
      ref={explorerRootRef}
      className="flex h-full min-h-0 min-w-0 flex-row"
      data-shell-workspace="files"
      data-shell-region="files-canvas"
      tabIndex={-1}
      onKeyDownCapture={handleEditorKeyDownCapture}
      onBlur={handleExplorerBlur}
    >
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* エディタが開いている場合はスプリットビュー、それ以外はメインファイラー */}
      {recordTableFile?.project_id && recordTableFile.record_table_id ? (
        <RecordTableEditor
          projectId={recordTableFile.project_id}
          tableId={recordTableFile.record_table_id}
          initialName={recordTableFile.name.replace(/\.dbtable$/i, "")}
          onClose={() => setRecordTableFile(null)}
          onChanged={() => {
            refresh();
          }}
        />
      ) : editingFile ? (
        <EditorPane
          editingFile={editingFile}
          closeEditor={closeEditor}
          handleFileClick={handleFileClick}
          onContextMenu={handleContextMenu}
          onBackgroundContextMenu={handleBackgroundContextMenu}
        />
      ) : (
        <UploadZone onContextMenu={handleBackgroundContextMenu}>
          <div
            ref={explorerScrollRef}
            className="flex h-full min-h-0 flex-col overflow-auto bg-background"
          >
            {/* ヘッダー */}
            <div className="flex min-h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-card/40 px-6">
              <div className="flex min-w-0 items-center gap-3">
                <h1 className="text-[16px] font-semibold">Files</h1>
                {isRemoteWorkspace && <span className="rounded border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[10px] font-medium text-amber-300">Read-only</span>}
              </div>
              {filerTab !== "hydrus" ? (
                <ExplorerToolbar
                  onNewFolder={() => setNewFolderOpen(true)}
                  onAddHfReference={() => setHfReferenceOpen(true)}
                />
              ) : null}
            </div>

            {/* Files tabs: project files / user files / HF / Hydrus */}
            <div className="flex shrink-0 items-center gap-5 border-b border-border px-6 pt-1">
              <Button
                variant={
                  filerTab === "workspace" && !isAbsoluteFilerPath
                    ? "default"
                    : "outline"
                }
                size="sm"
                className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", filerTab === "workspace" && !isAbsoluteFilerPath && "border-primary text-primary")}
                onClick={() => setFilerTab("workspace")}
              >
                {FILER_TAB_LABELS.workspace}
              </Button>
              <Button
                variant={
                  filerTab === "user" && !isAbsoluteFilerPath
                    ? "default"
                    : "outline"
                }
                size="sm"
                className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", filerTab === "user" && !isAbsoluteFilerPath && "border-primary text-primary")}
                onClick={() => setFilerTab("user")}
              >
                {FILER_TAB_LABELS.user}
              </Button>
              <Button
                variant={filerTab === "hf" ? "default" : "outline"}
                size="sm"
                className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", filerTab === "hf" && "border-primary text-primary")}
                onClick={() => setFilerTab("hf")}
              >
                {FILER_TAB_LABELS.hf}
              </Button>
              <Button
                variant={filerTab === "hydrus" ? "default" : "outline"}
                size="sm"
                className={cn("h-9 rounded-none border-0 border-b-2 border-transparent bg-transparent px-0 text-xs text-muted-foreground hover:bg-transparent hover:text-foreground", filerTab === "hydrus" && "border-primary text-primary")}
                onClick={() => setFilerTab("hydrus")}
              >
                {FILER_TAB_LABELS.hydrus}
              </Button>
              {selectedPaths.length > 0 && (
                <span
                  className="ml-auto shrink-0 pb-1 text-xs font-medium text-primary"
                  aria-live="polite"
                >
                  {selectedPaths.length} item
                  {selectedPaths.length === 1 ? "" : "s"} selected
                </span>
              )}
            </div>

            {/* Hydrus 検索バー（カスタムUIはこれだけ。結果は context の browseData に流す） */}
            {filerTab === "hydrus" && (
              <HydrusSearchBar
                onPagingChange={(controller) => {
                  hydrusPagingRef.current = controller;
                }}
                onResults={(data) => {
                  setBrowseData(data);
                  setHydrusError(null);
                }}
                onError={(msg) => setHydrusError(msg)}
              />
            )}
            {filerTab === "hydrus" && hydrusError && (
              <div className="py-2 text-center text-xs text-destructive">
                {hydrusError}
              </div>
            )}

            {/* HF タグ/作者フィルタ（creator_mapping.json があるリポのみ表示） */}
            {filerTab === "hf" && isHfMode && hfCreatorMapping && (
              <div className="mx-6 flex items-center gap-2 rounded-md border border-border bg-card/40 p-2">
                <Input
                  value={hfSearchQuery}
                  onChange={(e) => setHfSearchQuery(e.target.value)}
                  placeholder="作者名・フォルダ名・タグで検索..."
                  className="h-8"
                />
                {hfSearchQuery && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setHfSearchQuery("")}
                  >
                    クリア
                  </Button>
                )}
              </div>
            )}

            {/* ナビゲーション（ホーム・上位移動・パンくず） */}
            {filerTab !== "hydrus" && (
              <div className="flex shrink-0 items-center gap-2 border-b border-border px-6 py-1 text-sm">
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={homeNavigate}
                  title="ホーム"
                  aria-label="ホーム"
                >
                  <Home className="size-3" />
                </Button>
                {!loading &&
                  browseData?.can_go_up &&
                  browseData.parent_path !== null && (
                    <button
                      className="flex h-6 shrink-0 items-center gap-1 rounded px-2 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      onClick={goUp}
                    >
                      <ArrowUp className="size-3.5" />
                      <span>上のフォルダへ</span>
                    </button>
                  )}
                <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto whitespace-nowrap">
                  {breadcrumbs.slice(-4).map((segment, i, arr) => {
                    const fullIndex = breadcrumbs.length - arr.length + i;
                    let partialPath: string;
                    if (isHfMode) {
                      // HF: breadcrumbs[0]="HF", [1]="owner/name", [2..]=subPath
                      if (fullIndex === 0) {
                        partialPath = HF_PREFIX;
                      } else {
                        const parts = currentPath.split("|");
                        // リポジトリルートまで
                        const repoBase = parts.slice(0, 4).join("|");
                        if (fullIndex === 1) {
                          partialPath = repoBase;
                        } else {
                          const subCrumbs = breadcrumbs.slice(2, fullIndex + 1);
                          partialPath = repoBase + "|" + subCrumbs.join("/");
                        }
                      }
                    } else if (isAbsoluteFilerPath) {
                      const allSegments = currentPath
                        .split(/[/\\]/)
                        .filter(Boolean);
                      partialPath = allSegments.slice(0, fullIndex + 1).join("/");
                    } else if (contextRootPath) {
                      partialPath =
                        contextRootPath +
                        "/" +
                        breadcrumbs.slice(0, fullIndex + 1).join("/");
                    } else {
                      partialPath = breadcrumbs.slice(0, fullIndex + 1).join("/");
                    }
                    const isLast = fullIndex === breadcrumbs.length - 1;
                    return (
                      <span key={fullIndex} className="flex shrink-0 items-center gap-1">
                        <ChevronRight className="size-3 text-muted-foreground" />
                        {isLast ? (
                          <span className="text-xs font-medium text-foreground">{segment}</span>
                        ) : (
                          <Button
                            variant="ghost"
                            size="xs"
                            onClick={() => navigate(partialPath)}
                          >
                            {segment}
                          </Button>
                        )}
                      </span>
                    );
                  })}
                </div>
              </div>
            )}

            {/* ローディング */}
            {filerTab !== "hydrus" && loading && (
              <div className="grid grid-cols-2 gap-3 p-6 sm:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
                {Array.from({ length: 12 }).map((_, i) => (
                  <Skeleton key={i} className="h-32 rounded-md bg-muted/40" />
                ))}
              </div>
            )}

            {/* エラー */}
            {error && !loading && (
                <div className="mx-6 my-4 rounded border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* ファイル一覧: HF/Workspace/User/Hydrus すべて同じ FileGrid/FileList で描画 */}
            {!loading &&
              browseData &&
              (viewMode === "grid" ? (
                <FileGrid
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                  directories={visibleDirectories}
                  files={visibleFiles}
                  headerAddon={fileSearchControl ?? quickFilterControl}
                />
              ) : (
                <FileList
                  onFileClick={handleFileClick}
                  onContextMenu={handleContextMenu}
                  directories={visibleDirectories}
                  files={visibleFiles}
                  headerAddon={fileSearchControl ?? quickFilterControl}
                />
              ))}

            {/* 空フォルダ */}
            {(filerTab !== "hydrus" ||
              (quickFilterOpen && !!normalizedQuickFilter)) &&
              !loading &&
              browseData &&
              visibleDirectories.length === 0 &&
              visibleFiles.length === 0 && (
                <div className="mx-6 my-8 flex max-w-lg flex-col items-center overflow-hidden rounded-md border border-border bg-card/40 text-center text-sm text-muted-foreground">
                  <img
                    src="/images/ui/empty-files.png"
                    alt=""
                    className="aspect-[16/7] w-full object-cover"
                  />
                  <div className="px-6 py-5">
                    {quickFilterOpen && normalizedQuickFilter
                      ? "一致する項目はありません。"
                      : fileSearchActive
                      ? "検索結果はありません。"
                      : isHfMode
                        ? "HFリポジトリは空です。"
                        : "このフォルダは空です。ファイルをドラッグ&ドロップでアップロードできます。"}
                  </div>
                </div>
              )}
          </div>
        </UploadZone>
      )}
      </div>

      {/* コンテキストメニュー・ダイアログ: ファイラー・エディタ画面共通 */}
      <FileContextMenu
        item={ctxItem}
        position={ctxPos}
        onClose={() => {
          setCtxItem(null);
          setCtxPos(null);
        }}
        onRename={(item) => {
          setRenameTarget(item);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onProperties={(item) => {
          if ("type" in item) setPreviewFile(item as ExplorerFile);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onOpen={(item) => {
          handleFileClick(item as ExplorerFile);
          setCtxItem(null);
          setCtxPos(null);
        }}
        onNewFolder={() => setNewFolderOpen(true)}
        onNewTextFile={() => setNewTextFileOpen(true)}
        activeSelectionPaths={selectedPaths}
      />
      <NewFolderDialog open={newFolderOpen} onOpenChange={setNewFolderOpen} />
      <NewTextFileDialog
        open={newTextFileOpen}
        onOpenChange={setNewTextFileOpen}
        onCreated={(path, name) => {
          openEditorFromFiler({ path, name });
        }}
      />
      <RenameDialog
        item={renameTarget}
        open={renameTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
      />
      {viewerFile && browseData && (
        <FileViewer
          file={viewerFile}
          files={browseData.files}
          onClose={() => setViewerFile(null)}
          onNavigate={(f) => setViewerFile(f)}
          onBoundaryNavigate={isHydrusMode ? loadHydrusBoundary : undefined}
          onAdjacentFiles={isHydrusMode ? preloadHydrusAdjacent : undefined}
          returnFocusRef={viewerReturnFocusRef}
        />
      )}
      {previewFile && (
        <FilePreviewPanel
          file={previewFile}
          userId={userId}
          onClose={() => setPreviewFile(null)}
          onOpenWorkspace={(file) => {
            openEditorFromFiler(file);
          }}
        />
      )}
      <HfReferenceDialog
        open={hfReferenceOpen}
        onOpenChange={setHfReferenceOpen}
        onAdded={(path) => {
          if (path) navigate(path);
          else navigate(HF_PREFIX);
        }}
      />
    </div>
  );
}

// ─── ページエントリ ───
function FilerPageInner() {
  return (
    <ExplorerProvider>
      <FilesWorkspaceShellRegistration />
      <ExplorerContent />
    </ExplorerProvider>
  );
}

function FilesWorkspaceShellRegistration() {
  useWorkspaceShellRegistration({
    id: "files-workspace",
    workspaceNavigation: (
      <div
        className="h-full min-h-0 w-full"
        data-shell-slot="workspace-navigation"
        data-workspace="files"
      >
        <FilesBookmarkLauncherSidebar />
      </div>
    ),
    priority: 10,
  });
  return null;
}

export default function FilerPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center">
          <div className="text-sm text-muted-foreground">読み込み中...</div>
        </div>
      }
    >
      <FilerPageInner />
    </Suspense>
  );
}
