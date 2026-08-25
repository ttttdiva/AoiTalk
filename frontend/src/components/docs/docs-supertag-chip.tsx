"use client";

import type { KeyboardEvent } from "react";
import type { DocsSupertag } from "./types";
import { tagColorStyle } from "./docs-utils";

export type SupertagChipDirection = "previous" | "next" | "text";

export function DocsSupertagChip({
  tag,
  onRemove,
  onNavigate,
  onOpen,
  disabled = false,
}: {
  tag: DocsSupertag;
  onRemove: () => void;
  onNavigate: (direction: SupertagChipDirection) => void;
  onOpen?: () => void;
  disabled?: boolean;
}) {
  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onNavigate("previous");
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      onNavigate("next");
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onNavigate("text");
      return;
    }
    if (!disabled && (event.key === "Backspace" || event.key === "Delete")) {
      event.preventDefault();
      onRemove();
      return;
    }
    if (event.key === "Enter" && onOpen) {
      event.preventDefault();
      onOpen();
    }
  };

  return (
    <button
      type="button"
      data-docs-supertag-chip
      data-docs-supertag-id={tag.id}
      aria-label={`Supertag #${tag.name}`}
      className="shrink-0 rounded border px-1.5 py-0.5 text-[11px] font-medium leading-4 outline-none focus-visible:ring-2 focus-visible:ring-ring"
      style={tagColorStyle(tag.color)}
      title="矢印で移動、Backspace/Deleteで解除、Enterで定義を開く"
      disabled={disabled}
      onDoubleClick={onOpen}
      onKeyDown={handleKeyDown}
    >
      #{tag.name}
    </button>
  );
}
