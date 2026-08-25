"use client";

import { useRef, useState } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  ExplorerUploadError,
  explorerUpload,
} from "@/lib/explorer-api";
import { Button } from "@/components/ui/button";
import {
  RefreshCw,
  Upload,
  LayoutGrid,
  List,
  Plus,
} from "lucide-react";
import { toast } from "sonner";
import { HfUploadError, hfUploadFiles } from "@/lib/hf-api";
import { parseHfPath } from "@/lib/hf/virtual-path";
import { uploadFailureToastOptions } from "@/lib/upload-failure";

interface ExplorerToolbarProps {
  onNewFolder: () => void;
  onAddHfReference?: () => void;
}

export function ExplorerToolbar({
  onNewFolder,
  onAddHfReference,
}: ExplorerToolbarProps) {
  const {
    currentPath,
    refresh,
    viewMode,
    setViewMode,
    isHfMode,
    capabilities,
  } = useExplorer();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0 || uploading) return;
    setUploading(true);
    try {
      const result = isHfMode
        ? await hfUploadFiles(currentPath, files)
        : await explorerUpload(currentPath, files);
      if (result.failureCount > 0) {
        toast.warning(
          `${result.successCount}件成功、${result.failureCount}件失敗しました`,
          uploadFailureToastOptions(result.failures),
        );
      } else {
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
      } else if (error instanceof HfUploadError) {
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
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const hfPath = isHfMode ? parseHfPath(currentPath) : null;
  const canWrite = capabilities.canCreate && !isHfMode;
  const canUploadToHf = Boolean(
    isHfMode &&
      capabilities.canDelete &&
      hfPath?.kind === "repo" &&
      hfPath.accountId,
  );
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
      {isHfMode && onAddHfReference && (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
          onClick={onAddHfReference}
          title="HF参照を追加"
        >
          <Plus className="size-3.5" />
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
            title="アップロード"
          >
            <Upload className="size-3.5" />
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleUpload}
          />
        </>
      )}
      {isHfMode && (
        <>
          <Button
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:bg-surface-container-high hover:text-foreground"
            onClick={() => fileInputRef.current?.click()}
            disabled={!canUploadToHf || uploading}
            title={
              canUploadToHf
                ? "現在のHFディレクトリへアップロード"
                : "書き込み用アカウントに紐づくHFリポジトリで利用できます"
            }
          >
            <Upload className="size-3.5" />
          </Button>
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
