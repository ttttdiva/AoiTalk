"use client";

import {
  useCallback,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { useRouter } from "next/navigation";
import { chatApi } from "@/lib/chat-api";
import type { ConversationSession } from "@/lib/chat-api";
import { createLocalMessage } from "@/lib/chat-local-messages";
import { deepResearchApi, type DeepResearchJob } from "@/lib/deep-research-api";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import type { chatTimelineReducer } from "@/lib/chat-state";

type ChatTimelineAction = Parameters<typeof chatTimelineReducer>[1];

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
  activateSession: (sessionId: string) => void;
  includeProjectContext: boolean;
  addSession: (session: ConversationSession) => void;
  bumpSession: (sessionId: string) => void;
  updateSidebarTitle: (sessionId: string, title: string) => void;
  dispatchChatTimeline: Dispatch<ChatTimelineAction>;
  markWaitingResponse: (sessionId: string | null) => void;
  clearWaitingResponse: (sessionId: string | null) => void;
  setIsSending: Dispatch<SetStateAction<boolean>>;
  setCurrentSession: Dispatch<SetStateAction<ConversationSession | null>>;
};

/**
 * Deep Research メッセージ送信・ジョブ進捗ポーリングを担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの（`use-chat-messaging` の内部から呼ぶ）。
 * 依存配列は元コードと同一に保つ。
 */
export function useDeepResearchMessage({
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
}: UseDeepResearchMessageArgs) {
  const handleDeepResearchMessage = useCallback(
    async (content: string, projectId?: string) => {
      let sessionId = activeSessionId;
      const isNewSession = !sessionId;

      try {
        if (!sessionId) {
          const data = await chatApi.createSession(
            await chatApi.getCurrentCharacterName(),
            projectId,
          );
          sessionId = data.session.id;
          addSession(data.session);
          setCurrentSession(data.session);
          activateSession(sessionId);
          const href = `/chat?s=${encodeURIComponent(sessionId)}`;
          if (!navigateChatSessionInPlace(href)) {
            router.push(href);
          }
        }

        const userMessage = await chatApi.addMessage(sessionId, {
          role: "user",
          content,
        });

        if (isNewSession) {
          dispatchChatTimeline({
            type: "replace",
            messages: [userMessage.message],
          });
        } else {
          dispatchChatTimeline({
            type: "append",
            message: userMessage.message,
          });
        }

        const assistantTemp = createLocalMessage(
          sessionId,
          "assistant",
          "Deep Researchを開始しています。",
          { deep_research: true, status: "queued" },
        );
        dispatchChatTimeline({ type: "append", message: assistantTemp });
        markWaitingResponse(sessionId);
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

        let current = started;
        const updateAssistantTemp = (job: DeepResearchJob) => {
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
        };

        updateAssistantTemp(current);

        while (!["completed", "failed", "cancelled"].includes(current.status)) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          current = await deepResearchApi.getJob(current.id);
          updateAssistantTemp(current);
        }

        const finalContent = formatDeepResearchFinal(current);
        const savedAssistant = await chatApi.addMessage(sessionId, {
          role: "assistant",
          content: finalContent,
        });
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
        try {
          const titleResult = await chatApi.generateSessionTitle(sessionId);
          if (titleResult.title) {
            updateSidebarTitle(sessionId, titleResult.title);
            setCurrentSession((prev) =>
              prev && prev.id === sessionId
                ? { ...prev, title: titleResult.title }
                : prev,
            );
          }
        } catch (err) {
          console.warn("セッションタイトル生成に失敗:", err);
        }
        bumpSession(sessionId);
      } catch (err) {
        console.error("Deep Research送信失敗:", err);
        if (sessionId) {
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
        }
      } finally {
        clearWaitingResponse(sessionId);
        setIsSending(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      activeSessionId,
      addSession,
      activateSession,
      bumpSession,
      clearWaitingResponse,
      includeProjectContext,
      markWaitingResponse,
      router,
      updateSidebarTitle,
    ],
  );

  return { handleDeepResearchMessage };
}
