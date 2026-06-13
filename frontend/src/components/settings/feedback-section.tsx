"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { MessageSquareWarning, ChevronDown, ChevronUp, Check, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface FeedbackEntry {
  id: string;
  session_id: string | null;
  message: string;
  character: string | null;
  user_input: string | null;
  category: string;
  comment: string | null;
  resolved: boolean;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at?: string;
  timestamp?: string;
}

const CATEGORY_LABELS: Record<string, string> = {
  incorrect: "不正確",
  incomplete: "不完全",
  slow: "遅い",
  auto_failure: "自動記録",
  other: "その他",
};

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

export function FeedbackSection() {
  const [entries, setEntries] = useState<FeedbackEntry[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [includeResolved, setIncludeResolved] = useState(false);
  const [entryFilter, setEntryFilter] = useState<"all" | "manual" | "auto">("all");
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [selected, setSelected] = useState<FeedbackEntry | null>(null);

  const fetchFeedback = useCallback(
    async (withResolved?: boolean) => {
      setLoading(true);
      try {
        const resolved = withResolved ?? includeResolved;
        const data = await pyFetch<{ feedback: FeedbackEntry[]; count: number }>(
          `/feedback?include_resolved=${resolved}`
        );
        setEntries(data.feedback);
      } catch {
        setEntries([]);
      } finally {
        setLoading(false);
      }
    },
    [includeResolved]
  );

  const handleToggle = useCallback(() => {
    if (!expanded && entries.length === 0) fetchFeedback();
    setExpanded((v) => !v);
  }, [expanded, entries.length, fetchFeedback]);

  const handleResolve = useCallback(
    async (id: string) => {
      setResolvingId(id);
      try {
        await pyFetch(`/feedback/${id}/resolve`, { method: "POST" });
        setEntries((prev) =>
          prev.map((e) =>
            e.id === id ? { ...e, resolved: true, resolved_at: new Date().toISOString() } : e
          )
        );
      } catch {
        // ignore
      } finally {
        setResolvingId(null);
      }
    },
    []
  );

  const handleIncludeResolvedChange = useCallback(
    (checked: boolean) => {
      setIncludeResolved(checked);
      fetchFeedback(checked);
    },
    [fetchFeedback]
  );

  const formatDate = (iso: string) => {
    if (!iso) return "-";
    try {
      return new Date(iso).toLocaleString("ja-JP", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  const unresolvedCount = entries.filter((e) => !e.resolved).length;
  const filteredEntries = entries.filter((entry) => {
    const isAuto = entry.category === "auto_failure";
    if (entryFilter === "auto") return isAuto;
    if (entryFilter === "manual") return !isAuto;
    return true;
  });

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={handleToggle}
      >
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            <MessageSquareWarning className="size-4" />
            フィードバック
            {unresolvedCount > 0 && (
              <Badge variant="destructive" className="text-[10px]">
                {unresolvedCount}件未解決
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
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5">
              <Checkbox
                checked={includeResolved}
                onCheckedChange={(v) => handleIncludeResolvedChange(!!v)}
              />
              <span className="text-xs">解決済みも表示</span>
            </div>
            <Button variant="outline" size="sm" onClick={() => fetchFeedback()}>
              更新
            </Button>
            <div className="ml-auto flex items-center gap-1">
              {(["all", "manual", "auto"] as const).map((value) => (
                <Button
                  key={value}
                  type="button"
                  size="sm"
                  variant={entryFilter === value ? "default" : "outline"}
                  onClick={() => setEntryFilter(value)}
                >
                  {value === "all" ? "全て" : value === "manual" ? "手動" : "自動"}
                </Button>
              ))}
            </div>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              取得中...
            </div>
          ) : filteredEntries.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              フィードバックがありません
            </p>
          ) : (
            <div className="max-h-80 space-y-2 overflow-auto">
              {filteredEntries.map((entry) => (
                <div
                  key={entry.id}
                  className={`rounded-md border p-2.5 ${
                    entry.resolved ? "opacity-50" : ""
                  }`}
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelected(entry)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") setSelected(entry);
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 mb-1">
                        <Badge variant="outline" className="text-[10px]">
                          {CATEGORY_LABELS[entry.category] || entry.category}
                        </Badge>
                        {entry.category === "auto_failure" && (
                          <Badge variant="secondary" className="text-[10px]">
                            auto
                          </Badge>
                        )}
                        {entry.character && (
                          <Badge variant="secondary" className="text-[10px]">
                            {entry.character}
                          </Badge>
                        )}
                        <span className="text-[10px] text-muted-foreground">
                          {formatDate(entry.created_at || entry.timestamp || "")}
                        </span>
                      </div>
                      <p className="text-xs">{entry.message}</p>
                      {entry.comment && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {entry.comment}
                        </p>
                      )}
                      {entry.user_input && (
                        <p className="mt-1 text-[10px] text-muted-foreground truncate">
                          入力: {entry.user_input}
                        </p>
                      )}
                    </div>
                    {!entry.resolved && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={(event) => {
                          event.stopPropagation();
                          handleResolve(entry.id);
                        }}
                        disabled={resolvingId === entry.id}
                        title="解決済みにする"
                      >
                        {resolvingId === entry.id ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <Check className="size-3" />
                        )}
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      )}
      <Dialog open={!!selected} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>フィードバック詳細</DialogTitle>
          </DialogHeader>
          {selected && (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap gap-2">
                <Badge variant="outline">
                  {CATEGORY_LABELS[selected.category] || selected.category}
                </Badge>
                {selected.category === "auto_failure" && (
                  <Badge variant="secondary">auto</Badge>
                )}
                {selected.character && (
                  <Badge variant="secondary">{selected.character}</Badge>
                )}
                <Badge variant={selected.resolved ? "secondary" : "destructive"}>
                  {selected.resolved ? "解決済み" : "未解決"}
                </Badge>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">AI応答</p>
                <div className="mt-1 max-h-56 overflow-auto rounded-md border p-3 whitespace-pre-wrap">
                  {selected.message}
                </div>
              </div>
              {selected.user_input && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground">ユーザー入力</p>
                  <div className="mt-1 rounded-md border p-3 whitespace-pre-wrap">
                    {selected.user_input}
                  </div>
                </div>
              )}
              {selected.comment && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground">補足コメント</p>
                  <div className="mt-1 rounded-md border p-3 whitespace-pre-wrap">
                    {selected.comment}
                  </div>
                </div>
              )}
              <div className="text-xs text-muted-foreground">
                session: {selected.session_id || "-"} /{" "}
                {formatDate(selected.created_at || selected.timestamp || "")}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
