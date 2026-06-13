"use client";

import { useMemo } from "react";
import { Plus, MoreHorizontal, Trash2, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { ConversationSession } from "@/lib/chat-api";

type SessionListProps = {
  sessions: ConversationSession[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onCreateSession: () => void;
  onDeleteSession: (id: string) => void;
};

/** 相対時間を返す */
function formatRelativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffSec < 60) return "たった今";
  if (diffMin < 60) return `${diffMin}分前`;
  if (diffHour < 24) return `${diffHour}時間前`;
  if (diffDay < 7) return `${diffDay}日前`;
  if (diffDay < 30) return `${Math.floor(diffDay / 7)}週間前`;
  return date.toLocaleDateString("ja-JP");
}

export function SessionList({
  sessions,
  activeSessionId,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
}: SessionListProps) {
  const sortedSessions = useMemo(
    () =>
      [...sessions].sort((a, b) => {
        const dateA = a.last_activity ? new Date(a.last_activity).getTime() : 0;
        const dateB = b.last_activity ? new Date(b.last_activity).getTime() : 0;
        return dateB - dateA;
      }),
    [sessions]
  );

  return (
    <div className="flex h-full flex-col">
      {/* 新規会話ボタン */}
      <div className="border-b p-3">
        <Button
          variant="outline"
          size="default"
          className="w-full justify-start gap-2"
          onClick={onCreateSession}
        >
          <Plus className="size-4" data-icon="inline-start" />
          新規会話
        </Button>
      </div>

      {/* セッション一覧 */}
      <ScrollArea className="flex-1">
        <div className="flex flex-col gap-0.5 p-2">
          {sortedSessions.length === 0 && (
            <div className="px-3 py-8 text-center text-sm text-muted-foreground">
              会話がありません
            </div>
          )}
          {sortedSessions.map((session) => {
            const isActive = session.id === activeSessionId;
            return (
              <div
                key={session.id}
                className={cn(
                  "group relative flex cursor-pointer items-start gap-2 rounded-lg px-3 py-2 text-sm transition-colors hover:bg-muted",
                  isActive && "bg-muted"
                )}
                onClick={() => onSelectSession(session.id)}
              >
                <MessageSquare className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">
                    {session.title || "無題の会話"}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {session.character_name}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {formatRelativeTime(session.last_activity)}
                  </div>
                </div>

                {/* 削除メニュー */}
                <DropdownMenu>
                  <DropdownMenuTrigger
                    className={cn(
                      "mt-0.5 rounded p-0.5 opacity-0 transition-opacity hover:bg-accent group-hover:opacity-100",
                      isActive && "opacity-100"
                    )}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <MoreHorizontal className="size-4" />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent side="right" align="start">
                    <DropdownMenuItem
                      className="text-destructive focus:text-destructive"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteSession(session.id);
                      }}
                    >
                      <Trash2 className="mr-2 size-4" />
                      削除
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            );
          })}
        </div>
      </ScrollArea>
    </div>
  );
}
