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
import useSWR from "swr";
import {
  Plus,
  Menu,
  MessageSquare,
  MoreHorizontal,
  Trash2,
  Pencil,
} from "lucide-react";
import {
  chatApi,
  type ConversationSession,
  type ScenarioLogResponse,
} from "@/lib/chat-api";
import { useProject } from "@/contexts/project-context";
import { formatRelativeTime } from "@/lib/utils";
import { toast } from "sonner";
import { useChatSessions } from "@/contexts/chat-session-context";
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
  sortChatSessions,
  type ChatHistoryView,
} from "@/lib/chat-session-view";
import {
  ChatSessionContextMenu,
  type ChatSessionContextMenuState,
} from "@/components/chat/session-context-menu";

const CHAT_HISTORY_VIEW_KEY = "aoitalk-chat-history-view";

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
  const {
    sessions,
    sessionsError,
    fetchSessions,
    addSession,
    removeSession,
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
    const saved = localStorage.getItem(CHAT_HISTORY_VIEW_KEY);
    if (saved === "timeline" || saved === "project") setHistoryView(saved);
  }, []);

  useEffect(() => {
    localStorage.setItem(CHAT_HISTORY_VIEW_KEY, historyView);
  }, [historyView]);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

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

  // シナリオログコンテキストの取得を SWR に委譲（キーは activeSessionId 単位）。
  // activeSessionId が無ければキー null で未取得、取得中は data undefined のため
  // scenarioLogContext は null（従来の sessionId 不一致時 null と同義）。失敗時は
  // fetcher が null を返し従来の catch → null と一致させる。
  const { data: scenarioLogContext = null } =
    useSWR<ScenarioLogResponse | null>(
      activeSessionId
        ? ["chat-sidebar/scenario-log-context", activeSessionId]
        : null,
      ([, sessionId]: [string, string]) =>
        chatApi
          .getScenarioLogContextByConversation(sessionId)
          .catch(() => null),
      {
        revalidateOnFocus: false,
        revalidateOnReconnect: false,
      },
    );

  const handleCreateSession = useCallback(async () => {
    if (isCreatingSession) return;

    setIsCreatingSession(true);
    try {
      const characterName = await chatApi.getCurrentCharacterName();

      const data = await chatApi.createSession(
        characterName,
        selectedProjectId ?? undefined,
      );
      addSession(data.session);
      localStorage.setItem("aoitalk_last_session_id", data.session.id);

      const href = `/chat?s=${encodeURIComponent(data.session.id)}`;
      if (!navigateChatSessionInPlace(href)) {
        router.push(href);
      }
    } catch (err) {
      console.error("新規会話作成エラー:", err);
      localStorage.removeItem("aoitalk_last_session_id");
      if (!navigateChatSessionInPlace("/chat")) {
        router.push("/chat");
      }
    } finally {
      setIsCreatingSession(false);
    }
  }, [addSession, isCreatingSession, router, selectedProjectId]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      try {
        await chatApi.deleteSession(id);
        removeSession(id);
        if (activeSessionId === id) {
          localStorage.removeItem("aoitalk_last_session_id");
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

  const scenarioTitle = scenarioLogContext?.scenario?.title;
  const scenarioLogs = scenarioLogContext?.logs ?? [];

  return (
    <>
      {scenarioTitle && (
        <SidebarGroup>
          <div className="flex items-center justify-between px-2">
            <div className="min-w-0">
              <SidebarGroupLabel>ログ</SidebarGroupLabel>
              <div className="truncate px-2 text-xs text-muted-foreground">
                {scenarioTitle}
              </div>
            </div>
            <button
              onClick={handleCreateSession}
              disabled={isCreatingSession}
              className="p-1 rounded hover:bg-accent"
              aria-label="新規通常会話"
              title="新規通常会話"
            >
              <Plus className="size-4" />
              <span className="sr-only">新規通常会話</span>
            </button>
          </div>
          <SidebarGroupContent>
            <SidebarMenu>
              {scenarioLogs.length === 0 && (
                <li className="px-4 py-6 text-center text-xs text-muted-foreground">
                  ログがありません
                </li>
              )}
              {scenarioLogs.map((log) => {
                const isActive =
                  !!activeSessionId &&
                  log.conversation_session_id === activeSessionId;
                const href = log.href;
                return (
                  <SidebarMenuItem key={`${log.type}:${log.id}`}>
                    <SidebarMenuButton
                      isActive={isActive}
                      render={
                        href ? (
                          <button
                            type="button"
                            onClick={(event) =>
                              handleSidebarAnchorNavigation(event, href)
                            }
                          />
                        ) : (
                          <button type="button" disabled />
                        )
                      }
                      className="group/session-item"
                    >
                      <MessageSquare className="size-4 shrink-0" />
                      <div className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-sm">
                          {log.target_label || log.title}
                        </span>
                        <span className="truncate text-xs text-muted-foreground">
                          {log.type_label}
                          {log.updated_at && (
                            <> &middot; {formatRelativeTime(log.updated_at)}</>
                          )}
                          {log.count > 0 && <> &middot; {log.count}件</>}
                        </span>
                      </div>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      )}

      <SidebarGroup>
        <div className="flex items-center justify-between px-2">
          <SidebarGroupLabel>会話履歴</SidebarGroupLabel>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={handleCreateSession}
              disabled={isCreatingSession}
              className="rounded p-1 hover:bg-accent disabled:opacity-50"
              aria-label="新規会話"
              title="新規会話"
            >
              <Plus className="size-4" />
            </button>
            <DropdownMenu>
              <DropdownMenuTrigger
                className="rounded p-1 hover:bg-accent"
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
        <SidebarGroupContent>
          <SidebarMenu>
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
                    <li key={row.key} className="list-none px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {row.label} <span className="font-normal">({row.count})</span>
                    </li>
                  );
                }
                const s = row.session;
                const href = `/chat?s=${encodeURIComponent(s.id)}`;
                const projectName = s.project_id
                  ? (projectNameById.get(s.project_id) ?? "不明なプロジェクト")
                  : null;
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
                          onClick={(event) =>
                            handleSidebarAnchorNavigation(event, href)
                          }
                        />
                      }
                      className="group/session-item"
                    >
                      <MessageSquare className="size-4 shrink-0" />
                      <div className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-sm">
                          {s.title || "無題の会話"}
                        </span>
                        <span className="truncate text-xs text-muted-foreground">
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
        <DialogContent className="sm:max-w-md">
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
