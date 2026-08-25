"use client";

import { useCallback, useRef, useState } from "react";

import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";
import { chatApi } from "@/lib/chat-api";
import {
  buildTaskChatDraft,
  buildTaskChatSessionTitle,
} from "@/lib/task-agent";
import {
  clearChatDraftHandoff,
  storeChatDraftHandoff,
} from "@/lib/chat-draft-handoff";
import { useChatSessionsOptional } from "@/contexts/chat-session-context";
import { navigateChatSessionInPlace } from "@/lib/chat-navigation";
import { normalizeTaskTitle } from "@/components/tasks/task-form-utils";

/**
 * タスク詳細モーダルのチャット開始・Agentトリアージ処理をまとめた hook。
 * state（task / editTitle / editDescription）は呼び出し側が所有する。
 */
export function useTaskAgentActions({
  task,
  editTitle,
  editDescription,
  effectiveTaskId,
  onOpenChange,
  setTask,
  fetchTask,
  ensureTaskId,
}: {
  task: Task | null;
  editTitle: string;
  editDescription: string;
  effectiveTaskId: string | null;
  onOpenChange: (open: boolean) => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  fetchTask: () => Promise<void>;
  ensureTaskId: () => Promise<string | null>;
}) {
  const router = useRouter();
  const chatSessions = useChatSessionsOptional();
  const [openingChat, setOpeningChat] = useState(false);
  const [triagingAgent, setTriagingAgent] = useState(false);
  const openingChatRef = useRef(false);

  const handleOpenInChat = useCallback(async () => {
    if (!task || openingChatRef.current) return;

    const normalizedTitle = normalizeTaskTitle(editTitle || task.title);
    if (!normalizedTitle) {
      toast.error("Task title is required.");
      return;
    }

    let persistedTaskId: string | null = null;
    let createdSessionId: string | null = null;
    let createdReferenceId: string | null = null;
    let registeredSessionId: string | null = null;
    let handoffReady = false;

    openingChatRef.current = true;
    setOpeningChat(true);
    try {
      persistedTaskId = effectiveTaskId ?? (await ensureTaskId());
      if (!persistedTaskId) {
        throw new Error("タスクを保存できませんでした");
      }

      const taskSnapshot: Task = {
        ...task,
        id: persistedTaskId,
        title: normalizedTitle,
        description: editDescription.trim() || null,
      };
      const created = await chatApi.createSession(
        await chatApi.getCurrentCharacterName(),
        taskSnapshot.project_id || undefined,
      );
      const sessionId = created.session.id;
      createdSessionId = sessionId;
      const sessionTitle = buildTaskChatSessionTitle(normalizedTitle);

      await chatApi.updateSessionTitle(sessionId, sessionTitle);
      const reference = await taskApi.addReference(persistedTaskId, {
        reference_type: "conversation_session",
        relation_type: "related",
        target_id: sessionId,
        display_name: sessionTitle,
        metadata: {
          source: "task_open_in_chat",
        },
      });
      createdReferenceId = reference.id;
      storeChatDraftHandoff(window.sessionStorage, sessionId, {
        content: buildTaskChatDraft(taskSnapshot),
        generationProfile: "assisted_work",
        sourceTaskId: persistedTaskId,
      });
      if (chatSessions?.registerSession) {
        chatSessions.registerSession(created.session, {
          generationReady: true,
          activate: true,
        });
      } else {
        // Keep compatibility with isolated callers that provide the legacy
        // session actions without the shared provider helper.
        chatSessions?.addSession?.(created.session);
        chatSessions?.activateSession?.(sessionId);
      }
      registeredSessionId = sessionId;

      onOpenChange(false);
      const href = `/chat?s=${encodeURIComponent(sessionId)}`;
      if (!navigateChatSessionInPlace(href)) router.push(href);
      handoffReady = true;
    } catch (err) {
      if (!handoffReady) {
        if (createdSessionId) {
          clearChatDraftHandoff(window.sessionStorage, createdSessionId);
        }
        if (registeredSessionId) {
          chatSessions?.removeSession?.(registeredSessionId);
          chatSessions?.clearRequestedSession?.();
        }
        if (persistedTaskId && createdReferenceId) {
          await taskApi
            .removeReference(persistedTaskId, createdReferenceId)
            .catch((cleanupError) =>
              console.warn("Failed to roll back task reference", cleanupError),
            );
        }
        if (createdSessionId) {
          await chatApi
            .deleteSession(createdSessionId)
            .catch((cleanupError) =>
              console.warn("Failed to roll back chat session", cleanupError),
            );
        }
      }
      console.error("Failed to open task in chat", err);
      toast.error(
        err instanceof Error
          ? err.message
          : "タスクのチャットを開始できませんでした",
      );
    } finally {
      openingChatRef.current = false;
      setOpeningChat(false);
    }
  }, [
    editDescription,
    editTitle,
    effectiveTaskId,
    ensureTaskId,
    onOpenChange,
    router,
    task,
    chatSessions,
  ]);

  const handleRunAgentTriage = useCallback(async () => {
    if (!effectiveTaskId) return;
    setTriagingAgent(true);
    try {
      const result = await taskApi.runAgentTriage(effectiveTaskId);
      setTask((prev) =>
        prev
          ? ({
              ...prev,
              metadata: {
                ...(prev.metadata || {}),
                ...result.metadata,
              },
            } as Task)
          : prev,
      );
      await fetchTask();
      toast.success("Agent triage updated");
    } catch (err) {
      console.error("Agent triage failed", err);
      toast.error(err instanceof Error ? err.message : "Agent triage failed");
    } finally {
      setTriagingAgent(false);
    }
  }, [effectiveTaskId, fetchTask, setTask]);

  return {
    openingChat,
    triagingAgent,
    handleOpenInChat,
    handleRunAgentTriage,
  };
}
