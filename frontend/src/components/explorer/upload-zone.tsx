"use client";

import { useState, useCallback, useRef } from "react";
import { useExplorer } from "@/contexts/explorer-context";
import { ExplorerUploadError, explorerUpload } from "@/lib/explorer-api";
import { getDroppedExplorerFiles } from "@/lib/file-drop";
import { Upload } from "lucide-react";
import { toast } from "sonner";
import { hfUploadFiles } from "@/lib/hf-api";
import { parseHfPath } from "@/lib/hf/virtual-path";

interface UploadZoneProps {
  children: React.ReactNode;
  onContextMenu?: (e: React.MouseEvent) => void;
}

export function UploadZone({ children, onContextMenu }: UploadZoneProps) {
  const { currentPath, refresh, isHfMode, isHydrusMode } = useExplorer();
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const dragCounterRef = useRef(0);
  const hfPath = isHfMode ? parseHfPath(currentPath) : null;
  const uploadEnabled =
    !isHydrusMode &&
    (!isHfMode || Boolean(hfPath?.kind === "repo" && hfPath.accountId));

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    // 内部D&D（ファイル移動）の場合はアップロードUIを出さない
    if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (dragCounterRef.current === 1) {
      setIsDragging(uploadEnabled);
    }
  }, [uploadEnabled]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    // 内部D&D（ファイル移動）の場合はアップロードUIを出さない
    if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      // 内部D&D（ファイル移動）の場合は子要素に任せる
      if (e.dataTransfer.types.includes("application/x-explorer-paths")) return;

      e.preventDefault();
      e.stopPropagation();
      dragCounterRef.current = 0;
      setIsDragging(false);

      const files = await getDroppedExplorerFiles(e.dataTransfer);
      if (!files || files.length === 0) return;
      if (!uploadEnabled) {
        toast.info(
          isHydrusMode
            ? "Hydrusへのアップロードは対応していません"
            : "書き込み用アカウントに紐づくHFリポジトリで利用できます",
        );
        return;
      }

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
      }
    },
    [currentPath, isHfMode, isHydrusMode, refresh, uploadEnabled],
  );

  return (
    <div
      className="relative flex-1"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onContextMenu={onContextMenu}
    >
      {children}
      {isDragging && (
        <div className="absolute inset-0 z-40 flex items-center justify-center rounded-lg border-2 border-dashed border-blue-400 bg-blue-500/10">
          <div className="flex flex-col items-center gap-1 text-blue-500">
            <Upload className="size-8" />
            <span className="text-sm font-medium">
              ここにドロップしてアップロード
            </span>
          </div>
        </div>
      )}
      {uploading && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-background/60">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <div className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            アップロード中...
          </div>
        </div>
      )}
    </div>
  );
}
