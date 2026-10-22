import { useCallback, useEffect, useRef, useState } from "react";
import {
  localRuntimeReadinessView,
  type LocalRuntimeReadinessView,
  type LocalRuntimeStatusSnapshot,
} from "@/lib/local-runtime-status";

const DEFAULT_POLL_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 5000;

type SnapshotRecord = {
  key: string;
  value: LocalRuntimeStatusSnapshot;
};

type ErrorRecord = {
  key: string;
  message: string;
};

export type LocalRuntimeStatusState = {
  snapshot: LocalRuntimeStatusSnapshot | null;
  readiness: LocalRuntimeReadinessView | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
};

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return "ローカルモデルの状態取得がタイムアウトしました";
  }
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "ローカルモデルの状態を取得できませんでした";
}

async function readResponseError(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as
    | { detail?: unknown; message?: unknown; error?: unknown }
    | null;
  for (const value of [body?.detail, body?.message, body?.error]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return `ローカルモデルの状態取得に失敗しました (${response.status})`;
}

/**
 * Poll the selected managed local model's public readiness projection.
 *
 * This hook intentionally does not call prepare/start. It only observes the
 * backend-owned state so the ordinary Chat selector can explain cold starts
 * without mutating runtime/GPU state.
 */
export function useLocalRuntimeStatus({
  provider,
  model,
  runtime,
  enabled = true,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  provider?: string | null;
  model?: string | null;
  runtime?: string | null;
  enabled?: boolean;
  pollIntervalMs?: number;
}): LocalRuntimeStatusState {
  const providerId = provider?.trim().toLowerCase() ?? "";
  const modelId = model?.trim() ?? "";
  const runtimeId = runtime?.trim().toLowerCase() ?? "";
  const active = enabled && providerId === "openai_compatible_local" && Boolean(modelId);
  const requestKey = `${providerId}\u0000${modelId}\u0000${runtimeId}`;
  const [snapshotRecord, setSnapshotRecord] = useState<SnapshotRecord | null>(null);
  const [errorRecord, setErrorRecord] = useState<ErrorRecord | null>(null);
  const [inflightKey, setInflightKey] = useState<string | null>(null);
  const requestSeqRef = useRef(0);
  const requestInFlightRef = useRef(false);
  const requestControllerRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (!active || requestInFlightRef.current) return;
    const sequence = ++requestSeqRef.current;
    const requestKeyAtStart = requestKey;
    requestInFlightRef.current = true;
    setInflightKey(requestKeyAtStart);
    setErrorRecord(null);
    const query = new URLSearchParams({ model: modelId });
    const controller = new AbortController();
    requestControllerRef.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(
        `/api/python-proxy/llm/local-runtime/status?${query.toString()}`,
        {
          credentials: "include",
          cache: "no-store",
          signal: controller.signal,
        },
      );
      if (!response.ok) throw new Error(await readResponseError(response));
      const next = (await response.json()) as LocalRuntimeStatusSnapshot;
      if (sequence !== requestSeqRef.current) return;
      // A response for a previous selection must never become the current
      // model's readiness claim.
      if (
        (next.model && next.model.trim() !== modelId) ||
        (runtimeId && next.runtime && next.runtime.trim().toLowerCase() !== runtimeId)
      ) {
        setSnapshotRecord((previous) =>
          previous?.key === requestKeyAtStart ? null : previous,
        );
        return;
      }
      // Keep origin and value in one state update. A new selection can render
      // between request start and this response without relabeling an older
      // snapshot as belonging to the new model/runtime.
      setSnapshotRecord({ key: requestKeyAtStart, value: next });
      setErrorRecord(null);
    } catch (cause) {
      if (sequence !== requestSeqRef.current) return;
      setErrorRecord({ key: requestKeyAtStart, message: errorMessage(cause) });
    } finally {
      window.clearTimeout(timeout);
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
      }
      if (sequence === requestSeqRef.current) {
        requestInFlightRef.current = false;
        setInflightKey(null);
      }
    }
  }, [active, modelId, requestKey, runtimeId]);

  useEffect(() => {
    requestControllerRef.current?.abort();
    requestSeqRef.current += 1;
    requestInFlightRef.current = false;
    setSnapshotRecord(null);
    setErrorRecord(null);
    setInflightKey(null);
    if (!active) return;

    void refresh();
    const interval = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void refresh();
    }, Math.max(1000, pollIntervalMs));
    const onVisibilityChange = () => {
      if (document.hidden) return;
      void refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      requestControllerRef.current?.abort();
      requestControllerRef.current = null;
      requestSeqRef.current += 1;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [active, pollIntervalMs, refresh, runtimeId]);

  const selectionMatchesSnapshot = active && snapshotRecord?.key === requestKey;
  const currentSnapshot = selectionMatchesSnapshot && snapshotRecord
    ? snapshotRecord.value
    : null;
  const errorMatchesSelection = active && errorRecord?.key === requestKey;
  const currentError = errorMatchesSelection && errorRecord ? errorRecord.message : null;
  const loading = active && inflightKey === requestKey;
  const readiness = selectionMatchesSnapshot
    ? localRuntimeReadinessView(currentSnapshot, currentError)
    : errorMatchesSelection
      ? localRuntimeReadinessView(null, currentError)
    : loading
      ? { state: "unknown" as const, label: "状態を確認中" }
      : null;
  return {
    snapshot: currentSnapshot,
    readiness,
    loading,
    error: currentError,
    refresh,
  };
}
