"use client";

import {
  useEffect,
  useRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { ChatToolResultMetadata } from "@/lib/chat-api";
import type {
  ChatComposerSendResult,
  SubmittedSteeringInstruction,
} from "@/components/chat/chat-composer";
import type {
  ExternalModelPromptRequest,
  ToolPermissionRequest,
} from "@/components/chat/chat-permission-dialogs";

type PendingSessionMessage = {
  sessionId: string | null;
  settle?: (result: ChatComposerSendResult) => void;
};

type UseChatSessionTransientCleanupArgs = {
  activeSessionId: string | null;
  responsePollGenerationRef: RefObject<number>;
  clearStreamingInterval: () => void;
  streamBuffer: RefObject<string>;
  liveToolResultsRef: RefObject<ChatToolResultMetadata[]>;
  processedEventIdsRef: RefObject<Set<string>>;
  processedLegacyMessageRef: RefObject<unknown>;
  pendingMessageRef: RefObject<PendingSessionMessage | null>;
  setStreamingContent: Dispatch<SetStateAction<string>>;
  setLiveToolResults: Dispatch<SetStateAction<ChatToolResultMetadata[]>>;
  setSteeringInstructions: Dispatch<
    SetStateAction<SubmittedSteeringInstruction[]>
  >;
  setToolPermissionRequest: Dispatch<
    SetStateAction<ToolPermissionRequest | null>
  >;
  setExternalModelPromptRequest: Dispatch<
    SetStateAction<ExternalModelPromptRequest | null>
  >;
  setExternalModelPromptDraft: Dispatch<SetStateAction<string>>;
};

/** Clears every active-session-only display value at the session boundary. */
export function useChatSessionTransientCleanup({
  activeSessionId,
  responsePollGenerationRef,
  clearStreamingInterval,
  streamBuffer: streamBufferRef,
  liveToolResultsRef,
  processedEventIdsRef,
  processedLegacyMessageRef,
  pendingMessageRef,
  setStreamingContent,
  setLiveToolResults,
  setSteeringInstructions,
  setToolPermissionRequest,
  setExternalModelPromptRequest,
  setExternalModelPromptDraft,
}: UseChatSessionTransientCleanupArgs) {
  const transientStateSessionRef = useRef(activeSessionId);

  useEffect(() => {
    const previousSessionId = transientStateSessionRef.current;
    if (previousSessionId === activeSessionId) return;
    transientStateSessionRef.current = activeSessionId;

    responsePollGenerationRef.current += 1;
    clearStreamingInterval();
    streamBufferRef.current = "";
    setStreamingContent("");
    liveToolResultsRef.current = [];
    setLiveToolResults([]);
    setSteeringInstructions([]);
    setToolPermissionRequest(null);
    setExternalModelPromptRequest(null);
    setExternalModelPromptDraft("");
    processedEventIdsRef.current.clear();
    processedLegacyMessageRef.current = null;
    const pending = pendingMessageRef.current;
    if (pending && pending.sessionId !== activeSessionId) {
      pendingMessageRef.current = null;
      pending.settle?.("failed");
    }
  }, [
    activeSessionId,
    clearStreamingInterval,
    liveToolResultsRef,
    pendingMessageRef,
    processedEventIdsRef,
    processedLegacyMessageRef,
    responsePollGenerationRef,
    setExternalModelPromptDraft,
    setExternalModelPromptRequest,
    setLiveToolResults,
    setSteeringInstructions,
    setStreamingContent,
    setToolPermissionRequest,
    streamBufferRef,
  ]);
}
