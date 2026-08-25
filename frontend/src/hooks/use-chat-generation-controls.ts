"use client";

import {
  useCallback,
  useRef,
  type Dispatch,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  type SetStateAction,
} from "react";
import { toast } from "sonner";
import { chatApi } from "@/lib/chat-api";
import type {
  ChatToolResultMetadata,
  ConversationMessage,
} from "@/lib/chat-api";
import type {
  ChatGenerationEvent,
  ChatGenerationState,
} from "@/lib/chat-generation-state";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import { createLocalUserMessage } from "@/lib/chat-local-messages";
import type {
  ExternalModelPromptRequest,
  ToolPermissionRequest,
  ToolPermissionScope,
} from "@/components/chat/chat-permission-dialogs";
import { chatTimelineReducer } from "@/lib/chat-state";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type UseChatGenerationControlsArgs = {
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  toolPermissionRequest: ToolPermissionRequest | null;
  externalModelPromptRequest: ExternalModelPromptRequest | null;
  externalModelPromptDraft: string;
  responsePollGenerationRef: RefObject<number>;
  streamBuffer: RefObject<string>;
  liveToolResultsRef: RefObject<ChatToolResultMetadata[]>;
  streamingIntervalRef: RefObject<ReturnType<typeof setInterval> | null>;
  sendPermissionResponse: (
    requestId: string,
    approved: boolean,
    scope?: ToolPermissionScope,
    sessionId?: string | null,
  ) => void;
  sendExternalModelPromptResponse: (
    requestId: string,
    approved: boolean,
    editedPrompt: string,
    sessionId?: string | null,
  ) => void;
  stopGeneration: (sessionId: string) => boolean;
  sendSteering: (
    content: string,
    sessionId: string,
    clientMessageId?: string,
  ) => boolean;
  generationAgentRunId: string | null;
  generationStateRef: RefObject<ChatGenerationState>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  refreshPersistedMessages: (
    sessionId: string,
  ) => Promise<ConversationMessage[] | null>;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setStreamingContent: Dispatch<SetStateAction<string>>;
  setLiveToolResults: Dispatch<SetStateAction<ChatToolResultMetadata[]>>;
  setToolPermissionRequest: Dispatch<
    SetStateAction<ToolPermissionRequest | null>
  >;
  setExternalModelPromptRequest: Dispatch<
    SetStateAction<ExternalModelPromptRequest | null>
  >;
  setExternalModelPromptDraft: Dispatch<SetStateAction<string>>;
};

/**
 * ツール実行許可・外部モデル送信確認・応答停止・追加指示（ステアリング）を担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの。依存配列は元コードと同一に保つ。
 */
export function useChatGenerationControls({
  activeSessionId,
  activeSessionIdRef,
  toolPermissionRequest,
  externalModelPromptRequest,
  externalModelPromptDraft,
  responsePollGenerationRef,
  streamBuffer,
  liveToolResultsRef,
  streamingIntervalRef,
  sendPermissionResponse,
  sendExternalModelPromptResponse,
  stopGeneration,
  sendSteering,
  generationAgentRunId,
  generationStateRef,
  dispatchGeneration,
  refreshPersistedMessages,
  dispatchChatTimeline,
  setSteeringInstructions,
  setStreamingContent,
  setLiveToolResults,
  setToolPermissionRequest,
  setExternalModelPromptRequest,
  setExternalModelPromptDraft,
}: UseChatGenerationControlsArgs) {
  const stopOperationsRef = useRef<Set<string>>(new Set());

  const handleToolPermissionDecision = useCallback(
    (approved: boolean, scope: ToolPermissionScope = "once") => {
      if (!toolPermissionRequest) return;
      if (toolPermissionRequest.sessionId !== activeSessionIdRef.current) {
        setToolPermissionRequest(null);
        return;
      }
      sendPermissionResponse(
        toolPermissionRequest.requestId,
        approved,
        approved ? scope : "once",
        toolPermissionRequest.sessionId,
      );
      setToolPermissionRequest(null);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sendPermissionResponse, toolPermissionRequest],
  );

  const handleExternalModelPromptDecision = useCallback(
    (approved: boolean) => {
      if (!externalModelPromptRequest) return;
      if (externalModelPromptRequest.sessionId !== activeSessionIdRef.current) {
        setExternalModelPromptRequest(null);
        setExternalModelPromptDraft("");
        return;
      }
      sendExternalModelPromptResponse(
        externalModelPromptRequest.requestId,
        approved,
        approved ? externalModelPromptDraft : "",
        externalModelPromptRequest.sessionId,
      );
      setExternalModelPromptRequest(null);
      setExternalModelPromptDraft("");
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      externalModelPromptDraft,
      externalModelPromptRequest,
      sendExternalModelPromptResponse,
    ],
  );

  const handleExternalModelPromptKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      if (
        event.key !== "Enter" ||
        event.shiftKey ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        event.nativeEvent.isComposing
      ) {
        return;
      }
      if (!externalModelPromptDraft.trim()) return;
      event.preventDefault();
      handleExternalModelPromptDecision(true);
    },
    [handleExternalModelPromptDecision, externalModelPromptDraft],
  );

  const handleStopGeneration = useCallback(async () => {
    if (!activeSessionId) return;
    const operationSessionId = activeSessionId;
    if (stopOperationsRef.current.has(operationSessionId)) return;
    stopOperationsRef.current.add(operationSessionId);
    const stoppedLifecycle = generationStateRef.current.lifecycle;
    const stoppedAgentRunId = stoppedLifecycle.agentRunId ?? generationAgentRunId;
    const stoppedGenerationEpoch = stoppedLifecycle.generationEpoch;
    const stoppedClientMessageId = stoppedLifecycle.clientMessageId;
    dispatchGeneration({
      type: "stop_requested",
      sessionId: operationSessionId,
      agentRunId: stoppedAgentRunId,
      generationEpoch: stoppedGenerationEpoch,
      clientMessageId: stoppedClientMessageId,
    });
    try {
      const isStillStoppingTarget = () => {
        const current = generationStateRef.current.lifecycle;
        return (
          current.sessionId === operationSessionId &&
          current.generationEpoch === stoppedGenerationEpoch &&
          current.clientMessageId === stoppedClientMessageId &&
          (!stoppedAgentRunId || current.agentRunId === stoppedAgentRunId)
        );
      };
      const isTargetTerminal = () => {
        const terminal = generationStateRef.current.lastTerminal;
        return Boolean(
          terminal &&
            terminal.sessionId === operationSessionId &&
            terminal.generationEpoch === stoppedGenerationEpoch &&
            (!stoppedAgentRunId || terminal.agentRunId === stoppedAgentRunId),
        );
      };
      responsePollGenerationRef.current += 1;
      const finishStoppedState = (
        confirmedAgentRunId?: string | null,
        phase: "cancelled" | "completed" = "cancelled",
      ) => {
        if (activeSessionIdRef.current !== operationSessionId) return;
        dispatchGeneration({
          type: phase,
          sessionId: operationSessionId,
          agentRunId: confirmedAgentRunId ?? stoppedAgentRunId,
          generationEpoch: stoppedGenerationEpoch,
          clientMessageId: stoppedClientMessageId,
          eventId: `stop:${operationSessionId}:${
            confirmedAgentRunId ?? stoppedAgentRunId ?? "unknown"
          }`,
        });
        setSteeringInstructions([]);
        if (streamingIntervalRef.current) {
          clearInterval(streamingIntervalRef.current);
          streamingIntervalRef.current = null;
        }
        setStreamingContent("");
        liveToolResultsRef.current = [];
        setLiveToolResults([]);
      };
      const sent = stopGeneration(operationSessionId);
      try {
        let result: Awaited<ReturnType<typeof chatApi.stopGeneration>> | null =
          null;
        if (sent) {
          await new Promise((resolve) => setTimeout(resolve, 1_000));
          if (
            activeSessionIdRef.current !== operationSessionId ||
            !isStillStoppingTarget() ||
            isTargetTerminal()
          ) {
            return;
          }
          try {
            const status =
              await chatApi.getGenerationStatus(operationSessionId);
            if (
              activeSessionIdRef.current !== operationSessionId ||
              !isStillStoppingTarget() ||
              isTargetTerminal() ||
              (status.session_id != null &&
                status.session_id !== operationSessionId)
            ) {
              return;
            }
            if (!status.running) {
              await refreshPersistedMessages(operationSessionId);
              if (activeSessionIdRef.current !== operationSessionId) return;
              if (status.status === "completed" || status.status === "cancelled") {
                finishStoppedState(
                  status.agent_run_id,
                  status.status === "completed" ? "completed" : "cancelled",
                );
              } else {
                dispatchGeneration({
                  type: "status_restored",
                  sessionId: operationSessionId,
                  generationEpoch: stoppedGenerationEpoch,
                  clientMessageId: stoppedClientMessageId,
                  status,
                });
                toast.error("応答生成の正常終了を確認できませんでした");
              }
              return;
            }
            if (
              stoppedAgentRunId &&
              status.agent_run_id &&
              status.agent_run_id !== stoppedAgentRunId
            ) {
              // 停止完了後にキュー先頭が次の生成を始めている。
              // 新しい生成をRESTフォールバックで誤停止しない。
              return;
            }
          } catch (statusError) {
            console.warn("WebSocket停止後の状態確認に失敗:", statusError);
          }
        }
        if (
          activeSessionIdRef.current !== operationSessionId ||
          !isStillStoppingTarget() ||
          isTargetTerminal()
        ) {
          return;
        }
        result = await chatApi.stopGeneration(operationSessionId);
        if (activeSessionIdRef.current !== operationSessionId) return;
        if (result) {
          const savedMessages =
            result.messages ?? (result.message ? [result.message] : []);
          for (const message of savedMessages) {
            dispatchChatTimeline({ type: "append", message });
          }
          const refreshed =
            await refreshPersistedMessages(operationSessionId);
          if (activeSessionIdRef.current !== operationSessionId) return;
          if (result.status === "cancellation_pending") {
            dispatchGeneration({
              type: "cancellation_pending",
              sessionId: operationSessionId,
              agentRunId: result.agent_run_id ?? null,
              generationEpoch: stoppedGenerationEpoch,
              clientMessageId: stoppedClientMessageId,
              statusMessage: "停止処理を継続しています",
            });
            toast.info("停止処理を継続しています");
            return;
          }
          if (
            savedMessages.length === 0 &&
            refreshed === null &&
            !result.persistence_failed
          ) {
            toast.error("停止済み応答を再取得できませんでした");
          }
          const failedRunIds = result.persistence_failed_run_ids ?? [];
          const failedBufferRunId =
            failedRunIds.length === 1
              ? failedRunIds[0]
              : failedRunIds.length > 1
                ? null
                : result.agent_run_id;
          const failedBufferKey =
            failedRunIds.length > 0
              ? [...failedRunIds].sort().join("-")
              : (failedBufferRunId ?? operationSessionId);
          const shouldPreserveLiveBuffer =
            result.persistence_failed && streamBuffer.current.trim();
          if (shouldPreserveLiveBuffer) {
            dispatchChatTimeline({
              type: "append",
              message: {
                id: `temp-assistant-cancelled-${failedBufferKey}`,
                session_id: operationSessionId,
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
          finishStoppedState(result.agent_run_id);
          if (result.persistence_failed) {
            toast.error("停止しましたが、一部の途中応答を保存できませんでした");
          }
        }
      } catch (err) {
        if (activeSessionIdRef.current !== operationSessionId) return;
        console.error("応答停止に失敗:", err);
        try {
          const status =
            await chatApi.getGenerationStatus(operationSessionId);
          const refreshed =
            await refreshPersistedMessages(operationSessionId);
          if (activeSessionIdRef.current !== operationSessionId) return;
          if (!status.running) {
            if (status.status === "completed" || status.status === "cancelled") {
              finishStoppedState(
                status.agent_run_id,
                status.status === "completed" ? "completed" : "cancelled",
              );
            } else {
              dispatchGeneration({
                type: "status_restored",
                sessionId: operationSessionId,
                generationEpoch: stoppedGenerationEpoch,
                clientMessageId: stoppedClientMessageId,
                status,
              });
              toast.error("応答生成の正常終了を確認できませんでした");
            }
            if (refreshed === null) {
              toast.error(
                "停止状態は確認できましたが、履歴を再取得できませんでした",
              );
            }
            return;
          }
        } catch (reconcileError) {
          console.warn("停止失敗後の状態確認にも失敗:", reconcileError);
        }
        toast.error("応答停止に失敗しました");
      }
    } finally {
      stopOperationsRef.current.delete(operationSessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeSessionId,
    activeSessionIdRef,
    dispatchChatTimeline,
    dispatchGeneration,
    generationAgentRunId,
    generationStateRef,
    refreshPersistedMessages,
    liveToolResultsRef,
    stopGeneration,
    streamBuffer,
  ]);

  const handleSteerGeneration = useCallback(
    async (content: string) => {
      if (!activeSessionId) return;
      const instructionId = createClientMessageId();
      dispatchChatTimeline({
        type: "append",
        message: createLocalUserMessage(
          activeSessionId,
          content,
          instructionId,
          undefined,
          undefined,
          {
            delivery_mode: "immediate_interrupt",
            delivery_status: "sending",
          },
        ),
      });
      setSteeringInstructions((prev) => [
        ...prev,
        {
          id: instructionId,
          content,
          createdAt: new Date().toISOString(),
          status: "sending",
        },
      ]);

      const updateStatus = (status: SubmittedSteeringInstruction["status"]) => {
        setSteeringInstructions((prev) =>
          prev.map((item) =>
            item.id === instructionId ? { ...item, status } : item,
          ),
        );
        dispatchChatTimeline({
          type: "update_by_client_message_id",
          sessionId: activeSessionId,
          clientMessageId: instructionId,
          update: (message) => ({
            ...message,
            metadata: {
              ...message.metadata,
              delivery_mode: "immediate_interrupt",
              delivery_status: status,
            },
          }),
        });
      };

      const sent = sendSteering(content, activeSessionId, instructionId);
      if (!sent) {
        try {
          const result = await chatApi.steerGeneration(
            activeSessionId,
            content,
            instructionId,
            generationAgentRunId,
          );
          if (!result.interrupted) {
            updateStatus("failed");
            toast.error(
              result.status === "persistence_failed"
                ? "割り込みメッセージを会話履歴へ保存できませんでした"
                : "現在の応答へ割り込めませんでした",
            );
            return;
          }
          updateStatus("interrupting");
          if (result.persistence_failed) {
            toast.error("割り込み指示を会話履歴へ保存できませんでした");
          }
        } catch (err) {
          updateStatus("failed");
          console.error("追加指示の送信に失敗:", err);
          toast.error("追加指示の送信に失敗しました");
          return;
        }
      } else {
        // WebSocket delivery is only transport acceptance.  Keep the bubble
        // in "sending" until steering_update confirms or rejects the actual
        // interrupt.
        return;
      }
      toast.success("追加指示を送信しました");
    },
    [
      activeSessionId,
      dispatchChatTimeline,
      generationAgentRunId,
      sendSteering,
      setSteeringInstructions,
    ],
  );

  return {
    handleToolPermissionDecision,
    handleExternalModelPromptDecision,
    handleExternalModelPromptKeyDown,
    handleStopGeneration,
    handleSteerGeneration,
  };
}
