"use client";

import {
  useCallback,
  useRef,
  type Dispatch,
  type RefObject,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type {
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";
import {
  createLocalMessage,
  createLocalUserMessage,
} from "@/lib/chat-local-messages";
import { deepResearchApi, type DeepResearchJob } from "@/lib/deep-research-api";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";
import type { ChatGenerationEvent } from "@/lib/chat-generation-state";
import { getGenerationReadyNewChatMainRoute } from "@/hooks/use-chat-session-route";
import { hasExplicitSessionRoute } from "@/lib/chat-session-route";
import { applyPendingNewChatLlmSettingsToSession } from "@/lib/new-chat-llm-settings-store";
import { PendingLlmHandoffError } from "@/lib/chat-session-route-handoff";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

/** Shared provisional identity for a user turn sent from the new-chat view. */
export const OPTIMISTIC_NEW_CHAT_SESSION_PREFIX = "__new_chat_optimistic__:";

function formatDeepResearchProgress(job: DeepResearchJob): string {
  const latestEvent = job.events.at(-1);
  const questions = Object.entries(job.questions_by_iteration)
    .slice(-2)
    .flatMap(([iteration, items]) =>
      items.map((item) => `- ${iteration}: ${item}`),
    )
    .join("\n");
  return [
    "Deep Researchを実行中です。",
    "",
    `進捗: ${job.progress}% (${job.status})`,
    latestEvent ? `現在: ${latestEvent.message}` : null,
    job.sources.length > 0 ? `収集ソース: ${job.sources.length}件` : null,
    questions ? `\n検索クエリ:\n${questions}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function formatDeepResearchFinal(job: DeepResearchJob): string {
  if (job.status === "failed") {
    return `Deep Researchに失敗しました。\n\n${job.error || "原因不明のエラー"}`;
  }
  if (job.status === "cancelled") {
    return "Deep Researchはキャンセルされました。";
  }
  if (job.status === "interrupted" || job.interrupted) {
    return `Deep Researchは中断されました。\n\n${
      job.error || "実行プロセスが再起動されたため、調査を完了できませんでした。"
    }`;
  }
  return (
    job.report_markdown ||
    "Deep Researchは完了しましたが、レポート本文が空でした。"
  );
}

type DeepResearchFailurePhase =
  | "session-create"
  | "message-save"
  | "research-start"
  | "research-poll"
  | "assistant-save";

function createDeepResearchClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `deep-research-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

type FailureDetails = {
  message: string;
  code?: string;
  retryable?: boolean;
  requestId?: string | null;
};

type PendingAssistantPersistence = {
  sessionId: string;
  generationClientMessageId: string;
  assistantClientMessageId: string;
  assistantTempMessageId: string;
  content: string;
  metadata: Record<string, unknown>;
  researchStatus: DeepResearchJob["status"];
  researchError: string | null;
};

const MAX_PENDING_ASSISTANT_PERSISTENCE = 32;

function describeFailure(error: unknown): FailureDetails {
  if (!error || typeof error !== "object") {
    return { message: "原因不明のエラー" };
  }
  const value = error as {
    message?: unknown;
    code?: unknown;
    retryable?: unknown;
    requestId?: unknown;
    request_id?: unknown;
    error?: {
      message?: unknown;
      code?: unknown;
      retryable?: unknown;
      request_id?: unknown;
    };
  };
  const nested = value.error;
  const message =
    typeof value.message === "string" && value.message.trim()
      ? value.message.trim()
      : typeof nested?.message === "string" && nested.message.trim()
        ? nested.message.trim()
        : "原因不明のエラー";
  const code =
    typeof value.code === "string" && value.code.trim()
      ? value.code.trim()
      : typeof nested?.code === "string" && nested.code.trim()
        ? nested.code.trim()
        : undefined;
  const retryable =
    typeof value.retryable === "boolean"
      ? value.retryable
      : typeof nested?.retryable === "boolean"
        ? nested.retryable
        : undefined;
  const requestId =
    typeof value.requestId === "string" && value.requestId.trim()
      ? value.requestId.trim()
      : typeof value.request_id === "string" && value.request_id.trim()
        ? value.request_id.trim()
        : typeof nested?.request_id === "string" && nested.request_id.trim()
          ? nested.request_id.trim()
          : null;
  return { message, code, retryable, requestId };
}

function formatFailureMessage(
  error: unknown,
  phase: DeepResearchFailurePhase,
): string {
  const details = describeFailure(error);
  const prefix =
    phase === "message-save"
      ? "メッセージの保存に失敗しました。"
      : phase === "assistant-save"
        ? "Deep Researchの結果をメッセージとして保存できませんでした。"
        : "Deep Researchに失敗しました。";
  const lines = [prefix, "", details.message];
  if (details.code) lines.push(`エラーコード: ${details.code}`);
  if (details.requestId) lines.push(`参照ID: ${details.requestId}`);
  return lines.join("\n");
}

type UseDeepResearchMessageArgs = {
  router: ReturnType<typeof useRouter>;
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  activateSession: (sessionId: string) => void;
  includeProjectContext: boolean;
  addSession: (session: ConversationSession) => void;
  bumpSession: (sessionId: string) => void;
  updateSidebarTitle: (sessionId: string, title: string) => void;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  dispatchGeneration: Dispatch<ChatGenerationEvent>;
  upsertSession: (session: ConversationSession) => void;
};

/**
 * Deep Research メッセージ送信・ジョブ進捗ポーリングを担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの（`use-chat-messaging` の内部から呼ぶ）。
 * 依存配列は元コードと同一に保つ。
 */
export function useDeepResearchMessage({
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
}: UseDeepResearchMessageArgs) {
  const draftUserId = useCurrentUserId();
  // A completed report must remain retryable when only its assistant-message
  // persistence fails. Keep the bounded in-memory payload keyed by the same
  // client id sent to the idempotent message endpoint; a retry therefore
  // cannot launch a second research job or create a duplicate assistant row.
  const pendingAssistantPersistenceRef = useRef(
    new Map<string, PendingAssistantPersistence>(),
  );

  const retryDeepResearchAssistantPersistence = useCallback(
    async (assistantClientMessageId: string): Promise<boolean> => {
      const pending = pendingAssistantPersistenceRef.current.get(
        assistantClientMessageId,
      );
      if (!pending) return false;

      try {
        const savedAssistant = await chatApi.addMessage(pending.sessionId, {
          role: "assistant",
          content: pending.content,
          client_message_id: pending.assistantClientMessageId,
        });
        pendingAssistantPersistenceRef.current.delete(
          assistantClientMessageId,
        );
        dispatchChatTimeline({
          type: "replace_by_id",
          messageId: pending.assistantTempMessageId,
          message: {
            ...savedAssistant.message,
            metadata: {
              ...savedAssistant.message.metadata,
              ...pending.metadata,
              persistence_failed: false,
              persistence_retryable: false,
              client_message_id: pending.assistantClientMessageId,
            },
          },
          appendIfMissing: true,
        });
        dispatchGeneration({
          type:
            pending.researchStatus === "cancelled"
              ? "cancelled"
              : pending.researchStatus === "failed" ||
                  pending.researchStatus === "interrupted"
                ? "failed"
                : "completed",
          sessionId: pending.sessionId,
          clientMessageId: pending.generationClientMessageId,
          assistantMessageId: savedAssistant.message.id,
          statusMessage: pending.researchError,
          eventId: `deep-research:${pending.sessionId}:persistence-retry`,
        });
        bumpSession(pending.sessionId);
        return true;
      } catch (error) {
        const failure = describeFailure(error);
        const retryable = failure.retryable !== false;
        const metadata = {
          ...pending.metadata,
          persistence_failed: true,
          persistence_retryable: retryable,
          persistence_error_code: failure.code,
          persistence_request_id: failure.requestId,
        };
        pending.metadata = metadata;
        dispatchChatTimeline({
          type: "update_by_id",
          messageId: pending.assistantTempMessageId,
          update: (message) => ({ ...message, metadata }),
        });
        return false;
      }
    },
    [bumpSession, dispatchChatTimeline, dispatchGeneration],
  );

  const handleDeepResearchMessage = useCallback(
    async (
      content: string,
      projectId?: string,
      clientMessageId?: string,
    ) => {
      let sessionId = activeSessionId;
      const generationClientMessageId =
        clientMessageId ?? createDeepResearchClientMessageId();
      const isNewSession = !sessionId;
      const provisionalSessionId = isNewSession
        ? `${OPTIMISTIC_NEW_CHAT_SESSION_PREFIX}${generationClientMessageId}`
        : null;
      const assistantClientMessageId = `${generationClientMessageId}:assistant`;
      let optimisticMessageSessionId = sessionId ?? provisionalSessionId;
      let optimisticMessageAdded = false;
      let optimisticMessagePromoted = false;
      let assistantMessageAdded = false;
      let assistantMessagePersisted = false;
      let assistantTempMessageId: string | null = null;
      let assistantTempMessage: ConversationMessage | null = null;
      let finalAssistantContent: string | null = null;
      let terminalResearchJob: DeepResearchJob | null = null;
      let failurePhase: DeepResearchFailurePhase = isNewSession
        ? "session-create"
        : "message-save";
      const generationReadyMain = isNewSession
        ? getGenerationReadyNewChatMainRoute()
        : null;

      try {
        // Match regular chat: fail closed before showing a bubble when the
        // new-chat provider/model authority has not been resolved yet.
        if (isNewSession && !hasExplicitSessionRoute(generationReadyMain)) {
          throw new PendingLlmHandoffError(
            "Provider / Model の authoritative route を確定できないため、応答生成を開始しませんでした。",
          );
        }

        // Render the user turn before any network request. New-chat messages
        // use the same provisional identity as regular chat and are rebound
        // once createSession returns the authoritative session id.
        if (optimisticMessageSessionId) {
          dispatchChatTimeline({
            type: "append",
            message: createLocalUserMessage(
              optimisticMessageSessionId,
              content,
              generationClientMessageId,
            ),
          });
          optimisticMessageAdded = true;
        }

        if (!sessionId) {
          const data = await chatApi.createSession(
            await chatApi.getCurrentCharacterName(),
            projectId,
            undefined,
            null,
            generationReadyMain,
          );
          sessionId = data.session.id;
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
            if (optimisticMessageAdded && provisionalSessionId) {
              dispatchChatTimeline({
                type: "remove_client_message",
                sessionId: provisionalSessionId,
                clientMessageId: generationClientMessageId,
              });
            }
            addSession(data.session);
            upsertSession(data.session);
            activateSession(sessionId);
            const href = `/chat?s=${encodeURIComponent(sessionId)}`;
            if (!navigateChatSessionInPlace(href)) {
              router.push(href);
            }
            dispatchChatTimeline({
              type: "append",
              message: createLocalMessage(
                sessionId,
                "assistant",
                error instanceof PendingLlmHandoffError
                  ? error.message
                  : "選択した Provider / Model 設定を会話へ適用できなかったため、応答生成を開始しませんでした。",
              ),
            });
            return false;
          }
          if (provisionalSessionId) {
            dispatchChatTimeline({
              type: "rebind_client_message_session",
              fromSessionId: provisionalSessionId,
              toSessionId: sessionId,
              clientMessageId: generationClientMessageId,
            });
            optimisticMessageSessionId = sessionId;
          }
          addSession(data.session);
          upsertSession(data.session);
          activateSession(sessionId);
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }
        }

        if (!sessionId || activeSessionIdRef.current !== sessionId) {
          if (optimisticMessageAdded && optimisticMessageSessionId) {
            dispatchChatTimeline({
              type: "remove_client_message",
              sessionId: optimisticMessageSessionId,
              clientMessageId: generationClientMessageId,
            });
          }
          return false;
        }
        const operationSessionId = sessionId;
        const isCurrentOperation = () =>
          activeSessionIdRef.current === operationSessionId;
        dispatchGeneration({
          type: "dispatch_started",
          sessionId: operationSessionId,
          clientMessageId: generationClientMessageId,
        });

        failurePhase = "message-save";
        const userMessage = await chatApi.addMessage(operationSessionId, {
          role: "user",
          content,
          client_message_id: generationClientMessageId,
        });
        // Keep the optimistic row as the canonical live row. The REST
        // response may not carry client_message_id, so appending it would
        // duplicate the user turn; promotion only replaces its id.
        dispatchChatTimeline({
          type: "promote_client_message",
          sessionId: operationSessionId,
          clientMessageId: generationClientMessageId,
          serverMessageId: userMessage.message.id,
        });
        optimisticMessagePromoted = true;
        if (!isCurrentOperation()) return false;

        const assistantTemp = createLocalMessage(
          sessionId,
          "assistant",
          "Deep Researchを開始しています。",
          {
            deep_research: true,
            status: "queued",
            client_message_id: assistantClientMessageId,
          },
        );
        assistantMessageAdded = true;
        assistantTempMessageId = assistantTemp.id;
        assistantTempMessage = assistantTemp;
        dispatchChatTimeline({ type: "append", message: assistantTemp });
        dispatchGeneration({
          type: "dispatch_accepted",
          sessionId: operationSessionId,
          clientMessageId: generationClientMessageId,
          statusMessage: "Deep Researchをキューに追加しました",
        });
        bumpSession(sessionId);

        failurePhase = "research-start";
        const started = await deepResearchApi.startJob({
          query: content,
          mode: "detailed",
          max_iterations: 3,
          questions_per_iteration: 3,
          max_results_per_query: 5,
          // SearXNG is the local authority.  The server may still accept an
          // explicitly approved public engine list, but a fresh Enterprise
          // request must never inherit public defaults from the Personal UI.
          engines: ["searxng"],
          include_local_knowledge: includeProjectContext,
          session_id: operationSessionId,
          project_id: projectId ?? null,
        });
        if (!isCurrentOperation()) return false;

        failurePhase = "research-poll";
        let current = started;
        const updateAssistantTemp = (job: DeepResearchJob) => {
          if (!isCurrentOperation()) return false;
          const content =
            job.status === "completed" ||
            job.status === "failed" ||
            job.status === "cancelled" ||
            job.status === "interrupted"
              ? formatDeepResearchFinal(job)
              : formatDeepResearchProgress(job);
          const replacement = {
            ...assistantTemp,
            content,
            metadata: {
              ...assistantTemp.metadata,
              deep_research: true,
              job_id: job.id,
              status: job.status,
              progress: job.progress,
            },
          };
          dispatchChatTimeline({
            type: "replace_by_id",
            messageId: assistantTemp.id,
            message: replacement,
            appendIfMissing: true,
          });
          return true;
        };

        if (!updateAssistantTemp(current)) return false;

        while (
          !["completed", "failed", "cancelled", "interrupted"].includes(
            current.status,
          )
        ) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          current = await deepResearchApi.getJob(current.id);
          if (!updateAssistantTemp(current)) return false;
        }

        const finalContent = formatDeepResearchFinal(current);
        finalAssistantContent = finalContent;
        terminalResearchJob = current;
        failurePhase = "assistant-save";
        const savedAssistant = await chatApi.addMessage(operationSessionId, {
          role: "assistant",
          content: finalContent,
          client_message_id: assistantClientMessageId,
        });
        assistantMessagePersisted = true;
        pendingAssistantPersistenceRef.current.delete(assistantClientMessageId);
        if (!isCurrentOperation()) return false;
        dispatchChatTimeline({
          type: "replace_by_id",
          messageId: assistantTemp.id,
          message: {
            ...savedAssistant.message,
            metadata: {
              ...savedAssistant.message.metadata,
              deep_research: true,
              job_id: current.id,
              status: current.status,
              progress: current.progress,
              client_message_id: assistantClientMessageId,
            },
          },
          appendIfMissing: true,
        });
        dispatchGeneration({
          type:
            current.status === "cancelled"
              ? "cancelled"
              : current.status === "failed" || current.status === "interrupted"
                ? "failed"
                : "completed",
          sessionId: operationSessionId,
          clientMessageId: generationClientMessageId,
          assistantMessageId: savedAssistant.message.id,
          statusMessage: current.error ?? null,
          eventId: `deep-research:${current.id}:${current.status}`,
        });
        try {
          const titleResult = await chatApi.generateSessionTitle(operationSessionId);
          if (titleResult.title && isCurrentOperation()) {
            updateSidebarTitle(operationSessionId, titleResult.title);
          }
        } catch (err) {
          console.warn("セッションタイトル生成に失敗:", err);
        }
        if (isCurrentOperation()) bumpSession(operationSessionId);
        return true;
      } catch (err) {
        console.error("Deep Research送信失敗:", err);
        const failure = describeFailure(err);
        const failedOptimisticSessionId =
          optimisticMessageSessionId ?? provisionalSessionId ?? sessionId;
        if (
          optimisticMessageAdded &&
          !optimisticMessagePromoted &&
          failedOptimisticSessionId
        ) {
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId: failedOptimisticSessionId,
            clientMessageId: generationClientMessageId,
          });
        }
        const assistantPersistenceFailed =
          failurePhase === "assistant-save" &&
          assistantMessageAdded &&
          !assistantMessagePersisted &&
          Boolean(assistantTempMessageId && sessionId && assistantTempMessage);
        if (assistantPersistenceFailed) {
          // The research result is already complete. Do not remove it just
          // because the durable assistant row failed; retain the same
          // client-id identity and expose a persistence-only retry state.
          const retainedTempMessage = assistantTempMessage;
          const retainedTempMessageId = assistantTempMessageId;
          const retainedSessionId = sessionId;
          if (!retainedTempMessage || !retainedTempMessageId || !retainedSessionId) {
            return false;
          }
          const retryable = failure.retryable !== false;
          const metadata = {
            ...(retainedTempMessage.metadata ?? {}),
            deep_research: true,
            status: terminalResearchJob?.status ?? "completed",
            job_id: terminalResearchJob?.id,
            progress: terminalResearchJob?.progress ?? 100,
            client_message_id: assistantClientMessageId,
            persistence_failed: true,
            persistence_retryable: retryable,
            persistence_error_code: failure.code,
            persistence_request_id: failure.requestId,
          } satisfies Record<string, unknown>;
          pendingAssistantPersistenceRef.current.delete(
            assistantClientMessageId,
          );
          pendingAssistantPersistenceRef.current.set(
            assistantClientMessageId,
            {
              sessionId: retainedSessionId,
              generationClientMessageId,
              assistantClientMessageId,
              assistantTempMessageId: retainedTempMessageId,
              content:
                finalAssistantContent ?? retainedTempMessage.content,
              metadata,
              researchStatus: terminalResearchJob?.status ?? "completed",
              researchError: terminalResearchJob?.error ?? null,
            },
          );
          while (
            pendingAssistantPersistenceRef.current.size >
            MAX_PENDING_ASSISTANT_PERSISTENCE
          ) {
            const oldest =
              pendingAssistantPersistenceRef.current.keys().next().value;
            if (typeof oldest !== "string") break;
            pendingAssistantPersistenceRef.current.delete(oldest);
          }
          dispatchChatTimeline({
            type: "replace_by_id",
            messageId: retainedTempMessageId,
            message: {
              ...retainedTempMessage,
              content:
                finalAssistantContent ?? retainedTempMessage.content,
              metadata,
            },
            appendIfMissing: true,
          });
        }
        if (
          assistantMessageAdded &&
          !assistantMessagePersisted &&
          !assistantPersistenceFailed &&
          assistantTempMessageId &&
          sessionId
        ) {
          dispatchChatTimeline({
            type: "remove_client_message",
            sessionId,
            clientMessageId: assistantClientMessageId,
          });
        }
        if (
          sessionId &&
          activeSessionIdRef.current === sessionId &&
          !assistantPersistenceFailed
        ) {
          const failedSessionId = sessionId;
          dispatchChatTimeline({
            type: "append",
            message: createLocalMessage(
              failedSessionId,
              "assistant",
              formatFailureMessage(err, failurePhase),
              {
                deep_research: true,
                status: "failed",
                error_code: failure.code,
                retryable: failure.retryable,
                request_id: failure.requestId,
              },
            ),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId: generationClientMessageId,
            statusMessage: failure.message,
            eventId: `deep-research:${sessionId}:failed`,
          });
        }
        if (assistantPersistenceFailed && sessionId) {
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId: generationClientMessageId,
            assistantMessageId: assistantTempMessageId,
            statusMessage: failure.message,
            eventId: `deep-research:${sessionId}:persistence-failed`,
          });
        }
        return false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      activeSessionId,
      activeSessionIdRef,
      addSession,
      activateSession,
      bumpSession,
      includeProjectContext,
      dispatchGeneration,
      draftUserId,
      router,
      updateSidebarTitle,
      upsertSession,
    ],
  );

  return {
    handleDeepResearchMessage,
    retryDeepResearchAssistantPersistence,
  };
}
