"use client";

import { useState } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HelpPopover } from "@/components/ui/help-popover";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { ChevronDown, ChevronUp, Pencil, Plus, Trash2, Code2 } from "lucide-react";
import { useSnippets } from "@/contexts/snippets-context";
import type { Snippet } from "@/lib/snippets-api";

export function SnippetsSection() {
  const { snippets, save } = useSnippets();
  const [expanded, setExpanded] = useState(false);
  const [editIndex, setEditIndex] = useState<number | null>(null);
  const [form, setForm] = useState<Snippet>({
    prefix: "",
    body: "",
    description: "",
  });
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  const resetForm = () => {
    setForm({ prefix: "", body: "", description: "" });
    setEditIndex(null);
  };

  const handleSave = async () => {
    if (!form.prefix.trim() || !form.body.trim()) {
      setFeedback("prefix と本文を入力してください。");
      return;
    }
    setSaving(true);
    setFeedback(null);
    try {
      const newSnippets = [...snippets];
      const entry: Snippet = {
        ...form,
        prefix: form.prefix.trim(),
        body: form.body,
        description: form.description?.trim() || undefined,
      };
      if (editIndex !== null) {
        newSnippets[editIndex] = entry;
      } else {
        newSnippets.push(entry);
      }
      await save(newSnippets);
      resetForm();
      setFeedback("スニペットを保存しました。");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "保存に失敗しました。");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (index: number) => {
    setSaving(true);
    setFeedback(null);
    try {
      const newSnippets = snippets.filter((_, i) => i !== index);
      await save(newSnippets);
      if (editIndex === index) resetForm();
      setFeedback("スニペットを削除しました。");
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "削除に失敗しました。");
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (index: number) => {
    setEditIndex(index);
    setForm({ ...snippets[index] });
  };

  return (
    <Card size="sm" className="min-w-0 w-full max-w-full rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0">
      <CardHeader className="flex min-w-0 flex-row items-center gap-1 p-0 !px-0">
        <div
          className="flex min-w-0 flex-1 cursor-pointer select-none px-3 py-2"
          role="button"
          tabIndex={0}
          aria-expanded={expanded}
          aria-controls="snippets-content"
          data-settings-target-activation="snippets"
          onClick={() => setExpanded((v) => !v)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              setExpanded((v) => !v);
            }
          }}
        >
          <CardTitle className="flex min-w-0 w-full items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <Code2 className="size-4" />
              スニペット
              {snippets.length > 0 && (
                <span className="text-xs font-normal text-muted-foreground">
                  {snippets.length}件
                </span>
              )}
            </span>
            {expanded ? (
              <ChevronUp className="size-4" />
            ) : (
              <ChevronDown className="size-4" />
            )}
          </CardTitle>
        </div>
        <HelpPopover label="スニペット" className="mr-2">
          テキスト入力中に prefix を打つと候補が表示され、Tab で展開されます。
        </HelpPopover>
      </CardHeader>
      {expanded && (
      <CardContent id="snippets-content" className="min-w-0 w-full max-w-full space-y-4">
        {feedback && <p role="status" className="text-xs text-muted-foreground">{feedback}</p>}
        {/* 一覧 */}
        {snippets.length > 0 && (
          <div className="min-w-0 w-full max-w-full space-y-2">
            {snippets.map((s, i) => (
              <div
                key={i}
                className="flex min-w-0 w-full max-w-full items-start justify-between gap-2 rounded-md border p-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <code className="min-w-0 max-w-full break-all rounded bg-muted px-1.5 py-0.5 text-xs font-semibold">
                      {s.prefix}
                    </code>
                    {s.quickAccess === true && (
                      <Badge
                        data-testid="snippet-quick-access-badge"
                        variant="outline"
                        className="shrink-0 text-[10px]"
                      >
                        クイックアクセス
                      </Badge>
                    )}
                    {s.description && (
                      <span className="min-w-0 break-words text-xs text-muted-foreground">
                        {s.description}
                      </span>
                    )}
                  </div>
                  <pre className="mt-1 max-h-20 max-w-full overflow-auto whitespace-pre-wrap break-words text-xs text-muted-foreground">
                    {s.body}
                  </pre>
                </div>
                <div className="flex shrink-0 gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    onClick={() => startEdit(i)}
                  >
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7 text-destructive"
                    onClick={() => handleDelete(i)}
                    disabled={saving}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* 追加/編集フォーム */}
        <div className="min-w-0 w-full max-w-full space-y-2 rounded-md border p-3">
          <p className="text-xs font-medium">
            {editIndex !== null ? "スニペットを編集" : "新しいスニペット"}
          </p>
          <Input
            placeholder="prefix（例: todo）"
            value={form.prefix}
            onChange={(e) => setForm((f) => ({ ...f, prefix: e.target.value }))}
            className="h-8 min-w-0 w-full max-w-full text-sm"
          />
          <div className="min-w-0 w-full max-w-full">
            <LongTextEditor
              placeholder="展開されるテキスト"
              value={form.body}
              onChange={(value) => setForm((f) => ({ ...f, body: value }))}
              minHeight={96}
              maxHeight={240}
              fontSize={13}
            />
          </div>
          <Input
            placeholder="説明（任意）"
            value={form.description || ""}
            onChange={(e) =>
              setForm((f) => ({ ...f, description: e.target.value }))
            }
            className="h-8 min-w-0 w-full max-w-full text-sm"
          />
          <div className="flex min-w-0 flex-wrap gap-2">
            <Button
              size="sm"
              onClick={handleSave}
              disabled={!form.prefix.trim() || !form.body.trim() || saving}
            >
              <Plus className="mr-1 size-3.5" />
              {editIndex !== null ? "更新" : "追加"}
            </Button>
            {editIndex !== null && (
              <Button size="sm" variant="outline" onClick={resetForm}>
                キャンセル
              </Button>
            )}
          </div>
        </div>
      </CardContent>
      )}
    </Card>
  );
}
