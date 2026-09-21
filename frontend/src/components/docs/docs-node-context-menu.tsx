"use client";

import { useEffect, useRef, type CSSProperties } from "react";
import {
  CheckSquare,
  ExternalLink,
  Hash,
  LockKeyhole,
  MoveRight,
  Plus,
  Tag,
  Trash2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { DocsNode, DocsSupertag } from "./types";
import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

export type DocsNodeContextMenuPosition = {
  x: number;
  y: number;
};

export type DocsNodeContextMenuProps = {
  node: DocsNode;
  tags?: DocsSupertag[];
  /** A position means a pointer-opened menu; without it the menu is anchored to its trigger. */
  position?: DocsNodeContextMenuPosition | null;
  onClose: () => void;
  onCopyNodeId: (node: DocsNode) => void | Promise<unknown>;
  onDuplicateNode: (node: DocsNode) => void | Promise<unknown>;
  onArchiveNode: (node: DocsNode) => void | Promise<unknown>;
  onMoveNode: (node: DocsNode) => void | Promise<unknown>;
  onTaskifyNode: (node: DocsNode) => void | Promise<unknown>;
  onApplyTag: (node: DocsNode, tag: DocsSupertag) => void | Promise<unknown>;
  onOpenNode: (node: DocsNode) => void | Promise<unknown>;
  onCleanupNode?: (node: DocsNode) => void | Promise<unknown>;
  /** Lifecycle-aware affordances. Undefined preserves the ordinary menu. */
  canArchive?: boolean;
  canDuplicate?: boolean;
  canMove?: boolean;
  canTaskify?: boolean;
  canTag?: boolean;
  canCleanup?: boolean;
  protectedMessage?: string | null;
};

/**
 * The canonical Docs node menu.  Outline children and the document title use
 * this same renderer so labels, mnemonics, close behavior, and action wiring
 * cannot drift between the two entry points.
 */
export function DocsNodeContextMenu({
  node,
  tags = [],
  position = null,
  onClose,
  onCopyNodeId,
  onDuplicateNode,
  onArchiveNode,
  onMoveNode,
  onTaskifyNode,
  onApplyTag,
  onOpenNode,
  onCleanupNode,
  canArchive = true,
  canDuplicate = true,
  canMove = true,
  canTaskify = true,
  canTag = true,
  canCleanup = false,
  protectedMessage = null,
}: DocsNodeContextMenuProps) {
  const surfaceRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && surfaceRef.current?.contains(target)) return;
      onClose();
    };
    const handleContextMenu = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && surfaceRef.current?.contains(target)) return;
      // A second right-click is a new menu request.  Close this instance even
      // when the originating row stops propagation before it reaches document.
      onClose();
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("contextmenu", handleContextMenu, true);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("contextmenu", handleContextMenu, true);
    };
  }, [onClose]);

  const run = (action: () => void | Promise<unknown>) => {
    onClose();
    try {
      void Promise.resolve(action()).catch((error: unknown) => {
        toast.error(error instanceof Error ? error.message : "Docs操作に失敗しました");
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Docs操作に失敗しました");
    }
  };
  const style: CSSProperties | undefined = position
    ? { left: position.x, top: position.y }
    : undefined;

  return (
    <MenuMnemonicSurface
      ref={surfaceRef}
      data-docs-row-menu
      className={cn(
        "z-50 min-w-44 rounded-md border bg-popover p-1 text-xs shadow-lg",
        position ? "fixed" : "absolute right-0 top-7",
      )}
      style={style}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
      onContextMenu={(event) => event.preventDefault()}
    >
      <MenuButton icon={Hash} label="ノードIDをコピー" mnemonic="C" onClick={() => run(() => onCopyNodeId(node))} />
      {canDuplicate ? <MenuButton icon={Plus} label="複製" mnemonic="U" onClick={() => run(() => onDuplicateNode(node))} /> : null}
      {protectedMessage ? (
        <div
          role="status"
          className="my-1 flex items-start gap-2 rounded px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground"
          data-docs-protected-node
        >
          <LockKeyhole className="mt-0.5 size-3.5 shrink-0 text-amber-600" />
          <span>{protectedMessage}</span>
        </div>
      ) : null}
      {canArchive ? <MenuButton icon={Trash2} label="削除" mnemonic="D" onClick={() => run(() => onArchiveNode(node))} /> : null}
      {canCleanup && onCleanupNode ? <MenuButton icon={Trash2} label="staleをクリーンアップ" mnemonic="K" onClick={() => run(() => onCleanupNode(node))} /> : null}
      {canMove ? <MenuButton icon={MoveRight} label="別ページへ移動" mnemonic="M" onClick={() => run(() => onMoveNode(node))} /> : null}
      {canTaskify ? <MenuButton icon={CheckSquare} label="タスク化" mnemonic="T" onClick={() => run(() => onTaskifyNode(node))} /> : null}
      {canTag ? (
        <MenuButton
          icon={Tag}
          label="先頭タグを付与"
          mnemonic="G"
          onClick={() => {
            const tag = tags[0];
            if (tag) run(() => onApplyTag(node, tag));
            else onClose();
          }}
        />
      ) : null}
      <MenuButton icon={ExternalLink} label="右パネルで開く" mnemonic="O" onClick={() => run(() => onOpenNode(node))} />
    </MenuMnemonicSurface>
  );
}

function MenuButton({
  icon: Icon,
  label,
  mnemonic,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  mnemonic: string;
  onClick: () => void;
}) {
  return (
    <MenuMnemonicButton
      type="button"
      mnemonic={mnemonic}
      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent"
      onClick={onClick}
    >
      <Icon className="size-3.5" />
      {label}
    </MenuMnemonicButton>
  );
}
