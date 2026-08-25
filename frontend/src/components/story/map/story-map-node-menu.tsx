"use client";

import { useEffect } from "react";
import { createPortal } from "react-dom";
import { BookOpen, Copy, GitBranch, NotebookPen, Target, Trash2, Unlink, type LucideIcon } from "lucide-react";
import { MenuMnemonicButton, MenuMnemonicSurface } from "@/components/ui/menu-mnemonic";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { cn } from "@/lib/utils";
import type { StoryEpisodeView } from "@/lib/story/view-model";

export type StoryMapMenuState = {
  x: number;
  y: number;
  episodeId: string;
};

type StoryMapMenuEntry =
  | { key: string; separator: true }
  | {
      key: string;
      separator?: false;
      icon: LucideIcon;
      mnemonic: string;
      label: string;
      description?: string;
      disabled?: boolean;
      destructive?: boolean;
      action: () => void;
    };

type StoryMapNodeMenuProps = {
  menu: StoryMapMenuState | null;
  episode: StoryEpisodeView | null;
  isStart: boolean;
  hasLinks: boolean;
  onClose: () => void;
  onOpen: (episodeId: string) => void;
  onNewBranch: (episodeId: string) => void;
  onDuplicate: (episodeId: string) => void;
  onSetStart: (episodeId: string) => void;
  onEditPremise: (episodeId: string) => void;
  onDisconnect: (episodeId: string) => void;
  onDelete: (episodeId: string) => void;
};

/**
 * ノードの右クリックメニュー（設計書 §4.9「ノードの操作」）。
 * `session-context-menu.tsx` / `file-context-menu.tsx` と同じく、カーソル座標をアンカーにした
 * portal + `MenuMnemonic*` の流儀で実装する。
 */
export function StoryMapNodeMenu({
  menu,
  episode,
  isStart,
  hasLinks,
  onClose,
  onOpen,
  onNewBranch,
  onDuplicate,
  onSetStart,
  onEditPremise,
  onDisconnect,
  onDelete,
}: StoryMapNodeMenuProps) {
  const { ref, style } = useContextMenuPosition(menu ? { x: menu.x, y: menu.y } : null, {
    fallbackWidth: 264,
    fallbackHeight: 320,
  });

  useEffect(() => {
    if (!menu) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as globalThis.Node)) onClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menu, onClose, ref]);

  if (!menu || !episode || typeof document === "undefined") return null;

  const run = (action: (episodeId: string) => void) => () => {
    onClose();
    action(menu.episodeId);
  };

  const items: StoryMapMenuEntry[] = [
    { key: "open", icon: BookOpen, mnemonic: "O", label: "開く", action: run(onOpen) },
    {
      key: "branch",
      icon: GitBranch,
      mnemonic: "B",
      label: "続きの分岐を追加",
      description: "この章の続きとして、別パターンの章を白紙で作る",
      action: run(onNewBranch),
    },
    {
      key: "duplicate",
      icon: Copy,
      mnemonic: "D",
      label: "複製して分岐にする",
      description: "この章のコピーを、同じ親（前提章）からの別パターンとして隣に並べる",
      action: run(onDuplicate),
    },
    { key: "separator-1", separator: true },
    {
      key: "start",
      icon: Target,
      mnemonic: "S",
      label: "ここから始める",
      disabled: isStart,
      action: run(onSetStart),
    },
    { key: "premise", icon: NotebookPen, mnemonic: "P", label: "前提メモを編集", action: run(onEditPremise) },
    {
      key: "disconnect",
      icon: Unlink,
      mnemonic: "U",
      label: "接続をすべて外す",
      disabled: !hasLinks,
      action: run(onDisconnect),
    },
    { key: "separator-2", separator: true },
    {
      key: "delete",
      icon: Trash2,
      mnemonic: "X",
      label: "削除",
      destructive: true,
      action: run(onDelete),
    },
  ];

  return createPortal(
    <MenuMnemonicSurface
      ref={ref}
      className="fixed z-50 min-w-64 rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      onContextMenu={(event) => event.preventDefault()}
    >
      <div className="truncate px-2 py-1 text-[10px] font-semibold tracking-[0.12em] text-muted-foreground uppercase">
        {episode.title}
      </div>
      {items.map((item) =>
        item.separator ? (
          <div key={item.key} className="my-1 h-px bg-border" />
        ) : (
          <MenuMnemonicButton
            key={item.key}
            mnemonic={item.mnemonic}
            disabled={item.disabled}
            className={cn(
              "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent hover:text-accent-foreground",
              item.destructive && "text-destructive hover:bg-destructive/10 hover:text-destructive",
              item.disabled && "pointer-events-none opacity-40",
            )}
            onClick={item.action}
          >
            <item.icon className="mt-0.5 size-3.5 shrink-0" />
            <span className="min-w-0 flex-1">
              <span className="block">{item.label}</span>
              {item.description ? (
                <span className="mt-0.5 block text-[10px] leading-4 text-muted-foreground">{item.description}</span>
              ) : null}
            </span>
          </MenuMnemonicButton>
        ),
      )}
    </MenuMnemonicSurface>,
    document.body,
  );
}
