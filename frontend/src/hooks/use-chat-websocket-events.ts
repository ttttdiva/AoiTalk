"use client";

import { useEffect, type Dispatch, type RefObject } from "react";
import { toast } from "sonner";
import type {
  ChatToolResultMetadata,
  ConversationGenerationStatus,
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
import { getWebSocketMessageAgentRunId } from "@/lib/chat-websocket-events";
import { explorerBookmarks, explorerSearch } from "@/lib/explorer-api";
import type {
  ExternalModelPromptRequest,
  ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

type WSMessage = NonNullable<ReturnType<typeof useWebSocket>["lastMessage"]>;
type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

type UseChatWebSocketEventsArgs = {
  lastMessage: WSMessage | null;
  activeSessionId: string | null;
  streamBuffer: RefObject<string>;
  pendingAgentRunId: string | null;
  currentSession: ConversationSession | null;

  processedMsgRef: RefObject<string | null>;
  liveToolResultsRef: RefObject<ChatToolResultMetadata[]>;
  streamingIntervalRef: RefObject<ReturnType<typeof setInterval> | null>;
  responsePollGenerationRef: RefObject<number>;

  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  clearWaitingResponse: (sessionId: string | null) => void;
  refreshPersistedMessages: (
    sessionId: string,
  ) => Promise<ConversationMessage[] | null>;
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
  setRestoredGenerationStatus: (
    status: ConversationGenerationStatus | null,
  ) => void;
  setSteeringInstructions: (instructions: SubmittedSteeringInstruction[]) => void;
  setStreamingContent: (content: string) => void;
  setLiveToolResults: (results: ChatToolResultMetadata[]) => void;
  setCurrentSession: Dispatch<
    (prev: ConversationSession | null) => ConversationSession | null
  >;

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
  streamBuffer,
  pendingAgentRunId,
  currentSession,
  processedMsgRef,
  liveToolResultsRef,
  streamingIntervalRef,
  responsePollGenerationRef,
  dispatchChatTimeline,
  clearWaitingResponse,
  refreshPersistedMessages,
  maybeGenerateLoadedSessionTitle,
  updateSidebarTitle,
  setLlmModeState,
  setLlmModeOptions,
  setLlmModeLabels,
  setToolPermissionRequest,
  setExternalModelPromptRequest,
  setExternalModelPromptDraft,
  setRestoredGenerationStatus,
  setSteeringInstructions,
  setStreamingContent,
  setLiveToolResults,
  setCurrentSession,
  play,
  stopAudio,
  setVolume,
}: UseChatWebSocketEventsArgs) {
  useEffect(() => {
    if (!lastMessage) return;

    // 同じメッセージの再処理を防止（依存配列の他の値が変わった場合にも対応）
    const msgKey = JSON.stringify(lastMessage);
    if (processedMsgRef.current === msgKey) return;
    processedMsgRef.current = msgKey;

    const isForeignSessionEvent = (sessionId: unknown) =>
      typeof sessionId === "string" &&
      sessionId.length > 0 &&
      Boolean(activeSessionId) &&
      sessionId !== activeSessionId;

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
      if (data) {
        setToolPermissionRequest({
          requestId: String(data.request_id || ""),
          toolName: String(data.tool_name || "tool"),
          description: String(data.description || "ツール実行を許可しますか？"),
          toolArgs:
            data.tool_args && typeof data.tool_args === "object"
              ? (data.tool_args as Record<string, unknown>)
              : {},
        });
      }
      return;
    }

    if (lastMessage.type === "external_model_prompt_request") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (data) {
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
        };
        setExternalModelPromptRequest(request);
        setExternalModelPromptDraft(redactedPrompt);
        if (request.notify) {
          toast.info("外部モデル送信の確認が必要です");
        }
      }
      return;
    }

    if (lastMessage.type === "new_message") {
      const data = lastMessage.data as Record<string, unknown> | undefined;
      if (isForeignSessionEvent(data?.session_id)) return;
      if (data && data.type === "assistant") {
        clearWaitingResponse(activeSessionId);
        setRestoredGenerationStatus(null);
        setSteeringInstructions([]);
        const content = (data.message as string) || "";
        if (content && activeSessionId) {
          const agentRunId =
            getWebSocketMessageAgentRunId(lastMessage) ?? pendingAgentRunId;
          const metadata: ConversationMessage["metadata"] = {
            character: data.character,
            ...(typeof data.session_id === "string" && data.session_id
              ? {}
              : { transient_source: "unscoped_ws_new_message" }),
          };
          if (agentRunId) {
            metadata.agent_run_id = agentRunId;
          }
          dispatchChatTimeline({
            type: "append",
            message: createLocalMessage(
              activeSessionId,
              "assistant",
              content,
              metadata,
            ),
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
          responsePollGenerationRef.current += 1;
          clearWaitingResponse(sessionId);
          setRestoredGenerationStatus(null);
          setSteeringInstructions([]);
        }
        void (async () => {
          const persistedMessages =
            await refreshPersistedMessages(activeSessionId);
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
        setCurrentSession((prev) =>
          prev && prev.id === sessionId ? { ...prev, title } : prev,
        );
      }
      return;
    }

    if (lastMessage.type === "generated_image") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      const content = (lastMessage.content as string) || "";
      if (content && activeSessionId) {
        dispatchChatTimeline({
          type: "append_to_last_assistant",
          sessionId: activeSessionId,
          content,
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
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      setRestoredGenerationStatus(null);
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
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      setRestoredGenerationStatus(null);
      clearWaitingResponse(activeSessionId);
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      // ストリーミングバッファを定期的に反映
      streamingIntervalRef.current = setInterval(() => {
        setStreamingContent(streamBuffer.current);
      }, 50);
    }

    if (lastMessage.type === "stream_cancelled") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      responsePollGenerationRef.current += 1;
      clearWaitingResponse(activeSessionId);
      setRestoredGenerationStatus(null);
      if (streamingIntervalRef.current) {
        clearInterval(streamingIntervalRef.current);
        streamingIntervalRef.current = null;
      }
      setStreamingContent("");
      liveToolResultsRef.current = [];
      setLiveToolResults([]);
      setSteeringInstructions([]);
      toast.info(
        typeof lastMessage.message === "string"
          ? lastMessage.message
          : "応答生成を停止しました",
      );
      if (activeSessionId) {
        void refreshPersistedMessages(activeSessionId);
      }
      return;
    }

    if (lastMessage.type === "stream_end" || lastMessage.type === "response") {
      if (isForeignSessionEvent(lastMessage.session_id)) return;
      responsePollGenerationRef.current += 1;
      clearWaitingResponse(activeSessionId);
      setRestoredGenerationStatus(null);
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
          getWebSocketMessageAgentRunId(lastMessage) ?? pendingAgentRunId;
        const assistantMetadata: ConversationMessage["metadata"] = {};
        if (toolResults.length > 0) {
          assistantMetadata.tool_results = toolResults;
        }
        if (streamEndAgentRunId) {
          assistantMetadata.agent_run_id = streamEndAgentRunId;
        }
        dispatchChatTimeline({
          type: "append",
          message: createLocalMessage(
            activeSessionId,
            "assistant",
            finalContent,
            assistantMetadata,
          ),
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
    refreshPersistedMessages,
    streamBuffer,
    play,
    stopAudio,
    setVolume,
    updateSidebarTitle,
    currentSession,
    maybeGenerateLoadedSessionTitle,
    clearWaitingResponse,
    pendingAgentRunId,
  ]);
}
