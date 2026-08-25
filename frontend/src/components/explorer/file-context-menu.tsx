"use client";

import { useEffect } from "react";
import { createPortal } from "react-dom";
import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { useExplorer } from "@/contexts/explorer-context";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import {
  explorerDownloadPaths,
  explorerErrorMessage,
  explorerSetFolderThumbnail,
  explorerClearFolderThumbnail,
  type ExplorerDirectory,
  type ExplorerFile,
} from "@/lib/explorer-api";
import {
  useFilerOperations,
  useFilerUndoState,
} from "@/hooks/use-filer-operations";
import { toFilerDeleteTarget } from "@/lib/explorer/filer-operations";
import { isHfPath } from "@/lib/hf/virtual-path";
import { isHydrusPath } from "@/lib/hydrus/virtual-path";
import { isRecordTableFile } from "@/lib/record-tables-api";
import {
  FolderOpen,
  Download,
  Pencil,
  Copy,
  Scissors,
  ClipboardPaste,
  Trash2,
  Info,
  FolderPlus,
  FilePlus,
  RefreshCw,
  Image as ImageIcon,
  ImageOff,
  Undo2,
  Redo2,
} from "lucide-react";
import { toast } from "sonner";

// 画像ファイル拡張子（フォルダサムネに設定可能）
const IMAGE_EXTS = new Set([
  ".jpg",
  ".jpeg",
  ".png",
  ".gif",
  ".webp",
  ".bmp",
  ".svg",
]);

// テキストファイル拡張子（エディタで開けるもの）
const TEXT_EXTS = new Set([
  ".txt",
  ".md",
  ".json",
  ".yaml",
  ".yml",
  ".xml",
  ".csv",
  ".log",
  ".py",
  ".js",
  ".ts",
  ".jsx",
  ".tsx",
  ".html",
  ".css",
  ".sql",
  ".ini",
  ".cfg",
  ".bat",
  ".cmd",
  ".sh",
  ".ps1",
  ".vbs",
]);

interface FileContextMenuProps {
  item: (ExplorerDirectory | ExplorerFile) | null;
  position: { x: number; y: number } | null;
  onClose: () => void;
  onRename: (item: ExplorerDirectory | ExplorerFile) => void;
  onProperties: (item: ExplorerDirectory | ExplorerFile) => void;
  onOpen?: (item: ExplorerDirectory | ExplorerFile) => void;
  onNewFolder?: () => void;
  onNewTextFile?: () => void;
  activeSelectionPaths?: string[];
}

function isDir(item: ExplorerDirectory | ExplorerFile): boolean {
  return !("type" in item);
}

export function FileContextMenu({
  item,
  position,
  onClose,
  onRename,
  onProperties,
  onOpen,
  onNewFolder,
  onNewTextFile,
  activeSelectionPaths,
}: FileContextMenuProps) {
  const {
    currentPath,
    browseData,
    navigate,
    refresh,
    selectedItems,
    clearSelection,
    clipboard,
    setClipboard,
    isRemoteWorkspace,
    capabilities,
  } = useExplorer();
  const { deleteTargets, transfer, undo, redo } = useFilerOperations({
    capabilities,
    refresh,
  });
  const { canUndo, canRedo } = useFilerUndoState();
  const { ref: menuRef, style: menuStyle } = useContextMenuPosition(position, {
    fallbackWidth: item ? 180 : 200,
    fallbackHeight: item ? 320 : 160,
  });

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    if (position) {
      document.addEventListener("mousedown", handleClick);
    }
    return () => document.removeEventListener("mousedown", handleClick);
  }, [position, onClose, menuRef]);

  if (!position || typeof document === "undefined") return null;

  // ── 背景右クリック（アイテムなし）──
  if (!item) {
    const canPasteHere =
      clipboard &&
      (clipboard.operation === "cut"
        ? capabilities.canMove
        : capabilities.canCopy);
    const handlePasteBackground = async () => {
      if (!clipboard) return;
      await transfer({
        paths: clipboard.paths,
        destDir: currentPath,
        operation: clipboard.operation === "cut" ? "move" : "copy",
        onTransferred: () => {
          if (clipboard.operation === "cut") setClipboard(null);
        },
      });
      onClose();
    };

    const backgroundItems = [
      ...(capabilities.canCreate
        ? [
            {
              icon: FolderPlus,
              label: "新規フォルダ (F7)",
              mnemonic: "N",
              action: () => {
                onNewFolder?.();
                onClose();
              },
            },
            {
              icon: FilePlus,
              label: "新規テキストファイル (Shift+F7)",
              mnemonic: "T",
              action: () => {
                onNewTextFile?.();
                onClose();
              },
            },
          ]
        : []),
      ...(canPasteHere
        ? [
            {
              icon: ClipboardPaste,
              label: "貼り付け",
              mnemonic: "P",
              action: handlePasteBackground,
            },
          ]
        : []),
      ...(canUndo
        ? [
            {
              icon: Undo2,
              label: "元に戻す (Ctrl+Z)",
              mnemonic: "Z",
              action: () => {
                void undo();
                onClose();
              },
            },
          ]
        : []),
      ...(canRedo
        ? [
            {
              icon: Redo2,
              label: "やり直す (Ctrl+Y)",
              mnemonic: "Y",
              action: () => {
                void redo();
                onClose();
              },
            },
          ]
        : []),
      {
        icon: RefreshCw,
        label: "更新",
        mnemonic: "R",
        action: () => {
          refresh();
          onClose();
        },
      },
    ];

    return createPortal(
      <MenuMnemonicSurface
        ref={menuRef}
        className="fixed z-50 min-w-48 rounded-md border border-border bg-popover p-1 shadow-lg"
        style={menuStyle}
        onContextMenu={(e) => e.preventDefault()}
      >
        {backgroundItems.map((mi) => (
          <MenuMnemonicButton
            key={mi.label}
            mnemonic={mi.mnemonic}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-muted"
            onClick={mi.action}
          >
            <mi.icon className="size-3.5" />
            {mi.label}
          </MenuMnemonicButton>
        ))}
      </MenuMnemonicSurface>,
      document.body,
    );
  }

  // ── アイテム右クリック ──
  const isDirectory = isDir(item);
  const isRecordTable = !isDirectory && isRecordTableFile(item as ExplorerFile);
  const activePaths = selectedItems.has(item.path)
    ? (activeSelectionPaths ?? Array.from(selectedItems))
    : [item.path];
  const findItemByPath = (path: string) =>
    browseData
      ? (
          [...browseData.directories, ...browseData.files] as Array<
            ExplorerDirectory | ExplorerFile
          >
        ).find((candidate) => candidate.path === path)
      : null;
  const activeRegularPaths = activePaths.filter((path) => {
    const target = findItemByPath(path);
    return !target || !("type" in target) || !isRecordTableFile(target);
  });
  const isTextFile =
    !isDirectory && TEXT_EXTS.has((item as ExplorerFile).extension || "");
  const itemExt = !isDirectory
    ? ((item as ExplorerFile).extension || "").toLowerCase()
    : "";
  const normalizedExt = itemExt.startsWith(".") ? itemExt : `.${itemExt}`;
  const isImageFile = !isDirectory && IMAGE_EXTS.has(normalizedExt);

  // HF / Hydrus 仮想パスはフォルダサムネイル設定の対象外（.folder-thumb 書き込み不可）
  const isVirtualPath =
    isHfPath(item.path) || isHydrusPath(item.path) || isRecordTable;
  const canSetFolderThumb =
    isImageFile && !isVirtualPath && !isRemoteWorkspace && !!currentPath;
  const canClearFolderThumb =
    isDirectory &&
    !isVirtualPath &&
    !isRemoteWorkspace &&
    !!(item as ExplorerDirectory).has_explicit_thumb;

  const handleSetFolderThumb = async () => {
    try {
      await explorerSetFolderThumbnail(currentPath, item.path);
      refresh();
    } catch {
      // 失敗は握りつぶし（サーバ側 400 はログに出る）
    }
    onClose();
  };

  const handleClearFolderThumb = async () => {
    try {
      await explorerClearFolderThumbnail(item.path);
      refresh();
    } catch {
      // 失敗は握りつぶし
    }
    onClose();
  };

  const handleOpen = () => {
    if (isDirectory) {
      navigate(item.path);
    } else if (onOpen) {
      onOpen(item);
    }
    onClose();
  };

  const handleDownload = async () => {
    if (isRecordTable) return;
    if (activeRegularPaths.length === 0) {
      onClose();
      return;
    }

    try {
      await explorerDownloadPaths(activeRegularPaths);
      toast.success(
        activeRegularPaths.length === 1
          ? "ダウンロードを開始しました"
          : `${activeRegularPaths.length}件をダウンロードします`,
      );
    } catch (error) {
      toast.error(`ダウンロードに失敗しました: ${explorerErrorMessage(error)}`);
    }
    onClose();
  };

  const handleCopy = () => {
    if (activeRegularPaths.length > 0) {
      setClipboard({ paths: activeRegularPaths, operation: "copy" });
    }
    onClose();
  };

  const handleCut = () => {
    if (activeRegularPaths.length > 0) {
      setClipboard({ paths: activeRegularPaths, operation: "cut" });
    }
    onClose();
  };

  const handlePaste = async () => {
    if (!clipboard) return;
    const dest = isDirectory ? item.path : currentPath;
    await transfer({
      paths: clipboard.paths,
      destDir: dest,
      operation: clipboard.operation === "cut" ? "move" : "copy",
      onTransferred: () => {
        if (clipboard.operation === "cut") setClipboard(null);
      },
    });
    onClose();
  };

  const handleDelete = async () => {
    // 実行・確認・Undo登録は filer-operations 側へ集約
    const targets = activePaths.map((path) => {
      const target = findItemByPath(path) ?? (path === item.path ? item : null);
      if (target) return toFilerDeleteTarget(target);
      // 一覧から見つからない場合のみ最低限の情報で組み立てる
      return {
        path,
        name: path.split(/[/\\|]/).pop() || path,
        isDirectory: false,
      };
    });
    await deleteTargets(targets, { onDeleted: clearSelection });
    onClose();
  };

  const canPaste =
    clipboard &&
    (clipboard.operation === "cut" ? capabilities.canMove : capabilities.canCopy);

  const menuItems = [
    // 開く: フォルダは常に、ファイルはonOpenがある場合
    ...(isDirectory || (onOpen && (isTextFile || isRecordTable))
      ? [{ icon: FolderOpen, label: "開く", mnemonic: "O", action: handleOpen }]
      : []),
    // ダウンロード: 実体パスのファイル / フォルダ
    ...(!isRecordTable && !isVirtualPath
      ? [
          {
            icon: Download,
            label: "ダウンロード",
            mnemonic: "L",
            action: handleDownload,
          },
        ]
      : []),
    // 画像を親フォルダの代表サムネに設定
    ...(canSetFolderThumb
      ? [
          {
            icon: ImageIcon,
            label: "親フォルダのサムネに設定",
            mnemonic: "T",
            action: handleSetFolderThumb,
          },
        ]
      : []),
    // 明示設定されたフォルダサムネを解除
    ...(canClearFolderThumb
      ? [
          {
            icon: ImageOff,
            label: "サムネイル設定を解除",
            mnemonic: "U",
            action: handleClearFolderThumb,
          },
        ]
      : []),
    // リネーム: HF / Hydrus / 絶対パスでは不可（従来は空振りするメニューが出ていた）
    ...(capabilities.canRename
      ? [
          {
            icon: Pencil,
            label: "リネーム",
            mnemonic: "R",
            action: () => {
              onRename(item);
              onClose();
            },
          },
        ]
      : []),
    ...(capabilities.canCopy && !isRecordTable
      ? [{ icon: Copy, label: "コピー", mnemonic: "C", action: handleCopy }]
      : []),
    ...(capabilities.canMove && !isRecordTable
      ? [
          {
            icon: Scissors,
            label: "切り取り",
            mnemonic: "K",
            action: handleCut,
          },
        ]
      : []),
    ...(canPaste && !isRecordTable
      ? [
          {
            icon: ClipboardPaste,
            label: "貼り付け",
            mnemonic: "P",
            action: handlePaste,
          },
        ]
      : []),
    ...(capabilities.canDelete
      ? [
          {
            icon: Trash2,
            label: "削除",
            mnemonic: "D",
            action: handleDelete,
            destructive: true,
          },
        ]
      : []),
    ...(!isRecordTable
      ? [
          {
            icon: Info,
            label: "プロパティ",
            mnemonic: "I",
            action: () => {
              onProperties(item);
              onClose();
            },
          },
        ]
      : []),
  ];

  return createPortal(
    <MenuMnemonicSurface
      ref={menuRef}
      className="fixed z-50 min-w-40 rounded-md border border-border bg-popover p-1 shadow-lg"
      style={menuStyle}
      onContextMenu={(e) => e.preventDefault()}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
    >
      {menuItems.map((mi) => (
        <MenuMnemonicButton
          key={mi.label}
          mnemonic={mi.mnemonic}
          className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-muted ${
            "destructive" in mi ? "text-destructive" : ""
          }`}
          onClick={mi.action}
        >
          <mi.icon className="size-3.5" />
          {mi.label}
        </MenuMnemonicButton>
      ))}
    </MenuMnemonicSurface>,
    document.body,
  );
}
