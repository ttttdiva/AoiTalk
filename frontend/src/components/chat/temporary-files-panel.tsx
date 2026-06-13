"use client";

import { useCallback, useRef, type ChangeEvent } from "react";
import {
  FileText,
  ImageIcon,
  Paperclip,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type TemporaryFilesPanelProps = {
  files: File[];
  disabled?: boolean;
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveFile: (index: number) => void;
  onClearFiles: () => void;
  className?: string;
};

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isImageFile(file: File) {
  return file.type.startsWith("image/");
}

export function TemporaryFilesPanel({
  files,
  disabled = false,
  onAddFiles,
  onRemoveFile,
  onClearFiles,
  className,
}: TemporaryFilesPanelProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileSelect = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      if (event.target.files && event.target.files.length > 0) {
        onAddFiles(event.target.files);
        event.target.value = "";
      }
    },
    [onAddFiles],
  );

  return (
    <aside
      className={cn(
        "absolute inset-y-0 right-0 z-30 hidden w-72 flex-col border-l bg-background/95 shadow-xl backdrop-blur xl:flex",
        className,
      )}
    >
      <div className="flex min-h-12 items-center justify-between border-b px-3">
        <div className="flex min-w-0 items-center gap-2">
          <Paperclip className="size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">一時ファイル</div>
            <div className="text-[11px] text-muted-foreground">
              temp / {files.length} 件
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            title="一時ファイルを追加"
          >
            <Plus className="size-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={onClearFiles}
            disabled={disabled || files.length === 0}
            title="一時ファイルをすべて削除"
          >
            <Trash2 className="size-4" />
          </Button>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={handleFileSelect}
      />

      {files.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-sm text-muted-foreground">
          <Paperclip className="size-8 opacity-50" />
          <p>このチャットで一時的に扱うファイルを追加できます。</p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-3">
          <div className="space-y-2">
            {files.map((file, index) => (
              <div
                key={`${file.name}-${file.lastModified}-${index}`}
                className="group flex min-h-14 items-center gap-2 rounded-md border bg-background p-2"
              >
                <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted">
                  {isImageFile(file) ? (
                    <ImageIcon className="size-4 text-muted-foreground" />
                  ) : (
                    <FileText className="size-4 text-muted-foreground" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium">
                    {file.name}
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {formatFileSize(file.size)}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="size-7 shrink-0 opacity-70 group-hover:opacity-100"
                  onClick={() => onRemoveFile(index)}
                  disabled={disabled}
                  title="一時ファイルから削除"
                >
                  <X className="size-3.5" />
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
