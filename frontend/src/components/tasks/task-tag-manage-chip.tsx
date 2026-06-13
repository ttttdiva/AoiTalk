"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { MoreHorizontal, Trash2, Copy } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { type Space, type Tag } from "@/lib/task-api";

const TAG_COLORS = [
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

interface TaskTagManageChipProps {
  tag: Tag;
  currentSpaceId: string | null;
  spaces: Space[];
  onRename: (tagId: string, name: string) => Promise<void>;
  onColorChange: (tagId: string, color: string) => Promise<void>;
  onDelete: (tagId: string) => Promise<void>;
  onCopyToSpace: (tagId: string, spaceId: string) => Promise<void>;
}

export function TaskTagManageChip({
  tag,
  currentSpaceId,
  spaces,
  onRename,
  onColorChange,
  onDelete,
  onCopyToSpace,
}: TaskTagManageChipProps) {
  const [open, setOpen] = useState(false);
  const [editName, setEditName] = useState(tag.name);
  const [copySpaceId, setCopySpaceId] = useState("");
  const [savingName, setSavingName] = useState(false);
  const [savingColor, setSavingColor] = useState(false);
  const [copying, setCopying] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (open) {
      setEditName(tag.name);
      setCopySpaceId("");
    }
  }, [open, tag.name]);

  const otherSpaces = useMemo(
    () => spaces.filter((space) => space.id !== currentSpaceId),
    [currentSpaceId, spaces],
  );

  const handleRename = useCallback(async () => {
    const nextName = editName.trim();
    if (!nextName || nextName === tag.name) return;
    setSavingName(true);
    try {
      await onRename(tag.id, nextName);
      setOpen(false);
    } finally {
      setSavingName(false);
    }
  }, [editName, onRename, tag.id, tag.name]);

  const handleColorChange = useCallback(
    async (color: string) => {
      if (color === tag.color) return;
      setSavingColor(true);
      try {
        await onColorChange(tag.id, color);
        setOpen(false);
      } finally {
        setSavingColor(false);
      }
    },
    [onColorChange, tag.color, tag.id],
  );

  const handleCopy = useCallback(async () => {
    if (!copySpaceId) return;
    setCopying(true);
    try {
      await onCopyToSpace(tag.id, copySpaceId);
      setCopySpaceId("");
      setOpen(false);
    } finally {
      setCopying(false);
    }
  }, [copySpaceId, onCopyToSpace, tag.id]);

  const handleDelete = useCallback(async () => {
    const confirmed = window.confirm(
      `"${tag.name}" を削除します。関連タスクからも外れます。`,
    );
    if (!confirmed) return;
    setDeleting(true);
    try {
      await onDelete(tag.id);
      setOpen(false);
    } finally {
      setDeleting(false);
    }
  }, [onDelete, tag.id, tag.name]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className="group relative inline-flex shrink-0">
        <span
          className="inline-flex h-[18px] items-center rounded px-1.5 text-[10px] font-medium text-white"
          style={{ backgroundColor: tag.color || "#6B7280" }}
        >
          <span className="transition-opacity group-hover:opacity-0 group-focus-within:opacity-0">
            {tag.name}
          </span>
        </span>
        <PopoverTrigger
          render={
            <button
              type="button"
              className="pointer-events-none absolute inset-0 inline-flex items-center justify-center rounded text-white opacity-0 transition-opacity group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100"
              style={{ backgroundColor: tag.color || "#6B7280" }}
              aria-label={`${tag.name} actions`}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
              }}
            >
              <MoreHorizontal className="size-3.5" />
            </button>
          }
        />
      </div>
      <PopoverContent
        className="w-72 p-3"
        align="start"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="space-y-3">
          <div className="space-y-1.5">
            <p className="text-[11px] font-medium text-muted-foreground">
              Name
            </p>
            <div className="flex gap-2">
              <Input
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void handleRename();
                  }
                }}
                className="h-8 text-sm"
              />
              <Button
                type="button"
                size="sm"
                className="h-8 px-3"
                disabled={
                  savingName || !editName.trim() || editName.trim() === tag.name
                }
                onClick={() => void handleRename()}
              >
                Save
              </Button>
            </div>
          </div>

          <div className="space-y-1.5">
            <p className="text-[11px] font-medium text-muted-foreground">
              Color
            </p>
            <div className="grid grid-cols-9 gap-1.5">
              {TAG_COLORS.map((color) => (
                <button
                  key={color}
                  type="button"
                  className="size-5 rounded-full border-2 transition-transform hover:scale-110"
                  style={{
                    backgroundColor: color,
                    borderColor:
                      tag.color === color ? "currentColor" : "transparent",
                  }}
                  disabled={savingColor}
                  onClick={() => void handleColorChange(color)}
                />
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <p className="text-[11px] font-medium text-muted-foreground">
              Copy To Space
            </p>
            <div className="flex gap-2">
              <Select
                value={copySpaceId}
                onValueChange={(value) => setCopySpaceId(value ?? "")}
              >
                <SelectTrigger className="h-8 flex-1 text-sm">
                  <SelectValue placeholder="Select space" />
                </SelectTrigger>
                <SelectContent>
                  {otherSpaces.length === 0 ? (
                    <SelectItem value="__none__" disabled>
                      No other spaces
                    </SelectItem>
                  ) : (
                    otherSpaces.map((space) => (
                      <SelectItem key={space.id} value={space.id}>
                        {space.name}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 px-3"
                disabled={!copySpaceId || copying || otherSpaces.length === 0}
                onClick={() => void handleCopy()}
              >
                <Copy className="mr-1 size-3.5" />
                Copy
              </Button>
            </div>
          </div>

          <div className="border-t pt-3">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-8 w-full justify-start px-2 text-destructive hover:bg-destructive/10 hover:text-destructive"
              disabled={deleting}
              onClick={() => void handleDelete()}
            >
              <Trash2 className="mr-1.5 size-3.5" />
              Delete tag
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
