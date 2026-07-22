"use client";

import {
  useCallback,
  useEffect,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type {
  ConversationGenerationStatus,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import type { chatTimelineReducer } from "@/lib/chat-state";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";

const LAST_SESSION_KEY = "aoitalk_last_session_id";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

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
  markWaitingResponse: (sessionId: string | null) => void;
  clearWaitingResponse: (sessionId: string | null) => void;
  resetDisplayedGenerationState: () => void;
  setRestoredGenerationStatus: Dispatch<
    SetStateAction<ConversationGenerationStatus | null>
  >;
  setPendingAgentRunId: Dispatch<SetStateAction<string | null>>;
  setCurrentSession: Dispatch<SetStateAction<ConversationSession | null>>;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setIsLoadingMessages: Dispatch<SetStateAction<boolean>>;
  setSessionLoadError: Dispatch<SetStateAction<string | null>>;
  setScenarioSession: Dispatch<
    SetStateAction<
      Awaited<ReturnType<typeof chatApi.getScenarioPlaySessionByConversation>>
    >
  >;
  setWritingSession: Dispatch<
    SetStateAction<
      Awaited<ReturnType<typeof chatApi.getWritingSessionByConversation>>
    >
  >;
  setRoleplaySession: Dispatch<
    SetStateAction<{
      scenario: { id: string; title: string };
      character: {
        id: string;
        name: string;
        role?: string;
        description?: string;
      };
    } | null>
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
  markWaitingResponse,
  clearWaitingResponse,
  resetDisplayedGenerationState,
  setRestoredGenerationStatus,
  setPendingAgentRunId,
  setCurrentSession,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
  setScenarioSession,
  setWritingSession,
  setRoleplaySession,
}: UseChatPersistenceArgs) {
  const maybeGenerateLoadedSessionTitle = useCallback(
    async (session: ConversationSession, messages: ConversationMessage[]) => {
      if (
        isScenarioWorkflowSession(session) ||
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
        setCurrentSession((prev) =>
          prev && prev.id === session.id
            ? { ...prev, title: titleResult.title }
            : prev,
        );
      } catch (err) {
        console.warn("セッションタイトル生成に失敗:", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [updateSidebarTitle],
  );

  const refreshPersistedMessages = useCallback(async (sessionId: string) => {
    try {
      const data = await chatApi.getMessages(sessionId);
      const currentSessionId = activeSessionIdRef.current;
      if (currentSessionId && currentSessionId !== sessionId) return null;
      dispatchChatTimeline({
        type: "hydrate_persisted",
        sessionId,
        messages: data.messages,
      });
      return data.messages;
    } catch (err) {
      console.warn("保存済みメッセージの再取得に失敗:", err);
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const waitForPersistedAssistantResponse = useCallback(
    async (
      sessionId: string,
      knownAssistantIds: Set<string>,
      titleSession?: ConversationSession | null,
    ) => {
      const generation = ++responsePollGenerationRef.current;
      const timeoutAt = Date.now() + 300_000;

      const hasNewAssistantMessage = (
        persistedMessages: ConversationMessage[],
      ) =>
        persistedMessages.some(
          (message) =>
            message.role === "assistant" && !knownAssistantIds.has(message.id),
        );

      while (Date.now() < timeoutAt) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        if (responsePollGenerationRef.current !== generation) return;
        const currentSessionId = activeSessionIdRef.current;
        if (currentSessionId && currentSessionId !== sessionId) return;

        const persistedMessages = await refreshPersistedMessages(sessionId);
        if (!persistedMessages) continue;

        if (hasNewAssistantMessage(persistedMessages)) {
          clearWaitingResponse(sessionId);
          setRestoredGenerationStatus(null);
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
      if (finalMessages && hasNewAssistantMessage(finalMessages)) {
        if (titleSession) {
          void maybeGenerateLoadedSessionTitle(titleSession, finalMessages);
        }
      }
      clearWaitingResponse(sessionId);
      setRestoredGenerationStatus(null);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      clearWaitingResponse,
      maybeGenerateLoadedSessionTitle,
      refreshPersistedMessages,
    ],
  );

  const refreshGenerationStatus = useCallback(
    async (
      sessionId: string,
      knownMessages: ConversationMessage[],
      titleSession?: ConversationSession | null,
    ) => {
      try {
        const status = await chatApi.getGenerationStatus(sessionId);
        if (
          activeSessionIdRef.current &&
          activeSessionIdRef.current !== sessionId
        ) {
          return;
        }
        if (status.running) {
          setRestoredGenerationStatus(status);
          markWaitingResponse(sessionId);
          setPendingAgentRunId(status.agent_run_id ?? null);
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
          setRestoredGenerationStatus(null);
          if (status.status !== "idle") {
            clearWaitingResponse(sessionId);
            setPendingAgentRunId(null);
          }
        }
      } catch (err) {
        console.warn("生成状態の復元に失敗しました", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      clearWaitingResponse,
      markWaitingResponse,
      waitForPersistedAssistantResponse,
    ],
  );

  // ─── セッション選択時にメッセージを取得 ───
  useEffect(() => {
    if (!activeSessionId) {
      dispatchChatTimeline({ type: "clear" });
      resetDisplayedGenerationState();
      setIsLoadingMessages(false);
      setSessionLoadError(null);
      setScenarioSession(null);
      setWritingSession(null);
      setRoleplaySession(null);
      setCurrentSession(null);
      setSteeringInstructions([]);
      return;
    }

    let cancelled = false;
    resetDisplayedGenerationState();
    setIsLoadingMessages(true);
    setSessionLoadError(null);
    // 現在のセッションのtempメッセージのみ保持（別セッションのものはクリア）
    dispatchChatTimeline({
      type: "keep_transient_for_session",
      sessionId: activeSessionId,
    });
    setSteeringInstructions([]);
    setScenarioSession(null);
    setWritingSession(null);
    setRoleplaySession(null);

    (async () => {
      const loadScenarioContext = async (session: ConversationSession) => {
        const characterName = session.character_name || "";
        const roleplayMatch = characterName.match(
          /^scenario_roleplay:([^:]+):([^:]+)$/,
        );

        if (roleplayMatch) {
          const [, scenarioId, characterId] = roleplayMatch;
          try {
            const scenario = await chatApi.getScenario(scenarioId);
            const character = scenario.characters?.find(
              (item) => item.id === characterId,
            );
            if (!cancelled && character) {
              setRoleplaySession({
                scenario: { id: scenario.id, title: scenario.title },
                character,
              });
            }
          } catch (err) {
            console.warn("シナリオロールプレイ情報取得失敗:", err);
          }
          return;
        }

        if (!isScenarioWorkflowSession(session)) {
          return;
        }

        const [playResult, writingResult] = await Promise.allSettled([
          chatApi.getScenarioPlaySessionByConversation(activeSessionId),
          chatApi.getWritingSessionByConversation(activeSessionId),
        ]);

        if (cancelled) return;

        setScenarioSession(
          playResult.status === "fulfilled" ? playResult.value : null,
        );
        setWritingSession(
          writingResult.status === "fulfilled" ? writingResult.value : null,
        );
      };

      try {
        const data = await chatApi.resumeSession(activeSessionId);
        if (!cancelled) {
          setCurrentSession(data.session);
          if (isScenarioWorkflowSession(data.session)) {
            if (localStorage.getItem(LAST_SESSION_KEY) === activeSessionId) {
              localStorage.removeItem(LAST_SESSION_KEY);
            }
          } else {
            localStorage.setItem(LAST_SESSION_KEY, activeSessionId);
          }
          // API結果で置き換え。temp-メッセージはAPIに未反映のもののみ残す
          dispatchChatTimeline({
            type: "hydrate_persisted",
            sessionId: activeSessionId,
            messages: data.messages,
          });
          void refreshGenerationStatus(
            activeSessionId,
            data.messages,
            data.session,
          );
          void maybeGenerateLoadedSessionTitle(data.session, data.messages);
          await loadScenarioContext(data.session);
        }
      } catch (err) {
        console.error("セッション再開失敗:", err);
        if (!cancelled) {
          if (localStorage.getItem(LAST_SESSION_KEY) === activeSessionId) {
            localStorage.removeItem(LAST_SESSION_KEY);
          }
          dispatchChatTimeline({ type: "clear" });
          setCurrentSession(null);
          setScenarioSession(null);
          setWritingSession(null);
          setRoleplaySession(null);
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
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeSessionId,
    maybeGenerateLoadedSessionTitle,
    refreshGenerationStatus,
    resetDisplayedGenerationState,
    router,
    sessionLoadAttempt,
  ]);

  useEffect(() => {
    if (!activeSessionId || !isConnected) return;
    void refreshGenerationStatus(
      activeSessionId,
      messagesRef.current,
      currentSession,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, currentSession, isConnected, refreshGenerationStatus]);

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
  };
}
