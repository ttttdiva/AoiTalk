"use client";

import { useEffect, useMemo, useState } from "react";
import {
  computeStoryDiff,
  type StoryDiffGranularity,
  type StoryDiffOperation,
  type StoryDiffWorkerResponse,
} from "@/lib/story/diff.worker";

type DiffRequest = {
  oldBody: string;
  newBody: string;
  granularity: StoryDiffGranularity;
};

type DiffResult = {
  request: DiffRequest;
  operations: StoryDiffOperation[];
};

/**
 * 差分計算を Web Worker へ逃がすフック（設計 §6.5）。
 *
 * Worker を持たない環境（SSR・テスト・非対応ブラウザ）では同期計算へフォールバックする。
 * 同期フォールバックは effect ではなくレンダー時の useMemo で行い、
 * effect 内での同期 setState（cascading render）を避けている。
 */
export function useDiffWorker(
  oldBody: string,
  newBody: string,
  granularity: StoryDiffGranularity,
) {
  const request = useMemo<DiffRequest>(
    () => ({ oldBody, newBody, granularity }),
    [oldBody, newBody, granularity],
  );

  const inlineOperations = useMemo(() => {
    // SSR 中は Worker 版と同じ「計算中」を描画してハイドレーション差分を出さない。
    if (typeof window === "undefined") return null;
    if (typeof Worker !== "undefined") return null;
    return computeStoryDiff(request.oldBody, request.newBody, request.granularity);
  }, [request]);

  const [resolved, setResolved] = useState<DiffResult | null>(null);

  useEffect(() => {
    if (typeof Worker === "undefined") return;
    let active = true;
    const worker = new Worker(new URL("../../../lib/story/diff.worker.ts", import.meta.url));
    const settle = (operations: StoryDiffOperation[]) => {
      if (!active) return;
      active = false;
      setResolved({ request, operations });
      worker.terminate();
    };
    worker.onmessage = (event: MessageEvent<StoryDiffWorkerResponse>) => settle(event.data.operations);
    worker.onerror = () => settle(computeStoryDiff(request.oldBody, request.newBody, request.granularity));
    worker.postMessage({ oldBody: request.oldBody, newBody: request.newBody, granularity: request.granularity });
    return () => {
      active = false;
      worker.terminate();
    };
  }, [request]);

  const operations = inlineOperations ?? (resolved && resolved.request === request ? resolved.operations : null);
  return { operations: operations ?? [], pending: operations === null };
}
