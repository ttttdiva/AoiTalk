"use client";

import { useState, useCallback } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { Trash2, Filter } from "lucide-react";
import { taskApi, type Tag } from "@/lib/task-api";
import { cn } from "@/lib/utils";

export const TAG_COLORS = [
  "#6E3FC6",
  "#7C3AED",
  "#3B82F6",
  "#06B6D4",
  "#0D9488",
  "#22C55E",
  "#84CC16",
  "#EAB308",
  "#F97316",
  "#EF4444",
  "#EC4899",
  "#F43F5E",
  "#D946EF",
  "#78716C",
  "#6B7280",
  "#9CA3AF",
  "#D1D5DB",
];

interface TagPillProps {
  tag: Tag;
  onRemove?: () => void;
  onUpdated?: () => void;
  size?: "sm" | "md";
  onFilter?: () => void;
}

export function TagPill({
  tag,
  onRemove,
  onUpdated,
  size = "sm",
  onFilter,
}: TagPillProps) {
  const [open, setOpen] = useState(false);
  const [editName, setEditName] = useState(tag.name);

  const bgColor = tag.color || "#6B7280";

  const handleColorChange = useCallback(
    async (color: string | null) => {
      try {
        await taskApi.updateTag(tag.id, { color: color || undefined });
        onUpdated?.();
      } catch {}
    },
    [tag.id, onUpdated],
  );

  const handleNameSave = useCallback(async () => {
    if (!editName.trim() || editName.trim() === tag.name) return;
    try {
      await taskApi.updateTag(tag.id, { name: editName.trim() });
      onUpdated?.();
    } catch {}
  }, [tag.id, editName, tag.name, onUpdated]);

  const handleDelete = useCallback(async () => {
    setOpen(false);
    try {
      await taskApi.deleteTag(tag.id);
      onUpdated?.();
    } catch {}
  }, [tag.id, onUpdated]);

  const pillClass =
    size === "sm"
      ? "text-[10px] px-1.5 h-[18px] rounded"
      : "text-xs px-2 h-6 rounded-md";

  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (v) setEditName(tag.name);
      }}
    >
      <PopoverTrigger
        render={
          <span
            className={cn(
              "inline-flex items-center gap-0.5 font-medium cursor-pointer hover:opacity-80 transition-opacity text-white shrink-0",
              pillClass,
            )}
            style={{ backgroundColor: bgColor }}
          >
            {tag.name}
            {onRemove && (
              <button
                type="button"
                className="hover:bg-white/20 rounded-sm leading-none ml-0.5"
                onClick={(e) => {
                  e.stopPropagation();
                  e.preventDefault();
                  onRemove();
                }}
              >
                ×
              </button>
            )}
          </span>
        }
      />
      <PopoverContent
        className="w-56 p-3"
        align="start"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="space-y-3">
          <Input
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            onBlur={handleNameSave}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleNameSave();
              }
            }}
            className="h-8 text-sm"
          />
          <div className="grid grid-cols-9 gap-1.5">
            {TAG_COLORS.map((color) => (
              <button
                key={color}
                type="button"
                className={cn(
                  "size-5 rounded-full border-2 transition-transform hover:scale-110",
                  tag.color === color
                    ? "border-foreground scale-110"
                    : "border-transparent",
                )}
                style={{ backgroundColor: color }}
                onClick={() => handleColorChange(color)}
              />
            ))}
            <label
              className={cn(
                "size-5 rounded-full border-2 transition-transform hover:scale-110 relative overflow-hidden cursor-pointer",
                tag.color && !TAG_COLORS.includes(tag.color)
                  ? "border-foreground scale-110"
                  : "border-transparent",
              )}
              style={{ backgroundColor: tag.color || "#6B7280" }}
              title="カスタムカラー"
            >
              <span className="absolute inset-0 flex items-center justify-center text-white text-[10px] font-bold drop-shadow-[0_1px_1px_rgba(0,0,0,0.5)]">
                +
              </span>
              <input
                type="color"
                value={tag.color || "#6B7280"}
                onChange={(e) => handleColorChange(e.target.value)}
                className="absolute inset-0 opacity-0 cursor-pointer"
              />
            </label>
            <button
              type="button"
              className={cn(
                "size-5 rounded-full border-2 transition-transform hover:scale-110 flex items-center justify-center bg-muted",
                !tag.color
                  ? "border-foreground scale-110"
                  : "border-transparent",
              )}
              onClick={() => handleColorChange(null)}
              title="色なし"
            >
              <span className="text-[10px] text-muted-foreground">⊘</span>
            </button>
          </div>
          {onFilter && (
            <>
              <hr className="border-border" />
              <button
                type="button"
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground w-full px-1 py-0.5"
                onClick={() => {
                  setOpen(false);
                  onFilter();
                }}
              >
                <Filter className="size-3" />
                Filter by tag
              </button>
            </>
          )}
          <hr className="border-border" />
          <button
            type="button"
            className="flex items-center gap-1.5 text-xs text-destructive hover:bg-destructive/10 rounded px-1 py-0.5 transition-colors w-full"
            onClick={handleDelete}
          >
            <Trash2 className="size-3" />
            Delete
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
