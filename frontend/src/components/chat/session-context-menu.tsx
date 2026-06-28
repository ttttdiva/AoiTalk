"use client";

import { useEffect } from "react";
import { createPortal } from "react-dom";
import { Hash, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import type { ConversationSession } from "@/lib/chat-api";

export type ChatSessionContextMenuState = {
  x: number;
  y: number;
  session: ConversationSession;
};

type ChatSessionContextMenuProps = {
  menu: ChatSessionContextMenuState | null;
  onClose: () => void;
  onRename: (session: ConversationSession) => void;
  onDelete: (id: string) => void | Promise<void>;
};

async function copyTextToClipboard(text: string) {
  try {
    if (typeof navigator === "undefined" || !navigator.clipboard) {
      throw new Error("Clipboard API is unavailable");
    }
    await navigator.clipboard.writeText(text);
    return;
  } catch (err) {
    if (typeof document === "undefined") throw err;
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      if (!document.execCommand("copy")) throw err;
    } finally {
      document.body.removeChild(textarea);
    }
  }
}

export function ChatSessionContextMenu({
  menu,
  onClose,
  onRename,
  onDelete,
}: ChatSessionContextMenuProps) {
  const { ref, style } = useContextMenuPosition(
    menu ? { x: menu.x, y: menu.y } : null,
    { fallbackWidth: 192, fallbackHeight: 128 },
  );

  useEffect(() => {
    if (!menu) return;

    const handleClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        onClose();
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menu, onClose, ref]);

  if (!menu || typeof document === "undefined") return null;

  const handleRename = () => {
    onClose();
    onRename(menu.session);
  };

  const handleCopySessionId = async () => {
    onClose();
    try {
      await copyTextToClipboard(menu.session.id);
      toast.success("セッションIDをコピーしました");
    } catch (err) {
      console.error("セッションIDコピー失敗:", err);
      toast.error("セッションIDのコピーに失敗しました");
    }
  };

  const handleDelete = () => {
    onClose();
    void onDelete(menu.session.id);
  };

  return createPortal(
    <div
      ref={ref}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      role="menu"
      onContextMenu={(event) => event.preventDefault()}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        role="menuitem"
        onClick={handleRename}
      >
        <Pencil className="size-4" />
        タイトルを編集
      </button>
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        role="menuitem"
        onClick={handleCopySessionId}
      >
        <Hash className="size-4" />
        セッションIDをコピー
      </button>
      <div className="my-1 h-px bg-border" />
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive hover:bg-destructive/10 cursor-default"
        role="menuitem"
        onClick={handleDelete}
      >
        <Trash2 className="size-4" />
        削除
      </button>
    </div>,
    document.body,
  );
}
