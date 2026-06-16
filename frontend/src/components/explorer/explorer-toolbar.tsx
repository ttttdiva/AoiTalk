"use client";

import { useRef } from "react";
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
  Table2,
  Upload,
  Star,
  StarOff,
  LayoutGrid,
  List,
} from "lucide-react";
import { toast } from "sonner";

interface ExplorerToolbarProps {
  onNewFolder: () => void;
  onNewRecordTable?: () => void;
}

export function ExplorerToolbar({
  onNewFolder,
  onNewRecordTable,
}: ExplorerToolbarProps) {
  const {
    currentPath,
    refresh,
    viewMode,
    setViewMode,
    bookmarks,
    refreshBookmarks,
    isAbsoluteFilerPath,
    filerTab,
    contextRootPath,
  } = useExplorer();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const isBookmarked = bookmarks.some((b) => b.path === currentPath);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    try {
      const result = await explorerUpload(currentPath, files);
      toast.success(`${result.successCount}件アップロードしました`);
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
        toast.error("アップロードに失敗しました");
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
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
  const canWrite = !isAbsoluteFilerPath;
  const canCreateRecordTable =
    canWrite &&
    filerTab === "workspace" &&
    !!contextRootPath &&
    currentPath === contextRootPath &&
    !!onNewRecordTable;

  return (
    <div className="flex items-center gap-1 border-b px-2 py-1">
      <Button variant="ghost" size="icon-sm" onClick={refresh} title="更新">
        <RefreshCw className="size-3.5" />
      </Button>
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
          {canCreateRecordTable && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onNewRecordTable}
              title="新規DBテーブル"
            >
              <Table2 className="size-3.5" />
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => fileInputRef.current?.click()}
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
