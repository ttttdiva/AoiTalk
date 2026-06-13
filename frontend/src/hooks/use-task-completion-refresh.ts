"use client";

import { useEffect } from "react";
import { TASK_COMPLETION_REFRESH_EVENT } from "@/lib/task-completion-undo";

export function useTaskCompletionRefresh(refresh: () => void | Promise<void>) {
  useEffect(() => {
    const handler = () => {
      void refresh();
    };
    window.addEventListener(TASK_COMPLETION_REFRESH_EVENT, handler);
    return () => {
      window.removeEventListener(TASK_COMPLETION_REFRESH_EVENT, handler);
    };
  }, [refresh]);
}
