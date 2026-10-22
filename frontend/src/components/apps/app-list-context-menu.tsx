"use client";

import { useEffect } from "react";
import { createPortal } from "react-dom";
import { Archive, ArchiveRestore } from "lucide-react";

import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { permissionAtLeast, type AppSummary } from "@/lib/apps-api";

export type AppListContextMenuState = {
  x: number;
  y: number;
  app: AppSummary;
  archived: boolean;
};

type AppListContextMenuProps = {
  menu: AppListContextMenuState | null;
  onClose: () => void;
  onArchive: (app: AppSummary) => void | Promise<void>;
  onRestore: (app: AppSummary) => void | Promise<void>;
};

export function AppListContextMenu({
  menu,
  onClose,
  onArchive,
  onRestore,
}: AppListContextMenuProps) {
  const { ref, style } = useContextMenuPosition(
    menu ? { x: menu.x, y: menu.y } : null,
    { fallbackWidth: 196, fallbackHeight: 44 },
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

  if (
    !menu ||
    typeof document === "undefined" ||
    !permissionAtLeast(menu.app.permission, "admin")
  ) {
    return null;
  }

  const handleAction = () => {
    onClose();
    void (menu.archived ? onRestore(menu.app) : onArchive(menu.app));
  };

  return createPortal(
    <MenuMnemonicSurface
      ref={ref}
      className="fixed z-50 min-w-48 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      onContextMenu={(event) => event.preventDefault()}
    >
      <MenuMnemonicButton
        type="button"
        mnemonic={menu.archived ? "R" : "A"}
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground cursor-default"
        onClick={handleAction}
      >
        {menu.archived ? <ArchiveRestore className="size-4" /> : <Archive className="size-4" />}
        {menu.archived ? "アーカイブ解除" : "アーカイブ"}
      </MenuMnemonicButton>
    </MenuMnemonicSurface>,
    document.body,
  );
}
