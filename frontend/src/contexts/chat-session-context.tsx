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
  /** Mark a session read while preserving the assistant-completion fence. */
  markSessionRead: (sessionId: string) => Promise<void>;
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
// The history lifecycle is projected by the server from completion/read
// fences.  Optimistic read handling may update these fields for immediate UI
// feedback, but a later list response must be allowed to restore a newer
// completion (for example, a reply that arrived after the session was opened).
const SERVER_OWNED_HISTORY_FIELDS = new Set([
  "history_status",
  "history_completed_at",
  // This legacy field is projected from the same completion/read fence.
  "is_unread",
]);

type ReadCompletionFence = {
  completionAt: string | null;
  readAt: string;
  token: number;
};

function isHistoryCompletionAtOrBefore(
  completionAt: string | null | undefined,
  readFenceAt: string,
): boolean {
  if (typeof completionAt !== "string" || completionAt.length === 0) {
    return false;
  }
  const completionMs = Date.parse(completionAt);
  const readFenceMs = Date.parse(readFenceAt);
  if (Number.isFinite(completionMs) && Number.isFinite(readFenceMs)) {
    return completionMs <= readFenceMs;
  }
  // Generated server values are Z-suffixed UTC. Keep exact equality as a
  // defensive fallback if a malformed value reaches the client.
  return completionAt === readFenceAt;
}

function isHistoryCompletionStrictlyOlder(
  completionAt: string | null | undefined,
  currentCompletionAt: string | null | undefined,
): boolean {
  if (
    typeof completionAt !== "string" ||
    typeof currentCompletionAt !== "string" ||
    completionAt.length === 0 ||
    currentCompletionAt.length === 0
  ) {
    return false;
  }
  const completionMs = Date.parse(completionAt);
  const currentCompletionMs = Date.parse(currentCompletionAt);
  return (
    Number.isFinite(completionMs) &&
    Number.isFinite(currentCompletionMs) &&
    completionMs < currentCompletionMs
  );
}

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
  const readCompletionFencesRef = useRef<Map<string, ReadCompletionFence>>(
    new Map(),
  );
  const readFenceTokenRef = useRef(0);
  const sessionMutationVersionRef = useRef<
    Map<string, Map<string, number>>
  >(new Map());
  const mutationVersionRef = useRef(0);
  // 同時に走る fetchSessions() を1リクエストへ束ねる。各画面のQuick Panelや
  // route遷移直後の再試行が重なっても、GET /api/conversationsは1本にする。
  const inFlightFetchRef = useRef<Promise<void> | null>(null);
  const sessionsRef = useRef<ConversationSession[]>([]);
  sessionsRef.current = sessions;

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
        const remoteCompletionIsOlder = isHistoryCompletionStrictlyOlder(
          session.history_completed_at,
          previousSession.history_completed_at,
        );
        if (remoteCompletionIsOlder) {
          // A GET started before a newer completion can arrive after the
          // newer response. Never let that stale projection replace the
          // already observed server-owned history fields.
          merged.history_status = previousSession.history_status;
          merged.history_completed_at = previousSession.history_completed_at;
          merged.is_unread = previousSession.is_unread;
        } else {
          const readFence = readCompletionFencesRef.current.get(session.id);
          if (
            readFence &&
            session.history_status === "ready" &&
            ((readFence.completionAt &&
              isHistoryCompletionAtOrBefore(
                session.history_completed_at,
                readFence.completionAt,
              )) ||
              (!readFence.completionAt &&
                isHistoryCompletionAtOrBefore(
                  session.history_completed_at,
                  readFence.readAt,
                )))
          ) {
            // A list response can have started before the read API request and
            // arrive afterwards. Keep that same completion read, while allowing
            // a later completion to remain server-owned and ready.
            merged.history_status = "idle";
            merged.is_unread = false;
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

      const next = [...preservedSessions, ...mergedFetched];
      sessionsRef.current = next;
      return next;
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
    // Keep the read fence capture synchronous when a newly created session is
    // opened before React commits the queued state update.
    sessionsRef.current = [
      session,
      ...sessionsRef.current.filter((item) => item.id !== session.id),
    ];
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
    readCompletionFencesRef.current.delete(id);
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
        markSessionMutation(
          id,
          changedFields.filter((field) => !SERVER_OWNED_HISTORY_FIELDS.has(field)),
        );
        return next;
      });
    },
    [markSessionMutation],
  );

  const markSessionRead = useCallback(
    async (sessionId: string): Promise<void> => {
      // Keep a client-side request-entry fence as a fallback for a deep link
      // whose list response has not populated the context yet. Once a server
      // completion is known, completion ordering takes precedence over client
      // clock ordering so a newer response cannot be suppressed by skew.
      const readAt = new Date().toISOString();
      const readToken = readFenceTokenRef.current + 1;
      readFenceTokenRef.current = readToken;
      const rememberReadCompletion = (
        completionAt: unknown,
        updateRequestMetadata = true,
      ) => {
        const previousFence = readCompletionFencesRef.current.get(sessionId);
        let nextCompletionAt = previousFence?.completionAt ?? null;
        if (typeof completionAt === "string" && completionAt.length > 0) {
          if (
            !nextCompletionAt ||
            !isHistoryCompletionAtOrBefore(completionAt, nextCompletionAt)
          ) {
            nextCompletionAt = completionAt;
          }
        }
        readCompletionFencesRef.current.set(sessionId, {
          completionAt: nextCompletionAt,
          readAt: updateRequestMetadata ? readAt : previousFence?.readAt ?? readAt,
          token: updateRequestMetadata
            ? readToken
            : previousFence?.token ?? readToken,
        });
      };
      const currentSession = sessionsRef.current.find(
        (session) => session.id === sessionId,
      );
      rememberReadCompletion(currentSession?.history_completed_at);

      updateSession(sessionId, (session) => {
        // The updater is also the source of truth when React has batched a
        // preceding add/list update and the render ref still points at the
        // older snapshot.
        rememberReadCompletion(session.history_completed_at, false);
        return {
          ...session,
          is_unread: false,
          ...(session.history_status === "ready"
            ? { history_status: "idle" as const }
            : {}),
        };
      });

      try {
        await chatApi.markSessionRead(sessionId);
        await fetchSessions();
      } catch {
        // A failed read must not permanently suppress the server's ready
        // projection. A newer read may already own the same session fence, so
        // only revoke this operation's token and then reconcile from server.
        if (
          readCompletionFencesRef.current.get(sessionId)?.token === readToken
        ) {
          readCompletionFencesRef.current.delete(sessionId);
          await fetchSessions();
        }
      }
    },
    [fetchSessions, updateSession],
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
        markSessionRead,
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
