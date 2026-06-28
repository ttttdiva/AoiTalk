"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Brain,
  ChevronDown,
  ChevronUp,
  Plus,
  Pencil,
  Trash2,
  Loader2,
} from "lucide-react";
import { memoryApi, type DreamingMemory } from "@/lib/ecc-api";

export function MemorySection() {
  const [memories, setMemories] = useState<DreamingMemory[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editMemory, setEditMemory] = useState<DreamingMemory | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  // フォーム
  const [formContent, setFormContent] = useState("");

  const fetchMemories = useCallback(async () => {
    setLoading(true);
    try {
      const data = await memoryApi.list();
      setMemories(data.memories || []);
    } catch {
      setMemories([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleToggle = useCallback(() => {
    setExpanded((prev) => {
      if (!prev) fetchMemories();
      return !prev;
    });
  }, [fetchMemories]);

  // 新規作成ダイアログ
  const handleNewClick = useCallback(() => {
    setIsNew(true);
    setFormContent("");
    setEditMemory({
      id: "",
      content: "",
      memory_type: "fact",
      status: "active",
      source_type: "manual",
      created_at: null,
      updated_at: null,
    } as DreamingMemory);
  }, []);

  // 編集ダイアログ
  const handleEditClick = useCallback((mem: DreamingMemory) => {
    setIsNew(false);
    setFormContent(mem.content);
    setEditMemory(mem);
  }, []);

  // 保存
  const handleSave = useCallback(async () => {
    if (!formContent.trim()) return;
    setSaving(true);
    try {
      if (isNew) {
        await memoryApi.create({ content: formContent.trim() });
      } else if (editMemory) {
        await memoryApi.update(editMemory.id, { content: formContent.trim() });
      }
      setEditMemory(null);
      await fetchMemories();
    } catch (err) {
      console.error("メモリ保存失敗:", err);
    } finally {
      setSaving(false);
    }
  }, [formContent, isNew, editMemory, fetchMemories]);

  // 削除
  const handleDelete = useCallback(
    async (mem: DreamingMemory) => {
      if (!window.confirm(`「${mem.content.slice(0, 30)}...」を削除しますか？`))
        return;
      setDeleting(mem.id);
      try {
        await memoryApi.delete(mem.id);
        await fetchMemories();
      } catch {
        // ignore
      } finally {
        setDeleting(null);
      }
    },
    [fetchMemories],
  );

  // 全削除
  const handleDeleteAll = useCallback(async () => {
    if (!window.confirm("全てのメモリを削除しますか？この操作は取り消せません。"))
      return;
    try {
      await memoryApi.deleteAll();
      await fetchMemories();
    } catch {
      // ignore
    }
  }, [fetchMemories]);

  // トグル
  return (
    <>
      <Card size="sm">
        <CardHeader
          className="cursor-pointer select-none"
          onClick={handleToggle}
        >
          <CardTitle className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <Brain className="size-4" />
              Dreamingメモリ
              {memories.length > 0 && (
                <Badge variant="secondary" className="text-[10px]">
                  {memories.length}件
                </Badge>
              )}
            </span>
            {expanded ? (
              <ChevronUp className="size-4" />
            ) : (
              <ChevronDown className="size-4" />
            )}
          </CardTitle>
        </CardHeader>

        {expanded && (
          <CardContent className="space-y-3">
            {/* アクションバー */}
            <div className="flex items-center justify-between gap-2">
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={handleNewClick}>
                  <Plus className="size-3 mr-1" />
                  追加
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={fetchMemories}
                  disabled={loading}
                >
                  {loading && <Loader2 className="size-3 animate-spin mr-1" />}
                  更新
                </Button>
              </div>
              {memories.length > 0 && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-destructive"
                  onClick={handleDeleteAll}
                >
                  全削除
                </Button>
              )}
            </div>

            {/* メモリ一覧 */}
            {loading ? (
              <div className="flex items-center justify-center py-4 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin mr-2" />
                読み込み中...
              </div>
            ) : memories.length === 0 ? (
              <p className="text-sm text-muted-foreground py-2">
                メモリはまだありません。会話を通じて自動的に記憶されます。
              </p>
            ) : (
              <div className="max-h-96 overflow-auto space-y-1.5">
                {memories.map((mem) => (
                  <div
                    key={mem.id}
                    className="flex items-start justify-between gap-2 rounded-md border p-2.5"
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm leading-snug break-words">
                        {mem.content}
                      </p>
                      <div className="flex items-center gap-1.5 mt-1">
                        <Badge
                          variant="outline"
                          className="text-[9px] px-1 py-0"
                        >
                          {mem.source_type === "manual" ? "手動" : "Dreaming"}
                        </Badge>
                        <Badge
                          variant="outline"
                          className="text-[9px] px-1 py-0"
                        >
                          {mem.memory_type}
                        </Badge>
                        {mem.created_at && (
                          <span className="text-[10px] text-muted-foreground">
                            {new Date(mem.created_at).toLocaleDateString(
                              "ja-JP",
                            )}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="flex gap-0.5 shrink-0">
                      {/* トグル */}
                      {/* 編集 */}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="size-7 p-0"
                        onClick={() => handleEditClick(mem)}
                      >
                        <Pencil className="size-3" />
                      </Button>
                      {/* 削除 */}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="size-7 p-0 text-destructive"
                        onClick={() => handleDelete(mem)}
                        disabled={deleting === mem.id}
                      >
                        {deleting === mem.id ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <Trash2 className="size-3" />
                        )}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        )}
      </Card>

      {/* 編集/作成ダイアログ */}
      <Dialog
        open={!!editMemory}
        onOpenChange={(v) => !v && setEditMemory(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {isNew ? "メモリを追加" : "メモリを編集"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label className="text-xs">内容</Label>
              <Textarea
                value={formContent}
                onChange={(e) => setFormContent(e.target.value)}
                placeholder="例: PythonとTypeScriptを主に使う"
                rows={3}
                className="text-sm"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setEditMemory(null)}
              >
                キャンセル
              </Button>
              <Button
                size="sm"
                onClick={handleSave}
                disabled={saving || !formContent.trim()}
              >
                {saving && <Loader2 className="size-3 animate-spin mr-1" />}
                {isNew ? "追加" : "保存"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
