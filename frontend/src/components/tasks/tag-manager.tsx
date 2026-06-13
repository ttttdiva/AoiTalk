"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Tags, Plus, X, Pencil, Check } from "lucide-react";
import { taskApi, type Tag } from "@/lib/task-api";
import { cn } from "@/lib/utils";

const PRESET_COLORS = [
  "#ef4444", // red
  "#f97316", // orange
  "#eab308", // yellow
  "#22c55e", // green
  "#06b6d4", // cyan
  "#3b82f6", // blue
  "#8b5cf6", // violet
  "#ec4899", // pink
  "#6b7280", // gray
];

interface TagManagerProps {
  projectId: string;
  tags: Tag[];
  onUpdated: () => void;
}

export function TagManager({ projectId, tags, onUpdated }: TagManagerProps) {
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newColor, setNewColor] = useState(PRESET_COLORS[5]);
  const [creating, setCreating] = useState(false);

  // 編集状態
  const [editingTagId, setEditingTagId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editColor, setEditColor] = useState("");
  const [saving, setSaving] = useState(false);
  const editInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingTagId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingTagId]);

  const handleCreate = useCallback(async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      await taskApi.createTag(projectId, {
        name: newName.trim(),
        color: newColor,
      });
      setNewName("");
      onUpdated();
    } catch (err) {
      console.error("タグ作成失敗:", err);
    } finally {
      setCreating(false);
    }
  }, [newName, newColor, projectId, onUpdated]);

  const handleDelete = useCallback(
    async (tagId: string) => {
      try {
        await taskApi.deleteTag(tagId);
        onUpdated();
      } catch (err) {
        console.error("タグ削除失敗:", err);
      }
    },
    [onUpdated],
  );

  const startEditing = useCallback((tag: Tag) => {
    setEditingTagId(tag.id);
    setEditName(tag.name);
    setEditColor(tag.color || PRESET_COLORS[5]);
  }, []);

  const cancelEditing = useCallback(() => {
    setEditingTagId(null);
    setEditName("");
    setEditColor("");
  }, []);

  const handleSaveEdit = useCallback(async () => {
    if (!editingTagId || !editName.trim()) return;
    setSaving(true);
    try {
      await taskApi.updateTag(editingTagId, {
        name: editName.trim(),
        color: editColor,
      });
      setEditingTagId(null);
      onUpdated();
    } catch (err) {
      console.error("タグ更新失敗:", err);
    } finally {
      setSaving(false);
    }
  }, [editingTagId, editName, editColor, onUpdated]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button variant="outline" size="sm">
            <Tags className="size-4 mr-1" />
            タグ管理
          </Button>
        }
      />
      <PopoverContent className="w-80" align="end">
        <div className="grid gap-3">
          <h4 className="font-medium text-sm">タグ管理</h4>

          {/* 既存タグ一覧 */}
          {tags.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {tags.map((tag) =>
                editingTagId === tag.id ? (
                  /* 編集モード */
                  <div
                    key={tag.id}
                    className="w-full border rounded-md p-2 grid gap-2 bg-muted/30"
                  >
                    <div className="flex gap-2">
                      <Input
                        ref={editInputRef}
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        className="flex-1 h-7 text-sm"
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            handleSaveEdit();
                          }
                          if (e.key === "Escape") {
                            cancelEditing();
                          }
                        }}
                      />
                      <Button
                        size="sm"
                        className="h-7 px-2"
                        disabled={!editName.trim() || saving}
                        onClick={handleSaveEdit}
                      >
                        <Check className="size-3.5" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2"
                        onClick={cancelEditing}
                      >
                        <X className="size-3.5" />
                      </Button>
                    </div>
                    {/* 編集用カラーパレット */}
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs text-muted-foreground shrink-0">
                        色:
                      </span>
                      {PRESET_COLORS.map((color) => (
                        <button
                          key={color}
                          type="button"
                          className={cn(
                            "size-5 rounded-full border-2 transition-transform",
                            editColor === color
                              ? "border-foreground scale-110"
                              : "border-transparent hover:scale-110",
                          )}
                          style={{ backgroundColor: color }}
                          onClick={() => setEditColor(color)}
                        />
                      ))}
                      <label
                        className={cn(
                          "size-5 rounded-full border-2 transition-transform cursor-pointer hover:scale-110 relative overflow-hidden",
                          !PRESET_COLORS.includes(editColor)
                            ? "border-foreground scale-110"
                            : "border-transparent",
                        )}
                        style={{ backgroundColor: editColor }}
                        title="カスタムカラー"
                      >
                        <span className="absolute inset-0 flex items-center justify-center text-white text-[10px] font-bold drop-shadow-[0_1px_1px_rgba(0,0,0,0.5)]">
                          +
                        </span>
                        <input
                          type="color"
                          value={editColor}
                          onChange={(e) => setEditColor(e.target.value)}
                          className="absolute inset-0 opacity-0 cursor-pointer"
                        />
                      </label>
                    </div>
                  </div>
                ) : (
                  /* 表示モード */
                  <Badge
                    key={tag.id}
                    variant="secondary"
                    className="gap-1 pr-1 group cursor-pointer"
                    style={
                      tag.color
                        ? {
                            backgroundColor: tag.color + "22",
                            color: tag.color,
                            borderColor: tag.color + "44",
                          }
                        : undefined
                    }
                  >
                    {tag.name}
                    <button
                      type="button"
                      className="ml-0.5 rounded-full p-0.5 hover:bg-black/10 dark:hover:bg-white/10 opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={() => startEditing(tag)}
                      title="編集"
                    >
                      <Pencil className="size-3" />
                    </button>
                    <button
                      type="button"
                      className="rounded-full p-0.5 hover:bg-black/10 dark:hover:bg-white/10 opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={() => handleDelete(tag.id)}
                      title="削除"
                    >
                      <X className="size-3" />
                    </button>
                  </Badge>
                ),
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              タグがまだありません。
            </p>
          )}

          {/* 新規タグ作成 */}
          <div className="border-t pt-3 grid gap-2">
            <div className="flex gap-2">
              <Input
                placeholder="新しいタグ名"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="flex-1 h-8 text-sm"
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    handleCreate();
                  }
                }}
              />
              <Button
                size="sm"
                className="h-8 px-2"
                disabled={!newName.trim() || creating}
                onClick={handleCreate}
              >
                <Plus className="size-4" />
              </Button>
            </div>

            {/* カラーパレット */}
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground shrink-0">
                色:
              </span>
              {PRESET_COLORS.map((color) => (
                <button
                  key={color}
                  type="button"
                  className={cn(
                    "size-5 rounded-full border-2 transition-transform",
                    newColor === color
                      ? "border-foreground scale-110"
                      : "border-transparent hover:scale-110",
                  )}
                  style={{ backgroundColor: color }}
                  onClick={() => setNewColor(color)}
                />
              ))}
              <label
                className={cn(
                  "size-5 rounded-full border-2 transition-transform cursor-pointer hover:scale-110 relative overflow-hidden",
                  !PRESET_COLORS.includes(newColor)
                    ? "border-foreground scale-110"
                    : "border-transparent",
                )}
                style={{ backgroundColor: newColor }}
                title="カスタムカラー"
              >
                <span className="absolute inset-0 flex items-center justify-center text-white text-[10px] font-bold drop-shadow-[0_1px_1px_rgba(0,0,0,0.5)]">
                  +
                </span>
                <input
                  type="color"
                  value={newColor}
                  onChange={(e) => setNewColor(e.target.value)}
                  className="absolute inset-0 opacity-0 cursor-pointer"
                />
              </label>
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
