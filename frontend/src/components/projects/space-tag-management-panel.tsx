"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  Copy,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Tags,
  Trash2,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";

const TAG_COLORS = [
  "#ef4444",
  "#f97316",
  "#eab308",
  "#22c55e",
  "#06b6d4",
  "#3b82f6",
  "#8b5cf6",
  "#ec4899",
  "#6b7280",
];

interface SpaceInfo {
  id: string;
  name: string;
}

interface TagInfo {
  id: string;
  space_id: string;
  name: string;
  color: string | null;
  created_by?: string | null;
  created_at?: string | null;
}

interface SpaceTagManagementPanelProps {
  space: SpaceInfo;
  spaces: SpaceInfo[];
}

async function apiFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

export function SpaceTagManagementPanel({
  space,
  spaces,
}: SpaceTagManagementPanelProps) {
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [copying, setCopying] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [newName, setNewName] = useState("");
  const [newColor, setNewColor] = useState(TAG_COLORS[5]);
  const [editingTagId, setEditingTagId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editColor, setEditColor] = useState(TAG_COLORS[5]);
  const [copySpaceId, setCopySpaceId] = useState("");

  const otherSpaces = useMemo(
    () => spaces.filter((item) => item.id !== space.id),
    [space.id, spaces],
  );

  const fetchTags = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const rows = await apiFetch<TagInfo[]>(`/api/spaces/${space.id}/tags`);
      setTags(rows);
      setSelectedIds((prev) => {
        const existingIds = new Set(rows.map((tag) => tag.id));
        return new Set([...prev].filter((id) => existingIds.has(id)));
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "タグの取得に失敗しました");
      setTags([]);
    } finally {
      setLoading(false);
    }
  }, [space.id]);

  useEffect(() => {
    setSelectedIds(new Set());
    setEditingTagId(null);
    setCopySpaceId("");
    setMessage("");
    void fetchTags();
  }, [fetchTags]);

  const toggleSelected = useCallback((tagId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(tagId)) {
        next.delete(tagId);
      } else {
        next.add(tagId);
      }
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setSelectedIds((prev) =>
      prev.size === tags.length
        ? new Set()
        : new Set(tags.map((tag) => tag.id)),
    );
  }, [tags]);

  const handleCreate = useCallback(async () => {
    if (!newName.trim()) return;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await apiFetch(`/api/spaces/${space.id}/tags`, {
        method: "POST",
        body: JSON.stringify({ name: newName.trim(), color: newColor }),
      });
      setNewName("");
      await fetchTags();
    } catch (err) {
      setError(err instanceof Error ? err.message : "タグの作成に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [fetchTags, newColor, newName, space.id]);

  const startEditing = useCallback((tag: TagInfo) => {
    setEditingTagId(tag.id);
    setEditName(tag.name);
    setEditColor(tag.color || TAG_COLORS[5]);
  }, []);

  const cancelEditing = useCallback(() => {
    setEditingTagId(null);
    setEditName("");
    setEditColor(TAG_COLORS[5]);
  }, []);

  const handleSaveEdit = useCallback(async () => {
    if (!editingTagId || !editName.trim()) return;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await apiFetch(`/api/tags/${editingTagId}`, {
        method: "PATCH",
        body: JSON.stringify({ name: editName.trim(), color: editColor }),
      });
      cancelEditing();
      await fetchTags();
    } catch (err) {
      setError(err instanceof Error ? err.message : "タグの更新に失敗しました");
    } finally {
      setSaving(false);
    }
  }, [cancelEditing, editColor, editName, editingTagId, fetchTags]);

  const handleDelete = useCallback(
    async (tag: TagInfo) => {
      if (
        !window.confirm(
          `"${tag.name}" を削除します。関連タスクからも外れます。`,
        )
      ) {
        return;
      }
      setSaving(true);
      setError("");
      setMessage("");
      try {
        await apiFetch(`/api/tags/${tag.id}`, { method: "DELETE" });
        await fetchTags();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "タグの削除に失敗しました",
        );
      } finally {
        setSaving(false);
      }
    },
    [fetchTags],
  );

  const handleCopySelected = useCallback(async () => {
    if (!copySpaceId || selectedIds.size === 0) return;
    const selectedTags = tags.filter((tag) => selectedIds.has(tag.id));
    const targetSpace = spaces.find((item) => item.id === copySpaceId);
    setCopying(true);
    setError("");
    setMessage("");
    try {
      await Promise.all(
        selectedTags.map((tag) =>
          apiFetch(`/api/tags/${tag.id}/copy`, {
            method: "POST",
            body: JSON.stringify({ space_id: copySpaceId }),
          }),
        ),
      );
      setMessage(
        `${selectedTags.length}件のタグを${targetSpace?.name || "選択スペース"}へコピーしました`,
      );
      setSelectedIds(new Set());
      setCopySpaceId("");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "タグのコピーに失敗しました",
      );
    } finally {
      setCopying(false);
    }
  }, [copySpaceId, selectedIds, spaces, tags]);

  return (
    <Card className="flex-1">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <Tags className="size-4" />
          タグ管理: {space.name}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-[1fr_auto]">
          <div className="space-y-2">
            <Label className="text-xs">新しいタグ</Label>
            <div className="flex gap-2">
              <Input
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    void handleCreate();
                  }
                }}
                placeholder="タグ名"
              />
              <Button
                size="sm"
                className="h-10 px-3"
                disabled={saving || !newName.trim()}
                onClick={() => void handleCreate()}
              >
                {saving ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Plus className="size-4" />
                )}
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            <Label className="text-xs">色</Label>
            <ColorPicker value={newColor} onChange={setNewColor} />
          </div>
        </div>

        <Separator />

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void fetchTags()}
            disabled={loading}
          >
            <RefreshCw
              className={`mr-1 size-3.5 ${loading ? "animate-spin" : ""}`}
            />
            再読込
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={toggleAll}
            disabled={tags.length === 0}
          >
            {selectedIds.size === tags.length && tags.length > 0
              ? "選択解除"
              : "全選択"}
          </Button>
          <select
            value={copySpaceId}
            onChange={(event) => setCopySpaceId(event.target.value)}
            className="h-8 min-w-48 rounded-md border border-input bg-transparent px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-1 focus-visible:ring-ring/50 dark:bg-input/30"
            disabled={otherSpaces.length === 0}
          >
            <option value="">コピー先スペース</option>
            {otherSpaces.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            disabled={!copySpaceId || selectedIds.size === 0 || copying}
            onClick={() => void handleCopySelected()}
          >
            {copying ? (
              <Loader2 className="mr-1 size-3.5 animate-spin" />
            ) : (
              <Copy className="mr-1 size-3.5" />
            )}
            {selectedIds.size > 0 ? `${selectedIds.size}件をコピー` : "コピー"}
          </Button>
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        {message && <p className="text-xs text-muted-foreground">{message}</p>}

        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-12 w-full rounded-lg" />
            ))}
          </div>
        ) : tags.length > 0 ? (
          <div className="space-y-2">
            {tags.map((tag) => (
              <div
                key={tag.id}
                className="flex flex-wrap items-center gap-3 rounded-lg border p-2.5"
              >
                <Checkbox
                  checked={selectedIds.has(tag.id)}
                  onCheckedChange={() => toggleSelected(tag.id)}
                />
                {editingTagId === tag.id ? (
                  <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                    <Input
                      value={editName}
                      onChange={(event) => setEditName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          void handleSaveEdit();
                        }
                        if (event.key === "Escape") {
                          cancelEditing();
                        }
                      }}
                      className="h-8 min-w-48 flex-1 text-sm"
                    />
                    <ColorPicker value={editColor} onChange={setEditColor} />
                    <Button
                      size="sm"
                      className="h-8 px-2"
                      disabled={saving || !editName.trim()}
                      onClick={() => void handleSaveEdit()}
                    >
                      <Check className="size-3.5" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-8 px-2"
                      onClick={cancelEditing}
                    >
                      <X className="size-3.5" />
                    </Button>
                  </div>
                ) : (
                  <>
                    <Badge
                      variant="secondary"
                      className="max-w-72 truncate border"
                      style={
                        tag.color
                          ? {
                              backgroundColor: `${tag.color}22`,
                              borderColor: `${tag.color}44`,
                              color: tag.color,
                            }
                          : undefined
                      }
                    >
                      {tag.name}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {tag.color || "色なし"}
                    </span>
                    <div className="ml-auto flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 px-2"
                        onClick={() => startEditing(tag)}
                      >
                        <Pencil className="size-3.5" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 px-2 text-destructive hover:text-destructive"
                        disabled={saving}
                        onClick={() => void handleDelete(tag)}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            このスペースにはタグがありません
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ColorPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {TAG_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          className="size-6 rounded-full border-2 transition-transform hover:scale-105"
          style={{
            backgroundColor: color,
            borderColor: value === color ? "currentColor" : "transparent",
          }}
          onClick={() => onChange(color)}
        />
      ))}
      <label
        className="relative size-6 cursor-pointer overflow-hidden rounded-full border-2"
        style={{
          backgroundColor: value,
          borderColor: TAG_COLORS.includes(value)
            ? "transparent"
            : "currentColor",
        }}
      >
        <span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-white drop-shadow">
          +
        </span>
        <input
          type="color"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="absolute inset-0 cursor-pointer opacity-0"
        />
      </label>
    </div>
  );
}
