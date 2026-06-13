"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { X, StickyNote } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getMemo, saveMemo } from "@/lib/explorer-api";
import { useMarkdownShortcuts } from "@/hooks/use-markdown-shortcuts";
import { useSnippetAutocomplete } from "@/hooks/use-snippet-autocomplete";
import { SnippetPopup } from "@/components/ui/snippet-popup";
import { useSnippets } from "@/contexts/snippets-context";

export function GlobalMemoPad() {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useMarkdownShortcuts(textareaRef);
  const { snippets } = useSnippets();
  const { state: snippetState } = useSnippetAutocomplete(textareaRef, snippets);

  // グローバルイベント
  useEffect(() => {
    const handler = () =>
      setOpen((prev) => {
        if (prev) {
          // 閉じる時: loaded をリセットして次回再読み込み
          setLoaded(false);
        }
        return !prev;
      });
    window.addEventListener("global-open-memo", handler);
    return () => window.removeEventListener("global-open-memo", handler);
  }, []);

  // 開いた時にメモを読み込む
  useEffect(() => {
    if (!open || loaded) return;
    getMemo()
      .then((res) => {
        setContent(res.content);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, [open, loaded]);

  // 開いた時にフォーカス
  useEffect(() => {
    if (open && loaded) {
      setTimeout(() => textareaRef.current?.focus(), 50);
    }
  }, [open, loaded]);

  // debounce自動保存
  const handleChange = useCallback((value: string) => {
    setContent(value);
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(async () => {
      setSaving(true);
      try {
        await saveMemo(value);
      } catch {
        /* ignore */
      }
      setSaving(false);
    }, 1000);
  }, []);

  // 閉じる時に保存
  const handleClose = useCallback(async () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    if (loaded) {
      try {
        await saveMemo(content);
      } catch {
        /* ignore */
      }
    }
    setLoaded(false);
    setOpen(false);
  }, [content, loaded]);

  // Escape で閉じる
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        handleClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, handleClose]);

  if (!open) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-96 flex-col rounded-lg border bg-background shadow-xl">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <StickyNote className="size-4" />
          メモ帳
          {saving && (
            <span className="text-xs text-muted-foreground">保存中...</span>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={handleClose}
        >
          <X className="size-3.5" />
        </Button>
      </div>
      <textarea
        ref={textareaRef}
        value={content}
        onChange={(e) => handleChange(e.target.value)}
        className="h-64 w-full resize-y bg-transparent p-3 text-sm outline-none"
        placeholder="メモを入力..."
      />
      <SnippetPopup state={snippetState} />
    </div>
  );
}
