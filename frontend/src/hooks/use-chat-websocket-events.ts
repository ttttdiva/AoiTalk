"use client";

import {
  useEffect,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import { toast } from "sonner";
import type {
  ChatToolResultMetadata,
  ConversationMessage,
  ConversationSession,
  LlmMode,
} from "@/lib/chat-api";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import type { useWebSocket } from "@/hooks/use-websocket";
import type { chatTimelineReducer } from "@/lib/chat-state";
import {
  createLocalMessage,
  isChatToolResultMetadata,
} from "@/lib/chat-local-messages";
import {
  getCancelledAssistantPayloads,
  getWebSocketMessageClientMessageId,
  getWebSocketMessageEventKey,
  getWebSocketMessageId,
  getWebSocketMessageAgentRunId,
  getWebSocketMessageSessionId,
  isWebSocketMessageForSession,
} from "@/lib/chat-websocket-events";
import type {
  ChatGenerationEvent,
  ChatGenerationState,
} from "@/lib/chat-generation-state";
import { selectGenerationAgentRunId } from "@/lib/chat-generation-state";
import { explorerBookmarks, explorerSearch } from "@/lib/explorer-api";
import type {
  AskUserQuestionRequest,
  ExternalModelPromptRequest,
  PlanApprovalRequest,
  ToolPermissionRequest,
  ToolPermissionScope,
} from "@/components/chat/chat-permission-dialogs";

type WSMessage = NonNullable<ReturnType<typeof useWebSocket>["lastMessage"]>;
type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

type UseChatWebSocketEventsArgs = {
  lastMessage: WSMessage | null;
  activeSessionId: string | null;
  connectionGeneration?: number;
  streamBuffer: RefObject<string>;
  currentSession: ConversationSession | null;

  processedEventIdsRef: RefObject<Set<string>>;
  processedLegacyMessageRef: RefObject<unknown>;
  liveToolResultsRef: RefObject<ChatToolResultMetadata[]>;
  streamingIntervalRef: RefObject<ReturnType<typeof setInterval> | null>;
  responsePollGenerationRef: RefObject<number>;

  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  refreshPersistedMessages: (
    sessionId: string,
  ) => Promise<ConversationMessage[] | null>;
  /** Bump only after refresh confirms the assistant row is persisted. */
  bumpSessionForAssistant: (sessionId: string, messageId: string) => void;
  maybeGenerateLoadedSessionTitle: (
    session: ConversationSession,
    messages: ConversationMessage[],
  ) => Promise<void>;
  updateSidebarTitle: (sessionId: string, title: string) => void;

  setLlmModeState: (mode: LlmMode) => void;
  setLlmModeOptions: (modes: LlmMode[]) => void;
  setLlmModeLabels: (labels: Record<string, string>) => void;
  setToolPermissionRequest: (request: ToolPermissionRequest | null) => void;
  setExternalModelPromptRequest: (
    request: ExternalModelPromptRequest | null,
  ) => void;
  setExternalModelPromptDraft: (draft: string) => void;
  setAskUserQuestionRequest?: (request: AskUserQuestionRequest | null) => void;
  setAskUserQuestionDraft?: (draft: string) => void;
  setAskUserQuestionChoices?: (choices: string[]) => void;
  setPlanApprovalRequest?: (request: PlanApprovalRequest | null) => void;
  setPlanApprovalDraft?: (draft: string) => void;
  setPlanApprovalFeedbackDraft?: (draft: string) => void;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setStreamingContent: (content: string) => void;
  setLiveToolResults: (results: ChatToolResultMetadata[]) => void;

  generationState: ChatGenerationState;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;

  play: (item: { name: string; path: string; type: string }) => void;
  stopAudio: () => void;
  setVolume: (volume: number) => void;
};

/**
 * WebSocket 受信メッセージ（`lastMessage`）を処理する effect を担うフック。
 * `page.tsx` の該当 effect を挙動不変で移設したもの。依存配列は元コードと同一に保つ。
 */
export function useChatWebSocketEvents({
  lastMessage,
  activeSessionId,
  connectionGeneration,
  streamBuffer,
  currentSession,
  processedEventIdsRef,
  processedLegacyMessageRef,
  liveToolResultsRef,
  streamingIntervalRef,
  responsePollGenerationRef,
  dispatchChatTimeline,
  refreshPersistedMessages,
  bumpSessionForAssistant,
  maybeGenerateLoadedSessionTitle,
  updateSidebarTitle,
  setLlmModeState,
  setLlmModeOptions,
  setLlmModeLabels,
  setToolPermissionRequest,
  setExternalModelPromptRequest,
  setExternalModelPromptDraft,
  setAskUserQuestionRequest,
  setAskUserQuestionDraft,
  setAskUserQuestionChoices,
  setPlanApprovalRequest,
  setPlanApprovalDraft,
  setPlanApprovalFeedbackDraft,
  setSteeringInstructions,
  setStreamingContent,
  setLiveToolResults,
  generationState,
  dispatchGeneration,
  play,
  stopAudio,
  setVolume,
}: UseChatWebSocketEventsArgs) {
  useEffect(() => {
    if (!lastMessage) return;

    if (!isWebSocketMessageForSession(lastMessage, activeSessionId)) return;
    if (
      typeof lastMessage.__connection_generation === "number" &&
      (lastMessage.__connection_generation !== connectionGeneration ||
        lastMessage.__connection_session_id !== activeSessionId)
    ) {
      return;
    }

    // Stable event_id/sequence is the primary idempotency key.  Legacy
    // servers have no such field, so only the same object instance is ignored
    // on an effect re-run; identical payloads from newer events are never
    // collapsed by content.
    const eventSessionId = getWebSocketMessageSessionId(lastMessage);
    const eventAgentRunId = getWebSocketMessageAgentRunId(lastMessage);
    const eventClientMessageId =
      getWebSocketMessageClientMessageId(lastMessage);
    const eventId = getWebSocketMessageEventKey(lastMessage);
    const currentAgentRunId = activeSessionId
      ? selectGenerationAgentRunId(generationState, activeSessionId)
      : null;
    if (
      eventAgentRunId &&
      currentAgentRunId &&
      eventAgentRunId !== currentAgentRunId &&
      lastMessage.type !== "steering_update" &&
      lastMessage.type !== "live_voice.event"
    ) {
      return;
    }
    const isForeignSessionEvent = (sessionId: unknown) =>
      typeof sessionId === "string" &&
      sessionId.length > 0 &&
      Boolean(activeSessionId) &&
      sessionId !== activeSessionId;

    if (isForeignSessionEvent(eventSessionId)) return;

    const generationIsCorrelated = (() => {
      if (!activeSessionId || eventSessionId !== activeSessionId) return false;
      if (currentAgentRunId) {
        if (eventAgentRunId) return eventAgentRunId === currentAgentRunId;
        return Boolean(
          eventClientMessageId &&
            eventClientMessageId === generationState.lifecycle.clientMessageId,
        );
      }
      if (generationState.lifecycle.generationEpoch == null) return true;
      return Boolean(
        eventClientMessageId &&
          eventClientMessageId === generationState.lifecycle.clientMessageId,
      );
    })();

    const requiresGenerationCorrelation =
      lastMessage.type === "tool_start" ||
      lastMessage.type === "tool_end" ||
      lastMessage.type === "status_update" ||
      lastMessage.type === "reasoning_progress" ||
      lastMessage.type === "stream_start" ||
      lastMessage.type === "stream_cancelled" ||
      lastMessage.type === "stream_end" ||
      lastMessage.type === "response" ||
      (lastMessage.type === "conversation_persisted" &&
        lastMessage.role === "assistant");
    // REST dispatch acceptance can bind agent_run_id after the first WS event
    // arrives.  Do not consume that event's id until it can be correlated; the
    // generation-state update will rerun this effect with the same message.
    if (requiresGenerationCorrelation && !generationIsCorrelated) return;

    if (eventId) {
      if (processedEventIdsRef.current.has(eventId)) return;
      processedEventIdsRef.current.add(eventId);
      if (processedEventIdsRef.current.size > 512) {
        const oldest = processedEventIdsRef.current.values().next().value;
        if (oldest) processedEventIdsRef.current.delete(oldest);
      }
    } else {
      if (processedLegacyMessageRef.current === lastMessage) return;
      processedLegacyMessageRef.current = lastMessage;
    }

    if (lastMessage.type === "llm_mode_change") {
      const data = lastMessage.data as
        | {
            mode?: unknown;
            available_modes?: unknown;
            labels?: unknown;
          }
        | undefined;
      if (typeof data?.mode === "string" && data.mode.length > 0) {
        setLlmModeState(data.mode);
        if (
          Array.isArray(data.available_modes) &&
          data.available_modes.every((item) => typeof item === "string")
        ) {
          setLlmModeOptions(data.available_modes);
        }
        if (data.labels && typeof data.labels === "object") {
          setLlmModeLabels(data.labels as Record<string, string>);
        }
      }
      return;
    }

    if (lastMessage.type === "bgm_change") {
      const data = lastMessage.data as
        | { bgm_id: string; volume: number }
        | undefined;
      const bgm_id = data?.bgm_id;
      const volume = data?.volume;

      if (bgm_id === "stop") {
        stopAudio();
      } else if (bgm_id) {
        // BGMの解決（ブックマークから検索）
        (async () => {
          try {
            const bookmarkData = await explorerBookmarks();
            const bgmBookmark = bookmarkData.success
              ? bookmarkData.bookmarks.find(
                  (b) =>
                    b.name === "BGM" || b.path.toLowerCase().includes("bgm"),
                )
              : undefined;
            const searchRoot = bgmBookmark ? bgmBookmark.path : "";

            const searchRes = await explorerSearch(bgm_id, searchRoot, 1);
            if (searchRes.success && searchRes.results.length > 0) {
              const file = searchRes.results[0];
              play({
                name: file.name,
                path: file.path,
                type: "audio",
              });
              if (volume !== undefined) setVolume(volume);
            } else {
              // 1つも見つからない場合はファイル名完全一致を試みる
              // 検索でヒットしない場合もあるため
              console.warn(`BGM not found in search: ${bgm_id}`);
            }
          } catch (e) {
            console.error("BGM resolution failed:", e);
          }
        })();
      }
      return;
    }

    if (lastMessage.type === "external_llm_permission_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data && eventSessionId === activeSessionId && eventSessionId) {
        // scope_options は「1回だけ許可 / このセッション中は許可」の選択肢。
        // 未指定の古いバックエンドでは once のみとして扱う。
        const scopeOptions = Array.isArray(data.scope_options)
          ? (data.scope_options
              .map((item) => String(item))
              .filter(
                (item): item is ToolPermissionScope =>
                  item === "once" || item === "session",
              ) as ToolPermissionScope[])
          : (["once"] as ToolPermissionScope[]);

        setToolPermissionRequest({
          sessionId: eventSessionId,
          requestId: String(data.request_id || ""),
          toolName: String(data.tool_name || "tool"),
          description: String(data.description || "ツール実行を許可しますか？"),
          toolArgs:
            data.tool_args && typeof data.tool_args === "object"
              ? (data.tool_args as Record<string, unknown>)
              : {},
          scopeOptions: scopeOptions.length > 0 ? scopeOptions : ["once"],
        });
      }
      return;
    }

    if (lastMessage.type === "external_model_prompt_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data && eventSessionId === activeSessionId && eventSessionId) {
        const prompt = String(data.original_prompt || data.prompt || "");
        const redactedPrompt = String(data.redacted_prompt || prompt);
        const redactionFindings = Array.isArray(data.redaction_findings)
          ? data.redaction_findings
              .map((item) =>
                item && typeof item === "object"
                  ? {
                      category: String(
                        (item as Record<string, unknown>).category || "",
                      ),
                      placeholder: String(
                        (item as Record<string, unknown>).placeholder || "",
                      ),
                    }
                  : null,
              )
              .filter(
                (item): item is { category: string; placeholder: string } =>
                  Boolean(item?.category && item.placeholder),
              )
          : [];
        const request = {
          sessionId: eventSessionId,
          requestId: String(data.request_id || ""),
          provider: String(data.provider || ""),
          model: String(data.model || ""),
          description: String(
            data.description ||
              "分担先モデルへ送るプロンプトを確認してください",
          ),
          prompt,
          redactedPrompt,
          redactionFindings,
          notify: data.notify !== false,
          sourceKind: String(data.source_kind || ""),
          riskLevel: String(data.risk_level || ""),
          semanticStatus: String(data.semantic_status || ""),
          warning: String(data.warning || ""),
        };
        setExternalModelPromptRequest(request);
        setExternalModelPromptDraft(redactedPrompt);
        if (request.notify) {
          toast.info("外部モデル送信の確認が必要です");
        }
      }
      return;
    }

    if (lastMessage.type === "ask_user_question_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data && eventSessionId === activeSessionId && eventSessionId) {
        setAskUserQuestionRequest?.({
          sessionId: eventSessionId,
          requestId: String(data.request_id || ""),
          question: String(data.question || "確認してください"),
          inputType: String(data.input_type || "free_text"),
          choices: Array.isArray(data.choices)
            ? data.choices.map((item) => String(item))
            : [],
          allowMultiple: Boolean(data.allow_multiple),
          allowFreeText: data.allow_free_text !== false,
          revision: Number(data.revision || 0),
        });
        setAskUserQuestionDraft?.("");
        setAskUserQuestionChoices?.([]);
      }
      return;
    }

    if (lastMessage.type === "plan_approval_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data && eventSessionId === activeSessionId && eventSessionId) {
        const planText = String(data.plan_text || "");
        setPlanApprovalRequest?.({
          sessionId: eventSessionId,
          requestId: String(data.request_id || ""),
          planText,
          summary: String(data.summary || "実行前に計画を確認してください。"),
          revision: Number(data.revision || 0),
        });
        setPlanApprovalDraft?.(planText);
        setPlanApprovalFeedbackDraft?.("");
      }
      return;
    }

    // Live Voice transcripts are persisted by the provider-owned sideband
    // into the same ConversationSession as Chat.  Only the active durable
    // session may hydrate a message identified by the server message_id;
    // browser telemetry without an id is deliberately ignored to avoid a
    // second/local transcript store or cross-session leakage.
    if (lastMessage.type === "live_voice.event") {
      const eventSessionId = getWebSocketMessageSessionId(lastMessage);
      const messageId = getWebSocketMessageId(lastMessage);
      if (
        activeSessionId &&
        currentSession?.id === activeSessionId &&
        eventSessionId === activeSessionId &&
        messageId
      ) {
        void refreshPersistedMessages(activeSessionId);
      }
      return;
    }

    if (lastMessage.type === "new_message") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (isForeignSessionEvent(data?.session_id)) return;
      if (data && data.type === "assistant") {
        setSteeringInstructions([]);
        const content = (data.message as string) || "";
        if (content && activeSessionId) {
          const agentRunId =
            getWebSocketMessageAgentRunId(lastMessage) ??
            selectGenerationAgentRunId(generationState, activeSessionId);
          const messageId = getWebSocketMessageId(lastMessage);
          if (!agentRunId && !messageId) {
            void refreshPersistedMessages(activeSessionId);
            return;
          }
          const metadata: ConversationMessage["metadata"] = {
            character: data.character,
            ...(typeof data.session_id === "string" && data.session_id
              ? {}
              : { transient_source: "unscoped_ws_new_message" }),
          };
          if (agentRunId) {
            metadata.agent_run_id = agentRunId;
          }
          const liveAssistant = createLocalMessage(
            activeSessionId,
            "assistant",
            content,
            metadata,
          );
          dispatchChatTimeline({
            type: "append",
            message: messageId ? { ...liveAssistant, id: messageId } : liveAssistant,
          });
          void refreshPersistedMessages(activeSessionId);
        }
      }
      // user型のnew_messageは保存済みIDを含まないため、応答側で履歴を再取得してtemp表示を置き換える
      return;
    }

    if (lastMessage.type === "conversation_persisted") {
      const sessionId = (lastMessage.session_id as string) || "";
      if (activeSessionId && sessionId === activeSessionId) {
        if (lastMessage.role === "assistant") {
          setSteeringInstructions([]);
        }
        void (async () => {
          const persistedMessages =
            await refreshPersistedMessages(activeSessionId);
          if (lastMessage.role === "assistant" && persistedMessages) {
            const announcedMessageId = getWebSocketMessageId(lastMessage);
            const announcedRunId = getWebSocketMessageAgentRunId(lastMessage);
            const persistedAssistant = persistedMessages.find(
              (message) =>
                message.role === "assistant" &&
                ((announcedMessageId && message.id === announcedMessageId) ||
                  (announcedRunId &&
                    message.metadata?.agent_run_id === announcedRunId)),
            );
            if (persistedAssistant) {
              bumpSessionForAssistant(sessionId, persistedAssistant.id);
              dispatchGeneration({
                type: "assistant_persisted",
                sessionId,
                agentRunId: announcedRunId,
                clientMessageId: eventClientMessageId,
                assistantMessageId: persistedAssistant.id,
                eventId,
              });
            }
          }
          if (
            lastMessage.role === "assistant" &&
            currentSession?.id === activeSessionId &&
            persistedMessages
          ) {
            void maybeGenerateLoadedSessionTitle(
              currentSession,
              persistedMessages,
            );
          }
        })();
      }
      return;
    }

    if (lastMessage.type === "conversation_title_updated") {
      const sessionId = (lastMessage.session_id as string) || "";
      const title = (lastMessage.title as string) || "";
      if (sessionId && title) {
        updateSidebarTitle(sessionId, title);
      }
      return;
    }

    if (lastMessage.type === "generated_image") {
      const content =
        (typeof lastMessage.content === "string" && lastMessage.content) ||
        (typeof lastMessage.media_id === "string" &&
          `[GENERATED_IMAGE:${lastMessage.media_id}]`) ||
        "";
      if (content && activeSessionId) {
        const messageId = getWebSocketMessageId(lastMessage);
        const imageAgentRunId = getWebSocketMessageAgentRunId(lastMessage);
        if (!messageId && !imageAgentRunId) {
          void refreshPersistedMessages(activeSessionId);
          return;
        }
        dispatchChatTimeline({
          type: "append_to_last_assistant",
          sessionId: activeSessionId,
          content,
          messageId,
          agentRunId: imageAgentRunId,
        });
      }
      return;
    }

    if (
      lastMessage.type === "tool_start" ||
      lastMessage.type === "tool_end" ||
      lastMessage.type === "status_update" ||
      lastMessage.type === "reasoning_progress" ||
      lastMessage.type === "steering_update"
    ) {
      if (!activeSessionId) return;
      if (
        lastMessage.type !== "steering_update" &&
        !generationIsCorrelated
      ) {
        return;
      }
      const eventAgentRunId = getWebSocketMessageAgentRunId(lastMessage);
      if (lastMessage.type === "tool_start") {
        dispatchGeneration({
          type: "tool_started",
          sessionId: activeSessionId,
          agentRunId: eventAgentRunId,
          clientMessageId: eventClientMessageId,
          tool:
            typeof lastMessage.tool === "string" && lastMessage.tool
              ? lastMessage.tool
              : "tool",
          statusMessage:
            typeof lastMessage.message === "string"
              ? lastMessage.message
              : null,
          eventId,
        });
      } else if (lastMessage.type === "tool_end") {
        dispatchGeneration({
          type: "tool_finished",
          sessionId: activeSessionId,
          agentRunId: eventAgentRunId,
          clientMessageId: eventClientMessageId,
          statusMessage:
            typeof lastMessage.message === "string"
              ? lastMessage.message
              : null,
          eventId,
        });
      } else if (lastMessage.type !== "steering_update") {
        const status =
          typeof lastMessage.status === "string"
            ? lastMessage.status
            : lastMessage.type;
        const statusMessage =
          typeof lastMessage.message === "string"
            ? lastMessage.message
            : null;
        const terminalType =
          status === "completed"
            ? "completed"
            : status === "cancelled"
              ? "cancelled"
              : status === "failed"
                ? "failed"
                : status === "cancellation_pending"
                  ? "cancellation_pending"
                  : status === "cancellation_failed"
                    ? "cancellation_failed"
                    : null;
        if (terminalType) {
          dispatchGeneration({
            type: terminalType,
            sessionId: activeSessionId,
            agentRunId: eventAgentRunId,
            clientMessageId: eventClientMessageId,
            statusMessage,
            eventId,
          });
        } else {
          dispatchGeneration({
            type: "status_updated",
            sessionId: activeSessionId,
            agentRunId: eventAgentRunId,
            clientMessageId: eventClientMessageId,
            status,
            statusMessage,
            eventId,
          });
        }
      }
      if (lastMessage.type === "steering_update") {
        const clientMessageId = String(
          lastMessage.client_message_id || "",
        ).trim();
        if (clientMessageId) {
          const status =
            lastMessage.status === "rejected" ? "failed" : "interrupting";
          setSteeringInstructions((current) =>
            current.map((item) =>
              item.id === clientMessageId ? { ...item, status } : item,
            ),
          );
          dispatchChatTimeline({
            type: "update_by_client_message_id",
            sessionId: activeSessionId,
            clientMessageId,
            update: (message) => ({
              ...message,
              metadata: {
                ...message.metadata,
                delivery_mode: "immediate_interrupt",
                delivery_status: status,
              },
            }),
          });
          if (lastMessage.status === "rejected") {
            toast.error(
              String(lastMessage.message || "現在の応答へ割り込めませんでした"),
            );
          } else {
            toast.success("追加指示を送信しました");
          }
        }
      }
      if (
        lastMessage.type === "tool_end" &&
        isChatToolResultMetadata(lastMessage.tool_result)
      ) {
        const nextResults = [
          ...liveToolResultsRef.current,
          lastMessage.tool_result,
        ];
        liveToolResultsRef.current = nextResults;
        setLiveToolResults(nextResults);
      }
    }

    if (lastMessage.type === "stream_start") {
      if (!generationIsCorrelated) return;
      responsePollGenerationRef.current += 1;
      if (activeSessionId) {
        dispatchGeneration({
          type: "stream_started",
          sessionId: activeSessionId,
          agentRunId: getWebSocketMessageAgentRunId(lastMessage),
          clientMessageId: eventClientMessageId,
          statusMessage:
            typeof lastMessage.message === "string"
              ? lastMessage.message
              : "応答を生成しています",
          eventId,
        });
      }
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      // ストリーミングバッファを定期的に反映
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
      }
      streamingIntervalRef.current = setInterval(() => {
        setStreamingContent(streamBuffer.current);
      }, 50);
    }

    if (lastMessage.type === "stream_cancelled") {
      if (!generationIsCorrelated) return;
      responsePollGenerationRef.current += 1;
      if (lastMessage.status === "cancellation_pending") {
        if (activeSessionId) {
          dispatchGeneration({
            type: "cancellation_pending",
            sessionId: activeSessionId,
            agentRunId: getWebSocketMessageAgentRunId(lastMessage),
            clientMessageId: eventClientMessageId,
            statusMessage:
              typeof lastMessage.message === "string"
                ? lastMessage.message
                : "停止処理を継続しています",
            eventId,
          });
        }
        toast.info("停止処理を継続しています");
        return;
      }
      if (lastMessage.status === "cancellation_failed") {
        const failedRunId =
          getWebSocketMessageAgentRunId(lastMessage) ??
          selectGenerationAgentRunId(generationState, activeSessionId);
        const partialContent = streamBuffer.current;
        const partialToolResults = liveToolResultsRef.current;
        if (activeSessionId && partialContent.trim()) {
          dispatchChatTimeline({
            type: "append",
            message: {
              id: `temp-assistant-cancellation-failed-${failedRunId ?? activeSessionId}`,
              session_id: activeSessionId,
              role: "assistant",
              content: partialContent,
              metadata: {
                agent_run_id: failedRunId ?? undefined,
                generation_status: "cancellation_failed",
                partial: true,
                persistence_failed: true,
                tool_results: partialToolResults,
              },
              created_at: new Date().toISOString(),
              parent_message_id: null,
              branch_index: 0,
              is_active_branch: true,
            },
          });
        }
        if (activeSessionId) {
          dispatchGeneration({
            type: "cancellation_failed",
            sessionId: activeSessionId,
            agentRunId: failedRunId,
            clientMessageId: eventClientMessageId,
            statusMessage:
              typeof lastMessage.message === "string"
                ? lastMessage.message
                : "応答生成を完全に停止できませんでした",
            eventId,
          });
        }
        if (streamingIntervalRef.current) {
          clearInterval(streamingIntervalRef.current);
          streamingIntervalRef.current = null;
        }
        setStreamingContent("");
        liveToolResultsRef.current = [];
        setLiveToolResults([]);
        setSteeringInstructions([]);
        toast.error(
          typeof lastMessage.message === "string"
            ? lastMessage.message
            : "応答生成を停止できませんでした",
        );
        if (activeSessionId) {
          void refreshPersistedMessages(activeSessionId);
        }
        return;
      }
      if (activeSessionId) {
        dispatchGeneration({
          type: "cancelled",
          sessionId: activeSessionId,
          agentRunId:
            getWebSocketMessageAgentRunId(lastMessage) ??
            generationState.lifecycle.agentRunId,
          clientMessageId: eventClientMessageId,
          statusMessage:
            typeof lastMessage.message === "string"
              ? lastMessage.message
              : "応答生成を停止しました",
          eventId,
        });
      }
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
        streamingIntervalRef.current = null;
      }
      const cancelledAssistants =
        getCancelledAssistantPayloads(lastMessage);
      const failedRunIds = Array.isArray(
        lastMessage.persistence_failed_run_ids,
      )
        ? lastMessage.persistence_failed_run_ids.filter(
            (item): item is string => typeof item === "string",
          )
        : [];
      const liveRunId =
        getWebSocketMessageAgentRunId(lastMessage) ??
        selectGenerationAgentRunId(generationState, activeSessionId);
      const failedBufferRunId =
        failedRunIds.length === 1
          ? failedRunIds[0]
          : failedRunIds.length > 1
            ? null
            : liveRunId;
      const failedBufferKey =
        failedRunIds.length > 0
          ? [...failedRunIds].sort().join("-")
          : (failedBufferRunId ?? activeSessionId ?? "unknown");
      if (activeSessionId) {
        for (const cancelledAssistant of cancelledAssistants) {
          dispatchChatTimeline({
            type: "append",
            message: {
              id: cancelledAssistant.messageId,
              session_id: activeSessionId,
              role: "assistant",
              content: cancelledAssistant.content,
              metadata: cancelledAssistant.metadata,
              created_at: new Date().toISOString(),
              parent_message_id: null,
              branch_index: 0,
              is_active_branch: true,
            },
          });
        }
        if (
          lastMessage.persistence_failed === true &&
          streamBuffer.current.trim()
        ) {
          dispatchChatTimeline({
            type: "append",
            message: {
              id: `temp-assistant-cancelled-${failedBufferKey}`,
              session_id: activeSessionId,
              role: "assistant",
              content: streamBuffer.current,
              metadata: {
                agent_run_id: failedBufferRunId ?? undefined,
                generation_status: "cancelled",
                partial: true,
                persistence_failed: true,
                tool_results: liveToolResultsRef.current,
              },
              created_at: new Date().toISOString(),
              parent_message_id: null,
              branch_index: 0,
              is_active_branch: true,
            },
          });
        }
      }
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      setSteeringInstructions([]);
      if (lastMessage.persistence_failed === true) {
        toast.error("停止しましたが、一部の途中応答を保存できませんでした");
      } else {
        toast.info(
          typeof lastMessage.message === "string"
            ? lastMessage.message
            : "応答生成を停止しました",
        );
      }
      if (activeSessionId) {
        void refreshPersistedMessages(activeSessionId);
      }
      return;
    }

    if (lastMessage.type === "stream_end" || lastMessage.type === "response") {
      if (!generationIsCorrelated) return;
      const terminalMessageId = getWebSocketMessageId(lastMessage);
      if (activeSessionId) {
        dispatchGeneration({
          type: "completed",
          sessionId: activeSessionId,
          agentRunId: eventAgentRunId,
          clientMessageId: eventClientMessageId,
          assistantMessageId: terminalMessageId,
          eventId,
          awaitingPersistence: !terminalMessageId,
        });
      }
      setSteeringInstructions([]);
      // インターバル停止
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
        streamingIntervalRef.current = null;
      }

      const finalContent =
        (lastMessage.content as string) || streamBuffer.current;
      if (finalContent && activeSessionId) {
        const toolResults = liveToolResultsRef.current;
        const streamEndAgentRunId =
          getWebSocketMessageAgentRunId(lastMessage) ??
          selectGenerationAgentRunId(generationState, activeSessionId);
        const streamEndMessageId = terminalMessageId;
        if (
          !streamEndAgentRunId &&
          !streamEndMessageId &&
          !eventClientMessageId
        ) {
          void refreshPersistedMessages(activeSessionId);
          if (streamingIntervalRef.current) {
            clearInterval(streamingIntervalRef.current);
            streamingIntervalRef.current = null;
          }
          setStreamingContent("");
          liveToolResultsRef.current = [];
          setLiveToolResults([]);
          return;
        }
        const assistantMetadata: ConversationMessage["metadata"] = {};
        if (toolResults.length > 0) {
          assistantMetadata.tool_results = toolResults;
        }
        if (streamEndAgentRunId) {
          assistantMetadata.agent_run_id = streamEndAgentRunId;
        }
        const liveAssistant = createLocalMessage(
          activeSessionId,
          "assistant",
          finalContent,
          assistantMetadata,
        );
        dispatchChatTimeline({
          type: "append",
          message: streamEndMessageId
            ? { ...liveAssistant, id: streamEndMessageId }
            : liveAssistant,
        });
      }

      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      if (activeSessionId) {
        void refreshPersistedMessages(activeSessionId);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    lastMessage,
    activeSessionId,
    connectionGeneration,
    refreshPersistedMessages,
    streamBuffer,
    play,
    stopAudio,
    setVolume,
    updateSidebarTitle,
    currentSession,
    maybeGenerateLoadedSessionTitle,
    bumpSessionForAssistant,
    processedEventIdsRef,
    processedLegacyMessageRef,
    generationState,
    dispatchGeneration,
  ]);
}
