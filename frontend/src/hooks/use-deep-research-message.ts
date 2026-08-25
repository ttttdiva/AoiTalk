"use client";

import {
  useCallback,
  type Dispatch,
  type RefObject,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type { ConversationSession } from "@/lib/chat-api";
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
  return (
    job.report_markdown ||
    "Deep Researchは完了しましたが、レポート本文が空でした。"
  );
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
  const handleDeepResearchMessage = useCallback(
    async (
      content: string,
      projectId?: string,
      clientMessageId?: string,
    ) => {
      let sessionId = activeSessionId;
      const generationClientMessageId =
        clientMessageId ?? `deep-research:${Date.now()}`;
      const isNewSession = !sessionId;
      const provisionalSessionId = isNewSession
        ? `${OPTIMISTIC_NEW_CHAT_SESSION_PREFIX}${generationClientMessageId}`
        : null;
      let optimisticMessageSessionId = sessionId ?? provisionalSessionId;
      let optimisticMessageAdded = false;
      let optimisticMessagePromoted = false;
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

        const userMessage = await chatApi.addMessage(operationSessionId, {
          role: "user",
          content,
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
          { deep_research: true, status: "queued" },
        );
        dispatchChatTimeline({ type: "append", message: assistantTemp });
        dispatchGeneration({
          type: "dispatch_accepted",
          sessionId: operationSessionId,
          clientMessageId: generationClientMessageId,
          statusMessage: "Deep Researchをキューに追加しました",
        });
        bumpSession(sessionId);

        const started = await deepResearchApi.startJob({
          query: content,
          mode: "detailed",
          max_iterations: 3,
          questions_per_iteration: 3,
          max_results_per_query: 5,
          engines: ["searxng", "wikipedia", "arxiv", "openalex", "pubmed"],
          include_local_knowledge: includeProjectContext,
          project_id: projectId ?? null,
        });
        if (!isCurrentOperation()) return false;

        let current = started;
        const updateAssistantTemp = (job: DeepResearchJob) => {
          if (!isCurrentOperation()) return false;
          const content =
            job.status === "completed" || job.status === "failed"
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

        while (!["completed", "failed", "cancelled"].includes(current.status)) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          current = await deepResearchApi.getJob(current.id);
          if (!updateAssistantTemp(current)) return false;
        }

        const finalContent = formatDeepResearchFinal(current);
        const savedAssistant = await chatApi.addMessage(operationSessionId, {
          role: "assistant",
          content: finalContent,
        });
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
            },
          },
          appendIfMissing: true,
        });
        dispatchGeneration({
          type:
            current.status === "cancelled"
              ? "cancelled"
              : current.status === "failed"
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
        if (sessionId && activeSessionIdRef.current === sessionId) {
          const failedSessionId = sessionId;
          dispatchChatTimeline({
            type: "append",
            message: createLocalMessage(
              failedSessionId,
              "assistant",
              `Deep Researchに失敗しました。\n\n${
                err instanceof Error ? err.message : String(err)
              }`,
              { deep_research: true, status: "failed" },
            ),
          });
          dispatchGeneration({
            type: "failed",
            sessionId,
            clientMessageId: generationClientMessageId,
            statusMessage: err instanceof Error ? err.message : String(err),
            eventId: `deep-research:${sessionId}:failed`,
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

  return { handleDeepResearchMessage };
}
