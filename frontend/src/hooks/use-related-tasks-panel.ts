"use client";

import { useCallback, useEffect, useState } from "react";
import type { RelatedTaskSummary } from "@/components/chat/related-information-panel";

type UseRelatedTasksPanelArgs = {
  activeSessionId: string | null;
  isMobile: boolean;
  hasScenarioPanel: boolean;
};

/**
 * チャット右レール（関連情報パネル・関連タスク数・モバイルシート開閉）を担うフック。
 * `page.tsx` 由来のロジックを挙動不変で移設したもの。依存配列は元コードと同一に保つ。
 */
export function useRelatedTasksPanel({
  activeSessionId,
  isMobile,
  hasScenarioPanel,
}: UseRelatedTasksPanelArgs) {
  const [relatedPanelOpen, setRelatedPanelOpen] = useState(false);
  const [relatedTaskCount, setRelatedTaskCount] = useState(0);
  const [selectedRelatedTaskId, setSelectedRelatedTaskId] = useState<
    string | null
  >(null);
  const [mobileRailOpen, setMobileRailOpen] = useState(false);
  // hasScenarioPanel が false→true に変化したらモバイルレールを開く。
  // 旧: useEffect 内の同期 setState を、React 標準の「描画中に前回値と比較」パターンへ移設。
  // 初期比較値を false とすることで、マウント時に true の場合も開く元挙動を保持する。
  const [prevHasScenarioPanel, setPrevHasScenarioPanel] = useState(false);
  if (hasScenarioPanel !== prevHasScenarioPanel) {
    setPrevHasScenarioPanel(hasScenarioPanel);
    if (hasScenarioPanel) setMobileRailOpen(true);
  }

  useEffect(() => {
    const refreshRelatedTaskCount = async () => {
      if (!activeSessionId) {
        setRelatedTaskCount(0);
        return;
      }
      try {
        const response = await fetch(
          `/api/conversations/${encodeURIComponent(activeSessionId)}/related-tasks`,
          { credentials: "include", cache: "no-store" },
        );
        if (!response.ok) return;
        const data = (await response.json()) as { tasks?: unknown[] };
        setRelatedTaskCount(Array.isArray(data.tasks) ? data.tasks.length : 0);
      } catch {
        // 関連情報は補助表示のため、取得失敗でチャット全体を止めない。
      }
    };
    void refreshRelatedTaskCount();
    const interval = window.setInterval(() => void refreshRelatedTaskCount(), 5000);
    window.addEventListener("aoitalk-task-updated", refreshRelatedTaskCount);
    window.addEventListener("aoitalk-task-created", refreshRelatedTaskCount);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("aoitalk-task-updated", refreshRelatedTaskCount);
      window.removeEventListener("aoitalk-task-created", refreshRelatedTaskCount);
    };
  }, [activeSessionId]);

  const handleRelatedPanelToggle = useCallback(() => {
    setRelatedPanelOpen((open) => {
      const next = !open;
      if (isMobile) setMobileRailOpen(next);
      return next;
    });
  }, [isMobile]);

  const handleRelatedTasksChange = useCallback(
    (tasks: RelatedTaskSummary[]) => setRelatedTaskCount(tasks.length),
    [],
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
