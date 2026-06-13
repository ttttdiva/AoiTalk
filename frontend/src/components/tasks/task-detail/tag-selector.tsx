"use client";

import { useCallback, useState } from "react";

import { X } from "lucide-react";

import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { TaskTagManageChip } from "@/components/tasks/task-tag-manage-chip";
import type { Tag } from "@/lib/task-api";
import { cn } from "@/lib/utils";

/** ClickUp風タグセレクター: エリアクリックで全タグのドロップダウンを表示 */
export function TagSelector({
  taskTags,
  allTags,
  spaces,
  currentSpaceId,
  onToggle,
  onClear,
  onCreate,
  onRenameTag,
  onChangeTagColor,
  onDeleteTag,
  onCopyTagToSpace,
}: {
  taskTags: Tag[];
  allTags: Tag[];
  spaces: { id: string; name: string; slug: string }[];
  currentSpaceId: string | null;
  onToggle: (tagId: string) => void;
  onClear: () => void;
  onCreate: (name: string) => Promise<void>;
  onRenameTag: (tagId: string, name: string) => Promise<void>;
  onChangeTagColor: (tagId: string, color: string) => Promise<void>;
  onDeleteTag: (tagId: string) => Promise<void>;
  onCopyTagToSpace: (tagId: string, spaceId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);

  const trimmedSearch = search.trim();
  const normalizedSearch = trimmedSearch.toLowerCase();
  const assignedIds = new Set(taskTags.map((t) => t.id));
  const filtered = trimmedSearch
    ? allTags.filter((t) => t.name.toLowerCase().includes(normalizedSearch))
    : allTags;
  const exactMatch = trimmedSearch
    ? allTags.find((t) => t.name.toLowerCase() === normalizedSearch)
    : undefined;
  const canCreate = trimmedSearch.length > 0 && !exactMatch;

  const handleSubmitSearch = useCallback(async () => {
    if (!trimmedSearch) return;

    if (exactMatch) {
      onToggle(exactMatch.id);
      setSearch("");
      return;
    }

    if (!canCreate) return;
    setCreating(true);
    try {
      await onCreate(trimmedSearch);
      setSearch("");
    } catch (err) {
      console.error("タグ作成失敗", err);
    } finally {
      setCreating(false);
    }
  }, [canCreate, exactMatch, onCreate, onToggle, trimmedSearch]);

  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) setSearch("");
      }}
    >
      <PopoverTrigger
        nativeButton={false}
        render={
          <div className="flex items-center gap-1.5 min-h-[24px] cursor-pointer rounded px-1 -mx-1 hover:bg-muted/50 transition-colors flex-wrap">
            {taskTags.length === 0 ? (
              <span className="text-xs text-muted-foreground">Empty</span>
            ) : (
              taskTags.map((tag) => (
                <TaskTagManageChip
                  key={tag.id}
                  tag={tag}
                  spaces={spaces}
                  currentSpaceId={currentSpaceId}
                  onRename={onRenameTag}
                  onColorChange={onChangeTagColor}
                  onDelete={onDeleteTag}
                  onCopyToSpace={onCopyTagToSpace}
                />
              ))
            )}
            {taskTags.length > 0 && (
              <button
                type="button"
                className="ml-auto text-muted-foreground hover:text-foreground shrink-0"
                onClick={(e) => {
                  e.stopPropagation();
                  onClear();
                }}
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
        }
      />
      <PopoverContent className="w-72 p-0" align="start">
        <div className="p-2 space-y-2">
          {/* 選択中タグ */}
          {taskTags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {taskTags.map((tag) => (
                <div
                  key={tag.id}
                  className="inline-flex items-center gap-1 rounded bg-muted/40 px-1 py-1"
                >
                  <TaskTagManageChip
                    tag={tag}
                    spaces={spaces}
                    currentSpaceId={currentSpaceId}
                    onRename={onRenameTag}
                    onColorChange={onChangeTagColor}
                    onDelete={onDeleteTag}
                    onCopyToSpace={onCopyTagToSpace}
                  />
                  <button
                    type="button"
                    className="inline-flex size-4 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
                    aria-label={`${tag.name} を外す`}
                    onClick={() => onToggle(tag.id)}
                  >
                    <X className="size-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {/* 検索 */}
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search or add tags..."
            className="h-7 text-xs"
            autoFocus
            disabled={creating}
            onKeyDown={(e) => {
              if (e.key === "Enter" && trimmedSearch) {
                e.preventDefault();
                void handleSubmitSearch();
              }
            }}
          />
        </div>
        {/* タグ一覧 */}
        <div className="border-t max-h-48 overflow-y-auto p-1">
          <p className="px-2 py-1 text-[10px] text-muted-foreground">
            Select an option
          </p>
          {filtered.map((tag) => (
            <button
              key={tag.id}
              type="button"
              className={cn(
                "flex items-center gap-2 w-full rounded px-2 py-1 text-left hover:bg-accent transition-colors",
                assignedIds.has(tag.id) && "bg-accent/50",
              )}
              onClick={() => onToggle(tag.id)}
            >
              <span
                className="inline-flex items-center text-[10px] px-1.5 h-5 rounded font-medium text-white"
                style={{ backgroundColor: tag.color || "#6B7280" }}
              >
                {tag.name}
              </span>
            </button>
          ))}
          {filtered.length === 0 && trimmedSearch && !canCreate && (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">
              該当するタグなし
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
