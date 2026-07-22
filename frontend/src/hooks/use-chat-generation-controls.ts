"use client";

import {
  useCallback,
  type Dispatch,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  type SetStateAction,
} from "react";
import { toast } from "sonner";
import { chatApi } from "@/lib/chat-api";
import type { ConversationGenerationStatus } from "@/lib/chat-api";
import type { SubmittedSteeringInstruction } from "@/components/chat/chat-composer";
import type {
  ExternalModelPromptRequest,
  ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

function createClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type UseChatGenerationControlsArgs = {
  activeSessionId: string | null;
  toolPermissionRequest: ToolPermissionRequest | null;
  externalModelPromptRequest: ExternalModelPromptRequest | null;
  externalModelPromptDraft: string;
  responsePollGenerationRef: RefObject<number>;
  streamingIntervalRef: RefObject<ReturnType<typeof setInterval> | null>;
  sendPermissionResponse: (requestId: string, approved: boolean) => void;
  sendExternalModelPromptResponse: (
    requestId: string,
    approved: boolean,
    editedPrompt: string,
  ) => void;
  stopGeneration: (sessionId: string) => boolean;
  sendSteering: (content: string, sessionId: string) => boolean;
  clearWaitingResponse: (sessionId: string | null) => void;
  setRestoredGenerationStatus: Dispatch<
    SetStateAction<ConversationGenerationStatus | null>
  >;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setStreamingContent: Dispatch<SetStateAction<string>>;
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
  toolPermissionRequest,
  externalModelPromptRequest,
  externalModelPromptDraft,
  responsePollGenerationRef,
  streamingIntervalRef,
  sendPermissionResponse,
  sendExternalModelPromptResponse,
  stopGeneration,
  sendSteering,
  clearWaitingResponse,
  setRestoredGenerationStatus,
  setSteeringInstructions,
  setStreamingContent,
  setToolPermissionRequest,
  setExternalModelPromptRequest,
  setExternalModelPromptDraft,
}: UseChatGenerationControlsArgs) {
  const handleToolPermissionDecision = useCallback(
    (approved: boolean) => {
      if (!toolPermissionRequest) return;
      sendPermissionResponse(toolPermissionRequest.requestId, approved);
      setToolPermissionRequest(null);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sendPermissionResponse, toolPermissionRequest],
  );

  const handleExternalModelPromptDecision = useCallback(
    (approved: boolean) => {
      if (!externalModelPromptRequest) return;
      sendExternalModelPromptResponse(
        externalModelPromptRequest.requestId,
        approved,
        approved ? externalModelPromptDraft : "",
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
    responsePollGenerationRef.current += 1;
    clearWaitingResponse(activeSessionId);
    setRestoredGenerationStatus(null);
    setSteeringInstructions([]);
    if (streamingIntervalRef.current) {
      clearInterval(streamingIntervalRef.current);
      streamingIntervalRef.current = null;
    }
    setStreamingContent("");
    const sent = stopGeneration(activeSessionId);
    if (!sent) {
      try {
        await chatApi.stopGeneration(activeSessionId);
      } catch (err) {
        console.error("応答停止に失敗:", err);
        toast.error("応答停止に失敗しました");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, clearWaitingResponse, stopGeneration]);

  const handleSteerGeneration = useCallback(
    async (content: string) => {
      if (!activeSessionId) return;
      const instructionId = createClientMessageId();
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
      };

      const sent = sendSteering(content, activeSessionId);
      if (!sent) {
        try {
          await chatApi.steerGeneration(activeSessionId, content);
        } catch (err) {
          updateStatus("failed");
          console.error("追加指示の送信に失敗:", err);
          toast.error("追加指示の送信に失敗しました");
          return;
        }
      }
      updateStatus("queued");
      toast.success("追加指示を送信しました");
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeSessionId, sendSteering],
  );

  return {
    handleToolPermissionDecision,
    handleExternalModelPromptDecision,
    handleExternalModelPromptKeyDown,
    handleStopGeneration,
    handleSteerGeneration,
  };
}
