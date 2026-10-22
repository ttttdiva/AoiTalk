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
  deleteProjectRecordTable,
  isRecordTableFile,
  updateProjectRecordTable,
  RECORD_TABLE_EXTENSION,
} from "@/lib/record-tables-api";
import type {
  ExplorerDirectory,
  ExplorerFile,
  ExplorerMoveResult,
} from "@/lib/explorer-api";
import type { ConfirmOptions } from "@/hooks/use-confirm";
import type { FilerCapabilities } from "./filer-capabilities";
import {
  clearFilerUndoState,
  createFilerUndoState,
  pushFilerRedoEntry,
  pushFilerUndoEntry,
  restoreFilerRedoEntry,
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
let undoScopeGeneration = 0;
let historyBoundaryGeneration = 0;
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
  undoScopeGeneration += 1;
  historyBoundaryGeneration += 1;
  setUndoState(clearFilerUndoState());
}

// ─── Explorer 表示ハンドラ ───

/**
 * 通常の Files 一覧で削除中の項目を即時反映するための表示側ハンドラ。
 * レコードテーブルは専用の削除経路を持つため、
 * runFilerDelete からは通常の explorer ターゲットにだけ呼び出す。
 */
export interface ExplorerDeleteViewHandlers {
  /** 削除 API 完了まで表示だけ先に隠す（tombstone）。 */
  hide: (paths: string[]) => ExplorerDeleteViewOperation | void;
  /** 旧登録側との互換用。新規登録側は hide が返す operation を使う。 */
  rollback?: (paths: string[]) => void;
  /** 旧登録側との互換用。新規登録側は hide が返す operation を使う。 */
  commit?: (paths: string[]) => void;
  /** Undo で通常 Files の削除済み tombstone を復帰する。 */
  restore?: (paths: string[]) => void;
}

/**
 * 一つの削除操作が取得する opaque handle。
 *
 * モジュール singleton を API 完了後に再参照すると、Provider の差し替え
 * （principal / tab 境界）後に古い操作が新しい一覧を更新してしまうため、
 * hide の戻り値だけを通じて確定・ロールバックする。
 */
export interface ExplorerDeleteViewOperation {
  commit: (paths: string[]) => void;
  rollback: (paths: string[]) => void;
}

let explorerDeleteViewHandlers: ExplorerDeleteViewHandlers | null = null;

function createExplorerDeleteViewOperation(
  handlers: ExplorerDeleteViewHandlers | null,
  paths: string[],
): ExplorerDeleteViewOperation | null {
  if (!handlers || paths.length === 0) return null;
  const operation = handlers.hide(paths);
  if (
    operation &&
    typeof operation.commit === "function" &&
    typeof operation.rollback === "function"
  ) {
    return operation;
  }
  // Keep pre-handle registrations working while still capturing the exact
  // handler object at operation start (never re-read the module singleton).
  return {
    commit: (ownedPaths) => handlers.commit?.(ownedPaths),
    rollback: (ownedPaths) => handlers.rollback?.(ownedPaths),
  };
}

export function registerExplorerDeleteViewHandlers(
  handlers: ExplorerDeleteViewHandlers,
): () => void {
  explorerDeleteViewHandlers = handlers;
  return () => {
    if (explorerDeleteViewHandlers === handlers) {
      explorerDeleteViewHandlers = null;
    }
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

/**
 * 削除の UI 確定を refresh の完了に依存させない。refresh は権威データの
 * 再取得用なので、失敗しても削除 API の成否を上書きしない。
 */
function scheduleDeleteRefresh(refresh?: FilerRefresh): void {
  if (!refresh) return;
  void runRefresh(refresh).catch((error: unknown) => {
    console.error("[Files] 削除後の一覧更新に失敗しました:", error);
  });
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
  /** 削除成功時に呼ばれる（選択解除など）。成功したパスだけが渡される。 */
  onDeleted?: (deletedPaths?: string[]) => void;
}

/**
 * 選択項目をまとめて削除する。タブ種別に応じて explorer /
 * レコードテーブルへ振り分け、Undo 可能なものだけスタックへ積む。
 */
export async function runFilerDelete(
  params: RunFilerDeleteParams,
): Promise<boolean> {
  const { targets, capabilities, confirm, refresh, onDeleted } = params;
  if (!capabilities.canDelete || targets.length === 0) return false;
  const operationUndoScopeGeneration = undoScopeGeneration;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;

  // Capture singleton registrations before any confirmation/network await.
  // A principal/tab switch may replace the current Provider while this
  // operation is in flight; its opaque handle must remain bound to this
  // captured registration and never be applied to the replacement Provider.
  const deleteViewHandlers = explorerDeleteViewHandlers;

  const recordTables = targets.filter((t) => t.isRecordTable);
  const regularTargets = targets.filter((t) => !t.isRecordTable);

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

  // 確認がすべて完了した時点で、通常の Files だけを表示から先に隠す。
  // レコードテーブルにはこの tombstone を適用しない。
  const regularPaths = regularTargets.map((target) => target.path);
  const deleteViewOperation = createExplorerDeleteViewOperation(
    deleteViewHandlers,
    regularPaths,
  );

  // 途中で失敗しても、成功済み分は必ず Undo スタックへ積む
  const trashed: FilerDeleteUndoItem[] = [];
  const deletedRegularPaths: string[] = [];
  const successfulDeletedPathSet = new Set<string>();
  // 識別子が欠けていて削除できなかった .dbtable
  const skippedNames: string[] = [];
  let touchedRegular = false;

  const markDeleted = (path: string) => {
    successfulDeletedPathSet.add(path);
  };
  const successfulDeletedPaths = () => {
    const seen = new Set<string>();
    return targets.flatMap((target) => {
      if (!successfulDeletedPathSet.has(target.path) || seen.has(target.path)) {
        return [];
      }
      seen.add(target.path);
      return [target.path];
    });
  };
  const notifyDeleted = () => {
    const deletedPaths = successfulDeletedPaths();
    if (deletedPaths.length > 0) onDeleted?.(deletedPaths);
  };

  const commitUndoEntries = () => {
    if (
      operationUndoScopeGeneration !== undoScopeGeneration ||
      operationHistoryBoundaryGeneration !== historyBoundaryGeneration
    ) {
      return false;
    }
    const entries: FilerUndoEntry[] = [];
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
      touchedRegular = true;
      await deleteProjectRecordTable(table.projectId, table.tableId);
      markDeleted(target.path);
    }

    for (const target of regularTargets) {
      touchedRegular = true;
      const result = await explorerDelete(target.path);
      // API の成功直後に記録する。後続ターゲットが失敗してもこの項目は
      // commit 対象として確定し、未試行分だけ rollback する。
      deletedRegularPaths.push(target.path);
      markDeleted(target.path);
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
    if (deletedRegularPaths.length > 0) {
      deleteViewOperation?.commit(deletedRegularPaths);
    }
    notifyDeleted();
    const deletedRegularSet = new Set(deletedRegularPaths);
    const rollbackPaths = regularPaths.filter(
      (path) => !deletedRegularSet.has(path),
    );
    if (rollbackPaths.length > 0) {
      deleteViewOperation?.rollback(rollbackPaths);
    }
    if (touchedRegular) scheduleDeleteRefresh(refresh);
    toast.error(`削除に失敗しました: ${explorerErrorMessage(error)}`);
    return false;
  }

  const undoable = commitUndoEntries();
  if (deletedRegularPaths.length > 0) {
    deleteViewOperation?.commit(deletedRegularPaths);
  }
  notifyDeleted();
  if (touchedRegular) scheduleDeleteRefresh(refresh);

  const deletedCount = successfulDeletedPaths().length;
  if (deletedCount === 0) {
    toast.error(`削除できませんでした: ${skippedNames.join(", ")}`);
    return false;
  }

  const message =
    deletedCount === 1
      ? `「${targets.find((t) => successfulDeletedPathSet.has(t.path))?.name ?? targets[0].name}」を削除しました`
      : `${deletedCount}件を削除しました`;
  if (undoable) {
    // .dbtable は元に戻せないため、混在時は対象範囲を明示する
    const undoableCount = trashed.length;
    toast.success(message, {
      description:
        undoableCount < deletedCount
          ? `元に戻せるのは${undoableCount}件（レコードテーブルは復元できません）`
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
  const operationUndoScopeGeneration = undoScopeGeneration;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;

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
      if (
        operationUndoScopeGeneration === undoScopeGeneration &&
        operationHistoryBoundaryGeneration === historyBoundaryGeneration
      ) {
        setUndoState(
          pushFilerUndoEntry(undoState, {
            kind: "rename",
            path: committedPath,
            fromName: currentName,
            toName: committedName,
          }),
        );
      }
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
  const operationUndoScopeGeneration = undoScopeGeneration;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;

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

  if (
    renamed.length > 0 &&
    operationUndoScopeGeneration === undoScopeGeneration &&
    operationHistoryBoundaryGeneration === historyBoundaryGeneration
  ) {
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
  confirm?: ConfirmFn;
  /** Called only with source paths that completed successfully. */
  onTransferred?: (transferredPaths: string[]) => void;
}

interface FilerMoveRequest<T> {
  src: string;
  destDir: string;
  context: T;
}

interface FilerMoveSuccess<T> {
  request: FilerMoveRequest<T>;
  newPath: string;
  overwroteExisting: boolean;
  undoable: boolean;
  undoLimitation?: string;
}

interface FilerMoveFailure<T> {
  request: FilerMoveRequest<T>;
  error: unknown;
}

interface FilerMoveBatchResult<T> {
  succeeded: FilerMoveSuccess<T>[];
  skipped: FilerMoveRequest<T>[];
  overwriteBlocked: FilerMoveRequest<T>[];
  failures: FilerMoveFailure<T>[];
  attempted: boolean;
}

const FALLBACK_OVERWRITE_UNDO_LIMITATION =
  "上書き移動では移動先の以前の内容を保持しないため、通常のUndo/Redoでは元に戻せません";

function moveResultMetadata(
  result: ExplorerMoveResult,
  overwriteRequested: boolean,
): Pick<FilerMoveSuccess<unknown>, "overwroteExisting" | "undoable" | "undoLimitation"> {
  // The retry is sent only after a confirmed collision.  Prefer the backend's
  // explicit outcome because the collided target may disappear before retry;
  // use the request flag only for older endpoints without metadata.
  const overwroteExisting =
    typeof result.overwrote_existing === "boolean"
      ? result.overwrote_existing
      : typeof result.undoable === "boolean"
        ? !result.undoable
        : overwriteRequested;
  const undoable =
    typeof result.undoable === "boolean" ? result.undoable : !overwroteExisting;
  return {
    overwroteExisting,
    undoable,
    undoLimitation: overwroteExisting
      ? result.undo_limitation || FALLBACK_OVERWRITE_UNDO_LIMITATION
      : undefined,
  };
}

function isAlreadyExistsConflict(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const apiError = error as Error & { status?: unknown; code?: unknown };
  return apiError.status === 409 && apiError.code === "already_exists";
}

function normalizeFilerPath(path: string): string {
  return path.replace(/\\/g, "/").replace(/\/+$/, "");
}

function isMoveNoop(src: string, destDir: string): boolean {
  return (
    normalizeFilerPath(src) === normalizeFilerPath(destDir) ||
    normalizeFilerPath(parentDir(src)) === normalizeFilerPath(destDir)
  );
}

function overwriteConfirmOptions<T>(
  conflicts: FilerMoveRequest<T>[],
): ConfirmOptions {
  const fileList = conflicts
    .map(({ src }) => `• ${normalizeFilerPath(src)}`)
    .join("\n");
  return {
    title: "同名ファイルを一括確認",
    description: `${conflicts.length}件の同名ファイルが見つかりました。すべて上書きするか、すべてスキップするか選択してください。\n\n${fileList}`,
    confirmLabel: `すべて上書き (${conflicts.length}件)`,
    cancelLabel: "すべてスキップ",
    destructive: true,
  };
}

/** First try every move without overwrite, then confirm and retry only named conflicts. */
async function runMoveBatchWithOverwrite<T>(
  requests: FilerMoveRequest<T>[],
  confirm?: ConfirmFn,
  options: { allowOverwrite?: boolean } = {},
): Promise<FilerMoveBatchResult<T>> {
  const succeeded: FilerMoveSuccess<T>[] = [];
  const conflicts: FilerMoveRequest<T>[] = [];
  const skipped: FilerMoveRequest<T>[] = [];
  const overwriteBlocked: FilerMoveRequest<T>[] = [];
  const failures: FilerMoveFailure<T>[] = [];
  const allowOverwrite = options.allowOverwrite !== false;

  for (const request of requests) {
    try {
      const result = await explorerMove(request.src, request.destDir);
      succeeded.push({
        request,
        newPath: result.new_path || request.src,
        ...moveResultMetadata(result, false),
      });
    } catch (error) {
      if (isAlreadyExistsConflict(error)) {
        conflicts.push(request);
      } else {
        failures.push({ request, error });
      }
    }
  }

  if (conflicts.length === 0) {
    return {
      succeeded,
      skipped,
      overwriteBlocked,
      failures,
      attempted: requests.length > 0,
    };
  }

  if (!allowOverwrite) {
    overwriteBlocked.push(...conflicts);
    skipped.push(...conflicts);
    return {
      succeeded,
      skipped,
      overwriteBlocked,
      failures,
      attempted: requests.length > 0,
    };
  }

  let approved = false;
  if (confirm) {
    try {
      approved = await confirm(overwriteConfirmOptions(conflicts));
    } catch (error) {
      failures.push({ request: conflicts[0], error });
    }
  }

  if (!approved) {
    skipped.push(...conflicts);
    return {
      succeeded,
      skipped,
      overwriteBlocked,
      failures,
      attempted: requests.length > 0,
    };
  }

  for (const request of conflicts) {
    try {
      const result = await explorerMove(request.src, request.destDir, {
        overwrite: true,
      });
      succeeded.push({
        request,
        newPath: result.new_path || request.src,
        ...moveResultMetadata(result, true),
      });
    } catch (error) {
      failures.push({ request, error });
    }
  }

  return {
    succeeded,
    skipped,
    overwriteBlocked,
    failures,
    attempted: requests.length > 0,
  };
}

interface FilerMoveHistoryResult {
  inverse: FilerUndoEntry | null;
  remaining: FilerUndoEntry | null;
  skippedCount: number;
  overwriteBlockedCount: number;
  failures: unknown[];
  succeededCount: number;
  attempted: boolean;
}

async function applyFilerMoveHistoryEntry(
  entry: Extract<FilerUndoEntry, { kind: "move" }>,
  confirm?: ConfirmFn,
): Promise<FilerMoveHistoryResult> {
  const requests = entry.entries.map((item) => ({
    src: item.to,
    destDir: parentDir(item.from),
    context: item,
  }));
  const alreadyAtDestination = requests.filter((request) =>
    isMoveNoop(request.src, request.destDir),
  );
  const pending = requests.filter(
    (request) => !isMoveNoop(request.src, request.destDir),
  );
  const result = await runMoveBatchWithOverwrite(pending, confirm, {
    // Undo/Redo has no retained destination backup.  Refuse an overwrite so
    // a conflict cannot be reported as a fully restored history entry.
    allowOverwrite: false,
  });
  const reversibleSucceeded = result.succeeded.filter(
    ({ undoable }) => undoable,
  );
  const succeededItems = new Set(
    [
      ...alreadyAtDestination.map(({ context }) => context),
      ...reversibleSucceeded.map(({ request }) => request.context),
    ],
  );
  const inverseEntries = reversibleSucceeded
    .filter(({ request, newPath }) => newPath !== request.src)
    .map(({ request, newPath }) => ({ from: request.src, to: newPath }));
  const remainingEntries = entry.entries.filter(
    (item) => !succeededItems.has(item),
  );

  return {
    inverse:
      inverseEntries.length > 0
        ? { kind: "move", entries: inverseEntries }
        : null,
    remaining:
      remainingEntries.length > 0
        ? { kind: "move", entries: remainingEntries }
        : null,
    skippedCount: result.skipped.length,
    overwriteBlockedCount:
      result.overwriteBlocked.length +
      result.succeeded.filter(({ undoable }) => !undoable).length,
    failures: result.failures.map(({ error }) => error),
    succeededCount: succeededItems.size,
    attempted: result.attempted,
  };
}

/** 切り取り＆貼り付け / D&D / コピー貼り付けの共通実装。 */
export async function runFilerTransfer(
  params: RunFilerTransferParams,
): Promise<boolean> {
  const {
    paths,
    destDir,
    operation,
    capabilities,
    refresh,
    confirm,
    onTransferred,
  } = params;
  const operationUndoScopeGeneration = undoScopeGeneration;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;
  if (paths.length === 0) return false;
  if (operation === "move" && !capabilities.canMove) return false;
  if (operation === "copy" && !capabilities.canCopy) return false;

  if (operation === "copy") {
    const copiedPaths: string[] = [];
    let attempted = false;
    try {
      for (const src of paths) {
        // Dropping a folder onto itself is ignored.
        if (src === destDir) continue;
        attempted = true;
        await explorerCopy(src, destDir);
        copiedPaths.push(src);
      }
    } catch (error) {
      if (attempted) await runRefresh(refresh);
      toast.error(`コピーに失敗しました: ${explorerErrorMessage(error)}`);
      return false;
    }

    if (copiedPaths.length > 0) onTransferred?.(copiedPaths);
    if (attempted) await runRefresh(refresh);
    return copiedPaths.length > 0;
  }

  const moved: { from: string; to: string }[] = [];
  const overwrittenDestinations = new Set<string>();
  const transferredPaths: string[] = [];
  const overwriteLimitations: string[] = [];
  const failures: unknown[] = [];
  const requests = paths
    .filter((src) => !isMoveNoop(src, destDir))
    .map((src) => ({ src, destDir, context: src }));
  const result = await runMoveBatchWithOverwrite(requests, confirm);
  failures.push(...result.failures.map(({ error }) => error));
  for (const {
    request,
    newPath,
    overwroteExisting,
    undoable,
    undoLimitation,
  } of result.succeeded) {
    // A server-confirmed no-op is not a clipboard transfer or an Undo entry.
    if (newPath === request.src) continue;
    transferredPaths.push(request.src);
    if (undoable) {
      moved.push({ from: request.src, to: newPath });
    } else {
      overwriteLimitations.push(
        undoLimitation || FALLBACK_OVERWRITE_UNDO_LIMITATION,
      );
      if (overwroteExisting) {
        overwrittenDestinations.add(normalizeFilerPath(newPath));
      }
    }
  }

  // A later successful overwrite in this batch may replace a path recorded by
  // an earlier reversible move.  That earlier inverse would then move the
  // replacement content, so retain only moves to other destinations.
  const reversibleMoves = moved.filter(
    ({ to }) => !overwrittenDestinations.has(normalizeFilerPath(to)),
  );

  if (operationUndoScopeGeneration === undoScopeGeneration) {
    // A successful overwrite has no inverse that can restore the previous
    // destination content.  Older Undo/Redo entries may now point at that
    // replaced path, so treat it as a history boundary even when another
    // overwrite advanced the boundary while this operation was in flight.
    const boundaryWasCurrent =
      operationHistoryBoundaryGeneration === historyBoundaryGeneration;
    const historyBase =
      overwriteLimitations.length > 0 ? clearFilerUndoState() : undoState;
    if (overwriteLimitations.length > 0) {
      historyBoundaryGeneration += 1;
    }
    // Reversible moves from this same batch remain safe only when the
    // operation started in the current history boundary.  A stale operation
    // must not re-add entries after clearing newer history.
    if (reversibleMoves.length > 0 && boundaryWasCurrent) {
      setUndoState(
        pushFilerUndoEntry(historyBase, {
          kind: "move",
          entries: reversibleMoves,
        }),
      );
    } else if (historyBase !== undoState) {
      setUndoState(historyBase);
    }
  }
  if (transferredPaths.length > 0) onTransferred?.(transferredPaths);
  if (result.attempted) await runRefresh(refresh);

  if (overwriteLimitations.length > 0) {
    toast.info(
      `${overwriteLimitations.length}件を上書き移動しました。${overwriteLimitations[0]}`,
    );
  }

  if (failures.length > 0) {
    const completed = transferredPaths.length;
    const completedMessage = completed > 0 ? `${completed}件は移動しました。` : "";
    const otherFailureCount = failures.length - 1;
    const suffix = otherFailureCount > 0 ? `（ほか${otherFailureCount}件）` : "";
    const skippedMessage =
      result.skipped.length > 0
        ? ` ${result.skipped.length}件はスキップしました。`
        : "";
    toast.error(
      `${completedMessage}移動に失敗しました${suffix}: ${explorerErrorMessage(failures[0])}${skippedMessage}`,
    );
  } else if (result.skipped.length > 0) {
    toast.info(
      `${transferredPaths.length > 0 ? `${transferredPaths.length}件を移動しました。` : ""}${result.skipped.length}件をスキップしました`,
    );
  }

  return transferredPaths.length > 0;
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
    const deleteViewHandlers = explorerDeleteViewHandlers;
    if (direction === "undo") {
      const restored: FilerDeleteUndoItem[] = [];
      for (const item of entry.entries) {
        const result = await explorerRestore(item.token, item.originalPath);
        restored.push({
          token: item.token,
          originalPath: result.restored_path || item.originalPath,
        });
        // Call the handler captured at operation start.  The Provider's
        // scope-guarded callback turns this into a no-op after a boundary.
        deleteViewHandlers?.restore?.([item.originalPath]);
      }
      return { kind: "delete", entries: restored };
    }
    const regularPaths = entry.entries.map((item) => item.originalPath);
    const deleteViewOperation = createExplorerDeleteViewOperation(
      deleteViewHandlers,
      regularPaths,
    );
    const deleted: FilerDeleteUndoItem[] = [];
    const successfulPaths: string[] = [];
    try {
      for (const item of entry.entries) {
        const result = await explorerDelete(item.originalPath);
        if (!result.trash?.token) {
          throw new Error("削除の取り消し情報を取得できませんでした");
        }
        deleted.push({
          token: result.trash.token,
          originalPath: result.trash.original_path || item.originalPath,
        });
        successfulPaths.push(item.originalPath);
      }
    } catch (error) {
      const committedPaths = [...successfulPaths];
      if (committedPaths.length > 0) {
        deleteViewOperation?.commit(committedPaths);
      }
      const committedSet = new Set(committedPaths);
      const rollbackPaths = regularPaths.filter((path) => !committedSet.has(path));
      if (rollbackPaths.length > 0) {
        deleteViewOperation?.rollback(rollbackPaths);
      }
      throw error;
    }
    deleteViewOperation?.commit(successfulPaths);
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

  // Move entries are intercepted by the partial-safe, conflict-aware path
  // before reaching this generic history handler.
  throw new Error("Move history requires the conflict-aware handler");
}

export function canUndoFiler(): boolean {
  return undoState.undo.length > 0;
}

export function canRedoFiler(): boolean {
  return undoState.redo.length > 0;
}

async function applyMoveHistoryIfNeeded(
  direction: "undo" | "redo",
  refresh?: FilerRefresh,
  confirm?: ConfirmFn,
  capabilities?: FilerCapabilities,
): Promise<boolean | null> {
  const pendingEntry =
    direction === "undo" ? undoState.undo.at(-1) : undoState.redo.at(-1);
  if (pendingEntry?.kind !== "move") return null;
  if (!capabilities?.canMove) return false;

  const taken =
    direction === "undo"
      ? takeFilerUndoEntry(undoState)
      : takeFilerRedoEntry(undoState);
  if (!taken) return false;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;
  setUndoState(taken.state);

  try {
    const applied = await applyFilerMoveHistoryEntry(
      taken.entry as Extract<FilerUndoEntry, { kind: "move" }>,
      confirm,
    );
    if (operationHistoryBoundaryGeneration === historyBoundaryGeneration) {
      let nextState = getFilerUndoSnapshot();
      if (direction === "undo") {
        if (applied.inverse) {
          nextState = pushFilerRedoEntry(nextState, applied.inverse);
        }
        if (applied.remaining) {
          nextState = restoreFilerUndoEntry(nextState, applied.remaining);
        }
      } else {
        if (applied.inverse) {
          nextState = restoreFilerUndoEntry(nextState, applied.inverse);
        }
        if (applied.remaining) {
          nextState = restoreFilerRedoEntry(nextState, applied.remaining);
        }
      }
      setUndoState(nextState);
    }

    if (applied.attempted || applied.succeededCount > 0) {
      await runRefresh(refresh);
    }

    const verb = direction === "undo" ? "元に戻す" : "やり直す";
    if (applied.failures.length > 0) {
      const completedMessage =
        applied.succeededCount > 0
          ? `${applied.succeededCount}件は処理しました。`
          : "";
      const skippedMessage =
        applied.skippedCount > 0
          ? ` ${applied.skippedCount}件はスキップしました。`
          : "";
      const otherFailureCount = applied.failures.length - 1;
      const suffix = otherFailureCount > 0 ? `（ほか${otherFailureCount}件）` : "";
      toast.error(
        `${verb}に失敗しました。${completedMessage}${explorerErrorMessage(applied.failures[0])}${suffix}${skippedMessage}`,
      );
      if (applied.overwriteBlockedCount > 0) {
        toast.info(
          `${verb}できない項目があります。${applied.overwriteBlockedCount}件は上書きが必要なため、既存項目を失う操作を避けて${verb}を停止しました`,
        );
      }
    } else if (applied.overwriteBlockedCount > 0) {
      toast.info(
        `${verb}できない項目があります。${applied.overwriteBlockedCount}件は上書きが必要なため、既存項目を失う操作を避けて${verb}を停止しました`,
      );
    } else if (applied.skippedCount > 0) {
      toast.info(
        `${applied.succeededCount > 0 ? `${applied.succeededCount}件を処理しました。` : ""}${applied.skippedCount}件をスキップしました`,
      );
    } else {
      toast.success(direction === "undo" ? "元に戻しました" : "やり直しました");
    }
    return applied.succeededCount > 0;
  } catch (error) {
    if (operationHistoryBoundaryGeneration === historyBoundaryGeneration) {
      let nextState = getFilerUndoSnapshot();
      nextState =
        direction === "undo"
          ? restoreFilerUndoEntry(nextState, taken.entry)
          : restoreFilerRedoEntry(nextState, taken.entry);
      setUndoState(nextState);
    }
    await runRefresh(refresh);
    toast.error(
      `${direction === "undo" ? "元に戻せませんでした" : "やり直せませんでした"}: ${explorerErrorMessage(error)}`,
    );
    return false;
  }
}

export async function undoFilerOperation(
  refresh?: FilerRefresh,
  confirm?: ConfirmFn,
  capabilities?: FilerCapabilities,
): Promise<boolean> {
  const moveResult = await applyMoveHistoryIfNeeded(
    "undo",
    refresh,
    confirm,
    capabilities,
  );
  if (moveResult !== null) return moveResult;

  const taken = takeFilerUndoEntry(undoState);
  if (!taken) return false;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;
  setUndoState(taken.state);
  try {
    const inverse = await applyFilerUndoEntry(taken.entry, "undo");
    if (operationHistoryBoundaryGeneration === historyBoundaryGeneration) {
      setUndoState(pushFilerRedoEntry(getFilerUndoSnapshot(), inverse));
    }
    await runRefresh(refresh);
    toast.success("元に戻しました");
    return true;
  } catch (error) {
    // 失敗したエントリは復元不能とみなし、スタックから捨てる
    await runRefresh(refresh);
    toast.error(`元に戻せませんでした: ${explorerErrorMessage(error)}`);
    return false;
  }
}

export async function redoFilerOperation(
  refresh?: FilerRefresh,
  confirm?: ConfirmFn,
  capabilities?: FilerCapabilities,
): Promise<boolean> {
  const moveResult = await applyMoveHistoryIfNeeded(
    "redo",
    refresh,
    confirm,
    capabilities,
  );
  if (moveResult !== null) return moveResult;

  const taken = takeFilerRedoEntry(undoState);
  if (!taken) return false;
  const operationHistoryBoundaryGeneration = historyBoundaryGeneration;
  setUndoState(taken.state);
  try {
    const inverse = await applyFilerUndoEntry(taken.entry, "redo");
    if (operationHistoryBoundaryGeneration === historyBoundaryGeneration) {
      setUndoState(restoreFilerUndoEntry(getFilerUndoSnapshot(), inverse));
    }
    await runRefresh(refresh);
    toast.success("やり直しました");
    return true;
  } catch (error) {
    await runRefresh(refresh);
    toast.error(`やり直せませんでした: ${explorerErrorMessage(error)}`);
    return false;
  }
}
