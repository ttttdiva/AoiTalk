"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { useProject } from "@/contexts/project-context";
import { TaskDetailModal } from "@/components/tasks/task-detail-modal";

export function GlobalCreateTask() {
  const { selectedProjectId } = useProject();
  const [open, setOpen] = useState(false);
  const [initialProjectId, setInitialProjectId] = useState<string | null>(null);

  useEffect(() => {
    const handler = () => {
      if (!selectedProjectId) return;
      setInitialProjectId(selectedProjectId);
      setOpen(true);
    };
    window.addEventListener("global-create-task", handler);
    return () => window.removeEventListener("global-create-task", handler);
  }, [selectedProjectId]);

  const handleCreated = useCallback(() => {
    window.dispatchEvent(new Event("task-list-refresh"));
  }, []);

  const draftTask = useMemo(
    () => (initialProjectId ? { project_id: initialProjectId } : null),
    [initialProjectId],
  );

  if (!initialProjectId || !open) return null;

  return (
    <TaskDetailModal
      key={initialProjectId}
      taskId={null}
      draftTask={draftTask}
      open={true}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) {
          setOpen(false);
          setInitialProjectId(null);
        }
      }}
      onTaskUpdated={handleCreated}
    />
  );
}
