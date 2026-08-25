"use client";

import { useCallback, useMemo, useState, type MouseEvent } from "react";
import { useRouter } from "next/navigation";
import { Loader2, MessageSquare, Plus } from "lucide-react";
import { toast } from "sonner";
import { chatApi } from "@/lib/chat-api";
import { createRegularNewChatSession } from "@/lib/create-regular-new-chat-session";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { formatRelativeTime } from "@/lib/utils";
import {
  isChatSessionUnread,
  isChatSessionWorking,
  sortChatSessions,
} from "@/lib/chat-session-view";
import { useProject } from "@/contexts/project-context";
import { useChatSessionsOptional } from "@/contexts/chat-session-context";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Button } from "@/components/ui/button";

const QUICK_SESSION_LIMIT = 8;
const CHAT_LAST_SESSION_KEY = "aoitalk_last_session_id";

function persistLastSessionId(sessionId: string) {
  try {
    window.localStorage.setItem(CHAT_LAST_SESSION_KEY, sessionId);
  } catch {
    // localStorage が無効でも作成と遷移は継続する。
  }
}

function handleSessionNavigation(
  event: MouseEvent<HTMLAnchorElement>,
  href: string,
  router: ReturnType<typeof useRouter>,
) {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }
  event.preventDefault();
  if (!navigateChatSessionInPlace(href)) router.push(href);
}

/**
 * Quick Panel のチャット投影。
 *
 * ChatSidebar が履歴取得・ポーリング・全編集操作の単一 owner であり、
 * この投影は ChatSessionProvider の同じ state を読むだけに留める。
 * Quick Panel の開閉で再マウントされても追加 fetch/timer/effect は発生しない。
 */
export function ChatQuickPanelNavigation() {
  const router = useRouter();
  const { selectedProjectId } = useProject();
  const userId = useCurrentUserId();
  const chatSessions = useChatSessionsOptional();
  const [isCreatingSession, setIsCreatingSession] = useState(false);
  const sessions = useMemo(
    () => sortChatSessions(chatSessions?.sessions ?? []).slice(0, QUICK_SESSION_LIMIT),
    [chatSessions?.sessions],
  );

  const handleCreateSession = useCallback(async () => {
    if (isCreatingSession) return;
    setIsCreatingSession(true);
    try {
      const characterName = await chatApi.getCurrentCharacterName();
      const { session } = await createRegularNewChatSession({
        characterName,
        projectId: selectedProjectId ?? undefined,
        userId,
      });
      chatSessions?.addSession(session);
      persistLastSessionId(session.id);
      const href = `/chat?s=${encodeURIComponent(session.id)}`;
      if (!navigateChatSessionInPlace(href)) router.push(href);
    } catch (error) {
      console.error("Quick Panel の新規会話作成エラー:", error);
      toast.error("新規会話を作成できませんでした");
    } finally {
      setIsCreatingSession(false);
    }
  }, [chatSessions, isCreatingSession, router, selectedProjectId, userId]);

  return (
    <SidebarGroup data-testid="chat-quick-panel-navigation">
      <div className="flex items-center justify-between gap-2 px-2">
        <SidebarGroupLabel>最近の会話</SidebarGroupLabel>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="新規会話"
          title="新規会話"
          onClick={() => void handleCreateSession()}
          disabled={isCreatingSession}
        >
          {isCreatingSession ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <Plus className="size-4" />
          )}
        </Button>
      </div>
      <SidebarGroupContent>
        <SidebarMenu>
          {chatSessions?.sessionsError && (
            <li className="px-4 py-4 text-center text-xs text-destructive">
              {chatSessions.sessionsError}
            </li>
          )}
          {!chatSessions?.sessionsError && sessions.length === 0 && (
            <li className="px-4 py-4 text-center text-xs text-muted-foreground">
              会話がありません
            </li>
          )}
          {!chatSessions?.sessionsError &&
            sessions.map((session) => {
              const href = `/chat?s=${encodeURIComponent(session.id)}`;
              const working = isChatSessionWorking(session);
              const unread = isChatSessionUnread(session);
              return (
                <SidebarMenuItem key={session.id} data-chat-session-id={session.id}>
                  <SidebarMenuButton
                    render={
                      <a
                        href={href}
                        onClick={(event) =>
                          handleSessionNavigation(event, href, router)
                        }
                      />
                    }
                    className="group/session-item"
                  >
                    {working ? (
                      <Loader2 className="size-4 shrink-0 animate-spin text-sky-400" />
                    ) : (
                      <MessageSquare className="size-4 shrink-0" />
                    )}
                    {unread && (
                      <span
                        aria-label="未読の完了応答"
                        title="未読の完了応答"
                        className="size-2 shrink-0 rounded-full bg-sky-400"
                      />
                    )}
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-sm">
                        {session.title || "無題の会話"}
                      </span>
                      <span className="truncate text-xs text-muted-foreground">
                        {session.character_name}
                        {session.last_activity && (
                          <> · {formatRelativeTime(session.last_activity)}</>
                        )}
                      </span>
                    </span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              );
            })}
          {chatSessions?.sessions && chatSessions.sessions.length > QUICK_SESSION_LIMIT && (
            <li className="px-3 pt-2">
              <a
                href="/chat"
                className="block rounded-md px-2 py-1.5 text-center text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
                onClick={(event) => handleSessionNavigation(event, "/chat", router)}
              >
                履歴をすべて表示
              </a>
            </li>
          )}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
