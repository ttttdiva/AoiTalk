"use client";

import { useEffect, type ComponentType } from "react";
import { createPortal } from "react-dom";

import {
  MenuMnemonicButton,
  MenuMnemonicSurface,
} from "@/components/ui/menu-mnemonic";
import { useContextMenuPosition } from "@/hooks/use-context-menu-position";
import { cn } from "@/lib/utils";

/** カーソル座標アンカー用の 1 点。右クリックの clientX/clientY、または ⋯ ボタンの矩形から作る。 */
export type StoryMenuPoint = { x: number; y: number };

export type StoryMenuEntry =
  | { key: string; separator: true }
  | {
      key: string;
      separator?: false;
      label: string;
      /** §4.8 の「続きの分岐を追加 / 複製して分岐にする」など、挙動の説明を添える項目で使う。 */
      description?: string;
      icon?: ComponentType<{ className?: string }>;
      mnemonic?: string;
      disabled?: boolean;
      tone?: "default" | "primary" | "destructive";
      onSelect: () => void;
    };

/** ポインタ座標からメニューのアンカー点を作る（行・エディタの右クリック用）。 */
export function pointFromEvent(event: { clientX: number; clientY: number }): StoryMenuPoint {
  return { x: event.clientX, y: event.clientY };
}

/** ボタンの左下をアンカー点にする（⋯ ボタンから右クリックと同一のメニューを開くため）。 */
export function pointFromTrigger(element: HTMLElement): StoryMenuPoint {
  const rect = element.getBoundingClientRect();
  return { x: rect.left, y: rect.bottom + 4 };
}

/**
 * 章リスト行 / 本文エディタで共有するコンテキストメニュー。
 * 既存の chat / explorer のコンテキストメニューと同じ流儀（portal + カーソル座標アンカー）で描画する。
 */
export function StoryContextMenu({
  point,
  entries,
  label,
  onClose,
}: {
  point: StoryMenuPoint | null;
  entries: StoryMenuEntry[];
  label: string;
  onClose: () => void;
}) {
  const { ref, style } = useContextMenuPosition(point, {
    fallbackWidth: 256,
    fallbackHeight: 24 * entries.length + 16,
  });

  useEffect(() => {
    if (!point) return;

    const handlePointerDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
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
  }, [onClose, point, ref]);

  if (!point || typeof document === "undefined") return null;

  return createPortal(
    <MenuMnemonicSurface
      ref={ref}
      aria-label={label}
      className="fixed z-50 w-64 max-w-[calc(100vw-1rem)] rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-md"
      style={style}
      onContextMenu={(event) => event.preventDefault()}
    >
      {entries.map((entry) =>
        entry.separator ? (
          <div key={entry.key} className="my-1 h-px bg-border" />
        ) : (
          <MenuMnemonicButton
            key={entry.key}
            mnemonic={entry.mnemonic}
            disabled={entry.disabled}
            className={cn(
              "flex w-full cursor-default items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs disabled:pointer-events-none disabled:opacity-50",
              entry.tone === "destructive"
                ? "text-destructive hover:bg-destructive/10"
                : "hover:bg-accent hover:text-accent-foreground",
            )}
            onClick={() => {
              onClose();
              entry.onSelect();
            }}
          >
            {entry.icon ? (
              <entry.icon
                className={cn(
                  "mt-px size-3.5 shrink-0",
                  entry.tone === "primary" && "text-primary",
                )}
              />
            ) : (
              <span className="mt-px size-3.5 shrink-0" />
            )}
            <span className="min-w-0 flex-1">
              <span className={cn("block", entry.tone === "primary" && "font-medium")}>{entry.label}</span>
              {entry.description ? (
                <span className="mt-0.5 block text-[10px] leading-tight text-muted-foreground">
                  {entry.description}
                </span>
              ) : null}
            </span>
          </MenuMnemonicButton>
        ),
      )}
    </MenuMnemonicSurface>,
    document.body,
  );
}
