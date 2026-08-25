"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import {
  File,
  CheckSquare,
  FolderKanban,
  AppWindow,
  BookOpen,
  MessageSquare,
} from "lucide-react";
import { explorerSearch, type SearchResult } from "@/lib/explorer-api";
import { useProject } from "@/contexts/project-context";
import {
  useChatSessionsOptional,
} from "@/contexts/chat-session-context";
import type { ConversationSession } from "@/lib/chat-api";
import { TASK_STATUS_LABELS } from "@/lib/task-status";
import { appsApi, type AppSummary } from "@/lib/apps-api";
import { compactEntityId, matchEntityId, shortEntityId } from "@/lib/entity-id";
import { formatRelativeTime } from "@/lib/utils";

export type MentionType =
  | "file"
  | "task"
  | "project"
  | "app"
  | "docs"
  | "chat_session";

export interface MentionItem {
  [key: string]: unknown;
  type: MentionType;
  id: string;
  name: string;
  detail?: string;
}

interface MentionMenuProps {
  query: string;
  onSelect: (item: MentionItem) => void;
  onClose: () => void;
  projectId?: string | null;
  /** 現在開いている会話。自分自身を参照候補から除外するために使う。 */
  sessionId?: string | null;
}

type TaskMentionCandidate = {
  id: string;
  title: string;
  status: string;
  project_name?: string | null;
};

type DocsMentionCandidate = {
  id: string;
  title: string;
  tags?: string[] | null;
  project_id?: string | null;
  parent_title?: string | null;
};

type ChatSessionMentionCandidate = ConversationSession;
const EMPTY_CHAT_SESSIONS: ConversationSession[] = [];

function getChatSessionProjectId(session: ChatSessionMentionCandidate) {
  if (session.project_id) return session.project_id;
  const contextProjectId = session.context?.project_id;
  return typeof contextProjectId === "string" ? contextProjectId : null;
}

function normalizeSearchText(value: string) {
  return value.toLowerCase().trim();
}

function getTaskDetail(task: TaskMentionCandidate) {
  const statusLabel = TASK_STATUS_LABELS[task.status] ?? task.status;
  const shortId = shortEntityId(task.id);
  return [statusLabel, task.project_name, shortId].filter(Boolean).join(" / ");
}

function getDocsDetail(node: DocsMentionCandidate) {
  const shortId = shortEntityId(node.id);
  const compactId = compactEntityId(node.id);
  const project = node.project_id ? `Project ${node.project_id}` : null;
  return [shortId, compactId, project].filter(Boolean).join(" / ");
}

function getChatSessionDetail(session: ChatSessionMentionCandidate) {
  const shortId = shortEntityId(session.id);
  const projectId = getChatSessionProjectId(session);
  const project = projectId ? `Project ${shortEntityId(projectId)}` : null;
  const lastActivity = formatRelativeTime(session.last_activity);
  return [
    session.character_name,
    shortId,
    lastActivity,
    project,
  ]
    .filter(Boolean)
    .join(" / ");
}

function matchesChatSession(
  session: ChatSessionMentionCandidate,
  query: string,
) {
  if (!query) return true;
  const normalizedQuery = normalizeSearchText(query);
  const title = normalizeSearchText(session.title || "無題の会話");
  const character = normalizeSearchText(session.character_name);
  const projectId = normalizeSearchText(getChatSessionProjectId(session) ?? "");
  return (
    title.includes(normalizedQuery) ||
    character.includes(normalizedQuery) ||
    projectId.includes(normalizedQuery) ||
    matchEntityId(session.id, query) !== null
  );
}

export function MentionMenu({
  query,
  onSelect,
  onClose,
  projectId,
  sessionId,
}: MentionMenuProps) {
  const { projects } = useProject();
  const chatSessionsContext = useChatSessionsOptional();
  const fetchSessions = chatSessionsContext?.fetchSessions;
  const sessions = chatSessionsContext?.sessions ?? EMPTY_CHAT_SESSIONS;
  const [fileResults, setFileResults] = useState<SearchResult[]>([]);
  const [taskResults, setTaskResults] = useState<TaskMentionCandidate[]>([]);
  const [appResults, setAppResults] = useState<AppSummary[]>([]);
  const [docsResults, setDocsResults] = useState<DocsMentionCandidate[]>([]);
  const [selectionState, setSelectionState] = useState({ query: "", index: 0 });
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const docsAbortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const hasQuery = query.length > 0;
  // projectId は ChatComposer が解決した会話の実効スコープだけを使う。
  // グローバルな selectedProjectId へ fallback すると、プロジェクトなしの
  // 会話で別プロジェクトの候補を誤って表示してしまう。
  const effectiveProjectId =
    projectId && !projectId.startsWith("remote:") ? projectId : null;
  const currentSessionId = normalizeSearchText(sessionId ?? "");
  // Docsは会話の実効projectだけを正とし、グローバル選択projectへfallbackしない。
  // App-only / Story会話で別projectの候補を誤表示しないための境界。
  const docsProjectId = effectiveProjectId;

  useEffect(() => {
    // ChatPage/サイドバーが既に取得している共有セッション状態へ乗る。
    // fetchSessions は context 内で in-flight リクエストを束ねるため、
    // メニュー表示のたびに一覧 API を重複呼び出ししない。
    if (fetchSessions) void fetchSessions();
  }, [fetchSessions]);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    docsAbortRef.current?.abort();
    const docsAbortController = new AbortController();
    docsAbortRef.current = docsAbortController;
    let cancelled = false;

    debounceRef.current = setTimeout(async () => {
      const root = effectiveProjectId
        ? `_projects/project_${effectiveProjectId}`
        : "";

      if (hasQuery) {
        try {
          const res = await explorerSearch(query, root, 8);
          if (res.success) setFileResults(res.results);
        } catch {
          // ignore
        }
      } else {
        setFileResults([]);
      }

      try {
        const params = new URLSearchParams();
        if (effectiveProjectId) params.set("project_id", effectiveProjectId);
        const qs = params.toString();
        const res = await fetch(`/api/tasks${qs ? `?${qs}` : ""}`);
        if (res.ok) {
          const data = await res.json();
          const tasks = (data.tasks || data || [])
            .filter((task: TaskMentionCandidate) => task.status !== "closed")
            .filter((task: TaskMentionCandidate) => {
              if (!hasQuery) return true;
              const title = task.title.toLowerCase();
              const projectName = task.project_name?.toLowerCase() ?? "";
              return (
                title.includes(query.toLowerCase()) ||
                matchEntityId(task.id, query) !== null ||
                projectName.includes(query.toLowerCase())
              );
            })
            .slice(0, 8);
          setTaskResults(tasks);
        }
      } catch {
        // ignore
      }

      try {
        const response = await appsApi.list(effectiveProjectId || undefined);
        const normalizedQuery = normalizeSearchText(query);
        const apps = (response.apps || [])
          .filter((app) => {
            if (!hasQuery) return true;
            const haystack = normalizeSearchText(
              `${app.name} ${app.slug} ${app.description || ""}`,
            );
            return haystack.includes(normalizedQuery);
          })
          .slice(0, 8);
        setAppResults(apps);
      } catch {
        setAppResults([]);
      }

      if (!docsProjectId) {
        setDocsResults([]);
      } else {
        try {
          const params = new URLSearchParams({
            project: docsProjectId,
            limit: "8",
          });
          if (hasQuery) params.set("q", query);
          const response = await fetch(
            `/api/docs/search?${params.toString()}`,
            {
              signal: docsAbortController.signal,
            },
          );
          if (response.ok) {
            const data = await response.json();
            const results = Array.isArray(data?.results)
              ? data.results
              : Array.isArray(data?.data?.results)
                ? data.data.results
                : [];
            if (!cancelled) {
              setDocsResults(
                (results || [])
                  .filter(
                    (node: DocsMentionCandidate) =>
                      node.id &&
                      node.title &&
                      (!node.project_id || node.project_id === docsProjectId),
                  )
                  .slice(0, 8),
              );
            }
          } else {
            if (!cancelled) setDocsResults([]);
          }
        } catch {
          if (!cancelled) setDocsResults([]);
        }
      }
    }, 300);

    return () => {
      cancelled = true;
      if (debounceRef.current) clearTimeout(debounceRef.current);
      docsAbortController.abort();
    };
  }, [docsProjectId, effectiveProjectId, hasQuery, query]);

  const filteredProjects = useMemo(
    () =>
      (hasQuery
        ? projects.filter((project) =>
            project.name.toLowerCase().includes(query.toLowerCase()),
          )
        : projects
      ).slice(0, 5),
    [hasQuery, projects, query],
  );

  const allItems = useMemo<MentionItem[]>(
    () => [
      ...(hasQuery ? fileResults : []).map((file) => ({
        type: "file" as const,
        id: file.path,
        name: file.name,
        detail: file.path,
      })),
      ...taskResults.map((task) => ({
        type: "task" as const,
        id: task.id,
        name: task.title,
        detail: getTaskDetail(task),
      })),
      ...filteredProjects.map((project) => ({
        type: "project" as const,
        id: project.id,
        name: project.name,
      })),
      ...appResults.map((app) => ({
        type: "app" as const,
        id: app.id,
        name: app.name,
        detail: app.slug,
      })),
      ...docsResults.map((node) => ({
        type: "docs" as const,
        id: node.id,
        name: node.title,
        detail: getDocsDetail(node),
      })),
      ...sessions
        .filter(
          (session) => normalizeSearchText(session.id) !== currentSessionId,
        )
        .filter((session) => matchesChatSession(session, query))
        .slice(0, 8)
        .map((session) => ({
          type: "chat_session" as const,
          id: session.id,
          name: session.title || "無題の会話",
          detail: getChatSessionDetail(session),
        })),
    ],
    [
      appResults,
      docsResults,
      fileResults,
      filteredProjects,
      hasQuery,
      query,
      currentSessionId,
      sessions,
      taskResults,
    ],
  );

  const selectedIndex =
    selectionState.query === query
      ? Math.min(selectionState.index, Math.max(allItems.length - 1, 0))
      : 0;

  const setSelectedIndex = useCallback(
    (next: number | ((prev: number) => number)) => {
      setSelectionState((prev) => {
        const prevIndex = prev.query === query ? prev.index : 0;
        const nextIndex = typeof next === "function" ? next(prevIndex) : next;
        return { query, index: nextIndex };
      });
    },
    [query],
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!allItems.length) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, allItems.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (allItems[selectedIndex]) {
          onSelect(allItems[selectedIndex]);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    },
    [allItems, onClose, onSelect, selectedIndex, setSelectedIndex],
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  useEffect(() => {
    const el = listRef.current?.children[selectedIndex] as
      | HTMLElement
      | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  if (allItems.length === 0 && query.length > 0) {
    return (
      <div className="rounded-lg border bg-popover p-3 shadow-md">
        <p className="text-xs text-muted-foreground">候補が見つかりません</p>
      </div>
    );
  }

  if (allItems.length === 0) return null;

  const iconMap = {
    file: <File className="size-3.5 text-mention-file" />,
    task: <CheckSquare className="size-3.5 text-mention-task" />,
    project: <FolderKanban className="size-3.5 text-mention-project" />,
    app: <AppWindow className="size-3.5 text-mention-app" />,
    docs: <BookOpen className="size-3.5 text-mention-docs" />,
    chat_session: <MessageSquare className="size-3.5 text-mention-chat" />,
  };

  const labelMap = {
    file: "ファイル",
    task: "タスク",
    project: "プロジェクト",
    app: "アプリ",
    docs: "Docs",
    chat_session: "チャットセッション",
  };

  return (
    <div
      className="max-h-64 overflow-auto rounded-lg border bg-popover shadow-md"
      ref={listRef}
    >
      {(["file", "task", "project", "app", "docs", "chat_session"] as const).map((type) => {
        const items = allItems.filter((item) => item.type === type);
        if (items.length === 0) return null;

        return (
          <div key={type}>
            <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {labelMap[type]}
            </div>
            {items.map((item) => {
              const idx = allItems.indexOf(item);
              return (
                <button
                  key={`${item.type}-${item.id}`}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-sm hover:bg-accent/50 ${
                    idx === selectedIndex ? "bg-accent" : ""
                  }`}
                  onClick={() => onSelect(item)}
                  onMouseEnter={() => setSelectedIndex(idx)}
                >
                  {iconMap[item.type]}
                  <span className="truncate">{item.name}</span>
                  {item.detail && (
                    <span
                      className="ml-auto max-w-[120px] truncate text-[10px] text-muted-foreground"
                      title={
                        item.type === "docs" ||
                        item.type === "task" ||
                        item.type === "chat_session"
                          ? item.id
                          : item.detail
                      }
                    >
                      {item.detail}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
