"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { RelatedTaskSummary } from "@/components/chat/related-information-panel";

type UseRelatedTasksPanelArgs = {
  activeSessionId: string | null;
  isMobile: boolean;
};

/**
 * Chat mobile drawer state and the task-count callback used by the single
 * mounted RelatedInformationPanel.  Task fetching/polling belongs to that
 * panel only; keeping it here would create a second request loop.
 */
export function useRelatedTasksPanel({
  activeSessionId,
  isMobile,
}: UseRelatedTasksPanelArgs) {
  const [relatedPanelOpen, setRelatedPanelOpen] = useState(false);
  const [relatedTaskCount, setRelatedTaskCount] = useState(0);
  const [selectedRelatedTaskId, setSelectedRelatedTaskId] = useState<
    string | null
  >(null);
  const [mobileRailOpen, setMobileRailOpen] = useState(false);
  const activeSessionIdRef = useRef(activeSessionId);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  const handleRelatedPanelToggle = useCallback(() => {
    setRelatedPanelOpen((open) => {
      const next = !open;
      if (isMobile) setMobileRailOpen(next);
      return next;
    });
  }, [isMobile]);

  const handleRelatedTasksChange = useCallback(
    (tasks: RelatedTaskSummary[]) => {
      // Panel のアンマウント後に返る旧 session の結果をバッジへ反映しない。
      if (activeSessionIdRef.current === activeSessionId) {
        setRelatedTaskCount(tasks.length);
      }
    },
    [activeSessionId],
  );

  const notifyTaskUpdated = useCallback(() => {
    window.dispatchEvent(new Event("aoitalk-task-updated"));
  }, []);

  return {
    relatedPanelOpen,
    setRelatedPanelOpen,
    relatedTaskCount,
    selectedRelatedTaskId,
    setSelectedRelatedTaskId,
    mobileRailOpen,
    setMobileRailOpen,
    handleRelatedPanelToggle,
    handleRelatedTasksChange,
    notifyTaskUpdated,
  };
}
