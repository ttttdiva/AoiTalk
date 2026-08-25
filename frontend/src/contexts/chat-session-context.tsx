"use client";

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  useEffect,
  type ReactNode,
} from "react";
import { chatApi, type ConversationSession } from "@/lib/chat-api";

type ChatSessionContextValue = {
  sessions: ConversationSession[];
  sessionsError: string | null;
  fetchSessions: () => Promise<void>;
  addSession: (session: ConversationSession) => void;
  /** Register a route-created session and optionally mark its empty state idle. */
  registerSession: (
    session: ConversationSession,
    options?: { generationReady?: boolean; activate?: boolean },
  ) => void;
  requestedSessionId: string | null;
  activateSession: (sessionId: string) => void;
  clearRequestedSession: () => void;
  isGenerationReadySession: (sessionId: string) => boolean;
  consumeGenerationReadySession: (sessionId: string) => boolean;
  upsertSession: (session: ConversationSession) => void;
  removeSession: (id: string) => void;
  updateSession: (
    id: string,
    updater: (session: ConversationSession) => ConversationSession,
  ) => void;
  updateSessionTitle: (id: string, title: string) => void;
  bumpSession: (id: string) => void;
};

/** 会話履歴はProviderを正本にして全Workspace/Quick Panelから共有する。 */
const SESSIONS_REFRESH_INTERVAL_MS = 15000;

const ChatSessionContext = createContext<ChatSessionContextValue | null>(null);

export function ChatSessionProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<ConversationSession[]>([]);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [requestedSessionId, setRequestedSessionId] = useState<string | null>(
    null,
  );
  const optimisticSessionIdsRef = useRef<Set<string>>(new Set());
  const generationReadySessionIdsRef = useRef<Set<string>>(new Set());
  const removedSessionIdsRef = useRef<Set<string>>(new Set());
  const sessionMutationVersionRef = useRef<
    Map<string, Map<string, number>>
  >(new Map());
  const mutationVersionRef = useRef(0);
  // 同時に走る fetchSessions() を1リクエストへ束ねる。各画面のQuick Panelや
  // route遷移直後の再試行が重なっても、GET /api/conversationsは1本にする。
  const inFlightFetchRef = useRef<Promise<void> | null>(null);

  const markSessionMutation = useCallback((id: string, fields: string[]) => {
    if (fields.length === 0) return;
    mutationVersionRef.current += 1;
    const versions = new Map(sessionMutationVersionRef.current.get(id));
    for (const field of fields) {
      versions.set(field, mutationVersionRef.current);
    }
    sessionMutationVersionRef.current.set(id, versions);
  }, []);

  const mergeFetchedSessions = useCallback(
    (
      fetched: ConversationSession[],
      previous: ConversationSession[],
      mutationSnapshot: Map<string, Map<string, number>>,
    ): ConversationSession[] => {
      const previousById = new Map(previous.map((session) => [session.id, session]));
      const fetchedIds = new Set(fetched.map((session) => session.id));
      const mergedFetched = fetched.flatMap((session) => {
        if (removedSessionIdsRef.current.has(session.id)) return [];
        const previousSession = previousById.get(session.id);
        if (!previousSession) return [session];
        const currentVersions = sessionMutationVersionRef.current.get(session.id);
        const requestVersions = mutationSnapshot.get(session.id);
        if (
          (currentVersions?.get("*") ?? 0) >
          (requestVersions?.get("*") ?? 0)
        ) {
          return [previousSession];
        }
        const merged = { ...session } as ConversationSession;
        for (const field of Object.keys(previousSession)) {
          if (
            (currentVersions?.get(field) ?? 0) >
            (requestVersions?.get(field) ?? 0)
          ) {
            (merged as Record<string, unknown>)[field] = (
              previousSession as unknown as Record<string, unknown>
            )[field];
          }
        }
        return [merged];
      });
      const preservedSessions = previous.filter((session) => {
        if (fetchedIds.has(session.id)) return false;
        if (removedSessionIdsRef.current.has(session.id)) return false;
        const mutationVersions = sessionMutationVersionRef.current.get(session.id);
        const requestVersions = mutationSnapshot.get(session.id);
        const changedAfterRequest = [...(mutationVersions?.entries() ?? [])].some(
          ([field, version]) => version > (requestVersions?.get(field) ?? 0),
        );
        return (
          optimisticSessionIdsRef.current.has(session.id) ||
          changedAfterRequest
        );
      });

      for (const session of fetched) {
        optimisticSessionIdsRef.current.delete(session.id);
      }

      return [...preservedSessions, ...mergedFetched];
    },
    [],
  );

  const fetchSessions = useCallback(async () => {
    // 進行中の取得があれば、それに相乗りして重複 GET を避ける。
    if (inFlightFetchRef.current) return inFlightFetchRef.current;

    const pending = (async () => {
      try {
        const mutationSnapshot = new Map(
          [...sessionMutationVersionRef.current.entries()].map(
            ([id, versions]) => [id, new Map(versions)],
          ),
        );
        const data = await chatApi.listSessions();
        setSessions((prev) =>
          mergeFetchedSessions(data.conversations, prev, mutationSnapshot),
        );
        setSessionsError(null);
      } catch (err) {
        console.error("会話履歴取得エラー:", err);
        setSessionsError("会話履歴を取得できません");
      } finally {
        inFlightFetchRef.current = null;
      }
    })();
    inFlightFetchRef.current = pending;
    return pending;
  }, [mergeFetchedSessions]);

  useEffect(() => {
    const refreshIfVisible = () => {
      if (document.visibilityState === "hidden") return;
      void fetchSessions();
    };

    // ChatSidebarが閉じているTasks/Files等のrouteでもQuick Panelの履歴を
    // 直ちに表示できるよう、取得・ポーリングはProviderだけが担当する。
    void fetchSessions();
    const refreshTimer = window.setInterval(
      refreshIfVisible,
      SESSIONS_REFRESH_INTERVAL_MS,
    );
    document.addEventListener("visibilitychange", refreshIfVisible);
    window.addEventListener("focus", refreshIfVisible);
    return () => {
      window.clearInterval(refreshTimer);
      document.removeEventListener("visibilitychange", refreshIfVisible);
      window.removeEventListener("focus", refreshIfVisible);
    };
  }, [fetchSessions]);

  const upsertSession = useCallback((session: ConversationSession) => {
    markSessionMutation(session.id, ["*"]);
    removedSessionIdsRef.current.delete(session.id);
    setSessions((prev) => {
      const index = prev.findIndex((item) => item.id === session.id);
      if (index < 0) return [session, ...prev];
      return prev.map((item) => (item.id === session.id ? session : item));
    });
  }, [markSessionMutation]);

  const addSession = useCallback((session: ConversationSession) => {
    markSessionMutation(session.id, ["*"]);
    removedSessionIdsRef.current.delete(session.id);
    optimisticSessionIdsRef.current.add(session.id);
    setSessions((prev) => [
      session,
      ...prev.filter((item) => item.id !== session.id),
    ]);
  }, [markSessionMutation]);

  const registerSession = useCallback(
    (
      session: ConversationSession,
      options?: { generationReady?: boolean; activate?: boolean },
    ) => {
      if (options?.generationReady) {
        generationReadySessionIdsRef.current.add(session.id);
      } else {
        generationReadySessionIdsRef.current.delete(session.id);
      }
      addSession(session);
      if (options?.activate) setRequestedSessionId(session.id);
    },
    [addSession],
  );

  const activateSession = useCallback((sessionId: string) => {
    setRequestedSessionId(sessionId);
  }, []);

  const clearRequestedSession = useCallback(() => {
    setRequestedSessionId(null);
  }, []);

  const consumeGenerationReadySession = useCallback(
    (sessionId: string) => {
      const ready = generationReadySessionIdsRef.current.has(sessionId);
      if (ready) generationReadySessionIdsRef.current.delete(sessionId);
      return ready;
    },
    [],
  );

  const isGenerationReadySession = useCallback(
    (sessionId: string) => generationReadySessionIdsRef.current.has(sessionId),
    [],
  );

  const removeSession = useCallback((id: string) => {
    markSessionMutation(id, ["*"]);
    generationReadySessionIdsRef.current.delete(id);
    removedSessionIdsRef.current.add(id);
    optimisticSessionIdsRef.current.delete(id);
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, [markSessionMutation]);

  const updateSession = useCallback(
    (id: string, updater: (session: ConversationSession) => ConversationSession) => {
      setSessions((prev) => {
        let changedFields: string[] = [];
        const next = prev.map((session) => {
          if (session.id !== id) return session;
          const updated = updater(session);
          if (updated === session) return session;
          const previousRecord = session as unknown as Record<string, unknown>;
          const updatedRecord = updated as unknown as Record<string, unknown>;
          changedFields = Array.from(
            new Set([...Object.keys(previousRecord), ...Object.keys(updatedRecord)]),
          ).filter(
            (field) => !Object.is(previousRecord[field], updatedRecord[field]),
          );
          return changedFields.length > 0 ? updated : session;
        });
        if (changedFields.length === 0) return prev;
        markSessionMutation(id, changedFields);
        return next;
      });
    },
    [markSessionMutation],
  );

  const updateSessionTitle = useCallback(
    (id: string, title: string) => {
      updateSession(id, (session) => ({ ...session, title }));
    },
    [updateSession],
  );

  const bumpSession = useCallback((id: string) => {
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === id);
      if (idx < 0) return prev;
      const target = { ...prev[idx], last_activity: new Date().toISOString() };
      markSessionMutation(id, ["last_activity"]);
      if (idx === 0) return [target, ...prev.slice(1)];
      return [target, ...prev.slice(0, idx), ...prev.slice(idx + 1)];
    });
  }, [markSessionMutation]);

  return (
    <ChatSessionContext.Provider
      value={{
        sessions,
        sessionsError,
        fetchSessions,
        addSession,
        registerSession,
        consumeGenerationReadySession,
        isGenerationReadySession,
        requestedSessionId,
        activateSession,
        clearRequestedSession,
        upsertSession,
        removeSession,
        updateSession,
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

/**
 * Optional variant for small picker components that can also be rendered in
 * isolation (for example, unit tests or an embedded editor).  When the
 * provider is present this returns the exact same session store as
 * useChatSessions(); it never creates a second list or fetches a new API.
 */
export function useChatSessionsOptional() {
  return useContext(ChatSessionContext);
}
