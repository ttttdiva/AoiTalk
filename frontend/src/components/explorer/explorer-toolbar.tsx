"use client";

import { useRef, useState } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import {
  ExplorerUploadError,
  explorerUpload,
  explorerAddBookmark,
  explorerRemoveBookmark,
} from "@/lib/explorer-api";
import { Button } from "@/components/ui/button";
import {
  RefreshCw,
  FolderPlus,
  Upload,
  Star,
  StarOff,
  LayoutGrid,
  List,
  Plus,
} from "lucide-react";
import { toast } from "sonner";
import { hfUploadFiles } from "@/lib/hf-api";
import { parseHfPath } from "@/lib/hf/virtual-path";

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
    bookmarks,
    refreshBookmarks,
    isAbsoluteFilerPath,
    isHfMode,
  } = useExplorer();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const isBookmarked = bookmarks.some((b) => b.path === currentPath);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0 || uploading) return;
    setUploading(true);
    try {
      const result = isHfMode
        ? await hfUploadFiles(currentPath, files)
        : await explorerUpload(currentPath, files);
      if (result.failureCount > 0) {
        toast.warning(`${result.successCount}件成功、${result.failureCount}件失敗しました`);
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

  const toggleBookmark = async () => {
    try {
      if (isBookmarked) {
        await explorerRemoveBookmark(currentPath);
      } else {
        const name = currentPath.split("/").pop() || "Root";
        await explorerAddBookmark(name, currentPath);
      }
      refreshBookmarks();
    } catch {
      // bookmark error
    }
  };

  // 絶対パス閲覧時はwrite操作を無効化
  const hfPath = isHfMode ? parseHfPath(currentPath) : null;
  const canWrite = !isAbsoluteFilerPath && !isHfMode;
  const canUploadToHf = Boolean(
    isHfMode && hfPath?.kind === "repo" && hfPath.accountId,
  );
  return (
    <div className="flex items-center gap-1 border-b px-2 py-1">
      <Button variant="ghost" size="icon-sm" onClick={refresh} title="更新">
        <RefreshCw className="size-3.5" />
      </Button>
      {isHfMode && onAddHfReference && (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onAddHfReference}
          title="HF参照を追加"
        >
          <Plus className="size-3.5" />
        </Button>
      )}
      {canWrite && (
        <>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onNewFolder}
            title="新規フォルダ"
          >
            <FolderPlus className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
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
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={toggleBookmark}
        title="ブックマーク"
      >
        {isBookmarked ? (
          <Star className="size-3.5 fill-yellow-400 text-yellow-400" />
        ) : (
          <StarOff className="size-3.5" />
        )}
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={() => setViewMode(viewMode === "grid" ? "list" : "grid")}
        title={viewMode === "grid" ? "リスト表示" : "グリッド表示"}
      >
        {viewMode === "grid" ? (
          <List className="size-3.5" />
        ) : (
          <LayoutGrid className="size-3.5" />
        )}
      </Button>
    </div>
  );
}
