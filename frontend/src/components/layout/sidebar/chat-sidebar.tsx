"use client";

import { useRouter, useSearchParams } from "next/navigation";
import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  type FormEvent,
  type MouseEvent,
} from "react";
import {
  Plus,
  Menu,
  MessageSquare,
  Loader2,
  MoreHorizontal,
  Trash2,
  Pencil,
} from "lucide-react";
import {
  chatApi,
  type ConversationSession,
} from "@/lib/chat-api";
import { useProject } from "@/contexts/project-context";
import { formatRelativeTime } from "@/lib/utils";
import { toast } from "sonner";
import { useChatSessions } from "@/contexts/chat-session-context";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { createRegularNewChatSession } from "@/lib/create-regular-new-chat-session";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import {
  CHAT_SESSION_NAVIGATION_EVENT,
  navigateChatSessionInPlace,
  readChatSessionIdFromLocation,
} from "@/lib/chat-navigation";
import {
  groupChatSessionsByProject,
  isChatSessionUnread,
  isChatSessionWorking,
  sortChatSessions,
  type ChatHistoryView,
} from "@/lib/chat-session-view";
import {
  ChatSessionContextMenu,
  type ChatSessionContextMenuState,
} from "@/components/chat/session-context-menu";

const CHAT_HISTORY_VIEW_KEY = "aoitalk-chat-history-view";
const CHAT_LAST_SESSION_KEY = "aoitalk_last_session_id";

function readChatHistoryView(): ChatHistoryView | null {
  if (typeof window === "undefined") return null;
  try {
    const saved = window.localStorage.getItem(CHAT_HISTORY_VIEW_KEY);
    return saved === "timeline" || saved === "project" ? saved : null;
  } catch {
    // localStorageが無効な環境では既定の表示方法を維持する。
    return null;
  }
}

function persistChatHistoryView(historyView: ChatHistoryView) {
  try {
    window.localStorage.setItem(CHAT_HISTORY_VIEW_KEY, historyView);
  } catch {
    // localStorageが無効でも表示方法の変更はメモリ上で維持する。
  }
}

function persistLastSessionId(sessionId: string) {
  try {
    window.localStorage.setItem(CHAT_LAST_SESSION_KEY, sessionId);
  } catch {
    // localStorageが無効でも会話一覧の更新・遷移は継続する。
  }
}

function clearLastSessionId() {
  try {
    window.localStorage.removeItem(CHAT_LAST_SESSION_KEY);
  } catch {
    // localStorageが無効でも会話一覧の更新・遷移は継続する。
  }
}

function handleSidebarAnchorNavigation(
  event: MouseEvent<HTMLAnchorElement | HTMLButtonElement>,
  href: string,
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
  if (!navigateChatSessionInPlace(href)) {
    window.location.href = href;
  }
}

// ─── チャット用サイドバー ───
export function ChatSidebar() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const searchParamSessionId = searchParams.get("s") || null;
  const [activeSessionId, setActiveSessionId] = useState(searchParamSessionId);
  const { selectedProjectId, allProjects } = useProject();
  const userId = useCurrentUserId();
  const {
    sessions,
    sessionsError,
    addSession,
    removeSession,
    updateSession,
    updateSessionTitle,
  } = useChatSessions();
  const [isCreatingSession, setIsCreatingSession] = useState(false);
  const [titleEditSession, setTitleEditSession] =
    useState<ConversationSession | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const [isUpdatingTitle, setIsUpdatingTitle] = useState(false);
  const [sessionContextMenu, setSessionContextMenu] =
    useState<ChatSessionContextMenuState | null>(null);
  const [historyView, setHistoryView] = useState<ChatHistoryView>("timeline");

  useEffect(() => {
    const saved = readChatHistoryView();
    if (saved) setHistoryView(saved);
  }, []);

  useEffect(() => {
    persistChatHistoryView(historyView);
  }, [historyView]);

  useEffect(() => {
    setActiveSessionId(searchParamSessionId);
  }, [searchParamSessionId]);

  useEffect(() => {
    const syncActiveSessionId = () => {
      setActiveSessionId(readChatSessionIdFromLocation());
    };

    window.addEventListener(
      CHAT_SESSION_NAVIGATION_EVENT,
      syncActiveSessionId,
    );
    window.addEventListener("popstate", syncActiveSessionId);
    return () => {
      window.removeEventListener(
        CHAT_SESSION_NAVIGATION_EVENT,
        syncActiveSessionId,
      );
      window.removeEventListener("popstate", syncActiveSessionId);
    };
  }, []);

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
      addSession(session);
      persistLastSessionId(session.id);

      const href = `/chat?s=${encodeURIComponent(session.id)}`;
      if (!navigateChatSessionInPlace(href)) {
        router.push(href);
      }
    } catch (err) {
      console.error("新規会話作成エラー:", err);
      clearLastSessionId();
      if (!navigateChatSessionInPlace("/chat")) {
        router.push("/chat");
      }
    } finally {
      setIsCreatingSession(false);
    }
  }, [addSession, isCreatingSession, router, selectedProjectId, userId]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      try {
        await chatApi.deleteSession(id);
        removeSession(id);
        if (activeSessionId === id) {
          clearLastSessionId();
          router.push("/chat");
        }
      } catch (err) {
        console.error("セッション削除エラー:", err);
      }
    },
    [activeSessionId, router, removeSession],
  );

  const closeTitleEditDialog = useCallback(() => {
    setTitleEditSession(null);
    setTitleDraft("");
  }, []);

  const handleOpenTitleEdit = useCallback((session: ConversationSession) => {
    setTitleEditSession(session);
    setTitleDraft(session.title || "");
  }, []);

  const handleTitleEditOpenChange = useCallback(
    (open: boolean) => {
      if (!open && !isUpdatingTitle) {
        closeTitleEditDialog();
      }
    },
    [closeTitleEditDialog, isUpdatingTitle],
  );

  const handleUpdateSessionTitle = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!titleEditSession) return;

      const nextTitle = titleDraft.trim();
      if (!nextTitle) {
        toast.error("タイトルを入力してください");
        return;
      }
      if (nextTitle.length > 200) {
        toast.error("タイトルは200文字以内で入力してください");
        return;
      }
      if (nextTitle === (titleEditSession.title || "")) {
        closeTitleEditDialog();
        return;
      }

      setIsUpdatingTitle(true);
      try {
        await chatApi.updateSessionTitle(titleEditSession.id, nextTitle);
        updateSessionTitle(titleEditSession.id, nextTitle);
        toast.success("タイトルを更新しました");
        closeTitleEditDialog();
      } catch (err) {
        console.error("セッションタイトル更新エラー:", err);
        toast.error("タイトルの更新に失敗しました");
      } finally {
        setIsUpdatingTitle(false);
      }
    },
    [
      closeTitleEditDialog,
      titleDraft,
      titleEditSession,
      updateSessionTitle,
    ],
  );

  const handleSessionContextMenu = useCallback(
    (event: MouseEvent, session: ConversationSession) => {
      event.preventDefault();
      event.stopPropagation();
      setSessionContextMenu({
        x: event.clientX,
        y: event.clientY,
        session,
      });
    },
    [],
  );

  const closeSessionContextMenu = useCallback(() => {
    setSessionContextMenu(null);
  }, []);

  const markSessionRead = useCallback(
    (sessionId: string) => {
      updateSession(sessionId, (session) => ({
        ...session,
        is_unread: false,
      }));
      void chatApi.markSessionRead(sessionId).catch(() => {
        // 読み込み自体は継続し、次回の履歴取得でサーバー状態へ戻す。
      });
    },
    [updateSession],
  );

  const sortedSessions = useMemo(() => sortChatSessions(sessions), [sessions]);

  const projectNameById = useMemo(
    () => new Map(allProjects.map((project) => [project.id, project.name])),
    [allProjects],
  );
  const projectGroups = useMemo(
    () => groupChatSessionsByProject(sessions, projectNameById),
    [projectNameById, sessions],
  );
  const visibleRows: Array<
    | { kind: "group"; key: string; label: string; count: number }
    | { kind: "session"; session: ConversationSession }
  > = historyView === "project"
    ? projectGroups.flatMap((group) => [
        { kind: "group" as const, key: group.key, label: group.label, count: group.sessions.length },
        ...group.sessions.map((session) => ({ kind: "session" as const, session })),
      ])
    : sortedSessions.map((session) => ({ kind: "session" as const, session }));

  useEffect(() => {
    if (!activeSessionId) return;
    const row = [...document.querySelectorAll<HTMLElement>("[data-chat-session-id]")].find(
      (element) => element.dataset.chatSessionId === activeSessionId,
    );
    row?.scrollIntoView({ block: "nearest" });
  }, [activeSessionId, historyView, visibleRows.length]);

  return (
    <>
      <SidebarGroup className="min-h-0 flex-1 overflow-hidden bg-surface-charcoal p-0 text-on-surface">
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-border-subtle px-4">
          <SidebarGroupLabel className="h-auto p-0 text-base font-semibold tracking-normal text-on-surface">
            History
          </SidebarGroupLabel>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={handleCreateSession}
              disabled={isCreatingSession}
              className="flex size-6 items-center justify-center rounded text-text-secondary transition-colors hover:bg-surface-container-high hover:text-primary disabled:opacity-50"
              aria-label="新規会話"
              title="新規会話"
            >
              <Plus className="size-4" />
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger
                className="flex size-6 items-center justify-center rounded text-text-secondary transition-colors hover:bg-surface-container-high hover:text-primary"
                aria-label="表示方法"
                title="表示方法"
              >
                <Menu className="size-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>表示方法</DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={historyView}
                  onValueChange={setHistoryView}
                >
                  <DropdownMenuRadioItem value="timeline" closeOnClick>
                    時系列
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="project" closeOnClick>
                    プロジェクト別
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        <SidebarGroupContent className="min-h-0 flex-1 overflow-y-auto px-2 py-3">
          <SidebarMenu className="gap-1">
            {sessionsError && (
              <li className="px-4 py-6 text-center text-xs text-destructive">
                {sessionsError}
              </li>
            )}
            {!sessionsError && sortedSessions.length === 0 && (
              <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                会話がありません
              </li>
            )}
            {!sessionsError &&
              visibleRows.map((row) => {
                if (row.kind === "group") {
                  return (
              <li key={row.key} className="list-none px-2 pb-1 pt-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-text-secondary first:pt-0">
                      {row.label} <span className="font-normal">({row.count})</span>
                    </li>
                  );
                }
                const s = row.session;
                const href = `/chat?s=${encodeURIComponent(s.id)}`;
                const projectName = s.project_id
                  ? (projectNameById.get(s.project_id) ?? "不明なプロジェクト")
                  : null;
                const isWorking = isChatSessionWorking(s);
                const isUnread = isChatSessionUnread(s, activeSessionId);
                return (
                  <SidebarMenuItem
                    key={s.id}
                    data-chat-session-id={s.id}
                    onContextMenu={(event) =>
                      handleSessionContextMenu(event, s)
                    }
                  >
                    <SidebarMenuButton
                      isActive={activeSessionId === s.id}
                      render={
                        <button
                          type="button"
                          onClick={(event) => {
                            markSessionRead(s.id);
                            handleSidebarAnchorNavigation(event, href);
                          }}
                        />
                      }
                      className="group/session-item h-auto min-h-10 rounded-md border-l-2 border-transparent px-2 py-1.5 text-on-surface-variant data-active:!border-primary data-active:!bg-surface-slate data-active:!font-medium data-active:!text-on-surface data-active:!shadow-none data-active:hover:!bg-surface-slate hover:bg-surface-container hover:text-on-surface"
                    >
                      {isWorking ? (
                        <Loader2
                          className="size-4 shrink-0 animate-spin text-sky-400 group-data-active/menu-button:!text-primary"
                          aria-label="エージェントが作業中"
                        />
                      ) : (
                        <MessageSquare className="size-4 shrink-0 group-data-active/menu-button:!text-primary" />
                      )}
                      {isUnread && (
                        <span
                          aria-label="未読の完了応答"
                          title="未読の完了応答"
                          className="size-2 shrink-0 rounded-full bg-primary"
                        />
                      )}
                      <div className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-[13px] leading-[18px]">
                          {s.title || "無題の会話"}
                        </span>
                        <span className="truncate text-[11px] leading-4 text-text-secondary group-data-active/menu-button:!text-on-surface/70">
                          {projectName && (
                            <>プロジェクト: {projectName} &middot; </>
                          )}
                          {s.character_name}
                          {s.last_activity && (
                            <> &middot; {formatRelativeTime(s.last_activity)}</>
                          )}
                        </span>
                      </div>
                    </SidebarMenuButton>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        className="absolute right-1 top-1.5 rounded p-0.5 opacity-0 transition-opacity hover:bg-accent group-hover/menu-item:opacity-100 data-[state=open]:opacity-100"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <MoreHorizontal className="size-4" />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent side="right" align="start">
                        <DropdownMenuItem
                          mnemonic="E"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleOpenTitleEdit(s);
                          }}
                        >
                          <Pencil className="mr-2 size-4" />
                          タイトルを編集
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          mnemonic="D"
                          className="text-destructive focus:text-destructive"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteSession(s.id);
                          }}
                        >
                          <Trash2 className="mr-2 size-4" />
                          削除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </SidebarMenuItem>
                );
              })}
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
      <ChatSessionContextMenu
        menu={sessionContextMenu}
        onClose={closeSessionContextMenu}
        onRename={handleOpenTitleEdit}
        onDelete={handleDeleteSession}
      />
      <Dialog
        open={titleEditSession != null}
        onOpenChange={handleTitleEditOpenChange}
      >
        <DialogContent size="md">
          <DialogHeader>
            <DialogTitle>タイトルを編集</DialogTitle>
          </DialogHeader>
          <form className="space-y-4" onSubmit={handleUpdateSessionTitle}>
            <Input
              autoFocus
              value={titleDraft}
              maxLength={200}
              placeholder="会話タイトル"
              onChange={(event) => setTitleDraft(event.target.value)}
            />
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={closeTitleEditDialog}
                disabled={isUpdatingTitle}
              >
                キャンセル
              </Button>
              <Button
                type="submit"
                disabled={isUpdatingTitle || !titleDraft.trim()}
              >
                保存
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
