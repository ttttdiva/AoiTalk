"use client";

/* eslint-disable @next/next/no-img-element */

import { useState, useEffect } from "react";
import {
  explorerPreview,
  explorerInfo,
  explorerDownloadUrl,
  explorerDownloadResource,
  explorerErrorMessage,
  type ExplorerFile,
  type FilePreview,
  type FileInfo,
} from "@/lib/explorer-api";
import { Button } from "@/components/ui/button";
import { X, Download, FileIcon, Info, ExternalLink, Pencil } from "lucide-react";
import { hfServeUrl, hfTextUrl, isHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath, parseHydrusFileId } from "@/lib/hydrus/virtual-path";
import {
  hydrusGetMetadata,
  type HydrusFileMetadata,
} from "@/lib/hf-api";
import { getFileServeUrl } from "@/lib/explorer-serve-url";
import { formatExplorerDateTime } from "@/lib/explorer-format";
import { getFileExt } from "@/lib/utils";
import { toast } from "sonner";

const IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"]);
const TEXT_EXTS = new Set([
  "txt",
  "md",
  "json",
  "yaml",
  "yml",
  "csv",
  "py",
  "js",
  "ts",
  "tsx",
  "jsx",
  "html",
  "css",
  "xml",
  "log",
  "ini",
  "cfg",
  "sql",
  "bat",
  "cmd",
  "sh",
  "ps1",
  "vbs",
]);

async function loadHfPreview(file: ExplorerFile): Promise<FilePreview | null> {
  const ext = getFileExt(file.name);
  if (IMAGE_EXTS.has(ext)) {
    const url = hfServeUrl(file.path);
    return url ? { success: true, type: "image", data_url: url } : null;
  }
  if (TEXT_EXTS.has(ext)) {
    const url = hfTextUrl(file.path);
    if (!url) return null;
    try {
      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) return null;
      const j = await res.json();
      return {
        success: true,
        type: "text",
        content: j.text ?? "",
        truncated: !!j.truncated,
      };
    } catch {
      return null;
    }
  }
  return null;
}

function loadHydrusPreview(file: ExplorerFile): FilePreview | null {
  const mime = file.type || "";
  if (mime.startsWith("image/")) {
    return {
      success: true,
      type: "image",
      data_url: getFileServeUrl(file.path),
    };
  }
  return null;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatDuration(seconds?: number): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return null;
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const rest = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function hydrusRows(
  file: ExplorerFile,
  metadata: HydrusFileMetadata | null,
): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  const size = metadata?.size ?? file.size;
  if (typeof size === "number") rows.push(["Size", formatSize(size)]);
  const mime = metadata?.mime ?? file.type;
  if (mime) rows.push(["MIME", mime]);
  const width = metadata?.width;
  const height = metadata?.height;
  if (typeof width === "number" && typeof height === "number") {
    rows.push(["Resolution", `${width} × ${height}`]);
  }
  const duration = formatDuration(metadata?.duration);
  if (duration) rows.push(["Duration", duration]);
  const modified = metadata?.time_modified
    ? new Date(metadata.time_modified * 1000).toISOString()
    : file.modified_at;
  if (modified) rows.push(["Modified", formatExplorerDateTime(modified)]);
  if (typeof metadata?.file_id === "number") rows.push(["Hydrus File ID", String(metadata.file_id)]);
  const hash = typeof metadata?.hash === "string" ? metadata.hash : "";
  if (hash) rows.push(["Hash", hash]);
  if (typeof metadata?.num_frames === "number") rows.push(["Frames", String(metadata.num_frames)]);
  if (metadata?.has_audio != null) rows.push(["Audio", metadata.has_audio ? "あり" : "なし"]);
  if (metadata?.is_inbox != null) rows.push(["Inbox", metadata.is_inbox ? "あり" : "なし"]);
  if (metadata?.is_local != null) rows.push(["Local", metadata.is_local ? "あり" : "なし"]);
  if (metadata?.is_trashed != null) rows.push(["Trash", metadata.is_trashed ? "あり" : "なし"]);
  return rows;
}

interface FilePreviewPanelProps {
  file: ExplorerFile | null;
  onClose: () => void;
  /** Authenticated principal used to gate metadata from a previous session. */
  userId?: string | null;
  /** Open a text-like file in the canonical workspace editor. */
  onOpenWorkspace?: (file: ExplorerFile) => void;
}

export function FilePreviewPanel({
  file,
  onClose,
  userId = null,
  onOpenWorkspace,
}: FilePreviewPanelProps) {
  const [previewState, setPreviewState] = useState<{
    path: string | null;
    identity: string | null;
    preview: FilePreview | null;
    info: FileInfo | null;
  }>({ path: null, identity: null, preview: null, info: null });
  const [hydrusMetadata, setHydrusMetadata] = useState<HydrusFileMetadata | null>(null);
  const [hydrusMetadataKey, setHydrusMetadataKey] = useState<string | null>(null);

  useEffect(() => {
    if (!file) return;

    let cancelled = false;
    const identity = `${userId ?? "anonymous"}:${file.path}`;

    // HF / Hydrus は explorer API を使わず専用ルートで解決
    if (isHfPath(file.path)) {
      // Clear metadata from a previous Hydrus selection before loading HF.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setHydrusMetadata(null);
      setHydrusMetadataKey(null);
      loadHfPreview(file).then((preview) => {
        if (cancelled) return;
        setPreviewState({ path: file.path, identity, preview, info: null });
      });
      return () => {
        cancelled = true;
      };
    }
    if (isHydrusPath(file.path)) {
      const preview = loadHydrusPreview(file);
      const fileId = parseHydrusFileId(file.path);
      // Set the preview path immediately so metadata latency does not hide the
      // fallback ExplorerFile properties or the image preview.
      setHydrusMetadata(null);
      setHydrusMetadataKey(null);
      setPreviewState({ path: file.path, identity, preview, info: null });
      if (fileId == null) {
        return () => {
          cancelled = true;
        };
      }
      hydrusGetMetadata([fileId], false)
        .then((result) => {
          if (cancelled) return;
          const metadata = result.metadata?.[0] ?? null;
          // Hydrus returns an array even for one id.  Do not attach a row for
          // another file (or a response from a previous principal/path).
          if (metadata?.file_id !== fileId) {
            setHydrusMetadata(null);
            return;
          }
          setHydrusMetadata(metadata);
          setHydrusMetadataKey(identity);
        })
        .catch(() => {
          if (!cancelled) {
            setHydrusMetadata(null);
            setHydrusMetadataKey(null);
          }
        });
      return () => {
        cancelled = true;
      };
    }

    Promise.all([explorerPreview(file.path), explorerInfo(file.path)])
      .then(([preview, info]) => {
        if (cancelled) return;
        setPreviewState({ path: file.path, identity, preview, info });
      })
      .catch(() => {
        if (cancelled) return;
        setPreviewState({ path: file.path, identity, preview: null, info: null });
      });

    return () => {
      cancelled = true;
    };
  }, [file, userId]);

  if (!file) return null;

  const identity = `${userId ?? "anonymous"}:${file.path}`;
  const loading = previewState.path !== file.path || previewState.identity !== identity;
  const preview = loading ? null : previewState.preview;
  const info = loading ? null : previewState.info;
  const metadataForFile =
    !loading && hydrusMetadataKey === `${userId ?? "anonymous"}:${file.path}`
      ? hydrusMetadata
      : null;
  const hydrusProperties = isHydrusPath(file.path)
    ? hydrusRows(file, metadataForFile)
    : [];

  const handleDownload = async () => {
    try {
      if (isHfPath(file.path)) {
        const url = hfServeUrl(file.path);
        if (!url) throw new Error("ダウンロードURLを解決できませんでした");
        await explorerDownloadResource(url, file.name);
        return;
      }
      if (isHydrusPath(file.path)) {
        const url = getFileServeUrl(file.path);
        if (!url) throw new Error("ダウンロードURLを解決できませんでした");
        await explorerDownloadResource(url, file.name);
        return;
      }
      await explorerDownloadResource(
        explorerDownloadUrl(file.path),
        file.name,
      );
    } catch (error) {
      toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
    }
  };

  // Keep the extension in the same dotless form as TEXT_EXTS.  Explorer
  // responses are mixed (`".md"` vs `"md"`), so normalise both forms before
  // deciding whether the canonical workspace editor can open this file.
  const normalizedExt = (file.extension || getFileExt(file.name)).replace(/^\./, "").toLowerCase();
  const canOpenWorkspace = Boolean(
    onOpenWorkspace &&
      !isHydrusPath(file.path) &&
      !isHfPath(file.path) &&
      TEXT_EXTS.has(normalizedExt),
  );
  const fileKind = file.extension?.replace(/^\./, "").toUpperCase() || "FILE";

  return (
    <aside className="flex h-full w-[300px] shrink-0 flex-col border-l border-border bg-card max-md:absolute max-md:inset-y-0 max-md:right-0 max-md:z-30 max-md:shadow-xl">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex min-w-0 items-center gap-2">
          <Info className="size-4 shrink-0 text-muted-foreground" />
          <span className="text-[16px] font-semibold">Properties</span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-muted-foreground hover:bg-muted"
            onClick={handleDownload}
            title="ダウンロード"
            aria-label="ダウンロード"
          >
            <Download className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-muted-foreground hover:bg-muted"
            onClick={onClose}
            aria-label="閉じる"
          >
            <X className="size-4" />
          </Button>
        </div>
      </div>

      <div className="flex-1 space-y-6 overflow-auto p-4">
        <section className="flex flex-col items-center gap-2 text-center">
          <div className="flex size-16 items-center justify-center rounded-md border border-border bg-muted/30 text-primary">
            <FileIcon className="size-9" />
          </div>
          <h3 className="max-w-full break-all text-[16px] font-semibold leading-5">{file.name}</h3>
          <p className="text-xs text-muted-foreground">{fileKind} File</p>
        </section>

        <div className="h-px bg-border" />
        <section className="space-y-3">
          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">General</h4>
          <dl className="space-y-3 text-[13px]">
            <div className="grid grid-cols-[5.5rem,1fr] gap-2">
              <dt className="text-muted-foreground">Size</dt>
              <dd className="break-words">{isHydrusPath(file.path) ? hydrusProperties.find(([label]) => label === "Size")?.[1] ?? "-" : info ? formatSize(info.size_bytes) : typeof file.size === "number" ? formatSize(file.size) : "-"}</dd>
            </div>
            <div className="grid grid-cols-[5.5rem,1fr] gap-2">
              <dt className="text-muted-foreground">Modified</dt>
              <dd className="break-words">{isHydrusPath(file.path) ? hydrusProperties.find(([label]) => label === "Modified")?.[1] ?? "-" : info ? formatExplorerDateTime(info.modified_at) : formatExplorerDateTime(file.modified_at)}</dd>
            </div>
            {info?.created_at ? (
              <div className="grid grid-cols-[5.5rem,1fr] gap-2">
                <dt className="text-muted-foreground">Created</dt>
                <dd className="break-words">{formatExplorerDateTime(info.created_at)}</dd>
              </div>
            ) : null}
            <div className="grid grid-cols-[5.5rem,1fr] gap-2">
              <dt className="text-muted-foreground">Path</dt>
              <dd className="break-all font-mono text-[11px] text-muted-foreground">{file.path}</dd>
            </div>
          </dl>
        </section>

        <div className="h-px bg-border" />
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Preview</h4>
            {preview?.type === "text" && canOpenWorkspace ? (
              <button
                type="button"
                className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:text-primary/80"
                onClick={() => onOpenWorkspace?.(file)}
              >
                <ExternalLink className="size-3" /> Open
              </button>
            ) : null}
          </div>
        {loading ? (
          <div className="flex items-center justify-center rounded-md border border-border bg-muted/20 py-10">
            <div className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : preview?.type === "image" && preview.data_url ? (
          <img
            src={preview.data_url}
            alt={file.name}
            className="max-h-64 w-full rounded-md border border-border bg-black/20 object-contain"
          />
        ) : preview?.type === "text" && preview.content ? (
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-muted/40 p-3 font-mono text-[11px] leading-5 text-muted-foreground">
            {preview.content}
            {preview.truncated && (
              <span className="text-muted-foreground">
                {"\n"}...truncated...
              </span>
            )}
          </pre>
        ) : preview?.type === "office" && preview.content ? (
          <div className="space-y-2 rounded-md border border-border bg-muted/40 p-3">
            <span className="text-[10px] text-muted-foreground">
              Extracted text
            </span>
            <pre className="whitespace-pre-wrap break-all text-[11px]">
              {preview.content}
            </pre>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 rounded-md border border-border bg-muted/20 py-10 text-muted-foreground">
            <FileIcon className="size-10" />
            <span className="text-xs">Preview unavailable</span>
          </div>
        )}
        </section>
        {isHydrusPath(file.path) && hydrusProperties.length > 0 ? (
          <section className="space-y-2">
            <h4 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Hydrus</h4>
            <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3 text-xs">
              {hydrusProperties.filter(([label]) => !["Size", "Modified"].includes(label)).map(([label, value]) => (
                <div key={label} className="flex items-start justify-between gap-2">
                  <span className="text-muted-foreground">{label}</span>
                  <span className="max-w-[10rem] break-all text-right">{value}</span>
                </div>
              ))}
            </div>
          </section>
        ) : null}
      </div>

      {canOpenWorkspace ? (
        <div className="border-t border-border bg-card p-4">
          <Button
            type="button"
            variant="outline"
            className="h-9 w-full gap-2 border-primary/40 bg-primary/10 text-primary hover:bg-primary/20"
            onClick={() => onOpenWorkspace?.(file)}
          >
            <Pencil className="size-4" />
            Open in Files
          </Button>
        </div>
      ) : null}
    </aside>
  );
}
