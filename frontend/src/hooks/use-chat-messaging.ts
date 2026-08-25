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
import type {
  ChatCommandCapability,
  ChatResponseModelSelection,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import {
  attachmentsFromMessageMetadata,
  createLocalMessage,
  createLocalUserMessage,
} from "@/lib/chat-local-messages";
import { commandCapabilitiesFromMessageMetadata } from "@/lib/chat-commands";
import { loadStoredPlanningPolicy } from "@/lib/planning-policy";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";
import type { ChatGenerationEvent } from "@/lib/chat-generation-state";
import type {
  ChatComposerSendResult,
  SubmittedSteeringInstruction,
} from "@/components/chat/chat-composer";
import type { MentionItem } from "@/components/chat/mention-menu";
import type { ChatAppContextSelection } from "@/components/chat/app-context-picker";
import { useChatPersistence } from "@/hooks/use-chat-persistence";
import {
  OPTIMISTIC_NEW_CHAT_SESSION_PREFIX,
  useDeepResearchMessage,
} from "@/hooks/use-deep-research-message";
import { useGroupChatCreate } from "@/hooks/use-group-chat-create";
import { resolveMessageProjectId } from "@/lib/project-message-routing";
import { registerAndActivateChatSession } from "@/lib/chat-session-lifecycle";
import { applyPendingNewChatLlmSettingsToSession } from "@/lib/new-chat-llm-settings-store";
import { PendingLlmHandoffError } from "@/lib/chat-session-route-handoff";
import { getGenerationReadyNewChatMainRoute } from "@/hooks/use-chat-session-route";
import { hasExplicitSessionRoute } from "@/lib/chat-session-route";
import { awaitSessionLlmSettingsReady } from "@/lib/session-llm-settings-save-queue";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { toast } from "sonner";

const DISPATCH_FAILURE_MESSAGE =
  "応答生成を開始できませんでした。送信内容が保存されたかは確認できません。接続を確認し、会話を再読み込みしてからもう一度実行してください。";

const LLM_HANDOFF_FAILURE_MESSAGE =
  "選択した Provider / Model 設定を会話へ適用できなかったため、応答生成を開始しませんでした。Chat settings を確認してからもう一度送信してください。";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

export type PendingMessage = {
  sessionId: string;
  content: string;
  clientMessageId: string;
  projectId?: string;
  files?: File[];
  mentions?: MentionItem[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  appContext?: ChatAppContextSelection | null;
  commandCapabilities?: ChatCommandCapability[];
  toolsRequired?: boolean;
  /** WebSocket 再接続後の最終 dispatch 結果を composer へ通知する。 */
  settle?: (result: ChatComposerSendResult) => void;
};

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
    metadata?: Record<string, unknown>;
  }>;
  effectiveProjectId: string | undefined;
  isStoryChatSession: boolean;
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
  upsertSession: (session: ConversationSession) => void;
  bumpSession: (sessionId: string) => void;
  updateSidebarTitle: (sessionId: string, title: string) => void;

  // WebSocket
  sendMessage: ReturnType<
    typeof import("@/hooks/use-websocket").useWebSocket
  >["sendMessage"];

  // dispatch / setters
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  markWaitingResponse: (
    sessionId: string | null,
    clientMessageId?: string | null,
  ) => void;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setIsLoadingMessages: Dispatch<SetStateAction<boolean>>;
  setSessionLoadError: Dispatch<SetStateAction<string | null>>;
  setWritingSession: Dispatch<
    SetStateAction<
      Awaited<ReturnType<typeof import("@/lib/story/api").storyApi.getWritingSessionByConversation>>
    >
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
  isStoryChatSession,
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
  upsertSession,
  bumpSession,
  updateSidebarTitle,
  sendMessage,
  dispatchChatTimeline,
  dispatchGeneration,
  markWaitingResponse,
  setSteeringInstructions,
  setIsLoadingMessages,
  setSessionLoadError,
  setWritingSession,
}: UseChatMessagingArgs) {
  const draftUserId = useCurrentUserId();
  const {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    waitForPersistedAssistantResponse,
    bumpSessionForAssistant,
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
    dispatchGeneration,
    upsertSession,
    bumpSession,
    setSteeringInstructions,
    setIsLoadingMessages,
    setSessionLoadError,
    setWritingSession,
  });
  const branchSwitchInFlightRef = useRef<string | null>(null);

  const { handleDeepResearchMessage } = useDeepResearchMessage({
    router,
    activeSessionId,
    activeSessionIdRef,
    activateSession,
    includeProjectContext,
    addSession,
    bumpSession,
    updateSidebarTitle,
    dispatchChatTimeline,
    dispatchGeneration,
    upsertSession,
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
      appContext?: ChatAppContextSelection | null,
      mentions?: MentionItem[],
    ) => {
      const result = await chatApi.dispatchMessage(sessionId, {
        message: content,
        project_id: projectId,
        app_id: appContext?.appId ?? null,
        app_target_id: appContext?.targetId ?? null,
        generation_profile: generationProfile,
        planning_policy: loadStoredPlanningPolicy(
          typeof window !== "undefined" ? window.localStorage : null,
        ),
        include_project_context: includeProjectContext,
        response_model: responseModel,
        client_message_id: clientMessageId,
        command_capabilities: commandCapabilities,
        tools_required: toolsRequired,
        skip_user_persistence: persistence?.skipUserPersistence,
        persisted_user_message_id: persistence?.persistedUserMessageId,
        mentions,
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
        dispatchGeneration({
          type: "dispatch_accepted",
          sessionId,
          clientMessageId,
          agentRunId: result.agent_run_id,
          statusMessage: "応答をキューに追加しました",
        });
      }
      bumpSession(sessionId);
      return result;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bumpSession, dispatchGeneration, includeProjectContext],
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
        loadStoredPlanningPolicy(
          typeof window !== "undefined" ? window.localStorage : null,
        ),
        pending.includeProjectContext,
        undefined,
        undefined,
        pending.sessionId,
        pending.clientMessageId,
        pending.commandCapabilities,
        pending.toolsRequired,
        pending.appContext
          ? { appId: pending.appContext.appId, targetId: pending.appContext.targetId }
          : null,
      );
      // セッション切替直後は、前の接続の isConnected=true が一瞬残る。
      // 新しい WebSocket がまだ CONNECTING の場合は保留を維持し、
      // 実際に OPEN になった次の effect で送信する。
      if (cancelled || !accepted || pendingMessageRef.current !== pending)
        return;
      pendingMessageRef.current = null;
      markWaitingResponse(pending.sessionId, pending.clientMessageId);
      pending.settle?.("accepted");
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
    upsertSession,
    dispatchChatTimeline,
  });

  const queuePendingMessage = useCallback(
    (
      pending: Omit<PendingMessage, "settle">,
    ): Promise<ChatComposerSendResult> =>
      new Promise((resolve) => {
        pendingMessageRef.current = {
          ...pending,
          settle: resolve,
        };
      }),
    [pendingMessageRef],
  );

  // ─── メッセージ送信 ───
  const handleSendMessage = useCallback(
    async (
      content: string,
      files?: File[],
      mentions?: MentionItem[],
      generationProfile?: string,
      commandCapabilities?: ChatCommandCapability[],
      toolsRequired?: boolean,
      appContext?: ChatAppContextSelection | null,
    ) => {
      // 連打防止
      if (isSending) return "failed" as ChatComposerSendResult;
      const clientMessageId = createClientMessageId();
      const hasCommandCapabilities = Boolean(commandCapabilities?.length);
      const isDeepResearchSubmission =
        deepResearchEnabled && !hasCommandCapabilities;
      if (activeSessionId && !isDeepResearchSubmission) {
        dispatchGeneration({
          type: "dispatch_started",
          sessionId: activeSessionId,
          clientMessageId,
        });
      }
      const messageProjectId = isStoryChatSession
        ? undefined
        : resolveMessageProjectId({
            content,
            projects: allProjects,
            sessionProjectId: currentSession?.project_id,
            fallbackProjectId: effectiveProjectId,
          });
      if (isDeepResearchSubmission) {
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
          if (activeSessionId) {
            dispatchGeneration({
              type: "failed",
              sessionId: activeSessionId,
              clientMessageId,
              statusMessage: "Deep Researchではファイル添付を送信できません",
              eventId: `dispatch:${activeSessionId}:${clientMessageId}:failed`,
            });
          }
          return "failed" as ChatComposerSendResult;
        }
        return (
          await handleDeepResearchMessage(
            content,
            messageProjectId,
            clientMessageId,
          )
        )
          ? ("accepted" as ChatComposerSendResult)
          : ("failed" as ChatComposerSendResult);
      }

      // セッション未選択時は自動作成（選択中のプロジェクトを紐付け）
      let sessionId = activeSessionId;
      const generationReadyMain = sessionId
        ? null
        : getGenerationReadyNewChatMainRoute();
      if (!sessionId && !hasExplicitSessionRoute(generationReadyMain)) {
        // authoritative route の確認は server await より前に行う validation。
        // この場合は送信自体を開始しないため optimistic bubble も出さない。
        console.error(
          "Provider / Model の authoritative route を確定できないため、応答生成を開始しませんでした。",
        );
        return "failed" as ChatComposerSendResult;
      }
      const provisionalSessionId = `${OPTIMISTIC_NEW_CHAT_SESSION_PREFIX}${clientMessageId}`;
      let optimisticSessionId = sessionId ?? provisionalSessionId;

      // 送信処理が REST / WebSocket / 設定保存のいずれを選ぶ場合でも、
      // 最初の server await より前にこの submission 専用の user bubble を
      // 1 件だけ追加する。新規チャットはまだ session が存在しないため、
      // provisional id で描画し、create 成功後に reducer で session id を
      // rebinding する（provisional id を active にすることはない）。
      dispatchChatTimeline({
        type: "append",
        message: createLocalUserMessage(
          optimisticSessionId,
          content,
          clientMessageId,
          files,
          commandCapabilities,
        ),
      });

      if (!sessionId) {
        try {
          // generationReadyMain は上の validation で確定済み。
          // （型上 null が残るため、ここでは explicit route を再確認する。）
          if (!hasExplicitSessionRoute(generationReadyMain)) {
            throw new PendingLlmHandoffError(
              "Provider / Model の authoritative route を確定できないため、応答生成を開始しませんでした。",
            );
          }
          const canPersistInitialMessage =
            !files?.length && !mentions?.length && !hasCommandCapabilities;
          const data = await chatApi.createSession(
            await chatApi.getCurrentCharacterName(),
            messageProjectId,
            canPersistInitialMessage
              ? { content, client_message_id: clientMessageId }
              : undefined,
            appContext
              ? { appId: appContext.appId, targetId: appContext.targetId }
              : null,
            generationReadyMain,
          );
          sessionId = data.session.id;
          // 新規 session はまだ active にせず、先に optimistic bubble を
          // 実 session へ rebinding する。これにより hydration / navigation
          // が provisional id を観測する race を防ぐ。
          dispatchChatTimeline({
            type: "rebind_client_message_session",
            fromSessionId: provisionalSessionId,
            toSessionId: sessionId,
            clientMessageId,
          });
          optimisticSessionId = sessionId;
          await awaitSessionLlmSettingsReady(sessionId);
          try {
            const applied = await applyPendingNewChatLlmSettingsToSession(
              sessionId,
              draftUserId,
              generationReadyMain,
            );
            if (!applied) {
              throw new PendingLlmHandoffError(
                "表示中の Provider / Model をセッションへ確定できませんでした。",
              );
            }
          } catch (error) {
            console.error("Failed to apply pending new-chat LLM settings:", error);
            dispatchChatTimeline({
              type: "remove_client_message",
              sessionId: optimisticSessionId,
              clientMessageId,
            });
            addSession(data.session);
            activateSession(sessionId);
            const href = `/chat?s=${encodeURIComponent(sessionId)}`;
            if (!navigateChatSessionInPlace(href)) {
              router.push(href);
            }
            const failureMessage = createLocalMessage(
              sessionId,
              "assistant",
              error instanceof PendingLlmHandoffError
                ? `${LLM_HANDOFF_FAILURE_MESSAGE} (${error.message})`
                : LLM_HANDOFF_FAILURE_MESSAGE,
            );
            dispatchChatTimeline({ type: "append", message: failureMessage });
            dispatchGeneration({
              type: "failed",
              sessionId,
              clientMessageId,
              statusMessage: LLM_HANDOFF_FAILURE_MESSAGE,
              eventId: `dispatch:${sessionId}:${clientMessageId}:handoff-failed`,
            });
            return "failed" as ChatComposerSendResult;
          }
          if (data.initial_message) {
            // createSession が初回メッセージを同時保存した場合は、既存の
            // optimistic bubble を server id へ昇格させるだけで再 append
            // しない。REST dispatch の skipUserPersistence とも対応する。
            dispatchChatTimeline({
              type: "promote_client_message",
              sessionId,
              clientMessageId,
              serverMessageId: data.initial_message.id,
            });
          }
          registerAndActivateChatSession({
            session: data.session,
            addSession,
            activateSession,
            initializeGeneration: (newSessionId) => {
              dispatchGeneration({ type: "reset", sessionId: newSessionId });
              dispatchGeneration({
                type: "dispatch_started",
                sessionId: newSessionId,
                clientMessageId,
              });
            },
          });
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }

          const knownAssistantIds = new Set<string>();
          // 添付は WebSocket 側でアップロード処理が必要なためキューへ保持する。
          // メンションだけなら REST dispatch でも同じ構造化 payload を送れるので、
          // 新規セッション直後や WS 未接続でも ID の意味を失わない。
          if (files?.length) {
            return queuePendingMessage({
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
              appContext,
            });
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
                appContext,
                mentions,
              );
              markWaitingResponse(sessionId, clientMessageId);
            } catch (err) {
              console.error("新規セッションのREST送信失敗:", err);
              dispatchChatTimeline({
                type: "remove_client_message",
                sessionId: optimisticSessionId,
                clientMessageId,
              });
              const failureMessage = createDispatchFailureMessage(sessionId);
              dispatchChatTimeline({ type: "append", message: failureMessage });
              dispatchGeneration({
                type: "failed",
                sessionId,
                clientMessageId,
                statusMessage: DISPATCH_FAILURE_MESSAGE,
                eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
              });
              return "failed" as ChatComposerSendResult;
            }
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              data.session,
            );
          }

          return files?.length
            ? ("pending" as ChatComposerSendResult)
            : ("accepted" as ChatComposerSendResult);
        } catch (err) {
          console.error("セッション自動作成失敗:", err);
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId: optimisticSessionId,
            clientMessageId,
          });
          if (sessionId) {
            dispatchGeneration({
              type: "failed",
              sessionId,
              clientMessageId,
              statusMessage: "セッション自動作成に失敗しました",
              eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
            });
          }
          return "failed" as ChatComposerSendResult;
        }
      }
      try {
        await awaitSessionLlmSettingsReady(sessionId);
      } catch (err) {
        console.error("セッション設定の反映待機に失敗しました:", err);
        dispatchChatTimeline({
          type: "remove_client_message",
          sessionId: optimisticSessionId,
          clientMessageId,
        });
        dispatchGeneration({
          type: "failed",
          sessionId,
          clientMessageId,
          statusMessage: DISPATCH_FAILURE_MESSAGE,
          eventId: `dispatch:${sessionId}:${clientMessageId}:settings-failed`,
        });
        return "failed" as ChatComposerSendResult;
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
        if (files?.length) {
          bumpSession(sessionId);
          return queuePendingMessage({
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
            appContext,
          });
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
              appContext,
              mentions,
            );
            markWaitingResponse(sessionId, clientMessageId);
            void waitForPersistedAssistantResponse(
              sessionId,
              knownAssistantIds,
              currentSession,
            );
          } catch (err) {
            console.error("WebSocket未接続時のREST送信失敗:", err);
            dispatchChatTimeline({
              type: "remove_client_message",
              sessionId: optimisticSessionId,
              clientMessageId,
            });
            const failureMessage = createDispatchFailureMessage(sessionId);
            dispatchChatTimeline({ type: "append", message: failureMessage });
            dispatchGeneration({
              type: "failed",
              sessionId,
              clientMessageId,
              statusMessage: DISPATCH_FAILURE_MESSAGE,
              eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
            });
            return "failed" as ChatComposerSendResult;
          }
        }
        return files?.length
          ? ("pending" as ChatComposerSendResult)
          : ("accepted" as ChatComposerSendResult);
      }

      // グループチャットは共有セッション用WebSocketで送信する。
      if (isGroupChat && sessionId) {
        let accepted: boolean;
        try {
          accepted = await sendMessage(
            content,
            messageProjectId,
            files,
            mentions,
            generationProfile,
            loadStoredPlanningPolicy(
              typeof window !== "undefined" ? window.localStorage : null,
            ),
            includeProjectContext,
            undefined,
            undefined,
            sessionId,
            clientMessageId,
            commandCapabilities,
            toolsRequired,
            appContext
              ? { appId: appContext.appId, targetId: appContext.targetId }
              : null,
          );
        } catch (err) {
          console.error("グループチャットのWebSocket送信失敗:", err);
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId: optimisticSessionId,
            clientMessageId,
          });
          dispatchChatTimeline({
            type: "append",
            message: createDispatchFailureMessage(sessionId),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "failed" as ChatComposerSendResult;
        }
        if (!accepted) {
          return queuePendingMessage({
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
            appContext,
          });
        } else {
          bumpSession(sessionId);
          dispatchGeneration({
            type: "dispatch_accepted",
            sessionId,
            clientMessageId,
            statusMessage: "応答をキューに追加しました",
          });
        }
        return "accepted" as ChatComposerSendResult;
      }

      // 通常テキスト送信はRESTで先にDB保存してから生成をキューする。
      // ファイル/メンションはWebSocket側で添付ペイロードを組み立てる。
      if (files?.length || mentions?.length) {
        let accepted: boolean;
        try {
          accepted = await sendMessage(
            content,
            messageProjectId,
            files,
            mentions,
            generationProfile,
            loadStoredPlanningPolicy(
              typeof window !== "undefined" ? window.localStorage : null,
            ),
            includeProjectContext,
            undefined,
            undefined,
            sessionId,
            clientMessageId,
            commandCapabilities,
            toolsRequired,
            appContext
              ? { appId: appContext.appId, targetId: appContext.targetId }
              : null,
          );
        } catch (err) {
          console.error("添付/メンションのWebSocket送信失敗:", err);
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId: optimisticSessionId,
            clientMessageId,
          });
          dispatchChatTimeline({
            type: "append",
            message: createDispatchFailureMessage(sessionId),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "failed" as ChatComposerSendResult;
        }
        if (!accepted) {
          return queuePendingMessage({
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
            appContext,
          });
        } else {
          bumpSession(sessionId);
          markWaitingResponse(sessionId, clientMessageId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
        }
        return "accepted" as ChatComposerSendResult;
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
            appContext,
            mentions,
          );
          markWaitingResponse(sessionId, clientMessageId);
          void waitForPersistedAssistantResponse(
            sessionId,
            knownAssistantIds,
            currentSession,
          );
          return "accepted" as ChatComposerSendResult;
        } catch (err) {
          console.error("メッセージ送信失敗:", err);
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId: optimisticSessionId,
            clientMessageId,
          });
          const failureMessage = createDispatchFailureMessage(sessionId);
          dispatchChatTimeline({ type: "append", message: failureMessage });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId,
            statusMessage: DISPATCH_FAILURE_MESSAGE,
            eventId: `dispatch:${sessionId}:${clientMessageId}:failed`,
          });
          return "failed" as ChatComposerSendResult;
        }
      }
    },
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
      isSending,
      deepResearchEnabled,
      handleDeepResearchMessage,
      isGroupChat,
      dispatchMessageWithoutWebSocket,
      createDispatchFailureMessage,
      currentSession,
      dispatchChatTimeline,
      dispatchGeneration,
      isStoryChatSession,
      markWaitingResponse,
      queuePendingMessage,
      waitForPersistedAssistantResponse,
      draftUserId,
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
      // 添付付きメッセージの再実行では元の添付を引き継ぐ。バイナリは保存されて
      // いないため、バックエンドがプロジェクト内パスから実体を読み直す。
      const attachments = attachmentsFromMessageMetadata(sourceMessage.metadata);
      const branchClientMessageId = `branch:${sourceMessage.id}:${Date.now()}`;
      dispatchGeneration({
        type: "dispatch_started",
        sessionId: activeSessionId,
        clientMessageId: branchClientMessageId,
      });
      try {
        const result = await chatApi.dispatchMessage(activeSessionId, {
          message: content,
          client_message_id: branchClientMessageId,
          project_id: effectiveProjectId,
          generation_profile: "autonomous_work",
          include_project_context: includeProjectContext,
          edit_message_id: sourceMessage.id,
          response_model: responseModel,
          command_capabilities:
            commandCapabilities.length > 0 ? commandCapabilities : undefined,
          attachments: attachments.length > 0 ? attachments : undefined,
        });
        if (result.agent_run_id) {
          dispatchGeneration({
            type: "dispatch_accepted",
            sessionId: activeSessionId,
            clientMessageId: branchClientMessageId,
            agentRunId: result.agent_run_id,
            statusMessage: "応答をキューに追加しました",
          });
        }
        markWaitingResponse(activeSessionId, branchClientMessageId);
        bumpSession(activeSessionId);
        void waitForPersistedAssistantResponse(
          activeSessionId,
          knownAssistantIds,
          currentSession,
        );
      } catch (err) {
        // 失敗を黙って捨てると「再実行を押しても何も起きない」ように見える。
        console.error("分岐メッセージ送信失敗:", err);
        dispatchChatTimeline({
          type: "append",
          message: createDispatchFailureMessage(activeSessionId),
        });
        dispatchGeneration({
          type: "failed",
          sessionId: activeSessionId,
          clientMessageId: branchClientMessageId,
          statusMessage: DISPATCH_FAILURE_MESSAGE,
          eventId: `dispatch:${activeSessionId}:${branchClientMessageId}:failed`,
        });
      }
    },
    [
      activeSessionId,
      bumpSession,
      createDispatchFailureMessage,
      dispatchChatTimeline,
      dispatchGeneration,
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

  /**
   * 既存ブランチの active path をサーバー側で切り替える。
   * ブランチ切替は新しい generation を開始せず、成功後に full refresh で
   * server-authoritative な履歴を取り直す。
   */
  const handleSwitchBranch = useCallback(
    async (message: ConversationMessage, targetBranchIndex: number) => {
      const sessionId = activeSessionId;
      if (!sessionId) return;

      if (isSending || displayIsWaitingResponse || isStreaming) {
        toast.info("応答生成中は分岐を切り替えられません");
        return;
      }

      const branchCount =
        typeof message.branch_count === "number" &&
        Number.isInteger(message.branch_count)
          ? message.branch_count
          : null;
      if (branchCount === null || branchCount <= 1) {
        toast.error("切り替え可能な分岐情報を取得できませんでした");
        return;
      }
      if (
        !Number.isInteger(targetBranchIndex) ||
        targetBranchIndex < 0 ||
        targetBranchIndex >= branchCount
      ) {
        toast.error("無効な分岐が指定されました");
        return;
      }

      const currentBranchIndex =
        typeof message.branch_index === "number" &&
        Number.isInteger(message.branch_index)
          ? message.branch_index
          : 0;
      if (
        currentBranchIndex < 0 ||
        currentBranchIndex >= branchCount
      ) {
        toast.error("現在の分岐情報が不正です");
        return;
      }
      if (currentBranchIndex === targetBranchIndex) return;

      const inFlightKey = `${sessionId}:${message.id}`;
      if (branchSwitchInFlightRef.current === inFlightKey) return;
      if (branchSwitchInFlightRef.current) return;
      branchSwitchInFlightRef.current = inFlightKey;

      try {
        const result = await chatApi.switchBranch(
          sessionId,
          message.id,
          targetBranchIndex,
        );
        if (!result?.success) {
          throw new Error("ブランチ切替APIが失敗を返しました");
        }

        const refreshed = await refreshPersistedMessages(sessionId, {
          forceFull: true,
        });
        if (!refreshed) {
          throw new Error("ブランチ切替後の履歴再取得に失敗しました");
        }
        toast.success("分岐を切り替えました");
      } catch (error) {
        console.error("分岐切替失敗:", error);
        toast.error("分岐を切り替えられませんでした。会話を再読み込みしてください");
      } finally {
        if (branchSwitchInFlightRef.current === inFlightKey) {
          branchSwitchInFlightRef.current = null;
        }
      }
    },
    [
      activeSessionId,
      displayIsWaitingResponse,
      isSending,
      isStreaming,
      refreshPersistedMessages,
    ],
  );

  return {
    maybeGenerateLoadedSessionTitle,
    refreshPersistedMessages,
    bumpSessionForAssistant,
    handleCreateGroupChat,
    handleSendMessage,
    handleEditMessage,
    handleRerunMessage,
    handleSwitchBranch,
  };
}
