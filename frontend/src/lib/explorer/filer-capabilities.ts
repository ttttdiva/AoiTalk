/**
 * ファイラーのタブ / パス種別ごとの操作可否フラグ（純粋ロジック）。
 *
 * これまで削除・リネーム・移動の可否判定が page.tsx / file-context-menu.tsx に
 * それぞれ書かれていた。判定はここに一元化する。
 */

export type FilerTargetKind =
  | "workspace"
  | "user"
  | "remote"
  | "absolute";

export interface FilerCapabilities {
  /** 削除できるか */
  canDelete: boolean;
  /** リネームできるか */
  canRename: boolean;
  /** 移動（切り取り＆貼り付け / D&D）できるか */
  canMove: boolean;
  /** コピー（複製）できるか */
  canCopy: boolean;
  /** 新規フォルダ / 新規ファイル作成ができるか */
  canCreate: boolean;
  /** 削除前に確認ダイアログが必須か（元に戻せない削除） */
  deleteNeedsConfirm: boolean;
  /** 削除を Undo スタックへ積めるか */
  deleteUndoable: boolean;
}

export interface FilerCapabilityOptions {
  /** 管理者かどうか。 */
  isAdmin?: boolean;
}

export function isRemoteFilerPath(path: string): boolean {
  return path.startsWith("remote://");
}

const CAPABILITIES: Record<FilerTargetKind, FilerCapabilities> = {
  // ワークスペース / ユーザー領域: 全操作可。削除はゴミ箱経由で Undo 可。
  workspace: {
    canDelete: true,
    canRename: true,
    canMove: true,
    canCopy: true,
    canCreate: true,
    deleteNeedsConfirm: false,
    deleteUndoable: true,
  },
  user: {
    canDelete: true,
    canRename: true,
    canMove: true,
    canCopy: true,
    canCreate: true,
    deleteNeedsConfirm: false,
    deleteUndoable: true,
  },
  // リモートワークスペース: 現在のAPIは読み取り専用。
  remote: {
    canDelete: false,
    canRename: false,
    canMove: false,
    canCopy: false,
    canCreate: false,
    deleteNeedsConfirm: false,
    deleteUndoable: false,
  },
  // 管理者のローカル絶対パス: APIが対応する操作を許可する。
  // ワークスペース外の削除は物理削除になるため確認必須・Undo不可。
  absolute: {
    canDelete: true,
    canRename: true,
    canMove: true,
    canCopy: true,
    canCreate: true,
    deleteNeedsConfirm: true,
    deleteUndoable: false,
  },
};

export function resolveFilerTargetKind(params: {
  filerTab?: string | null;
  isAbsoluteFilerPath?: boolean;
  isRemoteWorkspace?: boolean;
}): FilerTargetKind {
  if (params.isRemoteWorkspace) return "remote";
  if (params.isAbsoluteFilerPath) return "absolute";
  if (params.filerTab === "user") return "user";
  return "workspace";
}

export function filerCapabilities(
  kind: FilerTargetKind,
  options?: FilerCapabilityOptions,
): FilerCapabilities {
  const base = CAPABILITIES[kind];
  // 絶対パスは管理者だけがバックエンドの境界チェックを通過できる。
  if (kind === "absolute" && !options?.isAdmin) {
    return {
      ...base,
      canDelete: false,
      canRename: false,
      canMove: false,
      canCopy: false,
      canCreate: false,
      deleteNeedsConfirm: false,
    };
  }
  return base;
}

export function resolveFilerCapabilities(
  params: {
    filerTab?: string | null;
    isAbsoluteFilerPath?: boolean;
    isRemoteWorkspace?: boolean;
  } & FilerCapabilityOptions,
): FilerCapabilities {
  return filerCapabilities(resolveFilerTargetKind(params), {
    isAdmin: params.isAdmin,
  });
}
