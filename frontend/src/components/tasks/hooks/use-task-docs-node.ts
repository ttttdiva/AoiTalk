"use client";

import { useCallback, useState } from "react";

import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { taskApi, type Task } from "@/lib/task-api";

/**
 * タスク詳細モーダルの Docsノート化・議事メモ化処理をまとめた hook。
 * ダイアログを閉じる処理（handleDialogOpenChange）は呼び出し側から受け取る。
 * 挙動は元の TaskDetailModal と完全一致させている。
 */
export function useTaskDocsNode({
  effectiveTaskId,
  task,
  handleDialogOpenChange,
  onTaskUpdated,
  setTask,
}: {
  effectiveTaskId: string | null;
  task: Task | null;
  handleDialogOpenChange: (nextOpen: boolean) => void;
  onTaskUpdated: () => void;
  setTask: React.Dispatch<React.SetStateAction<Task | null>>;
}) {
  const router = useRouter();
  const [docsNodeLoading, setDocsNodeLoading] = useState(false);

  const handleOpenDocsNode = useCallback(async () => {
    if (!effectiveTaskId || !task) return;
    if (task.knowledge_node_id) {
      handleDialogOpenChange(false);
      router.push(`/docs/${task.knowledge_node_id}`);
      return;
    }

    setDocsNodeLoading(true);
    try {
      const result = await taskApi.ensureDocsNode(effectiveTaskId);
      setTask((prev) =>
        prev ? { ...prev, knowledge_node_id: result.node.id } : prev,
      );
      onTaskUpdated();
      toast.success(
        result.created ? "Docsノートを作成しました" : "Docsノートを開きます",
      );
      handleDialogOpenChange(false);
      router.push(`/docs/${result.node.id}`);
    } catch (err) {
      console.error("Docsノート化に失敗しました:", err);
      toast.error("Docsノート化に失敗しました");
    } finally {
      setDocsNodeLoading(false);
    }
  }, [effectiveTaskId, handleDialogOpenChange, onTaskUpdated, router, task, setTask]);

  const handleOpenMeetingNote = useCallback(async () => {
    if (!effectiveTaskId || !task) return;
    setDocsNodeLoading(true);
    try {
      const response = await fetch(`/api/tasks/${effectiveTaskId}/meeting-note`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json() as { node: { id: string }; created: boolean };
      setTask((prev) =>
        prev ? { ...prev, knowledge_node_id: result.node.id } : prev,
      );
      onTaskUpdated();
      toast.success(result.created ? "議事メモを作成しました" : "議事メモを開きます");
      handleDialogOpenChange(false);
      router.push(`/docs/${result.node.id}`);
    } catch (err) {
      console.error("議事メモの作成に失敗しました:", err);
      toast.error("議事メモの作成に失敗しました");
    } finally {
      setDocsNodeLoading(false);
    }
  }, [effectiveTaskId, handleDialogOpenChange, onTaskUpdated, router, task, setTask]);

  return {
    docsNodeLoading,
    handleOpenDocsNode,
    handleOpenMeetingNote,
  };
}
