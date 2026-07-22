import { type CSSProperties, type RefObject } from "react";
import { createPortal } from "react-dom";
import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { Clock, ExternalLink, Copy, Trash2 } from "lucide-react";
import { type CtxMenuState } from "./reports-utils";

export function ReportsContextMenu({
  ctxMenu,
  ctxMenuRef,
  ctxMenuStyle,
  handleCtxEdit,
  handleCtxOpenDetail,
  handleCtxDuplicate,
  handleCtxDelete,
}: {
  ctxMenu: CtxMenuState | null;
  ctxMenuRef: RefObject<HTMLDivElement | null>;
  ctxMenuStyle: CSSProperties;
  handleCtxEdit: () => void;
  handleCtxOpenDetail: () => void;
  handleCtxDuplicate: () => void;
  handleCtxDelete: () => void;
}) {
  if (!ctxMenu || typeof document === "undefined") return null;
  return createPortal(
    <MenuMnemonicSurface
      ref={ctxMenuRef}
      data-ctx-menu
      className="fixed z-[100] min-w-[180px] rounded-md bg-popover p-1 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10"
      style={ctxMenuStyle}
      onContextMenu={(e) => e.preventDefault()}
    >
      <MenuMnemonicButton
        type="button"
        mnemonic="E"
        className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground"
        onClick={handleCtxEdit}
        disabled={!ctxMenu.entry.ended_at}
      >
        <Clock className="size-3.5" />
        編集
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="O"
        className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground"
        onClick={handleCtxOpenDetail}
      >
        <ExternalLink className="size-3.5" />
        タスク詳細を開く
      </MenuMnemonicButton>
      <MenuMnemonicButton
        type="button"
        mnemonic="C"
        className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent hover:text-accent-foreground disabled:opacity-50 disabled:pointer-events-none"
        onClick={handleCtxDuplicate}
        disabled={!ctxMenu.entry.ended_at}
      >
        <Copy className="size-3.5" />
        複製
      </MenuMnemonicButton>
      <div className="-mx-1 my-1 h-px bg-border" />
      <MenuMnemonicButton
        type="button"
        mnemonic="D"
        className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-destructive hover:bg-destructive/10"
        onClick={handleCtxDelete}
      >
        <Trash2 className="size-3.5" />
        削除
      </MenuMnemonicButton>
    </MenuMnemonicSurface>,
    document.body,
  );
}
