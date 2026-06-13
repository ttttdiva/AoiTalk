"use client";

/* eslint-disable @next/next/no-img-element */

import { useState, useEffect } from "react";
import {
  explorerPreview,
  explorerInfo,
  explorerDownloadUrl,
  type ExplorerFile,
  type FilePreview,
  type FileInfo,
} from "@/lib/explorer-api";
import { Button } from "@/components/ui/button";
import { X, Download, FileIcon } from "lucide-react";
import { hfServeUrl, hfTextUrl, isHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath } from "@/lib/hydrus/virtual-path";
import { getFileServeUrl } from "@/lib/explorer-serve-url";

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
]);

function getExt(name: string): string {
  return name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
}

async function loadHfPreview(file: ExplorerFile): Promise<FilePreview | null> {
  const ext = getExt(file.name);
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

interface FilePreviewPanelProps {
  file: ExplorerFile | null;
  onClose: () => void;
}

export function FilePreviewPanel({ file, onClose }: FilePreviewPanelProps) {
  const [previewState, setPreviewState] = useState<{
    path: string | null;
    preview: FilePreview | null;
    info: FileInfo | null;
  }>({ path: null, preview: null, info: null });

  useEffect(() => {
    if (!file) return;

    let cancelled = false;

    // HF / Hydrus は explorer API を使わず専用ルートで解決
    if (isHfPath(file.path)) {
      loadHfPreview(file).then((preview) => {
        if (cancelled) return;
        setPreviewState({ path: file.path, preview, info: null });
      });
      return () => {
        cancelled = true;
      };
    }
    if (isHydrusPath(file.path)) {
      const preview = loadHydrusPreview(file);
      Promise.resolve().then(() => {
        if (cancelled) return;
        setPreviewState({ path: file.path, preview, info: null });
      });
      return () => {
        cancelled = true;
      };
    }

    Promise.all([explorerPreview(file.path), explorerInfo(file.path)])
      .then(([preview, info]) => {
        if (cancelled) return;
        setPreviewState({ path: file.path, preview, info });
      })
      .catch(() => {
        if (cancelled) return;
        setPreviewState({ path: file.path, preview: null, info: null });
      });

    return () => {
      cancelled = true;
    };
  }, [file]);

  if (!file) return null;

  const loading = previewState.path !== file.path;
  const preview = loading ? null : previewState.preview;
  const info = loading ? null : previewState.info;

  const handleDownload = async () => {
    if (isHfPath(file.path)) {
      const url = hfServeUrl(file.path);
      if (url) window.open(url, "_blank");
      return;
    }
    if (isHydrusPath(file.path)) {
      window.open(getFileServeUrl(file.path), "_blank");
      return;
    }
    const url = await explorerDownloadUrl(file.path);
    window.open(url, "_blank");
  };

  return (
    <div className="flex h-full w-72 flex-col border-l bg-background">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="truncate text-xs font-medium">{file.name}</span>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={handleDownload}
            title="Download"
          >
            <Download className="size-3" />
          </Button>
          <Button variant="ghost" size="icon-xs" onClick={onClose}>
            <X className="size-3" />
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-3">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : preview?.type === "image" && preview.data_url ? (
          <img
            src={preview.data_url}
            alt={file.name}
            className="w-full rounded-md"
          />
        ) : preview?.type === "text" && preview.content ? (
          <pre className="whitespace-pre-wrap break-all rounded-md bg-muted p-2 text-[11px]">
            {preview.content}
            {preview.truncated && (
              <span className="text-muted-foreground">
                {"\n"}...truncated...
              </span>
            )}
          </pre>
        ) : preview?.type === "office" && preview.content ? (
          <div className="space-y-2">
            <span className="text-[10px] text-muted-foreground">
              Extracted text
            </span>
            <pre className="whitespace-pre-wrap break-all rounded-md bg-muted p-2 text-[11px]">
              {preview.content}
            </pre>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 py-8 text-muted-foreground">
            <FileIcon className="size-10" />
            <span className="text-xs">Preview unavailable</span>
          </div>
        )}
      </div>

      {info && (
        <div className="border-t p-3 text-[11px] text-muted-foreground">
          <div className="space-y-1">
            <div className="flex justify-between">
              <span>Size</span>
              <span>{formatSize(info.size_bytes)}</span>
            </div>
            <div className="flex justify-between">
              <span>Created</span>
              <span>
                {new Date(info.created_at).toLocaleDateString("ja-JP")}
              </span>
            </div>
            <div className="flex justify-between">
              <span>Modified</span>
              <span>
                {new Date(info.modified_at).toLocaleDateString("ja-JP")}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
