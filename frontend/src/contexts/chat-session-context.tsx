"use client";

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  type ReactNode,
} from "react";
import { chatApi, type ConversationSession } from "@/lib/chat-api";

export const CHAT_SESSION_TITLE_UPDATED_EVENT = "aoitalk-chat-session-title-updated";

type ChatSessionContextValue = {
  sessions: ConversationSession[];
  sessionsError: string | null;
  setSessions: React.Dispatch<React.SetStateAction<ConversationSession[]>>;
  fetchSessions: () => Promise<void>;
  addSession: (session: ConversationSession) => void;
  removeSession: (id: string) => void;
  updateSessionTitle: (id: string, title: string) => void;
  bumpSession: (id: string) => void;
};

const ChatSessionContext = createContext<ChatSessionContextValue | null>(null);

export function ChatSessionProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const optimisticSessionIdsRef = useRef<Set<string>>(new Set());

  const mergeFetchedSessions = useCallback(
    (
      fetched: ConversationSession[],
      previous: ConversationSession[],
    ): ConversationSession[] => {
      const fetchedIds = new Set(fetched.map((session) => session.id));
      const optimisticSessions = previous.filter(
        (session) =>
          optimisticSessionIdsRef.current.has(session.id) &&
          !fetchedIds.has(session.id),
      );

      for (const session of fetched) {
        optimisticSessionIdsRef.current.delete(session.id);
      }

      return [...optimisticSessions, ...fetched];
    },
    [],
  );

  const fetchSessions = useCallback(async () => {
    try {
      const data = await chatApi.listSessions();
      setSessions((prev) => mergeFetchedSessions(data.conversations, prev));
      setSessionsError(null);
    } catch (err) {
      console.error("会話履歴取得エラー:", err);
      setSessionsError("会話履歴を取得できません");
    }
  }, [mergeFetchedSessions]);

  const addSession = useCallback((session: ConversationSession) => {
    optimisticSessionIdsRef.current.add(session.id);
    setSessions((prev) => [
      session,
      ...prev.filter((item) => item.id !== session.id),
    ]);
  }, []);

  const removeSession = useCallback((id: string) => {
    optimisticSessionIdsRef.current.delete(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const updateSessionTitle = useCallback((id: string, title: string) => {
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, title } : s))
    );
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent(CHAT_SESSION_TITLE_UPDATED_EVENT, {
          detail: { sessionId: id, title },
        }),
      );
    }
  }, []);

  const bumpSession = useCallback((id: string) => {
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === id);
      if (idx < 0) return prev;
      const target = { ...prev[idx], last_activity: new Date().toISOString() };
      if (idx === 0) return [target, ...prev.slice(1)];
      return [target, ...prev.slice(0, idx), ...prev.slice(idx + 1)];
    });
  }, []);

  return (
    <ChatSessionContext.Provider
      value={{
        sessions,
        sessionsError,
        setSessions,
        fetchSessions,
        addSession,
        removeSession,
        updateSessionTitle,
        bumpSession,
      }}
    >
      {children}
    </ChatSessionContext.Provider>
  );
}

export function useChatSessions() {
  const ctx = useContext(ChatSessionContext);
  if (!ctx) throw new Error("useChatSessions must be used within ChatSessionProvider");
  return ctx;
}
