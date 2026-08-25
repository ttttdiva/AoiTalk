/**
 * ファイラーの Undo / Redo スタック（純粋ロジック）。
 *
 * - undo / redo それぞれ最大 {@link FILER_UNDO_LIMIT} 件を保持する。
 * - 新規操作を積むと redo スタックは破棄される。
 * - undo / redo の実行結果でエントリ内容が変わる（削除トークンの再発行など）ため、
 *   「取り出す」→「実行側が更新したエントリを反対側へ積み直す」の2段構成にしている。
 *
 * 副作用（API 呼び出し・トースト）は filer-operations.ts 側が持つ。
 */

/** undo / redo それぞれの保持件数上限 */
export const FILER_UNDO_LIMIT = 3;

/** 削除（explorer ゴミ箱トークン方式）1件分 */
export interface FilerDeleteUndoItem {
  /** バックエンドが発行した復元トークン */
  token: string;
  /** 削除前のパス */
  originalPath: string;
}

/** 移動1件分 */
export interface FilerMoveUndoItem {
  /** 移動前のフルパス */
  from: string;
  /** 移動後のフルパス */
  to: string;
}

/** 名前の一括置換で変更した1件分 */
export interface FilerRenameUndoItem {
  /** 置換前のフルパス */
  from: string;
  /** 置換後のフルパス */
  to: string;
}

export type FilerUndoEntry =
  | { kind: "delete"; entries: FilerDeleteUndoItem[] }
  | { kind: "rename"; path: string; fromName: string; toName: string }
  | { kind: "rename-batch"; entries: FilerRenameUndoItem[] }
  | { kind: "move"; entries: FilerMoveUndoItem[] }
  | { kind: "hydrus-delete"; fileIds: number[] };

export interface FilerUndoState {
  undo: FilerUndoEntry[];
  redo: FilerUndoEntry[];
}

export function createFilerUndoState(): FilerUndoState {
  return { undo: [], redo: [] };
}

function capped(entries: FilerUndoEntry[]): FilerUndoEntry[] {
  return entries.length <= FILER_UNDO_LIMIT
    ? entries
    : entries.slice(entries.length - FILER_UNDO_LIMIT);
}

/** 新規操作を undo スタックへ積む（redo スタックは破棄）。 */
export function pushFilerUndoEntry(
  state: FilerUndoState,
  entry: FilerUndoEntry,
): FilerUndoState {
  return { undo: capped([...state.undo, entry]), redo: [] };
}

/** undo 実行対象を取り出す（スタックからは除去済み）。 */
export function takeFilerUndoEntry(
  state: FilerUndoState,
): { state: FilerUndoState; entry: FilerUndoEntry } | null {
  const entry = state.undo.at(-1);
  if (!entry) return null;
  return { state: { undo: state.undo.slice(0, -1), redo: state.redo }, entry };
}

/** redo 実行対象を取り出す（スタックからは除去済み）。 */
export function takeFilerRedoEntry(
  state: FilerUndoState,
): { state: FilerUndoState; entry: FilerUndoEntry } | null {
  const entry = state.redo.at(-1);
  if (!entry) return null;
  return { state: { undo: state.undo, redo: state.redo.slice(0, -1) }, entry };
}

/** undo 成功後、逆操作エントリを redo スタックへ積む。 */
export function pushFilerRedoEntry(
  state: FilerUndoState,
  entry: FilerUndoEntry,
): FilerUndoState {
  return { undo: state.undo, redo: capped([...state.redo, entry]) };
}

/** redo 成功後、逆操作エントリを undo スタックへ戻す（redo は破棄しない）。 */
export function restoreFilerUndoEntry(
  state: FilerUndoState,
  entry: FilerUndoEntry,
): FilerUndoState {
  return { undo: capped([...state.undo, entry]), redo: state.redo };
}

/** タブ切り替えなどでスタックを全消去する。 */
export function clearFilerUndoState(): FilerUndoState {
  return createFilerUndoState();
}
