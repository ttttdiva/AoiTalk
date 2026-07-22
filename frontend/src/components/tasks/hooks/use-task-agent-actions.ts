"use client";

import { useCallback, useState } from "react";

import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";
import { chatApi } from "@/lib/chat-api";
import {
  buildTaskAgentPrompt,
  buildTaskAgentSessionTitle,
} from "@/lib/task-agent";
import { normalizeTaskTitle } from "@/components/tasks/task-form-utils";
import { shouldPrepareTaskForAgent } from "@/components/tasks/task-detail/task-detail-utils";

/**
 * タスク詳細モーダルのエージェント実行・トリアージ処理をまとめた hook。
 * state（task / editTitle / editDescription）は呼び出し側が所有する。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskAgentActions({
  task,
  editTitle,
  editDescription,
  effectiveTaskId,
  onOpenChange,
  setTask,
  fetchTask,
}: {
  task: Task | null;
  editTitle: string;
  editDescription: string;
  effectiveTaskId: string | null;
  onOpenChange: (open: boolean) => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
  fetchTask: () => Promise<void>;
}) {
  const router = useRouter();
  const [launchingAgent, setLaunchingAgent] = useState(false);
  const [triagingAgent, setTriagingAgent] = useState(false);

  const handleRunWithAgent = useCallback(async () => {
    if (!task) return;

    const normalizedTitle = normalizeTaskTitle(editTitle || task.title);
    if (!normalizedTitle) {
      toast.error("Task title is required.");
      return;
    }

    const taskSnapshot: Task = {
      ...task,
      title: normalizedTitle,
      description: editDescription.trim() || null,
    };

    setLaunchingAgent(true);
    try {
      let launchTask = taskSnapshot;
      if (effectiveTaskId && shouldPrepareTaskForAgent(task.metadata)) {
        setTriagingAgent(true);
        try {
          const result = await taskApi.runAgentTriage(effectiveTaskId);
          const metadata = {
            ...(launchTask.metadata || {}),
            ...result.metadata,
          };
          launchTask = { ...launchTask, metadata };
          setTask((prev) =>
            prev
              ? ({
                  ...prev,
                  metadata,
                } as Task)
              : prev,
          );
        } finally {
          setTriagingAgent(false);
        }
      }

      const created = await chatApi.createSession(
        await chatApi.getCurrentCharacterName(),
        launchTask.project_id || undefined,
      );
      const sessionId = created.session.id;

      await chatApi.updateSessionTitle(
        sessionId,
        buildTaskAgentSessionTitle(normalizedTitle),
      );
      await chatApi.dispatchMessage(sessionId, {
        message: buildTaskAgentPrompt(launchTask),
        project_id: launchTask.project_id || undefined,
        generation_profile: "assisted_work",
      });

      onOpenChange(false);
      router.push(`/chat?s=${sessionId}`);
    } catch (err) {
      console.error("Failed to start task agent", err);
      toast.error("Failed to start the task agent.");
    } finally {
      setLaunchingAgent(false);
    }
  }, [editDescription, editTitle, effectiveTaskId, onOpenChange, router, task, setTask]);

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
    launchingAgent,
    triagingAgent,
    handleRunWithAgent,
    handleRunAgentTriage,
  };
}
