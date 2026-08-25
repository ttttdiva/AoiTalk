"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CalendarDays, MessageSquare, Search, UserRound } from "lucide-react";
import type { ConversationSearchResult } from "@/lib/chat-api";
import { chatApi } from "@/lib/chat-api";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

type ConversationSearchDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId?: string | null;
  onSelectResult: (result: ConversationSearchResult) => void;
};

const ROLE_LABELS: Record<string, string> = {
  user: "User",
  assistant: "Assistant",
  system: "System",
};

function formatResultDate(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function ConversationSearchDialog({
  open,
  onOpenChange,
  projectId,
  onSelectResult,
}: ConversationSearchDialogProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ConversationSearchResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [projectOnly, setProjectOnly] = useState(false);
  const requestIdRef = useRef(0);

  const handleOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (!nextOpen) {
        requestIdRef.current += 1;
        setQuery("");
        setResults([]);
        setIsLoading(false);
      }
      onOpenChange(nextOpen);
    },
    [onOpenChange],
  );

  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    const requestId = ++requestIdRef.current;

    if (!trimmed) {
      return;
    }

    const timer = window.setTimeout(() => {
      setIsLoading(true);
      chatApi
        .searchConversations(trimmed, projectOnly ? projectId : null)
        .then((data) => {
          if (requestIdRef.current !== requestId) return;
          setResults(data.results);
        })
        .catch(() => {
          if (requestIdRef.current !== requestId) return;
          setResults([]);
        })
        .finally(() => {
          if (requestIdRef.current === requestId) {
            setIsLoading(false);
          }
        });
    }, 180);

    return () => window.clearTimeout(timer);
  }, [open, projectId, projectOnly, query]);

  const emptyLabel = useMemo(() => {
    if (!query.trim()) return "検索語を入力してください";
    if (isLoading) return "検索中...";
    return "見つかりません";
  }, [isLoading, query]);
  const visibleResults = query.trim() ? results : [];

  return (
    <CommandDialog
      open={open}
      onOpenChange={handleOpenChange}
      title="会話履歴検索"
      description="会話履歴を検索します"
      size="2xl"
    >
      <Command shouldFilter={false}>
        <CommandInput
          placeholder="会話履歴を検索..."
          value={query}
          onValueChange={setQuery}
        />
        {projectId && (
          <div className="flex items-center gap-2 border-b px-3 py-2 text-xs text-muted-foreground">
            <Checkbox
              checked={projectOnly}
              onCheckedChange={(value) => setProjectOnly(Boolean(value))}
              className="size-3.5"
            />
            <span>現在のプロジェクトのみ</span>
          </div>
        )}
        <CommandList className="max-h-[24rem]">
          <CommandEmpty>{emptyLabel}</CommandEmpty>
          {visibleResults.length > 0 && (
            <CommandGroup heading="検索結果">
              {visibleResults.map((result) => {
                const dateLabel = formatResultDate(
                  result.created_at ?? result.last_activity,
                );
                const roleLabel =
                  result.role && ROLE_LABELS[result.role]
                    ? ROLE_LABELS[result.role]
                    : result.match_type === "session"
                      ? "Session"
                      : "Message";
                return (
                  <CommandItem
                    key={result.id}
                    value={`${result.title} ${result.character_name} ${result.snippet}`}
                    onSelect={() => onSelectResult(result)}
                    className="items-start gap-3 py-2"
                  >
                    <MessageSquare className="mt-0.5 size-4 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="truncate font-medium">
                          {result.title}
                        </span>
                        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {roleLabel}
                        </span>
                      </div>
                      <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                        {result.snippet}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <UserRound className="size-3" />
                          {result.character_name}
                        </span>
                        {dateLabel && (
                          <span className="inline-flex items-center gap-1">
                            <CalendarDays className="size-3" />
                            {dateLabel}
                          </span>
                        )}
                      </div>
                    </div>
                    <Search className="mt-0.5 size-4 text-muted-foreground opacity-0 group-data-selected/command-item:opacity-100" />
                  </CommandItem>
                );
              })}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}
