"use client";

import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import { storyApi } from "@/lib/story/api";
import type {
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import type { ChatGenerationEvent } from "@/lib/chat-generation-state";
import type { chatTimelineReducer } from "@/lib/chat-state";
import { isTransientChatMessage } from "@/lib/chat-state";
import {
  deriveServerTime,
  getLastServerTime,
  mergePersistedById,
  readCachedMessages,
  setLastServerTime,
  writeCachedMessages,
} from "@/lib/chat-message-cache";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import {
  safeLocalStorageGetItem,
  safeLocalStorageRemoveItem,
  safeLocalStorageSetItem,
} from "@/lib/safe-storage";

const LAST_SESSION_KEY = "aoitalk_last_session_id";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

export type RefreshPersistedMessagesOptions = {
  /**
   * 差分取得ではなく、現在のサーバー側 active path 全体を再取得する。
   * ブランチ切替など、直前の server_time より前の履歴も置き換える必要が
   * ある操作で利用する。
   */
  forceFull?: boolean;
};

function isStoryWorkflowSession(session: ConversationSession) {
  const characterName = session.character_name || "";
  return (
    characterName.startsWith("story_") ||
    session.title?.startsWith("[執筆]")
  );
}

function compactTitleText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function fallbackTitleFromFirstUserMessage(content: string): string {
  const title = compactTitleText(content);
  return title.length > 40 ? `${title.slice(0, 37)}...` : title;
}

function shouldRequestSessionTitleGeneration(
  session: ConversationSession,
  messages: ConversationMessage[],
): boolean {
  const title = compactTitleText(session.title || "");
  const firstUserMessage = messages.find(
    (message) =>
      message.role === "user" && compactTitleText(message.content).length > 0,
  );
  if (!firstUserMessage) return false;

  if (!title) return true;
  return title === fallbackTitleFromFirstUserMessage(firstUserMessage.content);
}

type UseChatPersistenceArgs = {
  router: ReturnType<typeof useRouter>;
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  sessionLoadAttempt: number;
  messagesRef: RefObject<ConversationMessage[]>;
  currentSession: ConversationSession | null;
  isConnected: boolean;
  responsePollGenerationRef: RefObject<number>;
  updateSidebarTitle: (sessionId: string, title: string) => void;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  upsertSession: (session: ConversationSession) => void;
  /** Bump the sidebar only after an assistant message is durably persisted. */
  bumpSession: (sessionId: string) => void;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setIsLoadingMessages: Dispatch<SetStateAction<boolean>>;
  setSessionLoadError: Dispatch<SetStateAction<string | null>>;
  setWritingSession: Dispatch<
    SetStateAction<
      Awaited<ReturnType<typeof storyApi.getWritingSessionByConversation>>
    >
  >;
};

/**
 * 会話の永続化ポーリング・生成状態復元・セッションロード・タイトル生成を担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの（`use-chat-messaging` の内部から呼ぶ）。
 * 依存配列は元コードと同一に保つ。
 */
export function useChatPersistence({
  router,
  activeSessionId,
  activeSessionIdRef,
  sessionLoadAttempt,
  messagesRef,
  currentSession,
  isConnected,
  responsePollGenerationRef,
  updateSidebarTitle,
  dispatchChatTimeline,
  dispatchGeneration,
  upsertSession,
  bumpSession,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
  setWritingSession,
}: UseChatPersistenceArgs) {
  const statusRequestGenerationRef = useRef(0);
  // The first passive effect can run after a user submits from a freshly
  // mounted /chat route.  Do not clear the optimistic provisional row that
  // was appended before that effect; later null-session transitions still
  // clear the previous session normally.
  const previousActiveSessionIdRef = useRef<string | null | undefined>(
    undefined,
  );
  // A response can be observed through both the websocket event and the
  // fallback REST poll. Keep one activity bump per durable assistant message
  // so duplicate notifications do not continually rewrite last_activity.
  const assistantActivityKeysRef = useRef<Set<string>>(new Set());
  const bumpSessionForAssistant = useCallback(
    (sessionId: string, messageId: string) => {
      const key = `${sessionId}:${messageId}`;
      const keys = assistantActivityKeysRef.current;
      if (keys.has(key)) return;
      keys.add(key);
      if (keys.size > 512) {
        const oldest = keys.values().next().value;
        if (oldest) keys.delete(oldest);
      }
      bumpSession(sessionId);
    },
    [bumpSession],
  );
  const maybeGenerateLoadedSessionTitle = useCallback(
    async (session: ConversationSession, messages: ConversationMessage[]) => {
      if (
        isStoryWorkflowSession(session) ||
        !shouldRequestSessionTitleGeneration(session, messages)
      ) {
        return;
      }

      try {
        const titleResult = await chatApi.generateSessionTitle(session.id);
        if (!titleResult.title || activeSessionIdRef.current !== session.id) {
          return;
        }
        updateSidebarTitle(session.id, titleResult.title);
      } catch (err) {
        console.warn("セッションタイトル生成に失敗:", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [updateSidebarTitle],
  );

  const refreshPersistedMessages = useCallback(
    async (
      sessionId: string,
      options?: RefreshPersistedMessagesOptions,
    ) => {
      try {
        // 通常は前回の server_time を since に渡し、差分のみ取得する。
        // ブランチ切替後は inactive になった旧 active path を確実に除去し、
        // 新しい active path 全体を表示するため、full GET を行う。
        const since = options?.forceFull ? null : getLastServerTime(sessionId);
        const data = await chatApi.getMessages(sessionId, since ?? undefined);
        const currentSessionId = activeSessionIdRef.current;
        if (currentSessionId !== sessionId) return null;

        // since 指定時は既存の永続メッセージへ差分をマージ（id で重複排除）。
        // since なし（初回）は取得結果をそのまま採用する。
        const prevPersisted = since
          ? messagesRef.current.filter(
              (message) =>
                !isTransientChatMessage(message) &&
                message.session_id === sessionId,
            )
          : [];
        const mergedPersisted = since
          ? mergePersistedById(prevPersisted, data.messages)
          : data.messages;

        dispatchChatTimeline({
          type: "hydrate_persisted",
          sessionId,
          messages: mergedPersisted,
        });

        const nextServerTime =
          data.server_time ?? deriveServerTime(mergedPersisted);
        setLastServerTime(sessionId, nextServerTime);
        void writeCachedMessages(sessionId, mergedPersisted, nextServerTime);
        return mergedPersisted;
      } catch (err) {
        console.warn("保存済みメッセージの再取得に失敗:", err);
        return null;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const waitForPersistedAssistantResponse = useCallback(
    async (
      sessionId: string,
      knownAssistantIds: Set<string>,
      titleSession?: ConversationSession | null,
    ) => {
      const generation = ++responsePollGenerationRef.current;
      const timeoutAt = Date.now() + 300_000;

      while (Date.now() < timeoutAt) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (responsePollGenerationRef.current !== generation) return;
        const currentSessionId = activeSessionIdRef.current;
        if (currentSessionId !== sessionId) return;

        const persistedMessages = await refreshPersistedMessages(sessionId);
        if (!persistedMessages) continue;

        const newAssistantMessage = persistedMessages.find(
          (message) =>
            message.role === "assistant" && !knownAssistantIds.has(message.id),
        );
        if (newAssistantMessage) {
          bumpSessionForAssistant(sessionId, newAssistantMessage.id);
          const agentRunId =
            typeof newAssistantMessage.metadata?.agent_run_id === "string"
              ? newAssistantMessage.metadata.agent_run_id
              : null;
          dispatchGeneration({
            type: "assistant_persisted",
            sessionId,
            agentRunId,
            assistantMessageId: newAssistantMessage.id,
            eventId: `poll:message:${sessionId}:${newAssistantMessage.id}`,
          });
          try {
            const status = await chatApi.getGenerationStatus(sessionId);
            if (
              responsePollGenerationRef.current === generation &&
              activeSessionIdRef.current === sessionId &&
              (!status.session_id || status.session_id === sessionId)
            ) {
              dispatchGeneration({
                type: "status_restored",
                sessionId,
                status,
                eventId: `poll:terminal:${sessionId}:${status.updated_at ?? status.status}`,
              });
            }
          } catch {
            // 永続メッセージは表示済み。lifecycleは次のauthoritative statusまで維持する。
          }
          if (titleSession) {
            void maybeGenerateLoadedSessionTitle(
              titleSession,
              persistedMessages,
            );
          }
          return;
        }
      }

      const finalMessages = await refreshPersistedMessages(sessionId);
      const finalAssistantMessage = finalMessages?.find(
        (message) =>
          message.role === "assistant" && !knownAssistantIds.has(message.id),
      );
      if (finalMessages && finalAssistantMessage) {
        bumpSessionForAssistant(sessionId, finalAssistantMessage.id);
        const agentRunId =
          typeof finalAssistantMessage.metadata?.agent_run_id === "string"
            ? finalAssistantMessage.metadata.agent_run_id
            : null;
        dispatchGeneration({
          type: "assistant_persisted",
          sessionId,
          agentRunId,
          assistantMessageId: finalAssistantMessage.id,
          eventId: `poll:message:${sessionId}:${finalAssistantMessage.id}`,
        });
        if (titleSession) {
          void maybeGenerateLoadedSessionTitle(titleSession, finalMessages);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      dispatchGeneration,
      maybeGenerateLoadedSessionTitle,
      refreshPersistedMessages,
      bumpSessionForAssistant,
    ],
  );

  const refreshGenerationStatus = useCallback(
    async (
      sessionId: string,
      knownMessages: ConversationMessage[],
      titleSession?: ConversationSession | null,
    ) => {
      const statusGeneration = ++statusRequestGenerationRef.current;
      const isCurrentStatusRequest = (
        status?: { session_id?: string | null } | null,
      ) =>
        statusRequestGenerationRef.current === statusGeneration &&
        activeSessionIdRef.current === sessionId &&
        (status?.session_id == null || status.session_id === sessionId);
      try {
        const status = await chatApi.getGenerationStatus(sessionId);
        // Check the request/session identity before interpreting the response.
        // In particular, a malformed response from a session that was already
        // left must never synthesize an idle state for the newly active one.
        if (!isCurrentStatusRequest(status)) return;
        if (
          typeof status?.running !== "boolean" ||
          typeof status?.status !== "string"
        ) {
          throw new Error("生成状態レスポンスが不正です");
        }
        dispatchGeneration({
          type: "status_restored",
          sessionId,
          status,
          eventId: `poll:status:${sessionId}:${status.updated_at ?? statusGeneration}`,
        });
        if (status.running) {
          const knownAssistantIds = new Set(
            knownMessages
              .filter((message) => message.role === "assistant")
              .map((message) => message.id),
          );
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            titleSession,
          );
        } else {
          if (status.status !== "idle") {
            void refreshPersistedMessages(sessionId);
          }
        }
      } catch (err) {
        console.warn("生成状態の復元に失敗しました", err);
        if (
          statusRequestGenerationRef.current === statusGeneration &&
          activeSessionIdRef.current === sessionId
        ) {
          dispatchGeneration({ type: "hydration_failed", sessionId });
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      dispatchGeneration,
      refreshPersistedMessages,
      waitForPersistedAssistantResponse,
    ],
  );

  // ─── セッション選択時にメッセージを取得 ───
  useEffect(() => {
    // A→B→Aのような再選択でも、最初のA向け遅延responseを失効させる。
    statusRequestGenerationRef.current += 1;
    const previousActiveSessionId = previousActiveSessionIdRef.current;
    previousActiveSessionIdRef.current = activeSessionId;
    if (!activeSessionId) {
      if (previousActiveSessionId !== undefined) {
        dispatchChatTimeline({ type: "clear" });
      }
      dispatchGeneration({ type: "reset", sessionId: null });
      setIsLoadingMessages(false);
      setSessionLoadError(null);
      setWritingSession(null);
      setSteeringInstructions([]);
      return;
    }

    let cancelled = false;
    dispatchGeneration({ type: "reset", sessionId: activeSessionId });
    setIsLoadingMessages(true);
    setSessionLoadError(null);
    // 現在のセッションのtempメッセージのみ保持（別セッションのものはクリア）
    dispatchChatTimeline({
      type: "keep_transient_for_session",
      sessionId: activeSessionId,
    });
    setSteeringInstructions([]);
    setWritingSession(null);

    (async () => {
      const loadStoryContext = async (session: ConversationSession) => {
        if (!isStoryWorkflowSession(session)) {
          return;
        }

        const writingResult = await Promise.allSettled([
          storyApi.getWritingSessionByConversation(activeSessionId),
        ]);

        if (cancelled) return;

        setWritingSession(
          writingResult[0].status === "fulfilled" ? writingResult[0].value : null,
        );
      };

      // キャッシュ済みメッセージがあれば即描画（低帯域配慮）。
      // resume は履歴本文を含めず、続く messages GET で差分だけを取得する。
      let cachedMessages:
        | Awaited<ReturnType<typeof readCachedMessages>>
        | undefined;
      try {
        cachedMessages = await readCachedMessages(activeSessionId);
        if (
          !cancelled &&
          cachedMessages &&
          cachedMessages.messages.length > 0
        ) {
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: activeSessionId,
            messages: cachedMessages.messages,
          });
          setLastServerTime(activeSessionId, cachedMessages.serverTime);
          setIsLoadingMessages(false);
        }
      } catch {
        // キャッシュ読み出し失敗は無視して通常ロードへ。
      }

      try {
        const data = await chatApi.resumeSession(activeSessionId, false);
        let messagesForDisplay = cachedMessages?.messages ?? [];
        let nextServerTime = cachedMessages?.serverTime ?? null;
        try {
          const delta = await chatApi.getMessages(
            activeSessionId,
            cachedMessages?.serverTime ?? undefined,
          );
          messagesForDisplay = cachedMessages
            ? mergePersistedById(cachedMessages.messages, delta.messages)
            : delta.messages;
          nextServerTime =
            delta.server_time ?? deriveServerTime(messagesForDisplay);
        } catch (messageError) {
          // キャッシュがあればオフラインでも表示を継続する。初回だけは履歴取得失敗を通知する。
          if (!cachedMessages) throw messageError;
          console.warn(
            "メッセージ差分の取得に失敗したためキャッシュを表示します:",
            messageError,
          );
        }
        if (!cancelled) {
          upsertSession(data.session);
          if (isStoryWorkflowSession(data.session)) {
            if (safeLocalStorageGetItem(LAST_SESSION_KEY) === activeSessionId) {
              safeLocalStorageRemoveItem(LAST_SESSION_KEY);
            }
          } else {
            safeLocalStorageSetItem(LAST_SESSION_KEY, activeSessionId);
          }
          // API結果で置き換え。temp-メッセージはAPIに未反映のもののみ残す
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: activeSessionId,
            messages: messagesForDisplay,
          });
          setLastServerTime(activeSessionId, nextServerTime);
          void writeCachedMessages(
            activeSessionId,
            messagesForDisplay,
            nextServerTime,
          );
          void refreshGenerationStatus(
            activeSessionId,
            messagesForDisplay,
            data.session,
          );
          void maybeGenerateLoadedSessionTitle(
            data.session,
            messagesForDisplay,
          );
          await loadStoryContext(data.session);
        }
      } catch (err) {
        console.error("セッション再開失敗:", err);
        if (!cancelled) {
          if (safeLocalStorageGetItem(LAST_SESSION_KEY) === activeSessionId) {
            safeLocalStorageRemoveItem(LAST_SESSION_KEY);
          }
          dispatchChatTimeline({ type: "clear" });
          setWritingSession(null);
          setSessionLoadError(
            "会話セッションを表示できませんでした。履歴データは残っている可能性があるため、再読み込みしてください。",
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoadingMessages(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      statusRequestGenerationRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
      activeSessionId,
      maybeGenerateLoadedSessionTitle,
      refreshGenerationStatus,
      dispatchGeneration,
      router,
      sessionLoadAttempt,
      upsertSession,
  ]);

  useEffect(() => {
    if (!activeSessionId || !isConnected) return;
    void (async () => {
      const refreshed = await refreshPersistedMessages(activeSessionId);
      await refreshGenerationStatus(
        activeSessionId,
        refreshed ?? messagesRef.current,
        currentSession,
      );
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeSessionId,
    currentSession,
    isConnected,
    refreshGenerationStatus,
    refreshPersistedMessages,
  ]);

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
    bumpSessionForAssistant,
  };
}
