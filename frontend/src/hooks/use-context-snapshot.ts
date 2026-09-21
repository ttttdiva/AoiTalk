"use client";

import { useCallback, useEffect, useState, type RefObject } from "react";
import {
  chatApi,
  type ContextCapabilities,
  type ContextManifest,
  type ContextManifestInspector,
  type ContextSnapshot,
  type ContextSnapshotBinding,
} from "@/lib/chat-api";

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
  const [contextManifest, setContextManifest] =
    useState<ContextManifest | null>(null);
  const [contextInspector, setContextInspector] =
    useState<ContextManifestInspector | null>(null);
  const [contextCapabilities, setContextCapabilities] =
    useState<ContextCapabilities | null>(null);
  const [contextBinding, setContextBinding] =
    useState<ContextSnapshotBinding | null>(null);
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
          // The response carries an explicit server binding.  Prefer the
          // response's session/message IDs over provider metadata and reject
          // a late result whose binding no longer matches the active session.
          const binding = result.binding ?? result.turn_binding ?? null;
          const responseSessionId =
            binding?.session_id ?? result.session_id ?? result.snapshot?.session_id;
          if (responseSessionId && responseSessionId !== sessionId) {
            setContextSnapshot(null);
            setContextManifest(null);
            setContextInspector(null);
            setContextCapabilities(null);
            setContextBinding(null);
            setContextSnapshotStatus("unavailable");
            return;
          }
          if (
            binding &&
            binding.active_branch !== true &&
            (result.inspector || result.authorized_references?.length)
          ) {
            setContextSnapshot(null);
            setContextManifest(null);
            setContextInspector(null);
            setContextCapabilities(null);
            setContextBinding(null);
            setContextSnapshotStatus("unavailable");
            return;
          }
          const manifest = includeProjectContext
            ? (result.context_manifest ?? result.snapshot?.context_manifest ?? null)
            : null;
          const inspector = includeProjectContext
            ? (result.inspector ?? result.snapshot?.inspector ?? null)
            : null;
          const capabilities =
            result.capabilities ?? result.snapshot?.capabilities ?? null;
          const authorizedReferences = includeProjectContext
            ? (result.authorized_references ?? [])
            : [];
          const nextSnapshot = result.snapshot
            ? {
                ...result.snapshot,
                ...(manifest ? { context_manifest: manifest } : { context_manifest: null }),
                ...(inspector ? { inspector } : { inspector: null }),
                ...(capabilities ? { capabilities } : { capabilities: null }),
                authorized_references: authorizedReferences,
              }
            : null;
          setContextSnapshot(nextSnapshot);
          setContextManifest(manifest);
          setContextInspector(inspector);
          setContextCapabilities(capabilities);
          setContextBinding(binding);
          setContextSnapshotStatus(
            result.status ?? (nextSnapshot ? "available" : "unavailable"),
          );
        })
        .catch((err) => {
          if (activeSessionIdRef.current !== sessionId) return;
          console.warn("コンテキストSnapshotの取得に失敗:", err);
          setContextSnapshot(null);
          setContextManifest(null);
          setContextInspector(null);
          setContextCapabilities(null);
          setContextBinding(null);
          setContextSnapshotStatus("unavailable");
        }),
    [activeSessionIdRef, includeProjectContext],
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
    // Do not keep the previous session's manifest visible while the new
    // request is in flight.  This is especially important for forked chats,
    // where copied history intentionally has no current-turn Manifest.
    setContextSnapshot(null);
    setContextManifest(null);
    setContextInspector(null);
    setContextCapabilities(null);
    setContextBinding(null);
    setContextSnapshotStatus(activeSessionId ? "idle" : "unavailable");
  }

  const [prevIncludeProjectContext, setPrevIncludeProjectContext] = useState<
    boolean | undefined
  >(undefined);
  if (includeProjectContext !== prevIncludeProjectContext) {
    setPrevIncludeProjectContext(includeProjectContext);
    if (!includeProjectContext) {
      // Project Context can be toggled while a previous response is still
      // mounted. Remove the old Manifest synchronously so OFF never flashes
      // stale project/work evidence during the refresh.
      setContextManifest(null);
      setContextInspector(null);
      setContextSnapshot((previous) =>
        previous
          ? {
              ...previous,
              context_manifest: null,
              inspector: null,
              authorized_references: [],
            }
          : previous,
      );
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

  return {
    contextSnapshot,
    contextSnapshotStatus,
    contextManifest,
    contextInspector,
    contextCapabilities,
    contextBinding,
  };
}
