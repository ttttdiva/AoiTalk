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
import { formatExplorerDateTime } from "@/lib/explorer-format";
import { getFileExt } from "@/lib/utils";
import { toast } from "sonner";

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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
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

  useEffect(() => {
    if (!file) return;

    let cancelled = false;
    const identity = `${userId ?? "anonymous"}:${file.path}`;

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

  const handleDownload = async () => {
    try {
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
    onOpenWorkspace && TEXT_EXTS.has(normalizedExt),
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
              <dd className="break-words">{info ? formatSize(info.size_bytes) : typeof file.size === "number" ? formatSize(file.size) : "-"}</dd>
            </div>
            <div className="grid grid-cols-[5.5rem,1fr] gap-2">
              <dt className="text-muted-foreground">Modified</dt>
              <dd className="break-words">{info ? formatExplorerDateTime(info.modified_at) : formatExplorerDateTime(file.modified_at)}</dd>
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
