"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { useRouter } from "next/navigation";
import { chatApi, type ConversationSession } from "@/lib/chat-api";
import {
  CHAT_SESSION_NAVIGATION_EVENT,
  navigateChatSessionInPlace,
  readChatSessionIdFromLocation,
} from "@/lib/chat-navigation";
import {
  flattenChatSessionGroups,
  getAdjacentChatSessionId,
  groupChatSessionsByProject,
  sortChatSessions,
} from "@/lib/chat-session-view";

const LAST_SESSION_KEY = "aoitalk_last_session_id";

function isScenarioWorkflowSession(session: ConversationSession) {
  const characterName = session.character_name || "";
  return (
    /^scenario_roleplay:[^:]+:[^:]+$/.test(characterName) ||
    characterName.startsWith("scenario_") ||
    characterName.startsWith("trpg_room_") ||
    session.title?.startsWith("[シナリオ]") ||
    session.title?.startsWith("[執筆]") ||
    session.title?.startsWith("[TRPG]")
  );
}

type UseActiveChatSessionArgs = {
  searchParamSessionId: string | null;
  router: ReturnType<typeof useRouter>;
  allProjects: Array<{ id: string; name: string }>;
  sessions: ConversationSession[];
};

/**
 * アクティブセッションID の state / ref 同期、URL・キーボードナビゲーション、
 * 最終セッション復元を担うフック。
 * `page.tsx` の該当ロジックを挙動不変で移設したもの。
 */
export function useActiveChatSession({
  searchParamSessionId,
  router,
  allProjects,
  sessions,
}: UseActiveChatSessionArgs) {
  const [activeSessionId, setActiveSessionId] = useState(searchParamSessionId);
  const activeSessionIdRef = useRef(activeSessionId);

  useEffect(() => {
    const handleSessionShortcut = (event: KeyboardEvent) => {
      if (
        !event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
        (event.key !== "ArrowUp" && event.key !== "ArrowDown") ||
        event.isComposing || event.keyCode === 229
      ) return;

      const target = event.target as HTMLElement | null;
      const blocked = target?.closest(
        '[role="dialog"], [role="menu"], [role="listbox"], [role="combobox"], [data-radix-popper-content-wrapper]'
      );
      if (blocked) return;

      const savedView = localStorage.getItem("aoitalk-chat-history-view");
      const view = savedView === "project" ? "project" : "timeline";
      const projectNameById = new Map(allProjects.map((project) => [project.id, project.name]));
      const ordered = view === "project"
        ? flattenChatSessionGroups(groupChatSessionsByProject(sessions, projectNameById))
        : sortChatSessions(sessions);
      const nextId = getAdjacentChatSessionId(
        ordered,
        activeSessionIdRef.current,
        event.key === "ArrowDown" ? "down" : "up",
      );
      if (!nextId || nextId === activeSessionIdRef.current) return;
      event.preventDefault();
      navigateChatSessionInPlace(`/chat?s=${encodeURIComponent(nextId)}`);
    };
    window.addEventListener("keydown", handleSessionShortcut);
    return () => window.removeEventListener("keydown", handleSessionShortcut);
  }, [allProjects, sessions]);

  useEffect(() => {
    setActiveSessionId(searchParamSessionId);
  }, [searchParamSessionId]);

  useEffect(() => {
    const syncActiveSessionId = () => {
      setActiveSessionId(readChatSessionIdFromLocation());
    };

    window.addEventListener(CHAT_SESSION_NAVIGATION_EVENT, syncActiveSessionId);
    window.addEventListener("popstate", syncActiveSessionId);
    return () => {
      window.removeEventListener(
        CHAT_SESSION_NAVIGATION_EVENT,
        syncActiveSessionId,
      );
      window.removeEventListener("popstate", syncActiveSessionId);
    };
  }, []);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  const activateSession = useCallback((sessionId: string) => {
    activeSessionIdRef.current = sessionId;
    setActiveSessionId(sessionId);
  }, []);

  // 最後の通常チャットセッションIDを復元（シナリオ系は専用導線からのみ復元）
  useEffect(() => {
    if (activeSessionId) return;

    const lastId = localStorage.getItem(LAST_SESSION_KEY);
    if (!lastId) return;

    let cancelled = false;

    (async () => {
      try {
        const data = await chatApi.resumeSession(lastId);
        if (cancelled) return;

        if (isScenarioWorkflowSession(data.session)) {
          localStorage.removeItem(LAST_SESSION_KEY);
          return;
        }

        router.replace(`/chat?s=${lastId}`);
      } catch {
        if (!cancelled) {
          localStorage.removeItem(LAST_SESSION_KEY);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeSessionId, router]);

  return {
    activeSessionId,
    activeSessionIdRef,
    setActiveSessionId,
    activateSession,
  };
}
