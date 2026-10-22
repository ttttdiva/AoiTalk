"use client";

import { useConfirm } from "@/hooks/use-confirm";

import { useRef, useState } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  ExplorerUploadError,
  explorerUpload,
} from "@/lib/explorer-api";
import { Button } from "@/components/ui/button";
import {
  BookmarkPlus,
  Loader2,
  RefreshCw,
  Upload,
  Download,
  LayoutGrid,
  List,
  Plus,
} from "lucide-react";
import { toast } from "sonner";
import { uploadFailureToastOptions } from "@/lib/upload-failure";

interface ExplorerToolbarProps {
  onNewFolder: () => void;
  onAddBookmark?: () => void;
  onDownloadSelection?: () => void;
  hasDownloadSelection?: boolean;
}

export function ExplorerToolbar({
  onNewFolder,
  onAddBookmark,
  onDownloadSelection,
  hasDownloadSelection = false,
}: ExplorerToolbarProps) {
  const {
    currentPath,
    refresh,
    viewMode,
    setViewMode,
    capabilities,
  } = useExplorer();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const confirmOverwrite = useConfirm();

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    if (uploading) {
      // A pending transfer must stay visible; silently dropping the new
      // selection looks like an upload that does nothing.
      toast.info("アップロード中です。完了後にもう一度選択してください");
      e.target.value = "";
      return;
    }
    setUploading(true);
    const progressToastId = toast.loading(
      files.length === 1
        ? `「${files[0].name}」をアップロード中…`
        : `${files.length}件をアップロード中…`,
    );
    try {
      const result = await explorerUpload(currentPath, files, { confirmOverwrite });
      if (result.failureCount > 0) {
        toast.warning(
          `${result.successCount}件成功、${result.failureCount}件失敗しました`,
          uploadFailureToastOptions(result.failures),
        );
      } else if (result.successCount > 0) {
        toast.success(`${result.successCount}件アップロードしました`);
      }
      await refresh();
    } catch (error) {
      if (error instanceof ExplorerUploadError) {
        const { successCount, failureCount } = error.batchResult;
        if (successCount > 0) await refresh();
        toast.error(
          successCount > 0
            ? `${successCount}件アップロード、${failureCount}件失敗しました`
            : error.message,
          uploadFailureToastOptions(error.batchResult.failures),
        );
      } else {
        toast.error(
          error instanceof Error ? error.message : "アップロードに失敗しました",
        );
      }
    } finally {
      toast.dismiss(progressToastId);
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const canWrite = capabilities.canCreate;
  return (
    <div className="flex min-h-9 items-center justify-between gap-2 border-0 bg-transparent px-0 py-0">
      <div className="flex min-w-0 items-center gap-1">
      <Button
        variant="ghost"
        size="icon-sm"
        className="text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
        onClick={refresh}
        title="更新"
      >
        <RefreshCw className="size-3.5" />
      </Button>
      {onAddBookmark && (
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5 px-2 text-xs text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
          onClick={onAddBookmark}
          title="選択中の項目、または現在のフォルダーをお気に入りに登録"
          aria-label="お気に入りに登録"
        >
          <BookmarkPlus className="size-3.5" />
          <span>お気に入り</span>
        </Button>
      )}
      {canWrite && (
        <>
          <Button
            variant="default"
            size="sm"
            className="h-8 gap-1.5 rounded bg-primary-container px-3 text-xs font-semibold text-on-primary-container hover:bg-primary hover:text-primary-foreground"
            onClick={onNewFolder}
            title="新規フォルダ"
          >
            <Plus className="size-3.5" />
            <span className="hidden sm:inline">New</span>
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            title={uploading ? "アップロード中" : "アップロード"}
            aria-busy={uploading}
          >
            {uploading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Upload className="size-3.5" />
            )}
          </Button>
          {hasDownloadSelection && onDownloadSelection ? (
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
              onClick={onDownloadSelection}
              title="選択した項目をダウンロード"
              aria-label="選択した項目をダウンロード"
            >
              <Download className="size-3.5" />
            </Button>
          ) : null}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleUpload}
          />
        </>
      )}
      <div className="ml-1 flex items-center overflow-hidden rounded border border-border bg-surface-container">
        <Button
          variant="ghost"
          size="icon-sm"
          className={
            viewMode === "grid"
              ? "rounded-none bg-surface-container-high text-primary hover:bg-surface-container-highest"
              : "rounded-none text-muted-foreground hover:bg-surface-container-high"
          }
          onClick={() => setViewMode("grid")}
          title="グリッド表示"
          aria-pressed={viewMode === "grid"}
        >
          <LayoutGrid className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          className={
            viewMode === "list"
              ? "rounded-none border-l border-border bg-surface-container-high text-primary hover:bg-surface-container-highest"
              : "rounded-none border-l border-border text-muted-foreground hover:bg-surface-container-high"
          }
          onClick={() => setViewMode("list")}
          title="リスト表示"
          aria-pressed={viewMode === "list"}
        >
          <List className="size-3.5" />
        </Button>
      </div>
      </div>
    </div>
  );
}
