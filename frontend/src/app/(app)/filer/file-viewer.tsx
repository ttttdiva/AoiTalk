"use client";
/* eslint-disable @next/next/no-img-element */
import type * as React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, FileIcon, Info, Play, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { explorerInfo, type ExplorerFile } from "@/lib/explorer-api";
import { getFileServeUrl, getImageThumbnailUrl } from "@/lib/explorer-serve-url";
import { adjacentViewerFile, preloadViewerFiles, viewerFiles } from "@/lib/viewer-navigation";
import { cn } from "@/lib/utils";

const getFilerFileUrl = getFileServeUrl;
export function isImage(type: string) {
  return type.startsWith("image");
}
export function isVideo(type: string) {
  return type.startsWith("video");
}
export function FileViewer({
  file,
  files,
  onClose,
  onNavigate,
  returnFocusRef,
}: {
  file: ExplorerFile;
  files: ExplorerFile[];
  onClose: () => void;
  onNavigate: (file: ExplorerFile) => void;
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const mediaSurfaceRef = useRef<HTMLDivElement>(null);
  const initialFocusRef = useRef<HTMLButtonElement>(null);
  const touchStartRef = useRef<{ x: number; y: number; t: number } | null>(
    null,
  );
  const viewableFiles = useMemo(() => viewerFiles(files), [files]);
  const currentIndex = viewableFiles.findIndex((f) => f.path === file.path);
  const isImageFile = isImage(file.type || "");
  // Keep a synchronous path cursor in addition to React state.  Key events
  // can arrive several times before the parent re-renders `file`; using the
  // rendered index in that window would navigate to the same item repeatedly.
  const navigationPathRef = useRef(file.path);
  const preloadedImagesRef = useRef(new Map<string, HTMLImageElement>());

  const handleClose = useCallback(() => {
    onClose();
    window.setTimeout(() => {
      returnFocusRef?.current?.focus({ preventScroll: true });
    }, 0);
  }, [onClose, returnFocusRef]);

  useEffect(() => {
    navigationPathRef.current = file.path;
  }, [file.path]);

  const navigateTo = useCallback(
    (target: ExplorerFile) => {
      navigationPathRef.current = target.path;
      onNavigate(target);
    },
    [onNavigate],
  );

  const goPrev = useCallback(() => {
    const cursorPath = navigationPathRef.current;
    const adjacent = adjacentViewerFile(files, cursorPath, -1);
    if (adjacent) {
      navigateTo(adjacent);
    }
  }, [files, navigateTo]);

  const goNext = useCallback(() => {
    const cursorPath = navigationPathRef.current;
    const adjacent = adjacentViewerFile(files, cursorPath, 1);
    if (adjacent) {
      navigateTo(adjacent);
    }
  }, [files, navigateTo]);

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
    return () => {
      cancelled = true;
    };
  }, [currentIndex, file.path, files]);

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

  const toggleFullscreen = useCallback(async () => {
    try {
      if (document.fullscreenElement) {
        if (typeof document.exitFullscreen === "function") {
          await document.exitFullscreen();
        }
        return;
      }
      // Video keeps its native fullscreen target.  For images, fullscreen a
      // media surface that also owns the viewer's sizing/padding rather than
      // the bare <img>, so controls and the visual framing remain natural.
      const target =
        videoRef.current ?? (isImageFile ? mediaSurfaceRef.current : null);
      if (target && typeof target.requestFullscreen === "function") {
        await target.requestFullscreen();
      }
    } catch {
      // Fullscreen can be rejected by browser policy (or by a test/runtime
      // double).  It is an optional enhancement and must never surface an
      // unhandled promise rejection from the keyboard handler.
    }
  }, [isImageFile]);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      // video要素自体がフォーカスされている場合はネイティブ処理に任せる（二重発火防止）
      if (e.target === videoRef.current) return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        e.stopPropagation();
        void goPrev();
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        e.stopPropagation();
        void goNext();
        return;
      }
      const video = videoRef.current;
      if (e.key === "f" || e.key === "F") {
        e.preventDefault();
        void toggleFullscreen();
        return;
      }
      if (!video) return;
      if (e.key === " " || e.key === "k" || e.key === "K") {
        e.preventDefault();
        if (video.paused) {
          void video.play().catch(() => undefined);
        } else {
          video.pause();
        }
      }
      if (e.key === "j" || e.key === "J") video.currentTime -= 10;
      if (e.key === "l" || e.key === "L") video.currentTime += 10;
      if (e.key === "m" || e.key === "M") video.muted = !video.muted;
    };
    // Capture before Base UI's DialogPopup, which consumes arrow keys while
    // focus is inside the modal and otherwise prevents viewer navigation.
    window.addEventListener("keydown", handleKey, true);
    return () => window.removeEventListener("keydown", handleKey, true);
  }, [goPrev, goNext, toggleFullscreen]);

  const [fileInfo, setFileInfo] = useState<{
    size_bytes?: number;
    created_at?: string;
    modified_at?: string;
  } | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- file.path 切替時の即時リセット
    setFileInfo(null);
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
    // eslint-disable-next-line react-hooks/set-state-in-effect -- file.path 切替時の即時リセット
    setImageError(null);
  }, [file.path]);

  const handleOverlayClick = (event: React.MouseEvent) => {
    if (event.target === event.currentTarget) handleClose();
  };

  const extLabel = file.extension?.replace(/^\./, "").toUpperCase() || "MEDIA";
  const metadataRows = [
    ["Size", fileInfo?.size_bytes ?? file.size],
    ["Created", fileInfo?.created_at],
    ["Modified", fileInfo?.modified_at ?? file.modified_at],
    ["Path", file.path],
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
      <div
        ref={mediaSurfaceRef}
        data-files-viewer-media-surface="true"
        className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-black"
      >
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
                  <button key={item.path} type="button" className={cn("relative size-16 shrink-0 overflow-hidden rounded-md border border-border bg-muted/40 opacity-60 transition-opacity hover:opacity-100", active && "border-2 border-primary opacity-100 ring-2 ring-primary/20")} onClick={() => navigateTo(item)} title={item.name}>
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

