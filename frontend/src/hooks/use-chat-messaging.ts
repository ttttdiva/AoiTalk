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
  ChatCommandCapability,
  ChatResponseModelSelection,
  ConversationGenerationStatus,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import {
  createLocalMessage,
  createLocalUserMessage,
} from "@/lib/chat-local-messages";
import { commandCapabilitiesFromMessageMetadata } from "@/lib/chat-commands";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import { useChatPersistence } from "@/hooks/use-chat-persistence";
import { useDeepResearchMessage } from "@/hooks/use-deep-research-message";
import { useGroupChatCreate } from "@/hooks/use-group-chat-create";

const DISPATCH_FAILURE_MESSAGE =
  "送信は保存されましたが、応答生成を開始できませんでした。サーバーの生成処理が起動しているか確認してから、もう一度送信してください。";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

export type PendingMessage = {
  sessionId: string;
  content: string;
  clientMessageId: string;
  projectId?: string;
  files?: File[];
  mentions?: { type: string; id: string; name: string }[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  commandCapabilities?: ChatCommandCapability[];
  toolsRequired?: boolean;
};

function resolveProjectIdFromMessage(
  content: string,
  projects: Array<{
    id: string;
    name: string;
    slug?: string;
    aliases?: string[];
  }>,
): string | null {
  const normalizedContent = content.toLowerCase();
  const matches = projects.filter((project) => {
    const names = [project.name, project.slug, ...(project.aliases || [])]
      .filter((item): item is string => Boolean(item))
      .map((item) => item.toLowerCase());
    return names.some((name) => name && normalizedContent.includes(name));
  });
  return matches.length === 1 ? matches[0].id : null;
}

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type UseChatMessagingArgs = {
  // ナビゲーション・コンテキスト値
  router: ReturnType<typeof useRouter>;
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  activateSession: (sessionId: string) => void;
  allProjects: Array<{
    id: string;
    name: string;
    slug?: string;
    aliases?: string[];
  }>;
  effectiveProjectId: string | undefined;
  isScenarioChatSession: boolean;
  isGroupChat: boolean;
  includeProjectContext: boolean;
  deepResearchEnabled: boolean;
  sessionLoadAttempt: number;

  // 観測値
  messages: ConversationMessage[];
  messagesRef: RefObject<ConversationMessage[]>;
  currentSession: ConversationSession | null;
  isSending: boolean;
  isConnected: boolean;
  isStreaming: boolean;
  displayIsWaitingResponse: boolean;

  // refs
  responsePollGenerationRef: RefObject<number>;
  pendingMessageRef: RefObject<PendingMessage | null>;

  // Chat セッション context
  addSession: (session: ConversationSession) => void;
  bumpSession: (sessionId: string) => void;
  updateSidebarTitle: (sessionId: string, title: string) => void;

  // WebSocket
  sendMessage: ReturnType<
    typeof import("@/hooks/use-websocket").useWebSocket
  >["sendMessage"];

  // dispatch / setters
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  markWaitingResponse: (sessionId: string | null) => void;
  clearWaitingResponse: (sessionId: string | null) => void;
  resetDisplayedGenerationState: () => void;
  setIsSending: Dispatch<SetStateAction<boolean>>;
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
 * チャットのメッセージ送信・分岐・DeepResearch・グループ作成・永続化ポーリング・
 * セッションロードのロジックを担うフック。
 * `page.tsx` の該当 useCallback / useEffect を挙動不変で移設したもの。
 * 依存配列は元コードと同一に保つ。state / ref は `page.tsx` が保持し、引数で渡す。
 */
export function useChatMessaging({
  router,
  activeSessionId,
  activeSessionIdRef,
  activateSession,
  allProjects,
  effectiveProjectId,
  isScenarioChatSession,
  isGroupChat,
  includeProjectContext,
  deepResearchEnabled,
  sessionLoadAttempt,
  messages,
  messagesRef,
  currentSession,
  isSending,
  isConnected,
  isStreaming,
  displayIsWaitingResponse,
  responsePollGenerationRef,
  pendingMessageRef,
  addSession,
  bumpSession,
  updateSidebarTitle,
  sendMessage,
  dispatchChatTimeline,
  markWaitingResponse,
  clearWaitingResponse,
  resetDisplayedGenerationState,
  setIsSending,
  setRestoredGenerationStatus,
  setPendingAgentRunId,
  setCurrentSession,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
  setScenarioSession,
  setWritingSession,
  setRoleplaySession,
}: UseChatMessagingArgs) {
  const {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
  } = useChatPersistence({
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
  });

  const { handleDeepResearchMessage } = useDeepResearchMessage({
    router,
    activeSessionId,
    activateSession,
    includeProjectContext,
    addSession,
    bumpSession,
    updateSidebarTitle,
    dispatchChatTimeline,
    markWaitingResponse,
    clearWaitingResponse,
    setIsSending,
    setCurrentSession,
  });

  const dispatchMessageWithoutWebSocket = useCallback(
    async (
      sessionId: string,
      content: string,
      projectId?: string,
      generationProfile?: string,
      responseModel?: ChatResponseModelSelection,
      clientMessageId?: string,
      commandCapabilities?: ChatCommandCapability[],
      persistence?: {
        skipUserPersistence?: boolean;
        persistedUserMessageId?: string;
      },
      toolsRequired?: boolean,
    ) => {
      const result = await chatApi.dispatchMessage(sessionId, {
        message: content,
        project_id: projectId,
        generation_profile: generationProfile,
        include_project_context: includeProjectContext,
        response_model: responseModel,
        client_message_id: clientMessageId,
        command_capabilities: commandCapabilities,
        tools_required: toolsRequired,
        skip_user_persistence: persistence?.skipUserPersistence,
        persisted_user_message_id: persistence?.persistedUserMessageId,
      });
      if (clientMessageId && result.user_message_id) {
        dispatchChatTimeline({
          type: "promote_client_message",
          sessionId,
          clientMessageId,
          serverMessageId: result.user_message_id,
        });
      }
      if (result.agent_run_id) {
        setPendingAgentRunId(result.agent_run_id);
      }
      bumpSession(sessionId);
      return result;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bumpSession, includeProjectContext],
  );

  const createDispatchFailureMessage = useCallback(
    (sessionId: string) =>
      createLocalMessage(sessionId, "assistant", DISPATCH_FAILURE_MESSAGE),
    [],
  );


  // ─── WebSocket接続時に保留メッセージを送信 ───
  useEffect(() => {
    const pending = pendingMessageRef.current;
    if (!isConnected || !pending || pending.sessionId !== activeSessionId)
      return;
    let cancelled = false;
    void (async () => {
      const accepted = await sendMessage(
        pending.content,
        pending.projectId ?? effectiveProjectId,
        pending.files,
        pending.mentions,
        pending.generationProfile,
        pending.includeProjectContext,
        undefined,
        undefined,
        pending.sessionId,
        pending.clientMessageId,
        pending.commandCapabilities,
        pending.toolsRequired,
      );
      // セッション切替直後は、前の接続の isConnected=true が一瞬残る。
      // 新しい WebSocket がまだ CONNECTING の場合は保留を維持し、
      // 実際に OPEN になった次の effect で送信する。
      if (cancelled || !accepted || pendingMessageRef.current !== pending)
        return;
      pendingMessageRef.current = null;
      markWaitingResponse(pending.sessionId);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeSessionId,
    effectiveProjectId,
    isConnected,
    markWaitingResponse,
    sendMessage,
  ]);

  const { handleCreateGroupChat } = useGroupChatCreate({
    router,
    activateSession,
    addSession,
    setCurrentSession,
    dispatchChatTimeline,
  });

  // ─── メッセージ送信 ───
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: File[],
      mentions?: { type: string; id: string; name: string }[],
      generationProfile?: string,
      commandCapabilities?: ChatCommandCapability[],
      toolsRequired?: boolean,
    ) => {
      // 連打防止
      if (isSending) return;
      setIsSending(true);
      setRestoredGenerationStatus(null);
      setPendingAgentRunId(null);
      const clientMessageId = createClientMessageId();
      const messageProjectId = isScenarioChatSession
        ? undefined
        : (resolveProjectIdFromMessage(content, allProjects) ??
          effectiveProjectId);
      const hasCommandCapabilities = Boolean(commandCapabilities?.length);

      if (deepResearchEnabled && !hasCommandCapabilities) {
        if (files?.length) {
          if (activeSessionId) {
            dispatchChatTimeline({
              type: "append",
              message: createLocalMessage(
                activeSessionId,
                "assistant",
                "Deep Researchではファイル添付をまだ送信できません。通常チャットに切り替えて送信してください。",
              ),
            });
          }
          setIsSending(false);
          return;
        }
        await handleDeepResearchMessage(content, messageProjectId);
        return;
      }

      // セッション未選択時は自動作成（選択中のプロジェクトを紐付け）
      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const canPersistInitialMessage =
            !files?.length && !mentions?.length && !hasCommandCapabilities;
          const data = await chatApi.createSession(
            await chatApi.getCurrentCharacterName(),
            messageProjectId,
            canPersistInitialMessage
              ? { content, client_message_id: clientMessageId }
              : undefined,
          );
          sessionId = data.session.id;
          // サイドバーにセッションを追加
          addSession(data.session);
          setCurrentSession(data.session);
          activateSession(sessionId);
          // 新規セッションなのでメッセージをクリアしてからユーザーメッセージを追加
          const userMsg =
            data.initial_message ??
            createLocalUserMessage(
              sessionId,
              content,
              clientMessageId,
              files,
              commandCapabilities,
            );
          dispatchChatTimeline({ type: "replace", messages: [userMsg] });
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }

          const knownAssistantIds = new Set<string>();
          if (files?.length || mentions?.length) {
            pendingMessageRef.current = {
              sessionId,
              content,
              clientMessageId,
              projectId: messageProjectId,
              files,
              mentions,
              generationProfile,
              includeProjectContext,
              commandCapabilities,
              toolsRequired,
            };
          } else {
            try {
              await dispatchMessageWithoutWebSocket(
                sessionId,
                content,
                messageProjectId,
                generationProfile,
                undefined,
                clientMessageId,
                commandCapabilities,
                data.initial_message
                  ? {
                      skipUserPersistence: true,
                      persistedUserMessageId: data.initial_message.id,
                    }
                  : undefined,
                toolsRequired,
              );
              markWaitingResponse(sessionId);
            } catch (err) {
              console.error("新規セッションのREST送信失敗:", err);
              const failureMessage = createDispatchFailureMessage(sessionId);
              dispatchChatTimeline({ type: "append", message: failureMessage });
              clearWaitingResponse(sessionId);
              setTimeout(() => setIsSending(false), 500);
              return;
            }
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              data.session,
            );
          }

          setTimeout(() => setIsSending(false), 500);
          return;
        } catch (err) {
          console.error("セッション自動作成失敗:", err);
          clearWaitingResponse(sessionId ?? null);
          setIsSending(false);
          return;
        }
      }
      const knownAssistantIds = new Set(
        messages
          .filter(
            (message) =>
              message.session_id === sessionId && message.role === "assistant",
          )
          .map((message) => message.id),
      );

      // WebSocket未接続の場合は保留して待機
      if (!isConnected) {
        const userMsg = createLocalUserMessage(
          sessionId,
          content,
          clientMessageId,
          files,
          commandCapabilities,
        );
        dispatchChatTimeline({ type: "append", message: userMsg });
        if (files?.length || mentions?.length) {
          pendingMessageRef.current = {
            sessionId,
            content,
            clientMessageId,
            projectId: messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext,
            commandCapabilities,
            toolsRequired,
          };
          bumpSession(sessionId);
        } else {
          try {
            await dispatchMessageWithoutWebSocket(
              sessionId,
              content,
              messageProjectId,
              generationProfile,
              undefined,
              clientMessageId,
              commandCapabilities,
              undefined,
              toolsRequired,
            );
            markWaitingResponse(sessionId);
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              currentSession,
            );
          } catch (err) {
            console.error("WebSocket未接続時のREST送信失敗:", err);
            const failureMessage = createDispatchFailureMessage(sessionId);
            dispatchChatTimeline({ type: "append", message: failureMessage });
            clearWaitingResponse(sessionId);
          }
        }
        setTimeout(() => setIsSending(false), 500);
        return;
      }

      // ユーザーメッセージをローカルに即座追加
      const userMsg = createLocalUserMessage(
        sessionId,
        content,
        clientMessageId,
        files,
        commandCapabilities,
      );
      dispatchChatTimeline({ type: "append", message: userMsg });

      // グループチャットは共有セッション用WebSocketで送信する。
      if (isGroupChat && sessionId) {
        const accepted = await sendMessage(
          content,
          messageProjectId,
          files,
          mentions,
          generationProfile,
          includeProjectContext,
          undefined,
          undefined,
          sessionId,
          clientMessageId,
          commandCapabilities,
          toolsRequired,
        );
        if (!accepted) {
          pendingMessageRef.current = {
            sessionId,
            content,
            clientMessageId,
            projectId: messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext,
            commandCapabilities,
            toolsRequired,
          };
        } else {
          bumpSession(sessionId);
        }
        setIsSending(false);
        clearWaitingResponse(sessionId);
        return;
      }

      // 通常テキスト送信はRESTで先にDB保存してから生成をキューする。
      // ファイル/メンションはWebSocket側で添付ペイロードを組み立てる。
      if (files?.length || mentions?.length) {
        const accepted = await sendMessage(
          content,
          messageProjectId,
          files,
          mentions,
          generationProfile,
          includeProjectContext,
          undefined,
          undefined,
          sessionId,
          clientMessageId,
          commandCapabilities,
          toolsRequired,
        );
        if (!accepted) {
          pendingMessageRef.current = {
            sessionId,
            content,
            clientMessageId,
            projectId: messageProjectId,
            files,
            mentions,
            generationProfile,
            includeProjectContext,
            commandCapabilities,
            toolsRequired,
          };
        } else {
          bumpSession(sessionId);
          markWaitingResponse(sessionId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
        }
      } else {
        try {
          await dispatchMessageWithoutWebSocket(
            sessionId,
            content,
            messageProjectId,
            generationProfile,
            undefined,
            clientMessageId,
            commandCapabilities,
            undefined,
            toolsRequired,
          );
          markWaitingResponse(sessionId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
        } catch (err) {
          console.error("メッセージ送信失敗:", err);
          const failureMessage = createDispatchFailureMessage(sessionId);
          dispatchChatTimeline({ type: "append", message: failureMessage });
          clearWaitingResponse(sessionId);
        }
      }
      setTimeout(() => setIsSending(false), 500);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      activeSessionId,
      isConnected,
      sendMessage,
      router,
      effectiveProjectId,
      allProjects,
      includeProjectContext,
      messages,
      activateSession,
      addSession,
      bumpSession,
      clearWaitingResponse,
      isSending,
      deepResearchEnabled,
      handleDeepResearchMessage,
      isGroupChat,
      dispatchMessageWithoutWebSocket,
      createDispatchFailureMessage,
      currentSession,
      isScenarioChatSession,
      markWaitingResponse,
      waitForPersistedAssistantResponse,
    ],
  );

  const dispatchBranchMessage = useCallback(
    async (
      sourceMessage: ConversationMessage,
      content: string,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (
        !activeSessionId ||
        isSending ||
        displayIsWaitingResponse ||
        isStreaming
      ) {
        return;
      }
      const knownAssistantIds = new Set(
        messages
          .filter((message) => message.role === "assistant")
          .map((message) => message.id),
      );
      const commandCapabilities = commandCapabilitiesFromMessageMetadata(
        sourceMessage.metadata,
      );
      setIsSending(true);
      setPendingAgentRunId(null);
      try {
        const result = await chatApi.dispatchMessage(activeSessionId, {
          message: content,
          project_id: effectiveProjectId,
          generation_profile: "autonomous_work",
          include_project_context: includeProjectContext,
          edit_message_id: sourceMessage.id,
          response_model: responseModel,
          command_capabilities:
            commandCapabilities.length > 0 ? commandCapabilities : undefined,
        });
        if (result.agent_run_id) {
          setPendingAgentRunId(result.agent_run_id);
        }
        markWaitingResponse(activeSessionId);
        bumpSession(activeSessionId);
        void waitForPersistedAssistantResponse(
          activeSessionId,
          knownAssistantIds,
          currentSession,
        );
      } catch (err) {
        console.error("分岐メッセージ送信失敗:", err);
        clearWaitingResponse(activeSessionId);
      } finally {
        setTimeout(() => setIsSending(false), 500);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      activeSessionId,
      bumpSession,
      clearWaitingResponse,
      effectiveProjectId,
      includeProjectContext,
      isSending,
      isStreaming,
      displayIsWaitingResponse,
      markWaitingResponse,
      messages,
      currentSession,
      waitForPersistedAssistantResponse,
    ],
  );

  const handleEditMessage = useCallback(
    (messageId: string, newContent: string) => {
      const source = messages.find((message) => message.id === messageId);
      if (!source) return;
      void dispatchBranchMessage(source, newContent);
    },
    [dispatchBranchMessage, messages],
  );

  const handleRerunMessage = useCallback(
    (
      message: ConversationMessage,
      responseModel?: ChatResponseModelSelection,
    ) => {
      if (message.role === "user") {
        void dispatchBranchMessage(message, message.content, responseModel);
        return;
      }

      const index = messages.findIndex((item) => item.id === message.id);
      const source = [...messages]
        .slice(0, index >= 0 ? index : messages.length)
        .reverse()
        .find((item) => item.role === "user");
      if (!source) return;
      void dispatchBranchMessage(source, source.content, responseModel);
    },
    [dispatchBranchMessage, messages],
  );

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    handleCreateGroupChat,
    handleSendMessage,
    handleEditMessage,
    handleRerunMessage,
  };
}
