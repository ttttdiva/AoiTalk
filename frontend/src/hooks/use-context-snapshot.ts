"use client";

import { useCallback, useEffect, useState, type RefObject } from "react";
import { chatApi, type ContextSnapshot } from "@/lib/chat-api";

type UseContextSnapshotArgs = {
  activeSessionId: string | null;
  activeSessionIdRef: RefObject<string | null>;
  /** snapshot 再取得の依存に含める観測値（挙動不変のため元の依存配列を維持）。 */
  includeProjectContext: boolean;
  llmMode: string;
  messagesLength: number;
  liveToolResultsLength: number;
  chatBusy: boolean;
};

/**
 * コンテキスト Snapshot の取得・再取得を担うフック。
 * `page.tsx` の該当ロジックを挙動不変で移設したもの。
 */
export function useContextSnapshot({
  activeSessionId,
  activeSessionIdRef,
  includeProjectContext,
  llmMode,
  messagesLength,
  liveToolResultsLength,
  chatBusy,
}: UseContextSnapshotArgs) {
  const [contextSnapshot, setContextSnapshot] =
    useState<ContextSnapshot | null>(null);
  const [contextSnapshotStatus, setContextSnapshotStatus] = useState("idle");

  const refreshContextSnapshot = useCallback(
    // "loading" 設定を取得ライフサイクル（promise チェーン）内へ移し、effect から
    // 同期 setState を呼ばないようにする（react-hooks/set-state-in-effect 対策）。
    // effect は paint 後に実行され loading 表示は元々非同期のため、挙動は不変。
    (sessionId: string) =>
      Promise.resolve()
        .then(() => {
          setContextSnapshotStatus("loading");
          return chatApi.getContextSnapshot(sessionId);
        })
        .then((result) => {
          if (activeSessionIdRef.current !== sessionId) return;
          setContextSnapshot(result.snapshot ?? null);
          setContextSnapshotStatus(
            result.status ?? (result.snapshot ? "available" : "unavailable"),
          );
        })
        .catch((err) => {
          if (activeSessionIdRef.current !== sessionId) return;
          console.warn("コンテキストSnapshotの取得に失敗:", err);
          setContextSnapshot(null);
          setContextSnapshotStatus("unavailable");
        }),
    [activeSessionIdRef],
  );

  // セッション ID の変化に応じた状態リセットを、React 標準の「描画中に前回値と比較」
  // パターンで行う（旧: useEffect 内の同期 setState を移設）。前回値の初期を undefined の
  // sentinel にすることで、初回描画（マウント）でも比較が発火し、セッション無しなら
  // "unavailable" にリセットする元挙動を保持する。
  const [prevActiveSessionId, setPrevActiveSessionId] = useState<
    string | null | undefined
  >(undefined);
  if (activeSessionId !== prevActiveSessionId) {
    setPrevActiveSessionId(activeSessionId);
    if (!activeSessionId) {
      setContextSnapshot(null);
      setContextSnapshotStatus("unavailable");
    }
  }

  useEffect(() => {
    if (!activeSessionId) return;
    void refreshContextSnapshot(activeSessionId);
  }, [
    activeSessionId,
    includeProjectContext,
    llmMode,
    messagesLength,
    liveToolResultsLength,
    chatBusy,
    refreshContextSnapshot,
  ]);

  return { contextSnapshot, contextSnapshotStatus };
}
