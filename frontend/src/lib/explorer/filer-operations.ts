"use client";

/**
 * ファイラーの破壊的操作（削除 / リネーム / 移動）の実行ロジックを1本化したモジュール。
 *
 * ファイラー本体（ExplorerProvider 配下）とサイドバー（Provider の外）の両方から
 * 同じ Undo スタックを共有する必要があるため、React context ではなく
 * モジュールレベルの軽量ストアで状態を保持する。
 */

import { toast } from "sonner";
import {
  explorerDelete,
  explorerMove,
  explorerCopy,
  explorerRename,
  explorerRestore,
  explorerErrorMessage,
} from "@/lib/explorer-api";
import {
  hfDeleteFiles,
  hydrusDeleteFiles,
  hydrusUndeleteFiles,
  type HfDeleteItem,
} from "@/lib/hf-api";
import {
  deleteProjectRecordTable,
  isRecordTableFile,
  updateProjectRecordTable,
  RECORD_TABLE_EXTENSION,
} from "@/lib/record-tables-api";
import type { ExplorerDirectory, ExplorerFile } from "@/lib/explorer-api";
import { isHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath, parseHydrusFileId } from "@/lib/hydrus/virtual-path";
import type { ConfirmOptions } from "@/hooks/use-confirm";
import type { FilerCapabilities } from "./filer-capabilities";
import {
  clearFilerUndoState,
  createFilerUndoState,
  pushFilerRedoEntry,
  pushFilerUndoEntry,
  restoreFilerUndoEntry,
  takeFilerRedoEntry,
  takeFilerUndoEntry,
  type FilerDeleteUndoItem,
  type FilerRenameUndoItem,
  type FilerUndoEntry,
  type FilerUndoState,
} from "./filer-undo-stack";

type ConfirmFn = (options?: ConfirmOptions) => Promise<boolean>;
export type FilerRefresh = () => void | boolean | Promise<void | boolean>;

// ─── モジュールレベルストア ───

let undoState: FilerUndoState = createFilerUndoState();
const listeners = new Set<() => void>();

function setUndoState(next: FilerUndoState) {
  if (next === undoState) return;
  undoState = next;
  for (const listener of listeners) listener();
}

export function subscribeFilerUndo(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getFilerUndoSnapshot(): FilerUndoState {
  return undoState;
}

/** タブ切り替えなどでスタックを全消去する。 */
export function clearFilerUndoHistory(): void {
  setUndoState(clearFilerUndoState());
}

// ─── Hydrus 表示ハンドラ ───

/**
 * Hydrus タブは explorer の一覧 API を持たない検索結果ビューで、
 * ページ結果は HydrusSearchBar 側にキャッシュされる。そのため削除後の
 * `refresh()`（= explorerList("Hydrus")）は失敗し、再検索しても削除が反映されない。
 * 代わりに ExplorerProvider が表示側の除外/復帰を担い、ここへ登録する。
 */
export interface HydrusViewHandlers {
  /** 削除済み file_id を一覧表示から除外する */
  prune: (fileIds: number[]) => void;
  /** Undo で復元した file_id を一覧表示へ戻す */
  restore: (fileIds: number[]) => void;
}

let hydrusViewHandlers: HydrusViewHandlers | null = null;

export function registerHydrusViewHandlers(
  handlers: HydrusViewHandlers,
): () => void {
  hydrusViewHandlers = handlers;
  return () => {
    if (hydrusViewHandlers === handlers) hydrusViewHandlers = null;
  };
}

// ─── 共通ヘルパ ───

function parentDir(path: string): string {
  const index = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return index <= 0 ? "" : path.slice(0, index);
}

async function runRefresh(refresh?: FilerRefresh): Promise<boolean> {
  if (!refresh) return true;
  const result = await refresh();
  // Context-backed refresh returns false when a newer navigation superseded
  // the operation. Legacy callers return void and remain successful.
  return result !== false;
}

// ─── 削除 ───

export interface FilerDeleteTarget {
  path: string;
  name: string;
  isDirectory: boolean;
  /** .dbtable（レコードテーブル）なら true */
  isRecordTable?: boolean;
  /** .dbtable の識別子（欠けている場合は削除をスキップする） */
  recordTable?: { projectId: string; tableId: string } | null;
}

/** ExplorerDirectory / ExplorerFile を削除ターゲットへ変換する。 */
export function toFilerDeleteTarget(
  item: ExplorerDirectory | ExplorerFile,
): FilerDeleteTarget {
  const isDirectory = !("type" in item);
  if (!isDirectory && isRecordTableFile(item as ExplorerFile)) {
    const file = item as ExplorerFile;
    return {
      path: file.path,
      name: file.name,
      isDirectory: false,
      isRecordTable: true,
      recordTable:
        file.project_id && file.record_table_id
          ? { projectId: file.project_id, tableId: file.record_table_id }
          : null,
    };
  }
  return { path: item.path, name: item.name, isDirectory };
}

export interface RunFilerDeleteParams {
  targets: FilerDeleteTarget[];
  capabilities: FilerCapabilities;
  refresh?: FilerRefresh;
  /** 確認ダイアログ。未指定時は確認が必要な操作をスキップする。 */
  confirm?: ConfirmFn;
  /** 削除成功時に呼ばれる（選択解除など） */
  onDeleted?: () => void;
}

/**
 * 選択項目をまとめて削除する。タブ種別に応じて explorer / HF / Hydrus /
 * レコードテーブルへ振り分け、Undo 可能なものだけスタックへ積む。
 */
export async function runFilerDelete(
  params: RunFilerDeleteParams,
): Promise<boolean> {
  const { targets, capabilities, confirm, refresh, onDeleted } = params;
  if (!capabilities.canDelete || targets.length === 0) return false;

  const recordTables = targets.filter((t) => t.isRecordTable);
  const others = targets.filter((t) => !t.isRecordTable);
  const hfTargets = others.filter((t) => isHfPath(t.path));
  const hydrusTargets = others.filter((t) => isHydrusPath(t.path));
  const regularTargets = others.filter(
    (t) => !isHfPath(t.path) && !isHydrusPath(t.path),
  );

  // レコードテーブルは元に戻せないため常に確認する
  if (recordTables.length > 0) {
    const ok = await askConfirm(confirm, {
      title: "レコードテーブルを削除",
      description:
        recordTables.length === 1
          ? `「${recordTables[0].name}」を削除します。元に戻せません。`
          : `${recordTables.length}件のレコードテーブルを削除します。元に戻せません。`,
      confirmLabel: "削除",
      destructive: true,
    });
    if (!ok) return false;
  }

  // HF はゴミ箱が無いので確認必須
  if (capabilities.deleteNeedsConfirm && hfTargets.length > 0) {
    const ok = await askConfirm(confirm, {
      title: "HF上のファイルを削除",
      description:
        hfTargets.length === 1
          ? `「${hfTargets[0].name}」をHFリポジトリから削除します。元に戻せません。`
          : `${hfTargets.length}件をHFリポジトリから削除します。元に戻せません。`,
      confirmLabel: "削除",
      destructive: true,
    });
    if (!ok) return false;
  }

  // 管理者のローカル絶対パスはワークスペースのゴミ箱へ入らず物理削除される。
  if (capabilities.deleteNeedsConfirm && regularTargets.length > 0) {
    const ok = await askConfirm(confirm, {
      title: "ファイルを完全に削除",
      description:
        regularTargets.length === 1
          ? `「${regularTargets[0].name}」を完全に削除します。元に戻せません。`
          : `${regularTargets.length}件を完全に削除します。元に戻せません。`,
      confirmLabel: "削除",
      destructive: true,
    });
    if (!ok) return false;
  }

  // 途中で失敗しても、成功済み分は必ず Undo スタックへ積む
  const trashed: FilerDeleteUndoItem[] = [];
  const hydrusDeletedIds: number[] = [];
  // 識別子が欠けていて削除できなかった .dbtable
  const skippedNames: string[] = [];
  let touchedNonHydrus = false;

  const commitUndoEntries = () => {
    const entries: FilerUndoEntry[] = [];
    if (capabilities.deleteUndoable && hydrusDeletedIds.length > 0) {
      entries.push({ kind: "hydrus-delete", fileIds: [...hydrusDeletedIds] });
    }
    if (capabilities.deleteUndoable && trashed.length > 0) {
      entries.push({ kind: "delete", entries: [...trashed] });
    }
    for (const entry of entries) {
      setUndoState(pushFilerUndoEntry(undoState, entry));
    }
    return entries.length > 0;
  };

  try {
    for (const target of recordTables) {
      const table = target.recordTable;
      if (!table) {
        skippedNames.push(target.name);
        continue;
      }
      touchedNonHydrus = true;
      await deleteProjectRecordTable(table.projectId, table.tableId);
    }

    if (hfTargets.length > 0) {
      touchedNonHydrus = true;
      const items: HfDeleteItem[] = hfTargets.map((target) => ({
        path: target.path,
        isDirectory: target.isDirectory,
      }));
      await hfDeleteFiles(items);
    }

    if (hydrusTargets.length > 0) {
      const fileIds = hydrusTargets
        .map((target) => parseHydrusFileId(target.path))
        .filter((id): id is number => id !== null);
      if (fileIds.length > 0) {
        await hydrusDeleteFiles(fileIds);
        hydrusDeletedIds.push(...fileIds);
        // Hydrus は refresh が使えないので表示側から直接取り除く
        hydrusViewHandlers?.prune(fileIds);
      }
    }

    for (const target of regularTargets) {
      touchedNonHydrus = true;
      const result = await explorerDelete(target.path);
      if (result.trash?.token) {
        trashed.push({
          token: result.trash.token,
          originalPath: result.trash.original_path || target.path,
        });
      }
    }
  } catch (error) {
    // 成功済み分を捨てないよう、エラー時も Undo エントリを確定させる
    commitUndoEntries();
    if (touchedNonHydrus) await runRefresh(refresh);
    toast.error(`削除に失敗しました: ${explorerErrorMessage(error)}`);
    return false;
  }

  const undoable = commitUndoEntries();
  onDeleted?.();
  // Hydrus のみの削除では explorer の一覧APIを叩かない（失敗してエラー表示になるため）
  if (touchedNonHydrus) await runRefresh(refresh);

  const deletedCount = targets.length - skippedNames.length;
  if (deletedCount === 0) {
    toast.error(`削除できませんでした: ${skippedNames.join(", ")}`);
    return false;
  }

  const message =
    deletedCount === 1
      ? `「${targets.find((t) => !skippedNames.includes(t.name))?.name ?? targets[0].name}」を削除しました`
      : `${deletedCount}件を削除しました`;
  if (undoable) {
    // .dbtable / HF は元に戻せないため、混在時は対象範囲を明示する
    const undoableCount = trashed.length + hydrusDeletedIds.length;
    toast.success(message, {
      description:
        undoableCount < deletedCount
          ? `元に戻せるのは${undoableCount}件（レコードテーブル・HFは復元できません）`
          : undefined,
      action: {
        label: "元に戻す",
        onClick: () => {
          void undoFilerOperation(refresh);
        },
      },
    });
  } else {
    toast.success(message);
  }
  if (skippedNames.length > 0) {
    toast.error(`削除できなかった項目: ${skippedNames.join(", ")}`);
  }
  return true;
}

async function askConfirm(
  confirm: ConfirmFn | undefined,
  options: ConfirmOptions,
): Promise<boolean> {
  if (!confirm) return false;
  return confirm(options);
}

// ─── リネーム ───

export interface RunFilerRenameParams {
  path: string;
  currentName: string;
  newName: string;
  capabilities: FilerCapabilities;
  refresh?: FilerRefresh;
  /** Called only after a successful rename and its refresh have completed. */
  onRenamed?: (result: {
    oldPath: string;
    newPath: string;
    newName: string;
  }) => void;
  /** .dbtable の場合のみ設定（Undo 対象外） */
  recordTable?: { projectId: string; tableId: string } | null;
}

export async function runFilerRename(
  params: RunFilerRenameParams,
): Promise<boolean> {
  const { capabilities, path, currentName, refresh } = params;
  const newName = params.newName.trim();
  if (!capabilities.canRename || !newName || newName === currentName) {
    return false;
  }

  // Keep the server-confirmed path/name separate from the requested values.
  // In particular, a project API may normalize the name or return a path that
  // changes the item's sort position; selection restoration must follow that
  // actual path after refresh rather than reconstructing it locally.
  let committedPath = path;
  let committedName = newName;

  try {
    if (params.recordTable) {
      // レコードテーブルはテーブル名の更新。拡張子は保存しない。
      const tableName = newName.endsWith(RECORD_TABLE_EXTENSION)
        ? newName.slice(0, -RECORD_TABLE_EXTENSION.length)
        : newName;
      if (!tableName) return false;
      await updateProjectRecordTable(
        params.recordTable.projectId,
        params.recordTable.tableId,
        { name: tableName },
      );
      committedName = tableName;
    } else {
      const result = await explorerRename(path, newName);
      committedPath = result.new_path || path;
      committedName = result.new_name || newName;
      // 実際に確定したパス（new_path）を保持して Undo の対象を取り違えないようにする
      setUndoState(
        pushFilerUndoEntry(undoState, {
          kind: "rename",
          path: committedPath,
          fromName: currentName,
          toName: committedName,
        }),
      );
    }
  } catch (error) {
    toast.error(`名前の変更に失敗しました: ${explorerErrorMessage(error)}`);
    return false;
  }

  const refreshedCurrentDirectory = await runRefresh(refresh);
  if (refreshedCurrentDirectory) {
    params.onRenamed?.({
      oldPath: path,
      newPath: committedPath,
      newName: committedName,
    });
  }
  return true;
}

export interface FilerRenameBatchItem {
  path: string;
  currentName: string;
  newName: string;
}

export interface RunFilerRenameBatchParams {
  items: FilerRenameBatchItem[];
  capabilities: FilerCapabilities;
  refresh?: FilerRefresh;
}

export interface FilerRenameBatchResult {
  renamed: number;
  failed: number;
}

function pathDepth(path: string): number {
  return path.split(/[/\\|]/).filter(Boolean).length;
}

function leafName(path: string): string {
  return path.split(/[/\\|]/).pop() || path;
}

function pathWithName(path: string, name: string): string {
  const parent = parentDir(path);
  return parent ? `${parent}/${name}` : name;
}

/** 検索結果の名前置換を一つの Undo エントリとして実行する。 */
export async function runFilerRenameBatch(
  params: RunFilerRenameBatchParams,
): Promise<FilerRenameBatchResult> {
  if (!params.capabilities.canRename || params.items.length === 0) {
    return { renamed: 0, failed: 0 };
  }

  // 子のパスを先に変更してから親を変更する。検索結果に親子が混在しても
  // 後続のパスが移動で無効にならないようにする。
  const items = params.items
    .map((item) => ({ ...item, newName: item.newName.trim() }))
    .filter((item) => item.newName && item.newName !== item.currentName)
    .sort((left, right) => pathDepth(right.path) - pathDepth(left.path));
  if (items.length === 0) return { renamed: 0, failed: 0 };

  const renamed: FilerRenameUndoItem[] = [];
  const failures: string[] = [];
  for (const item of items) {
    try {
      const result = await explorerRename(item.path, item.newName);
      renamed.push({
        from: item.path,
        to:
          result.new_path ||
          pathWithName(item.path, result.new_name || item.newName),
      });
    } catch (error) {
      failures.push(`${item.currentName}: ${explorerErrorMessage(error)}`);
    }
  }

  if (renamed.length > 0) {
    setUndoState(
      pushFilerUndoEntry(undoState, {
        kind: "rename-batch",
        entries: renamed,
      }),
    );
    await runRefresh(params.refresh);
    toast.success(`${renamed.length}件の名前を置換しました`);
  }
  if (failures.length > 0) {
    const suffix = failures.length > 1 ? `（ほか${failures.length - 1}件）` : "";
    toast.error(`名前を置換できない項目があります${suffix}: ${failures[0]}`);
  }

  return { renamed: renamed.length, failed: failures.length };
}

// ─── 移動 / コピー ───

export interface RunFilerTransferParams {
  paths: string[];
  destDir: string;
  operation: "move" | "copy";
  capabilities: FilerCapabilities;
  refresh?: FilerRefresh;
  onTransferred?: () => void;
}

/** 切り取り＆貼り付け / D&D / コピー貼り付けの共通実装。 */
export async function runFilerTransfer(
  params: RunFilerTransferParams,
): Promise<boolean> {
  const { paths, destDir, operation, capabilities, refresh, onTransferred } =
    params;
  if (paths.length === 0) return false;
  if (operation === "move" && !capabilities.canMove) return false;
  if (operation === "copy" && !capabilities.canCopy) return false;

  const moved: { from: string; to: string }[] = [];
  try {
    for (const src of paths) {
      // 自分自身へのドロップは無視
      if (src === destDir) continue;
      if (operation === "move") {
        const result = await explorerMove(src, destDir);
        const newPath = result.new_path || src;
        // 親フォルダへ戻すなど、移動先が現在位置と同じ場合は成功した no-op。
        // このケースを Undo スタックへ積むと、Undo が意図せず再移動になる。
        if (newPath !== src) moved.push({ from: src, to: newPath });
      } else {
        await explorerCopy(src, destDir);
      }
    }
  } catch (error) {
    // 成功済みの移動分は Undo できるようスタックへ積んでから通知する
    if (moved.length > 0) {
      setUndoState(
        pushFilerUndoEntry(undoState, { kind: "move", entries: moved }),
      );
    }
    await runRefresh(refresh);
    toast.error(
      `${operation === "move" ? "移動" : "コピー"}に失敗しました: ${explorerErrorMessage(error)}`,
    );
    return false;
  }

  if (moved.length > 0) {
    setUndoState(
      pushFilerUndoEntry(undoState, { kind: "move", entries: moved }),
    );
  }
  onTransferred?.();
  await runRefresh(refresh);
  return true;
}

// ─── Undo / Redo ───

/**
 * エントリの逆操作（undo）または再実行（redo）を行い、反対側へ積むエントリを返す。
 */
async function applyFilerUndoEntry(
  entry: FilerUndoEntry,
  direction: "undo" | "redo",
): Promise<FilerUndoEntry> {
  if (entry.kind === "delete") {
    if (direction === "undo") {
      const restored: FilerDeleteUndoItem[] = [];
      for (const item of entry.entries) {
        const result = await explorerRestore(item.token, item.originalPath);
        restored.push({
          token: item.token,
          originalPath: result.restored_path || item.originalPath,
        });
      }
      return { kind: "delete", entries: restored };
    }
    const deleted: FilerDeleteUndoItem[] = [];
    for (const item of entry.entries) {
      const result = await explorerDelete(item.originalPath);
      if (!result.trash?.token) {
        throw new Error("削除の取り消し情報を取得できませんでした");
      }
      deleted.push({
        token: result.trash.token,
        originalPath: result.trash.original_path || item.originalPath,
      });
    }
    return { kind: "delete", entries: deleted };
  }

  if (entry.kind === "rename") {
    // undo / redo とも「現在のパスを fromName へ戻す」形で対称に扱える
    const result = await explorerRename(entry.path, entry.fromName);
    return {
      kind: "rename",
      path: result.new_path || entry.path,
      fromName: entry.toName,
      toName: entry.fromName,
    };
  }

  if (entry.kind === "rename-batch") {
    const reversed: FilerRenameUndoItem[] = [];
    // entry.entries は子から親の順。Undo は親を先に戻し、Redo は子を先に戻す。
    for (const item of [...entry.entries].reverse()) {
      const currentPath = direction === "undo" ? item.to : item.from;
      const targetName =
        direction === "undo" ? leafName(item.from) : leafName(item.to);
      const result = await explorerRename(currentPath, targetName);
      const nextPath =
        result.new_path || pathWithName(currentPath, result.new_name || targetName);
      reversed.push(
        direction === "undo"
          ? { from: nextPath, to: item.to }
          : { from: item.from, to: nextPath },
      );
    }
    return { kind: "rename-batch", entries: reversed };
  }

  if (entry.kind === "move") {
    const reversed: { from: string; to: string }[] = [];
    for (const item of entry.entries) {
      const result = await explorerMove(item.to, parentDir(item.from));
      reversed.push({ from: item.to, to: result.new_path || item.from });
    }
    return { kind: "move", entries: reversed };
  }

  // hydrus-delete: 表示側もページキャッシュを迂回して直接更新する
  if (direction === "undo") {
    await hydrusUndeleteFiles(entry.fileIds);
    hydrusViewHandlers?.restore(entry.fileIds);
  } else {
    await hydrusDeleteFiles(entry.fileIds);
    hydrusViewHandlers?.prune(entry.fileIds);
  }
  return { kind: "hydrus-delete", fileIds: entry.fileIds };
}

/** Hydrus は explorer の一覧APIを持たないため refresh を呼ばない。 */
function entryNeedsRefresh(entry: FilerUndoEntry): boolean {
  return entry.kind !== "hydrus-delete";
}

export function canUndoFiler(): boolean {
  return undoState.undo.length > 0;
}

export function canRedoFiler(): boolean {
  return undoState.redo.length > 0;
}

export async function undoFilerOperation(
  refresh?: FilerRefresh,
): Promise<boolean> {
  const taken = takeFilerUndoEntry(undoState);
  if (!taken) return false;
  setUndoState(taken.state);
  const needsRefresh = entryNeedsRefresh(taken.entry);
  try {
    const inverse = await applyFilerUndoEntry(taken.entry, "undo");
    setUndoState(pushFilerRedoEntry(getFilerUndoSnapshot(), inverse));
    if (needsRefresh) await runRefresh(refresh);
    toast.success("元に戻しました");
    return true;
  } catch (error) {
    // 失敗したエントリは復元不能とみなし、スタックから捨てる
    if (needsRefresh) await runRefresh(refresh);
    toast.error(`元に戻せませんでした: ${explorerErrorMessage(error)}`);
    return false;
  }
}

export async function redoFilerOperation(
  refresh?: FilerRefresh,
): Promise<boolean> {
  const taken = takeFilerRedoEntry(undoState);
  if (!taken) return false;
  setUndoState(taken.state);
  const needsRefresh = entryNeedsRefresh(taken.entry);
  try {
    const inverse = await applyFilerUndoEntry(taken.entry, "redo");
    setUndoState(restoreFilerUndoEntry(getFilerUndoSnapshot(), inverse));
    if (needsRefresh) await runRefresh(refresh);
    toast.success("やり直しました");
    return true;
  } catch (error) {
    if (needsRefresh) await runRefresh(refresh);
    toast.error(`やり直せませんでした: ${explorerErrorMessage(error)}`);
    return false;
  }
}
