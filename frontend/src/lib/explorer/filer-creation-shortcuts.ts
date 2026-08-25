/**
 * Files画面で新規項目を作成するショートカットの判定。
 *
 * CmdはmacOS、CtrlはWindows/Linuxのプライマリ修飾キーとして扱う。
 * `code` も確認することで、キーボード配列によって `key` の値が変わる
 * 場合でも物理キーの Ctrl/Cmd+N を認識できる。
 */
export type FilerCreationShortcutAction = "folder" | "text-file";

export interface FilerCreationShortcutEvent {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

export interface FilerCreationShortcutOptions {
  /** 作成権限があるか。権限がなくてもブラウザ既定動作は抑止する。 */
  canCreate: boolean;
  /** 新規フォルダ / 新規テキストファイルのいずれかが開いているか。 */
  creationDialogOpen: boolean;
  /** 入力欄・エディタにフォーカスしているか。 */
  inputFocused: boolean;
}

export interface FilerCreationShortcutResolution {
  /** Ctrl/Cmd+N系のイベントだったか。 */
  matched: boolean;
  /** ブラウザの新規ウィンドウ等の既定動作を抑止すべきか。 */
  preventDefault: boolean;
  /** 実際に開くモーダル。抑止のみの場合はnull。 */
  action: FilerCreationShortcutAction | null;
}

export function getFilerCreationShortcutAction(
  event: FilerCreationShortcutEvent,
): FilerCreationShortcutAction | null {
  const primaryModifier = event.ctrlKey || event.metaKey;
  const isNKey = event.key.toLowerCase() === "n" || event.code === "KeyN";
  if (!primaryModifier || event.altKey || !isNKey) return null;
  return event.shiftKey ? "text-file" : "folder";
}

/**
 * Ctrl/Cmd+N系ショートカットのイベント消費とモーダル分岐を一元化する。
 * 権限なし・入力欄・作成モーダル表示中でも `preventDefault` は維持し、
 * ブラウザの新規ウィンドウ操作だけをFiles画面側で奪う。
 */
export function resolveFilerCreationShortcut(
  event: FilerCreationShortcutEvent,
  options: FilerCreationShortcutOptions,
): FilerCreationShortcutResolution {
  const action = getFilerCreationShortcutAction(event);
  if (!action) {
    return { matched: false, preventDefault: false, action: null };
  }
  return {
    matched: true,
    preventDefault: true,
    action:
      options.canCreate && !options.creationDialogOpen && !options.inputFocused
        ? action
        : null,
  };
}
