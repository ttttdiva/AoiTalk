"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ChatApiError,
  chatApi,
  type ConversationMessage,
  type DotsDialogueChatAvailability,
} from "@/lib/chat-api";

export type DotsDialogueAvailabilityView = {
  selectable: boolean;
  state: string;
  conversationId: string | null;
  bindingId: string | null;
  generation: number | null;
  subscriptionExpiresAt: string | null;
  latestRequestId: string | null;
  latestRequestState: string | null;
  message: string;
};

const CHECKING_VIEW: DotsDialogueAvailabilityView = {
  selectable: false,
  state: "checking",
  conversationId: null,
  bindingId: null,
  generation: null,
  subscriptionExpiresAt: null,
  latestRequestId: null,
  latestRequestState: null,
  message: "GPT dots接続状態を確認しています。",
};

const FIXED_CHAT_ONLY_VIEW: DotsDialogueAvailabilityView = {
  ...CHECKING_VIEW,
  state: "fixed_chat_only",
  message: "GPT dotsは承認済みの既存Chatでのみ利用できます。",
};

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function parseAvailability(
  value: DotsDialogueChatAvailability,
  targetConversationRequired = true,
): DotsDialogueAvailabilityView {
  const state = stringOrNull(value.state) ?? "invalid_response";
  const conversationId =
    stringOrNull(value.target_chat_id) ?? stringOrNull(value.conversation_id);
  const bindingId = stringOrNull(value.binding_id);
  const generation =
    typeof value.generation === "number" && Number.isFinite(value.generation)
      ? value.generation
      : null;
  const selectable =
    value.selectable === true &&
    Boolean((conversationId || !targetConversationRequired) && bindingId) &&
    generation !== null &&
    generation > 0;
  const latestRequestState = stringOrNull(value.latest_request_state);
  let message: string;

  if (selectable) {
    switch (latestRequestState) {
      case "pending":
        message = "GPT dots依頼は登録済みです。返信待ちです。";
        break;
      case "delivery_accepted":
        message = "GPT dotsへ依頼を配送しました。返信待ちです。";
        break;
      case "read":
        message = "GPT dotsが依頼を読みました。返信待ちです。";
        break;
      case "replied":
        message = "GPT dotsの返信は会話履歴に保存されています。";
        break;
      case "expired":
        message =
          "直近のGPT dots依頼は期限切れです。新しい依頼は有効な購読が必要です。";
        break;
      case "delivery_failed":
        message = "直近のGPT dots依頼は配送できませんでした。";
        break;
      case "cancelled":
        message = "直近のGPT dots依頼はキャンセル済みです。";
        break;
      default:
        message = value.subscription_expires_at
          ? `承認済みDOTS接続は有効です（期限 ${value.subscription_expires_at}）。`
          : "承認済みDOTS接続は有効です。";
    }
    if (latestRequestState && value.subscription_expires_at) {
      message += ` 専用接続は有効です（期限 ${value.subscription_expires_at}）。`;
    }
  } else {
    switch (state) {
      case "subscription_missing":
        message =
          "GPT dotsのホスト購読がありません。ホストの標準Events購読を確認してください。";
        break;
      case "subscription_expired":
        message =
          "GPT dotsのホスト購読が期限切れです。ホスト側の標準Events更新を待っています。";
        break;
      case "subscription_revoked":
        message = "GPT dotsのホスト購読は解除されています。";
        break;
      case "conversation_unavailable":
        message = "承認済みのGPT dots会話を利用できません。";
        break;
      case "binding_unavailable":
        message = "承認済みのGPT dots会話bindingを利用できません。";
        break;
      case "connection_unavailable":
        message = "このアカウントでは専用GPT dots接続を利用できません。";
        break;
      case "feature_unavailable":
        message = "GPT dots Chat経路は利用できません。";
        break;
      default:
        message = "GPT dotsは承認済みの既存Chatでのみ利用できます。";
    }
  }

  return {
    selectable,
    state,
    conversationId,
    bindingId,
    generation,
    subscriptionExpiresAt: stringOrNull(value.subscription_expires_at),
    latestRequestId: stringOrNull(value.latest_request_id),
    latestRequestState,
    message,
  };
}

export function useDotsDialogueAvailability(
  sessionId: string | null | undefined,
  userId?: string | null,
): DotsDialogueAvailabilityView {
  const [snapshot, setSnapshot] = useState<{
    scopeKey: string | null;
    view: DotsDialogueAvailabilityView;
  }>({ scopeKey: null, view: CHECKING_VIEW });
  const normalizedSessionId = sessionId?.trim() || null;
  const normalizedUserId = userId?.trim() || null;
  const scopeKey = `${normalizedUserId ?? "__anonymous__"}\u0000${
    normalizedSessionId ?? "__new_chat__"
  }`;

  useEffect(() => {
    if (!normalizedUserId && !normalizedSessionId) return;

    let cancelled = false;
    let loading = false;
    let intervalHandle: number | null = null;
    const controller = new AbortController();
    const refresh = async () => {
      if (loading || cancelled) return;
      loading = true;
      try {
        const response = await chatApi.getDotsDialogueChatAvailability(
          normalizedSessionId,
          controller.signal,
        );
        if (cancelled) return;
        const isNewChatConnectionRead = normalizedSessionId === null;
        const parsed = parseAvailability(response, !isNewChatConnectionRead);
        const responseOwnerId = stringOrNull(response.owner_user_id);
        if (
          normalizedUserId &&
          ((isNewChatConnectionRead && responseOwnerId !== normalizedUserId) ||
            (responseOwnerId && responseOwnerId !== normalizedUserId))
        ) {
          setSnapshot({
            scopeKey,
            view: {
              ...CHECKING_VIEW,
              state: "binding_unavailable",
              message: "承認済みのGPT dots会話bindingを利用できません。",
            },
          });
          if (intervalHandle != null) window.clearInterval(intervalHandle);
          return;
        }
        if (isNewChatConnectionRead) {
          setSnapshot({
            scopeKey,
            view: parsed.selectable
              ? {
                  ...parsed,
                  state: "connection_ready",
                  conversationId: null,
                  message: "GPT dots接続を新規Chatで利用できます。",
                }
              : parsed,
          });
          return;
        }
        if (parsed.conversationId !== normalizedSessionId) {
          setSnapshot({
            scopeKey,
            view: FIXED_CHAT_ONLY_VIEW,
          });
          if (intervalHandle != null) window.clearInterval(intervalHandle);
          return;
        }
        setSnapshot({ scopeKey, view: parsed });
      } catch (error) {
        if (cancelled) return;
        const errorCode =
          error &&
          typeof error === "object" &&
          "code" in error &&
          typeof error.code === "string"
            ? error.code
            : "availability_unavailable";
        setSnapshot({
          scopeKey,
          view: {
            ...CHECKING_VIEW,
            state: errorCode,
            message:
              "GPT dots接続状態を確認できません。再確認してから送信してください。",
          },
        });
      } finally {
        loading = false;
      }
    };

    intervalHandle = window.setInterval(() => void refresh(), 10_000);
    void refresh();
    return () => {
      cancelled = true;
      controller.abort();
      if (intervalHandle != null) window.clearInterval(intervalHandle);
    };
  }, [normalizedSessionId, normalizedUserId, scopeKey]);

  if (!normalizedSessionId && !normalizedUserId) return FIXED_CHAT_ONLY_VIEW;
  return snapshot.scopeKey === scopeKey
    ? snapshot.view
    : CHECKING_VIEW;
}

type RequestHistorySyncOptions = {
  sessionId: string | null | undefined;
  messages: ConversationMessage[];
  onPersistedHistory: (
    sessionId: string,
    messages: ConversationMessage[],
  ) => void;
};

function isRequestMessage(message: ConversationMessage): string | null {
  const metadata = message.metadata;
  if (
    !metadata ||
    metadata.external_dots_dialogue !== true ||
    metadata.dots_dialogue_kind !== "request"
  ) {
    return null;
  }
  return stringOrNull(metadata.request_id);
}

const TERMINAL_REQUEST_STATES = new Set([
  "replied",
  "cancelled",
  "expired",
  "delivery_failed",
]);

/** Refreshes normal conversation history after the owner API records a DOTS reply. */
export function useDotsDialogueHistorySync({
  sessionId,
  messages,
  onPersistedHistory,
}: RequestHistorySyncOptions): (requestId: string) => void {
  const normalizedSessionId = sessionId?.trim() || null;
  const watchersRef = useRef(new Map<string, AbortController>());
  const completedRef = useRef(new Set<string>());
  const historyCallbackRef = useRef(onPersistedHistory);
  useLayoutEffect(() => {
    historyCallbackRef.current = onPersistedHistory;
  }, [onPersistedHistory]);

  const watchRequest = useCallback(
    (rawRequestId: string) => {
      const requestId = rawRequestId.trim();
      if (!normalizedSessionId || !requestId) return;
      const key = `${normalizedSessionId}\u0000${requestId}`;
      if (watchersRef.current.has(key) || completedRef.current.has(key)) return;
      const controller = new AbortController();
      watchersRef.current.set(key, controller);

      void (async () => {
        let delayMs = 3000;
        while (!controller.signal.aborted) {
          try {
            const ownerRequest = await chatApi.getDotsDialogueOwnerRequest(
              requestId,
              controller.signal,
            );
            const state = ownerRequest.state.toLowerCase();
            if (state === "replied") {
              const history = await chatApi.getMessages(
                normalizedSessionId,
                undefined,
                controller.signal,
              );
              if (!controller.signal.aborted) {
                historyCallbackRef.current(
                  normalizedSessionId,
                  history.messages,
                );
                completedRef.current.add(key);
                while (completedRef.current.size > 128) {
                  const oldest = completedRef.current.values().next().value;
                  if (!oldest) break;
                  completedRef.current.delete(oldest);
                }
              }
              return;
            }
            if (TERMINAL_REQUEST_STATES.has(state)) {
              completedRef.current.add(key);
              while (completedRef.current.size > 128) {
                const oldest = completedRef.current.values().next().value;
                if (!oldest) break;
                completedRef.current.delete(oldest);
              }
              return;
            }
          } catch (error) {
            if (controller.signal.aborted) return;
            if (
              error instanceof ChatApiError &&
              error.status != null &&
              error.status < 500
            ) {
              completedRef.current.add(key);
              return;
            }
          }
          await new Promise<void>((resolve) => {
            let onAbort: () => void = () => {};
            const timeout = window.setTimeout(() => {
              controller.signal.removeEventListener("abort", onAbort);
              resolve();
            }, delayMs);
            onAbort = () => {
              window.clearTimeout(timeout);
              controller.signal.removeEventListener("abort", onAbort);
              resolve();
            };
            controller.signal.addEventListener("abort", onAbort, {
              once: true,
            });
          });
          delayMs = Math.min(delayMs * 2, 30_000);
        }
      })().finally(() => {
        watchersRef.current.delete(key);
      });
    },
    [normalizedSessionId],
  );

  useEffect(() => {
    if (!normalizedSessionId) return;
    for (const message of messages) {
      if (message.session_id !== normalizedSessionId) continue;
      const requestId = isRequestMessage(message);
      if (requestId) watchRequest(requestId);
    }
  }, [messages, normalizedSessionId, watchRequest]);

  useEffect(
    () => () => {
      for (const controller of watchersRef.current.values()) controller.abort();
      watchersRef.current.clear();
    },
    [normalizedSessionId],
  );

  return watchRequest;
}
