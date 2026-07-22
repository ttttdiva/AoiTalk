"use client";

import {
  useCallback,
  useEffect,
  useState,
  type DragEvent as ReactDragEvent,
} from "react";
import { getDroppedExplorerFiles } from "@/lib/file-drop";

function hasDraggedFiles(
  dataTransfer: DataTransfer | null,
): dataTransfer is DataTransfer {
  return Boolean(
    dataTransfer && Array.from(dataTransfer.types).includes("Files"),
  );
}

type UseChatAttachedFilesArgs = {
  /** 添付ファイルを紐づけるセッションキー（未選択時はドラフト用キー）。 */
  temporaryFileSessionKey: string;
  /** 生成処理中はドロップを受け付けない。 */
  chatBusy: boolean;
};

/**
 * セッション別の一時添付ファイルとドラッグ&ドロップ処理を担うフック。
 * `page.tsx` の該当ロジックを挙動不変で移設したもの。
 */
export function useChatAttachedFiles({
  temporaryFileSessionKey,
  chatBusy,
}: UseChatAttachedFilesArgs) {
  const [temporaryFilesBySession, setTemporaryFilesBySession] = useState<
    Record<string, File[]>
  >({});

  const attachedFiles = temporaryFilesBySession[temporaryFileSessionKey] ?? [];

  const setAttachedFiles = useCallback(
    (next: File[] | ((prev: File[]) => File[])) => {
      setTemporaryFilesBySession((prev) => {
        const current = prev[temporaryFileSessionKey] ?? [];
        const resolved = typeof next === "function" ? next(current) : next;
        const updated = { ...prev };

        if (resolved.length === 0) {
          delete updated[temporaryFileSessionKey];
        } else {
          updated[temporaryFileSessionKey] = resolved;
        }

        return updated;
      });
    },
    [temporaryFileSessionKey],
  );

  const appendDroppedFiles = useCallback(
    async (dataTransfer: DataTransfer) => {
      const droppedItems = await getDroppedExplorerFiles(dataTransfer);
      const files = droppedItems.map((item) => item.file);
      if (files.length === 0) return;

      setAttachedFiles((prev) => [...prev, ...files]);
    },
    [setAttachedFiles],
  );

  const handleChatFileDragOver = useCallback(
    (event: ReactDragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = chatBusy ? "none" : "copy";
    },
    [chatBusy],
  );

  const handleChatFileDrop = useCallback(
    async (event: ReactDragEvent<HTMLDivElement>) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      if (chatBusy) return;

      await appendDroppedFiles(event.dataTransfer);
    },
    [appendDroppedFiles, chatBusy],
  );

  useEffect(() => {
    const handleWindowFileDragOver = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = chatBusy ? "none" : "copy";
    };

    const handleWindowFileDrop = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;

      event.preventDefault();
      event.stopPropagation();
      if (chatBusy) return;

      void appendDroppedFiles(event.dataTransfer);
    };

    window.addEventListener("dragover", handleWindowFileDragOver);
    window.addEventListener("drop", handleWindowFileDrop);
    return () => {
      window.removeEventListener("dragover", handleWindowFileDragOver);
      window.removeEventListener("drop", handleWindowFileDrop);
    };
  }, [appendDroppedFiles, chatBusy]);

  return {
    attachedFiles,
    setAttachedFiles,
    handleChatFileDragOver,
    handleChatFileDrop,
  };
}
