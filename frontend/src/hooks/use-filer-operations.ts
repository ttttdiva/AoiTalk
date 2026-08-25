"use client";

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useConfirm } from "@/hooks/use-confirm";
import type { FilerCapabilities } from "@/lib/explorer/filer-capabilities";
import {
  canRedoFiler,
  canUndoFiler,
  getFilerUndoSnapshot,
  redoFilerOperation,
  runFilerDelete,
  runFilerRename,
  runFilerRenameBatch,
  runFilerTransfer,
  subscribeFilerUndo,
  undoFilerOperation,
  type FilerDeleteTarget,
  type FilerRefresh,
} from "@/lib/explorer/filer-operations";

/** Undo / Redo スタックの有無を購読する（メニューやボタンの活性判定用）。 */
export function useFilerUndoState(): { canUndo: boolean; canRedo: boolean } {
  const state = useSyncExternalStore(
    subscribeFilerUndo,
    getFilerUndoSnapshot,
    getFilerUndoSnapshot,
  );
  return {
    canUndo: state.undo.length > 0,
    canRedo: state.redo.length > 0,
  };
}

export interface UseFilerOperationsParams {
  capabilities: FilerCapabilities;
  refresh: FilerRefresh;
}

/**
 * 破壊的操作（削除 / リネーム / 移動）と Undo / Redo をまとめて提供する。
 * capabilities / refresh は ref 経由で参照するため、返す関数の同一性は保たれる。
 */
export function useFilerOperations({
  capabilities,
  refresh,
}: UseFilerOperationsParams) {
  const confirm = useConfirm();
  const paramsRef = useRef({ capabilities, refresh, confirm });
  // 返す関数の同一性を保つため、最新値は描画後に ref へ同期する
  useEffect(() => {
    paramsRef.current = { capabilities, refresh, confirm };
  });

  const deleteTargets = useCallback(
    (targets: FilerDeleteTarget[], options?: { onDeleted?: () => void }) => {
      const { capabilities, refresh, confirm } = paramsRef.current;
      return runFilerDelete({
        targets,
        capabilities,
        confirm,
        refresh,
        onDeleted: options?.onDeleted,
      });
    },
    [],
  );

  const rename = useCallback(
    (params: {
      path: string;
      currentName: string;
      newName: string;
      recordTable?: { projectId: string; tableId: string } | null;
      onRenamed?: (result: {
        oldPath: string;
        newPath: string;
        newName: string;
      }) => void;
    }) => {
      const { capabilities, refresh } = paramsRef.current;
      return runFilerRename({ ...params, capabilities, refresh });
    },
    [],
  );

  const renameBatch = useCallback(
    (items: Parameters<typeof runFilerRenameBatch>[0]["items"]) => {
      const { capabilities, refresh } = paramsRef.current;
      return runFilerRenameBatch({ items, capabilities, refresh });
    },
    [],
  );

  const transfer = useCallback(
    (params: {
      paths: string[];
      destDir: string;
      operation: "move" | "copy";
      onTransferred?: () => void;
    }) => {
      const { capabilities, refresh } = paramsRef.current;
      return runFilerTransfer({ ...params, capabilities, refresh });
    },
    [],
  );

  const undo = useCallback(
    () => undoFilerOperation(paramsRef.current.refresh),
    [],
  );
  const redo = useCallback(
    () => redoFilerOperation(paramsRef.current.refresh),
    [],
  );

  return { deleteTargets, rename, renameBatch, transfer, undo, redo };
}

export { canRedoFiler, canUndoFiler };
