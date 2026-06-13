"use client";

import { useState, useEffect, useCallback } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LongTextEditor } from "@/components/editor/long-text-editor";
import { cn } from "@/lib/utils";
import { Plus, Pencil, ExternalLink } from "lucide-react";
import {
  pyFetch,
  type LoreBook,
  type LoreBookEntry,
} from "@/lib/scenarios-page-utils";

function LoreBookEditor({ scenarioId }: { scenarioId: string }) {
  const [books, setBooks] = useState<LoreBook[]>([]);
  const [selectedBook, setSelectedBook] = useState<LoreBook | null>(null);
  const [loading, setLoading] = useState(true);
  const [bookName, setBookName] = useState("");
  const [entry, setEntry] = useState<Partial<LoreBookEntry> | null>(null);
  const [keywordInput, setKeywordInput] = useState("");
  const [saving, setSaving] = useState(false);

  const loadBooks = useCallback(async () => {
    setLoading(true);
    try {
      const data = await pyFetch<{ worldbooks: LoreBook[] }>(
        `/worldbooks?scenario_id=${encodeURIComponent(scenarioId)}`,
      );
      setBooks(data.worldbooks ?? []);
    } finally {
      setLoading(false);
    }
  }, [scenarioId]);

  const loadDetail = useCallback(async (bookId: string) => {
    const data = await pyFetch<LoreBook | { worldbook: LoreBook }>(
      `/worldbooks/${bookId}`,
    );
    const detail = "worldbook" in data ? data.worldbook : data;
    setSelectedBook(detail);
  }, []);

  useEffect(() => {
    loadBooks();
  }, [loadBooks]);

  const createBook = async () => {
    if (!bookName.trim()) return;
    setSaving(true);
    try {
      const data = await pyFetch<{ worldbook: LoreBook }>("/worldbooks", {
        method: "POST",
        body: JSON.stringify({
          scenario_id: scenarioId,
          name: bookName.trim(),
          description: "",
          is_enabled: true,
        }),
      });
      setBookName("");
      await loadBooks();
      await loadDetail(data.worldbook.id);
    } finally {
      setSaving(false);
    }
  };

  const saveEntry = async () => {
    if (!selectedBook || !entry?.content?.trim()) return;
    setSaving(true);
    try {
      const body = {
        name: entry.name || "",
        content: entry.content,
        keywords: keywordInput
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        priority: Number(entry.priority ?? 0),
        is_enabled: entry.is_enabled !== false,
        case_sensitive: entry.case_sensitive === true,
        constant: entry.constant === true,
      };
      if (entry.id) {
        await pyFetch(`/worldbooks/entries/${entry.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      } else {
        await pyFetch(`/worldbooks/${selectedBook.id}/entries`, {
          method: "POST",
          body: JSON.stringify(body),
        });
      }
      setEntry(null);
      setKeywordInput("");
      await loadDetail(selectedBook.id);
      await loadBooks();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
      <div className="space-y-3">
        <div className="rounded-md border p-3">
          <div className="space-y-2">
            <Label>ロアブック名</Label>
            <Input
              value={bookName}
              onChange={(event) => setBookName(event.target.value)}
              placeholder="世界観・陣営・舞台設定"
            />
            <Button size="sm" onClick={createBook} disabled={saving || !bookName.trim()}>
              <Plus className="mr-1 size-3.5" />
              作成
            </Button>
          </div>
        </div>
        <div className="rounded-md border p-3">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="text-sm font-medium">参照・インポート元</h4>
            <ExternalLink className="size-3.5 text-muted-foreground" />
          </div>
          <div className="space-y-1 text-sm">
            <a
              className="block text-primary underline-offset-2 hover:underline"
              href="https://booru.chub.ai/lorebooks"
              target="_blank"
              rel="noreferrer"
            >
              Chub Lorebooks
            </a>
            <a
              className="block text-primary underline-offset-2 hover:underline"
              href="https://docs.sillytavern.app/usage/core-concepts/worldinfo/"
              target="_blank"
              rel="noreferrer"
            >
              SillyTavern World Info
            </a>
            <a
              className="block text-primary underline-offset-2 hover:underline"
              href="https://chub.ai/characters"
              target="_blank"
              rel="noreferrer"
            >
              Chub Characters
            </a>
          </div>
        </div>
        <div className="space-y-2">
          {loading ? (
            <div className="py-4 text-center text-xs text-muted-foreground">
              読み込み中...
            </div>
          ) : books.length === 0 ? (
            <div className="rounded-md border p-3 text-sm text-muted-foreground">
              ロアブックがありません
            </div>
          ) : (
            books.map((book) => (
              <button
                key={book.id}
                type="button"
                className={cn(
                  "w-full rounded-md border p-3 text-left text-sm hover:bg-muted",
                  selectedBook?.id === book.id && "border-primary bg-muted",
                )}
                onClick={() => loadDetail(book.id)}
              >
                <div className="font-medium">{book.name}</div>
                <div className="text-xs text-muted-foreground">
                  {book.is_enabled ? "有効" : "無効"}
                </div>
              </button>
            ))
          )}
        </div>
      </div>

      <div className="space-y-3">
        {selectedBook ? (
          <>
            <div className="flex items-center justify-between">
              <h4 className="text-sm font-medium">{selectedBook.name}</h4>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setEntry({
                    name: "",
                    content: "",
                    keywords: [],
                    priority: 0,
                    is_enabled: true,
                    case_sensitive: false,
                    constant: false,
                  });
                  setKeywordInput("");
                }}
              >
                <Plus className="mr-1 size-3.5" />
                エントリ
              </Button>
            </div>
            <div className="space-y-2">
              {(selectedBook.entries ?? []).map((item) => (
                <div key={item.id} className="rounded-md border p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium">{item.name || "無題"}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {item.constant && <Badge variant="outline">常時</Badge>}
                        {item.keywords.map((keyword) => (
                          <Badge key={keyword} variant="secondary">
                            {keyword}
                          </Badge>
                        ))}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setEntry(item);
                        setKeywordInput((item.keywords ?? []).join(", "));
                      }}
                    >
                      <Pencil className="size-3.5" />
                    </Button>
                  </div>
                  <p className="mt-2 line-clamp-3 text-sm text-muted-foreground">
                    {item.content}
                  </p>
                </div>
              ))}
            </div>
            {entry && (
              <div className="rounded-md border border-primary/30 p-3">
                <div className="grid gap-3 md:grid-cols-2">
                  <div className="space-y-1">
                    <Label>名前</Label>
                    <Input
                      value={entry.name ?? ""}
                      onChange={(event) =>
                        setEntry((prev) => ({
                          ...(prev ?? {}),
                          name: event.target.value,
                        }))
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>キーワード（カンマ区切り）</Label>
                    <Input
                      value={keywordInput}
                      onChange={(event) => setKeywordInput(event.target.value)}
                    />
                  </div>
                </div>
                <div className="mt-3 space-y-1">
                  <Label>内容</Label>
                  <LongTextEditor
                    value={entry.content ?? ""}
                    onChange={(value) =>
                      setEntry((prev) => ({
                        ...(prev ?? {}),
                        content: value,
                      }))
                    }
                    minHeight={180}
                    maxHeight={420}
                    fontSize={13}
                  />
                </div>
                <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap gap-3 text-sm">
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={entry.constant === true}
                        onChange={(event) =>
                          setEntry((prev) => ({
                            ...(prev ?? {}),
                            constant: event.target.checked,
                          }))
                        }
                      />
                      常時挿入
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={entry.case_sensitive === true}
                        onChange={(event) =>
                          setEntry((prev) => ({
                            ...(prev ?? {}),
                            case_sensitive: event.target.checked,
                          }))
                        }
                      />
                      大文字小文字を区別
                    </label>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => setEntry(null)}>
                      キャンセル
                    </Button>
                    <Button size="sm" onClick={saveEntry} disabled={saving}>
                      保存
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="rounded-md border p-6 text-center text-sm text-muted-foreground">
            ロアブックを選択してください
          </div>
        )}
      </div>
    </div>
  );
}

export { LoreBookEditor };
