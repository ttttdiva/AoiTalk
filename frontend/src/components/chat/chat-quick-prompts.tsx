"use client";

import { useMemo, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { useSnippets } from "@/contexts/snippets-context";
import type { Snippet } from "@/lib/snippets-api";

/** The controls needed by the chat composer to render quick prompts. */
export type ChatQuickPromptsProps = {
  sendDisabled: boolean;
  onSendPrompt: (content: string) => void;
};

type QuickSnippet = Snippet & {
  quickAccess?: boolean;
};

type QuickPromptForm = {
  prefix: string;
  body: string;
};

const EMPTY_FORM: QuickPromptForm = { prefix: "", body: "" };

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * A horizontally scrolling set of user-defined prompt shortcuts.
 *
 * Snippets are stored in one global array.  Every quick prompt keeps its
 * source index so editing/deleting a quick prompt cannot accidentally target
 * the index of the filtered list.
 */
export function ChatQuickPrompts({
  sendDisabled,
  onSendPrompt,
}: ChatQuickPromptsProps) {
  const { snippets, save } = useSnippets();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<QuickPromptForm>(EMPTY_FORM);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const quickPrompts = useMemo(
    () =>
      snippets
        .map((snippet, index) => ({
          source: snippet as QuickSnippet,
          sourceIndex: index,
        }))
        .filter(({ source }) => source.quickAccess === true),
    [snippets],
  );

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingIndex(null);
  };

  const updateForm = (next: Partial<QuickPromptForm>) => {
    setForm((current) => ({ ...current, ...next }));
    setError(null);
    setStatus(null);
  };

  const startEdit = (sourceIndex: number) => {
    const source = snippets[sourceIndex] as QuickSnippet | undefined;
    if (!source || source.quickAccess !== true) return;
    setEditingIndex(sourceIndex);
    setForm({ prefix: source.prefix, body: source.body });
    setError(null);
    setStatus(null);
  };

  const handleSave = async () => {
    const prefix = form.prefix.trim();
    const body = form.body.trim();
    if (!prefix || !body) {
      setError("表示名と送信内容を入力してください。");
      setStatus(null);
      return;
    }

    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const nextSnippets = [...snippets];
      if (editingIndex === null) {
        nextSnippets.push({ prefix, body, quickAccess: true });
      } else {
        const source = nextSnippets[editingIndex];
        if (!source) {
          throw new Error("クイックプロンプトが見つかりません。");
        }
        nextSnippets[editingIndex] = {
          ...source,
          prefix,
          body,
          quickAccess: true,
        };
      }
      const wasEditing = editingIndex !== null;
      await save(nextSnippets);
      resetForm();
      setStatus(
        wasEditing
          ? "クイックプロンプトを更新しました。"
          : "クイックプロンプトを追加しました。",
      );
    } catch (saveError) {
      // Keep the draft and edit index intact when persistence fails.  The
      // SnippetsProvider also updates its state only after save resolves.
      setError(
        errorMessage(saveError, "クイックプロンプトの保存に失敗しました。"),
      );
      setStatus(null);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (sourceIndex: number) => {
    if (!snippets[sourceIndex]) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const nextSnippets = snippets.filter((_, index) => index !== sourceIndex);
      await save(nextSnippets);
      if (editingIndex === sourceIndex) {
        resetForm();
      } else if (editingIndex !== null && editingIndex > sourceIndex) {
        // Keep the editor attached to the same source object after indexes
        // shift in the global list.
        setEditingIndex(editingIndex - 1);
      }
      setStatus("クイックプロンプトを削除しました。");
    } catch (saveError) {
      setError(
        errorMessage(saveError, "クイックプロンプトの削除に失敗しました。"),
      );
      setStatus(null);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="chat-quick-prompts"
      data-chat-quick-prompts="true"
      data-quick-prompts="true"
      className="flex min-w-0 flex-nowrap items-center gap-2 overflow-x-auto"
    >
      <div
        data-quick-prompt-strip="true"
        className="flex min-w-0 flex-1 flex-nowrap gap-2 overflow-x-auto"
      >
        {quickPrompts.map(({ source, sourceIndex }) => (
          <Button
            key={sourceIndex}
            type="button"
            variant="outline"
            size="sm"
            className="max-w-56 shrink-0 rounded-full"
            title={`${source.prefix}: ${source.body}`}
            aria-label={source.prefix}
            data-quick-prompt="true"
            data-quick-prompt-index={sourceIndex}
            data-testid={`chat-quick-prompt-${sourceIndex}`}
            disabled={sendDisabled}
            onClick={() => onSendPrompt(source.body)}
          >
            <span className="truncate">{source.prefix}</span>
          </Button>
        ))}
      </div>

      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="shrink-0"
              aria-label="クイックプロンプトを管理"
              title="クイックプロンプトを管理"
              data-testid="chat-quick-prompts-trigger"
            />
          }
        >
          <Plus className="size-4" />
        </PopoverTrigger>
        <PopoverContent
          side="top"
          align="end"
          className="w-[min(24rem,calc(100vw-1rem))]"
          data-testid="chat-quick-prompts-popover"
        >
          <div className="space-y-1">
            <p className="text-sm font-medium">クイックプロンプト</p>
            <p className="text-xs text-muted-foreground">
              表示名と送信内容を登録します。
            </p>
          </div>

          {error && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}
          {status && (
            <p role="status" className="text-xs text-muted-foreground">
              {status}
            </p>
          )}

          {quickPrompts.length > 0 && (
            <div className="space-y-1.5" data-testid="chat-quick-prompts-list">
              <p className="text-xs font-medium">現在のクイック一覧</p>
              {quickPrompts.map(({ source, sourceIndex }) => (
                <div
                  key={sourceIndex}
                  className="flex items-center gap-2 rounded-md border px-2 py-1.5"
                  data-quick-prompt-item="true"
                  data-quick-prompt-index={sourceIndex}
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium">
                      {source.prefix}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {source.body}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`${source.prefix}を編集`}
                    title={`${source.prefix}を編集`}
                    data-quick-prompt-edit="true"
                    onClick={() => startEdit(sourceIndex)}
                    disabled={saving}
                  >
                    <Pencil />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="text-destructive"
                    aria-label={`${source.prefix}を削除`}
                    title={`${source.prefix}を削除`}
                    data-quick-prompt-delete="true"
                    onClick={() => void handleDelete(sourceIndex)}
                    disabled={saving}
                  >
                    <Trash2 />
                  </Button>
                </div>
              ))}
            </div>
          )}

          <form
            className="space-y-2 border-t pt-2"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSave();
            }}
          >
            <p className="text-xs font-medium">
              {editingIndex === null
                ? "クイックプロンプトを追加"
                : "クイックプロンプトを編集"}
            </p>
            <Input
              aria-label="表示名"
              placeholder="表示名"
              value={form.prefix}
              onChange={(event) => updateForm({ prefix: event.target.value })}
              disabled={saving}
            />
            <Textarea
              aria-label="送信内容"
              placeholder="送信内容"
              value={form.body}
              onChange={(event) => updateForm({ body: event.target.value })}
              rows={4}
              disabled={saving}
            />
            <div className="flex gap-2">
              <Button type="submit" size="sm" disabled={saving}>
                {editingIndex === null ? "追加" : "更新"}
              </Button>
              {editingIndex !== null && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={resetForm}
                  disabled={saving}
                >
                  キャンセル
                </Button>
              )}
            </div>
          </form>
        </PopoverContent>
      </Popover>
    </div>
  );
}
