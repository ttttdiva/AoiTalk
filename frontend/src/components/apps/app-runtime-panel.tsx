"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { CircleAlert, Clock3, History, Loader2, WifiOff } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AppRuntimeRenderer } from "@/components/apps/app-runtime-renderer";
import { formatRuntimeStatus, formatRuntimeTimestamp, runtimeStatusExplanation, runtimeStatusLabel } from "@/components/apps/app-runtime-format";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { withBasePath } from "@/lib/base-path";
import { appsApi, type AppRuntimeAction, type AppRuntimeDeclaration, type AppRuntimeFile, type AppRuntimeLogs, type AppRuntimeRun } from "@/lib/apps-api";
import { explorerDownloadResource } from "@/lib/explorer-api";

type AppRuntimePanelProps = {
  appId: string;
  projectId?: string;
  canRun: boolean;
  onSwitchToLegacy: () => void;
  onUnavailable?: () => void;
};

const TERMINAL_STATUSES = new Set([
  "succeeded",
  "success",
  "failed",
  "error",
  "cancelled",
  "canceled",
  "completed",
  "timed_out",
  "rejected",
  "blocked",
]);
const ACTIVE_ACTION_STATUSES = new Set([
  "queued",
  "dispatching",
  "accepted",
  "starting",
  "running",
  "stopping",
  "unknown",
]);
const CONFIRMED_TERMINAL_STATUSES = new Set(["succeeded", "success", "failed", "error", "cancelled", "canceled", "completed", "timed_out", "rejected", "blocked"]);

type PendingRuntimeAction = {
  action_id: string;
  idempotency_key: string;
  created_at: number;
};

type RuntimeRequestGuard = {
  actionId: string;
  token: symbol;
  scopeKey: string;
  scopeGeneration: number;
};

function pendingStorageKey(appId: string, projectId?: string, userId?: string | null): string {
  const authScope = userId?.trim() || "anonymous";
  return `aoitalk:app-runtime:pending:v1:${encodeURIComponent(authScope)}:${encodeURIComponent(appId)}:${encodeURIComponent(projectId || "personal")}`;
}

function readPendingActions(appId: string, projectId?: string, userId?: string | null): PendingRuntimeAction[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(pendingStorageKey(appId, projectId, userId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const now = Date.now();
    return parsed.filter((item): item is PendingRuntimeAction => Boolean(
      item
      && typeof item === "object"
      && typeof (item as PendingRuntimeAction).action_id === "string"
      && typeof (item as PendingRuntimeAction).idempotency_key === "string"
      && typeof (item as PendingRuntimeAction).created_at === "number"
      && now - (item as PendingRuntimeAction).created_at < 24 * 60 * 60 * 1000,
    ));
  } catch {
    return [];
  }
}

function writePendingActions(appId: string, actions: PendingRuntimeAction[], projectId?: string, userId?: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (actions.length) window.localStorage.setItem(pendingStorageKey(appId, projectId, userId), JSON.stringify(actions));
    else window.localStorage.removeItem(pendingStorageKey(appId, projectId, userId));
  } catch {
    // Storage can be unavailable in private browsing. The in-memory fence still applies.
  }
}

function savePendingAction(appId: string, action: PendingRuntimeAction, projectId?: string, userId?: string | null): void {
  const next = readPendingActions(appId, projectId, userId).filter((item) => item.action_id !== action.action_id);
  next.push(action);
  writePendingActions(appId, next, projectId, userId);
}

function removePendingByKey(appId: string, idempotencyKey: string, projectId?: string, userId?: string | null): void {
  writePendingActions(appId, readPendingActions(appId, projectId, userId).filter((item) => item.idempotency_key !== idempotencyKey), projectId, userId);
}

function isTerminal(status?: string): boolean {
  return Boolean(status && TERMINAL_STATUSES.has(status.toLowerCase()));
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `app-runtime-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function runtimeErrorUnavailable(error: unknown): boolean {
  if (!isRecord(error) || error.status !== 409) return false;
  const detail = error.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return false;
  const nested = "detail" in detail && detail.detail && typeof detail.detail === "object" && !Array.isArray(detail.detail)
    ? detail.detail
    : detail;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return false;
  const code = "code" in nested && typeof nested.code === "string" ? nested.code : "";
  return code === "app_runtime_not_configured";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isDefinitiveHttpError(value: unknown): boolean {
  if (!isRecord(value) || typeof value.status !== "number") return false;
  return value.status < 500;
}

function runtimeLogsText(value: AppRuntimeLogs["logs"]): string {
  if (typeof value === "string") return value;
  if (!isRecord(value)) return "";
  if (typeof value.data === "string") return value.data;
  const entries = Array.isArray(value.entries) ? value.entries : [];
  return entries.map((entry) => isRecord(entry) && typeof entry.line === "string" ? entry.line : "").filter(Boolean).join("\n");
}

function isBrowserFile(value: unknown): value is File {
  return typeof File !== "undefined" && value instanceof File;
}

async function prepareRuntimeInput(
  appId: string,
  actionId: string,
  input: Record<string, unknown>,
  projectId?: string,
): Promise<Record<string, unknown>> {
  const prepare = async (value: unknown): Promise<unknown> => {
    if (isBrowserFile(value)) {
      const metadata = await appsApi.uploadRuntimeActionFile(appId, actionId, value, projectId);
      const fileId = metadata.id.trim();
      const filename = metadata.filename.trim();
      const contentType = metadata.content_type.trim();
      if (!fileId || !filename || !contentType || !Number.isInteger(metadata.size_bytes) || metadata.size_bytes < 0 || !/^[0-9a-f]{64}$/iu.test(metadata.sha256)) {
        throw new Error("アップロードしたファイルの検証済みmetadataを受け取れませんでした");
      }
      return {
        file_id: fileId,
        filename,
        content_type: contentType,
        size_bytes: metadata.size_bytes,
        sha256: metadata.sha256,
      };
    }
    if (Array.isArray(value)) return Promise.all(value.map((item) => prepare(item)));
    if (isRecord(value)) {
      const entries = await Promise.all(Object.entries(value).map(async ([key, child]) => [key, await prepare(child)] as const));
      return Object.fromEntries(entries);
    }
    return value;
  };
  const prepared = await prepare(input);
  return isRecord(prepared) ? prepared : {};
}

export function AppRuntimePanel({ appId, projectId, canRun, onSwitchToLegacy, onUnavailable }: AppRuntimePanelProps) {
  const currentUserId = useCurrentUserId();
  const authScope = currentUserId?.trim() || "anonymous";
  const runtimeScopeKey = `${appId}\u0000${projectId || ""}\u0000${authScope}`;
  const runtimeSWRKey = appId ? ["app-runtime", appId, projectId || "", authScope] as const : null;
  const runsSWRKey = appId ? ["app-runtime-runs", appId, projectId || "", authScope] as const : null;
  const { data, error, isLoading, mutate } = useSWR<AppRuntimeDeclaration>(
    runtimeSWRKey,
    () => appsApi.getRuntime(appId, projectId),
  );
  const { data: runsData, mutate: mutateRuns } = useSWR<AppRuntimeRun[]>(
    runsSWRKey,
    () => appsApi.listRuntimeRuns(appId, projectId, 20),
  );
  const declaration = data;
  const [online, setOnline] = useState(true);
  const [busyActionId, setBusyActionId] = useState<string | null>(null);
  const [run, setRun] = useState<AppRuntimeRun | null>(null);
  const [runFiles, setRunFiles] = useState<AppRuntimeFile[]>([]);
  const [pendingAction, setPendingAction] = useState<PendingRuntimeAction | null>(null);
  const [pendingReconcileState, setPendingReconcileState] = useState<"idle" | "checking" | "found" | "missing" | "error">("idle");
  const [logs, setLogs] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [stateScopeKey, setStateScopeKey] = useState(runtimeScopeKey);
  const scopeRef = useRef(runtimeScopeKey);
  const scopeGenerationRef = useRef(0);
  const pendingScopeRef = useRef(runtimeScopeKey);
  const [activeActionState, setActiveActionState] = useState<{ scopeKey: string; generation: number; runs: Record<string, string[]> }>({ scopeKey: runtimeScopeKey, generation: 0, runs: {} });
  const pendingActionRef = useRef(new Map<string, symbol>());
  const busyActionScopeRef = useRef<string | null>(runtimeScopeKey);
  const [activeRunRefreshNonce, setActiveRunRefreshNonce] = useState(0);
  const [activeRunRefreshState, setActiveRunRefreshState] = useState<{ scopeKey: string; generation: number; busy: boolean }>({ scopeKey: runtimeScopeKey, generation: 0, busy: false });
  const activeRunRefreshScopeRef = useRef<{ key: string; generation: number } | null>({ key: runtimeScopeKey, generation: 0 });
  const pollAttemptsRef = useRef(0);
  const activeRunIdRef = useRef<string | null>(null);
  const knownRunStatusRef = useRef(new Map<string, string>());
  const runIntentRef = useRef({ generation: 0, runId: null as string | null });
  const runtimeFileDownloadControllersRef = useRef(new Set<AbortController>());
  const [runRefreshNonce, setRunRefreshNonce] = useState(0);
  const unavailableNotifiedRef = useRef(false);
  const abortRuntimeFileDownloads = useCallback(() => {
    for (const controller of runtimeFileDownloadControllersRef.current) controller.abort();
    runtimeFileDownloadControllersRef.current.clear();
  }, []);

  if (scopeRef.current !== runtimeScopeKey) {
    abortRuntimeFileDownloads();
    scopeRef.current = runtimeScopeKey;
    scopeGenerationRef.current += 1;
    knownRunStatusRef.current.clear();
    activeRunIdRef.current = null;
    runIntentRef.current = { generation: runIntentRef.current.generation + 1, runId: null };
    // An in-flight request from the previous scope cannot keep the new
    // renderer busy. Its promise still owns its symbol and therefore cannot
    // clear a request started in the new scope when it settles.
    pendingActionRef.current.clear();
    busyActionScopeRef.current = null;
    activeRunRefreshScopeRef.current = null;
    unavailableNotifiedRef.current = false;
  }
  const scopeGeneration = scopeGenerationRef.current;
  const pendingScopeChanged = pendingScopeRef.current !== runtimeScopeKey;
  if (pendingScopeChanged) pendingScopeRef.current = runtimeScopeKey;

  useEffect(() => {
    if (stateScopeKey === runtimeScopeKey) return;
    setStateScopeKey(runtimeScopeKey);
    setBusyActionId(null);
    setRun(null);
    setRunFiles([]);
    setPendingAction(null);
    setPendingReconcileState("idle");
    setLogs(null);
    setRunError(null);
  }, [runtimeScopeKey, stateScopeKey]);

  useEffect(() => {
    const update = () => setOnline(typeof navigator === "undefined" ? true : navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  useEffect(() => () => abortRuntimeFileDownloads(), [abortRuntimeFileDownloads]);

  useEffect(() => {
    if (unavailableNotifiedRef.current) return;
    if (runtimeErrorUnavailable(error)) {
      unavailableNotifiedRef.current = true;
      onUnavailable?.();
    }
  }, [error, onUnavailable]);

  const actions = declaration?.actions || [];
  const latestRuns = useMemo(() => runsData || declaration?.latest_runs || [], [declaration?.latest_runs, runsData]);
  const availability = declaration?.availability;
  const unavailableReason = availability?.available === false ? availability.reason || "このAppは現在利用できません。" : null;
  const activeActionRuns = useMemo(() => activeActionState.scopeKey === runtimeScopeKey && activeActionState.generation === scopeGeneration ? activeActionState.runs : {}, [activeActionState.generation, activeActionState.runs, activeActionState.scopeKey, runtimeScopeKey, scopeGeneration]);
  const scopeStateReady = stateScopeKey === runtimeScopeKey;
  const currentRun = scopeStateReady ? run : null;
  const currentLogs = scopeStateReady ? logs : null;
  const currentRunFiles = scopeStateReady ? runFiles : [];
  const currentRunError = scopeStateReady ? runError : null;
  const currentPendingReconcileState = scopeStateReady ? pendingReconcileState : "idle";
  const pendingActionForScope = !scopeStateReady || pendingScopeChanged ? null : pendingAction;
  const currentBusyActionId = busyActionScopeRef.current === runtimeScopeKey ? busyActionId : null;
  const currentActiveRunRefreshBusy = activeRunRefreshState.scopeKey === runtimeScopeKey && activeRunRefreshState.generation === scopeGeneration && activeRunRefreshState.busy && activeRunRefreshScopeRef.current?.key === runtimeScopeKey && activeRunRefreshScopeRef.current.generation === scopeGeneration;
  const rendererBusyActionIds = new Set([
    ...Object.keys(activeActionRuns),
    ...(pendingActionForScope ? [pendingActionForScope.action_id] : []),
    ...(currentBusyActionId ? [currentBusyActionId] : []),
  ]);

  const observeRun = useCallback((candidate: AppRuntimeRun) => {
    if (scopeRef.current !== runtimeScopeKey || scopeGenerationRef.current !== scopeGeneration) return;
    const previousStatus = knownRunStatusRef.current.get(candidate.id);
    if (previousStatus && CONFIRMED_TERMINAL_STATUSES.has(previousStatus) && (ACTIVE_ACTION_STATUSES.has(candidate.status) || candidate.status === "unknown")) return;
    knownRunStatusRef.current.set(candidate.id, candidate.status);
    if (CONFIRMED_TERMINAL_STATUSES.has(candidate.status) && candidate.idempotency_key) {
      if (scopeRef.current === runtimeScopeKey && scopeGenerationRef.current === scopeGeneration) {
        removePendingByKey(appId, candidate.idempotency_key, projectId, authScope);
        setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
      }
    }
    setActiveActionState((currentState) => {
      if (scopeRef.current !== runtimeScopeKey || scopeGenerationRef.current !== scopeGeneration) return currentState;
      const sameScope = currentState.scopeKey === runtimeScopeKey && currentState.generation === scopeGeneration;
      const current = sameScope ? currentState.runs : {};
      const next = { ...current };
      const currentIds = next[candidate.action_id] || [];
      if (ACTIVE_ACTION_STATUSES.has(candidate.status) || candidate.status === "unknown") {
        if (!currentIds.includes(candidate.id)) next[candidate.action_id] = [...currentIds, candidate.id];
      } else if (CONFIRMED_TERMINAL_STATUSES.has(candidate.status)) {
        const remaining = currentIds.filter((runId) => runId !== candidate.id);
        if (remaining.length) next[candidate.action_id] = remaining;
        else delete next[candidate.action_id];
      }
      const currentKeys = Object.keys(current);
      const nextKeys = Object.keys(next);
      if (sameScope && currentKeys.length === nextKeys.length && currentKeys.every((key) => JSON.stringify(current[key]) === JSON.stringify(next[key]))) return currentState;
      return { scopeKey: runtimeScopeKey, generation: scopeGeneration, runs: next };
    });
  }, [appId, authScope, projectId, runtimeScopeKey, scopeGeneration]);

  const claimRunIntent = useCallback((runId: string): number => {
    abortRuntimeFileDownloads();
    const generation = runIntentRef.current.generation + 1;
    runIntentRef.current = { generation, runId };
    return generation;
  }, [abortRuntimeFileDownloads]);

  const refreshRun = useCallback(async (runId: string, expectedGeneration = runIntentRef.current.generation) => {
    try {
      const result = await appsApi.getRuntimeRun(appId, runId, projectId);
      if (scopeRef.current !== runtimeScopeKey || scopeGenerationRef.current !== scopeGeneration || runIntentRef.current.generation !== expectedGeneration || runIntentRef.current.runId !== runId) return null;
      if (activeRunIdRef.current !== result.run.id) {
        activeRunIdRef.current = result.run.id;
        setLogs(null);
        setRunFiles([]);
      }
      observeRun(result.run);
      setRun(result.run);
      return result.run;
    } catch (refreshError) {
      if (scopeRef.current === runtimeScopeKey && scopeGenerationRef.current === scopeGeneration && runIntentRef.current.generation === expectedGeneration && runIntentRef.current.runId === runId) {
        setRunError(refreshError instanceof Error ? refreshError.message : "実行状態を読み込めませんでした");
      }
      return null;
    }
  }, [appId, observeRun, projectId, runtimeScopeKey, scopeGeneration]);

  const selectRun = useCallback((runId: string) => {
    const generation = claimRunIntent(runId);
    void refreshRun(runId, generation);
  }, [claimRunIntent, refreshRun]);

  const reconcilePending = useCallback(async () => {
    if (!appId || !online) return;
    const pending = readPendingActions(appId, projectId, authScope);
    if (!pending.length) {
      setPendingAction(null);
      setPendingReconcileState("idle");
      return;
    }
    setPendingReconcileState("checking");
    let found = false;
    let failed = false;
    const reconcileGeneration = runIntentRef.current.generation;
    const reconcileScopeGeneration = scopeGeneration;
    for (const item of pending) {
      try {
        // Reconciliation is deliberately one exact GET. A transport failure
        // must never be turned into a second POST that could execute later.
        const matches = await appsApi.listRuntimeRuns(appId, projectId, 1, item.idempotency_key);
        const matching = matches.find((candidate) => candidate.idempotency_key === item.idempotency_key);
        if (matching) {
          if (CONFIRMED_TERMINAL_STATUSES.has(matching.status)) {
            if (scopeRef.current === runtimeScopeKey && scopeGenerationRef.current === reconcileScopeGeneration) {
              removePendingByKey(appId, item.idempotency_key, projectId, authScope);
            }
          }
          found = true;
          if (scopeRef.current === runtimeScopeKey && scopeGenerationRef.current === reconcileScopeGeneration && runIntentRef.current.generation === reconcileGeneration) {
            claimRunIntent(matching.id);
            activeRunIdRef.current = matching.id;
            setLogs(null);
            setRunFiles([]);
            observeRun(matching);
            setRun(matching);
          }
        }
      } catch {
        // Keep the key for the next reload/reconnect. The server remains the
        // source of truth for whether the original POST arrived.
        failed = true;
      }
    }
    if (scopeRef.current === runtimeScopeKey && scopeGenerationRef.current === reconcileScopeGeneration) {
      const remaining = readPendingActions(appId, projectId, authScope);
      setPendingAction(remaining[0] || null);
      setPendingReconcileState(remaining.length ? (failed ? "error" : "missing") : (found ? "found" : "idle"));
    }
  }, [appId, authScope, claimRunIntent, observeRun, online, projectId, runtimeScopeKey, scopeGeneration]);

  useEffect(() => {
    void reconcilePending();
  }, [reconcilePending]);

  useEffect(() => {
    const pending = readPendingActions(appId, projectId, authScope);
    setPendingAction(pending[0] || null);
    setPendingReconcileState(pending.length ? "idle" : "idle");
  }, [appId, authScope, projectId]);

  useEffect(() => {
    latestRuns.forEach(observeRun);
  }, [latestRuns, observeRun]);

  useEffect(() => {
    if (!online) return;
    const cleanups: Array<() => void> = [];
    for (const runId of Object.values(activeActionRuns).flat()) {
      if (currentRun?.id === runId) continue;
      let cancelled = false;
      let attempts = 0;
      let timer: number | undefined;
      const pollActiveRun = async () => {
        attempts += 1;
        try {
          const result = await appsApi.getRuntimeRun(appId, runId, projectId);
          if (cancelled || result.run.id !== runId) return;
          observeRun(result.run);
          if (ACTIVE_ACTION_STATUSES.has(result.run.status) && attempts < 60) {
            timer = window.setTimeout(() => void pollActiveRun(), 1200);
          }
        } catch {
          if (!cancelled && attempts < 60) {
            timer = window.setTimeout(() => void pollActiveRun(), 1200);
          } else if (!cancelled) {
            setRunError("実行中Actionの状態を自動確認できません。履歴からRunを選び、手動で再確認してください。");
          }
        }
      };
      timer = window.setTimeout(() => void pollActiveRun(), 800);
      cleanups.push(() => {
        cancelled = true;
        if (timer !== undefined) window.clearTimeout(timer);
      });
    }
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [activeActionRuns, activeRunRefreshNonce, appId, currentRun?.id, observeRun, online, projectId]);

  useEffect(() => {
    if (!currentRun?.id || isTerminal(currentRun.status) || !online) {
      pollAttemptsRef.current = 0;
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      pollAttemptsRef.current += 1;
      const next = await refreshRun(currentRun.id);
      if (!cancelled && next) setRunRefreshNonce((value) => value + 1);
      if (!cancelled && next && !isTerminal(next.status) && pollAttemptsRef.current < 60) {
        timer = window.setTimeout(() => void poll(), 1200);
      } else if (!cancelled && next && !isTerminal(next.status)) {
        setRunError("実行状態の自動監視を停止しました。再読込で最新状態を確認してください。");
      }
    };
    timer = window.setTimeout(() => void poll(), 800);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentRun?.id, currentRun?.status, online, refreshRun]);

  useEffect(() => {
    if (!currentRun?.id) {
      setLogs(null);
      setRunFiles([]);
      return;
    }
    if (!online) return;
    let cancelled = false;
    const loadLogs = () => appsApi.getRuntimeRunLogs(appId, currentRun.id, projectId).then((result) => {
      if (!cancelled) setLogs(runtimeLogsText(result.logs));
    }).catch(() => {
      if (!cancelled) setLogs(null);
    });
    void loadLogs();
    const retryTimer = isTerminal(currentRun.status) ? window.setTimeout(() => void loadLogs(), 1200) : undefined;
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [appId, currentRun?.finished_at, currentRun?.id, currentRun?.result, currentRun?.started_at, currentRun?.status, online, projectId, runRefreshNonce]);

  useEffect(() => {
    if (!currentRun?.id || !online) return;
    let cancelled = false;
    const loadFiles = () => appsApi.listRuntimeRunFiles(appId, currentRun.id, projectId).then((files) => {
      if (!cancelled) setRunFiles(files);
    }).catch(() => {
      // A run without output files is a normal terminal state.
      if (!cancelled) setRunFiles([]);
    });
    void loadFiles();
    const retryTimer = isTerminal(currentRun.status) ? window.setTimeout(() => void loadFiles(), 1200) : undefined;
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [appId, currentRun?.finished_at, currentRun?.id, currentRun?.result, currentRun?.started_at, currentRun?.status, online, projectId, runRefreshNonce]);

  const locateUncertainRun = useCallback(async (idempotencyKey: string, requestGuard: RuntimeRequestGuard): Promise<AppRuntimeRun | null> => {
    const expectedGeneration = runIntentRef.current.generation;
    const exactRuns = await appsApi.listRuntimeRuns(appId, projectId, 1, idempotencyKey);
    const matching = exactRuns.find((candidate) => candidate.idempotency_key === idempotencyKey) || null;
    if (matching) {
      if (CONFIRMED_TERMINAL_STATUSES.has(matching.status)) {
        if (scopeRef.current === requestGuard.scopeKey && scopeGenerationRef.current === requestGuard.scopeGeneration) {
          removePendingByKey(appId, idempotencyKey, projectId, authScope);
        }
      }
      const requestIsCurrent = () => scopeRef.current === requestGuard.scopeKey
        && scopeGenerationRef.current === requestGuard.scopeGeneration
        && pendingActionRef.current.get(requestGuard.actionId) === requestGuard.token;
      if (!requestIsCurrent() || runIntentRef.current.generation !== expectedGeneration) return matching;
      setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
      setPendingReconcileState("found");
      claimRunIntent(matching.id);
      activeRunIdRef.current = matching.id;
      setLogs(null);
      setRunFiles([]);
      observeRun(matching);
      setRun(matching);
    }
    return matching;
  }, [appId, authScope, claimRunIntent, observeRun, projectId]);

  const refreshKnownRuns = useCallback(async () => {
    if (!online || currentActiveRunRefreshBusy) return;
    const scopeAtEntry = runtimeScopeKey;
    const scopeGenerationAtEntry = scopeGeneration;
    const runIds = Object.values(activeActionRuns).flat();
    activeRunRefreshScopeRef.current = { key: scopeAtEntry, generation: scopeGenerationAtEntry };
    setActiveRunRefreshState({ scopeKey: scopeAtEntry, generation: scopeGenerationAtEntry, busy: true });
    try {
      const results = await Promise.allSettled(runIds.map((runId) => appsApi.getRuntimeRun(appId, runId, projectId)));
      if (scopeRef.current !== scopeAtEntry || scopeGenerationRef.current !== scopeGenerationAtEntry) return;
      let failed = false;
      results.forEach((result, index) => {
        if (result.status === "fulfilled" && result.value.run.id === runIds[index]) observeRun(result.value.run);
        else failed = true;
      });
      try {
        await mutateRuns();
      } catch {
        failed = true;
      }
      if (scopeRef.current === scopeAtEntry && scopeGenerationRef.current === scopeGenerationAtEntry) {
        setActiveRunRefreshNonce((value) => value + 1);
        if (failed) setRunError("一部の実行状態を確認できませんでした。自動確認を再開しています。");
      }
    } finally {
      if (scopeRef.current === scopeAtEntry && scopeGenerationRef.current === scopeGenerationAtEntry && activeRunRefreshScopeRef.current?.key === scopeAtEntry && activeRunRefreshScopeRef.current.generation === scopeGenerationAtEntry) {
        activeRunRefreshScopeRef.current = null;
        setActiveRunRefreshState({ scopeKey: scopeAtEntry, generation: scopeGenerationAtEntry, busy: false });
      }
    }
  }, [activeActionRuns, appId, currentActiveRunRefreshBusy, mutateRuns, observeRun, online, projectId, runtimeScopeKey, scopeGeneration]);

  const runAction = useCallback((action: AppRuntimeAction, input: Record<string, unknown>) => {
    if (!action.available || !canRun || !online) return;
    if (activeActionRuns[action.action_id]?.length) {
      setRunError("このActionは現在実行中です。完了後に明示的に再実行してください。");
      return;
    }
    if (pendingActionForScope?.action_id === action.action_id || readPendingActions(appId, projectId, authScope).some((item) => item.action_id === action.action_id)) {
      setRunError("前回の実行要求を確認中です。状態を再読込してから、重複の可能性を了承して明示的に再実行してください。");
      return;
    }
    const pending = pendingActionRef.current.get(action.action_id);
    if (pending) return;
    const idempotencyKey = newIdempotencyKey();
    const scopeAtEntry = runtimeScopeKey;
    const requestToken = Symbol(action.action_id);
    const scopeGenerationAtEntry = scopeGeneration;
    const isCurrentScope = () => scopeRef.current === scopeAtEntry && scopeGenerationRef.current === scopeGenerationAtEntry;
    const requestGuard: RuntimeRequestGuard = { actionId: action.action_id, token: requestToken, scopeKey: scopeAtEntry, scopeGeneration: scopeGenerationAtEntry };
    const isCurrentRequest = () => isCurrentScope() && pendingActionRef.current.get(action.action_id) === requestToken;
    savePendingAction(appId, { action_id: action.action_id, idempotency_key: idempotencyKey, created_at: Date.now() }, projectId, authScope);
    setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
    setPendingReconcileState("idle");
    void (async () => {
      if (isCurrentScope()) {
        setBusyActionId(action.action_id);
        setRunError(null);
      }
      let postStarted = false;
      try {
        const preparedInput = await prepareRuntimeInput(appId, action.action_id, input, projectId);
        if (!isCurrentScope()) {
          return;
        }
        try {
          postStarted = true;
          const response = await appsApi.runRuntimeAction(appId, action.action_id, preparedInput, { idempotencyKey, projectId });
          if (CONFIRMED_TERMINAL_STATUSES.has(response.run.status)) {
            if (isCurrentScope()) {
              removePendingByKey(appId, idempotencyKey, projectId, authScope);
            }
          }
          if (!isCurrentScope()) return;
          setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
          claimRunIntent(response.run.id);
          observeRun(response.run);
          setRun(response.run);
          setRunFiles([]);
          setLogs(null);
          if (["queued", "dispatching", "accepted"].includes(response.run.status)) {
            toast.success("Appの実行要求を受理しました");
          } else if (["failed", "cancelled", "blocked"].includes(response.run.status)) {
            toast.error(`Appの実行は${runtimeStatusLabel(response.run.status)}です`);
          } else if (response.run.status === "unknown") {
            toast.warning("Appの実行状態を確認できません");
          } else {
            toast.message("Appの実行状態を表示しています");
          }
          await Promise.all([mutate(), mutateRuns()]);
        } catch (firstError) {
          if (!isCurrentScope()) {
            return;
          }
          if (!postStarted || isDefinitiveHttpError(firstError)) throw firstError;
          const observed = await locateUncertainRun(idempotencyKey, requestGuard);
          if (!isCurrentRequest()) return;
          if (observed) return;
          setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
          setPendingReconcileState("missing");
          throw new Error(`${firstError instanceof Error ? firstError.message : "通信に失敗しました"}。Actionは再送していません。再読込で状態を確認してください。`);
        }
      } catch (actionError) {
        if (!isCurrentScope()) {
          return;
        }
        if (!postStarted || isDefinitiveHttpError(actionError)) {
          removePendingByKey(appId, idempotencyKey, projectId, authScope);
          setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
        } else {
          setPendingAction(readPendingActions(appId, projectId, authScope)[0] || null);
          setPendingReconcileState("error");
        }
        setRunError(actionError instanceof Error ? actionError.message : "Appを実行できませんでした");
      } finally {
        if (pendingActionRef.current.get(action.action_id) === requestToken) {
          pendingActionRef.current.delete(action.action_id);
          if (isCurrentScope()) {
            busyActionScopeRef.current = null;
            setBusyActionId(null);
          }
        }
      }
    })();
    busyActionScopeRef.current = scopeAtEntry;
    pendingActionRef.current.set(action.action_id, requestToken);
  }, [activeActionRuns, appId, authScope, canRun, claimRunIntent, locateUncertainRun, mutate, mutateRuns, observeRun, online, pendingActionForScope, projectId, runtimeScopeKey, scopeGeneration]);

  const releasePending = useCallback(() => {
    if (!pendingActionForScope || (currentPendingReconcileState !== "missing" && currentPendingReconcileState !== "error")) return;
    removePendingByKey(appId, pendingActionForScope.idempotency_key, projectId, authScope);
    setPendingAction(null);
    setPendingReconcileState("idle");
    setRunError("前回要求の保護を解除しました。実行が遅れて届く可能性を確認したうえで、必要な場合だけ再実行してください。");
  }, [appId, authScope, currentPendingReconcileState, pendingActionForScope, projectId]);

  const downloadFile = useCallback(async (file: AppRuntimeFile) => {
    if (!currentRun?.id) return;
    const runIdAtEntry = currentRun.id;
    const runIntentGenerationAtEntry = runIntentRef.current.generation;
    const scopeAtEntry = runtimeScopeKey;
    const scopeGenerationAtEntry = scopeGeneration;
    const isCurrentDownload = () => scopeRef.current === scopeAtEntry
      && scopeGenerationRef.current === scopeGenerationAtEntry
      && runIntentRef.current.generation === runIntentGenerationAtEntry
      && runIntentRef.current.runId === runIdAtEntry;
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    const downloadUrl = withBasePath(
      `/api/python-proxy/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runIdAtEntry)}/files/${encodeURIComponent(file.artifact_id)}/download${query}`,
    );
    const controller = new AbortController();
    runtimeFileDownloadControllersRef.current.add(controller);
    try {
      // Keep the authenticated proxy URL. The canonical download_url is
      // metadata only and is never navigated to directly.
      await explorerDownloadResource(downloadUrl, file.filename, {
        requireAttachment: true,
        rejectRedirects: true,
        signal: controller.signal,
      });
    } catch (downloadError) {
      if (!controller.signal.aborted && isCurrentDownload()) setRunError(downloadError instanceof Error ? downloadError.message : "ファイルを取得できませんでした");
    } finally {
      runtimeFileDownloadControllersRef.current.delete(controller);
    }
  }, [appId, currentRun?.id, projectId, runtimeScopeKey, scopeGeneration]);

  if (isLoading) return <Card className="border-border/80 bg-card/80"><CardContent className="flex items-center gap-2 p-5 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />利用画面を読み込み中…</CardContent></Card>;
  if (error && !runtimeErrorUnavailable(error)) return <Card className="border-destructive/30 bg-destructive/5"><CardContent className="space-y-3 p-4"><p className="flex items-center gap-2 text-sm text-destructive"><CircleAlert className="size-4" />宣言UIを読み込めませんでした: {error instanceof Error ? error.message : "不明なエラー"}</p><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => void mutate()}>再読み込み</Button><Button type="button" size="sm" variant="ghost" onClick={onSwitchToLegacy}>従来のTarget画面を使う</Button></div></CardContent></Card>;
  if (runtimeErrorUnavailable(error)) return null;
  if (!declaration) return null;
  if (!declaration.ui?.root) return <Card className="border-destructive/30 bg-destructive/5"><CardContent className="space-y-3 p-4"><p className="flex items-center gap-2 text-sm text-destructive"><CircleAlert className="size-4" />このReleaseには利用画面の宣言UIがありません。Releaseの内容を確認してください。</p><Button type="button" size="sm" variant="outline" onClick={() => void mutate()}>再読み込み</Button></CardContent></Card>;

  return (
    <div className="space-y-4" data-testid="app-runtime-panel">
      <Card className="border-border/80 bg-card/80">
        <CardHeader className="flex-row items-start justify-between gap-3 border-b border-border/70">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base"><span>利用</span><Badge variant="outline" className="font-mono text-[10px]">{declaration.release?.version || "Release"}</Badge>{online ? null : <Badge variant="outline" className="gap-1 text-[10px]"><WifiOff className="size-3" />オフライン</Badge>}</CardTitle>
          </div>
          {Object.values(activeActionRuns).flat().length ? <Button type="button" size="sm" variant="outline" onClick={() => void refreshKnownRuns()} disabled={!online || currentActiveRunRefreshBusy}>{currentActiveRunRefreshBusy ? <Loader2 className="size-3.5 animate-spin" /> : null}実行中のRunを再確認</Button> : null}
          <Button type="button" size="sm" variant="outline" onClick={onSwitchToLegacy}>従来のTarget画面</Button>
        </CardHeader>
        <CardContent className="space-y-4 p-4">
          {unavailableReason ? <p className="rounded-md border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground">{unavailableReason}</p> : null}
          {!canRun ? <p className="rounded-md border border-border/70 bg-muted/20 p-3 text-xs text-muted-foreground">実行にはrunner以上の権限が必要です。</p> : null}
          {pendingActionForScope ? <div className="space-y-2 rounded-md border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground"><p>{currentPendingReconcileState === "checking" ? "前回の実行要求を同じ認証・App・Project範囲で再照合しています。" : currentPendingReconcileState === "missing" ? "この要求はまだ見つかりません。遅れて実行される可能性があるため、重複実行に注意してください。" : currentPendingReconcileState === "error" ? "状態を確認できませんでした。再照合するか、重複の可能性を確認して保護を解除してください。" : "前回の実行要求を確認しています。"}</p><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => void reconcilePending()} disabled={currentPendingReconcileState === "checking"}>{currentPendingReconcileState === "checking" ? <Loader2 className="size-3.5 animate-spin" /> : null}状態を再照合</Button>{(currentPendingReconcileState === "missing" || currentPendingReconcileState === "error") ? <Button type="button" size="sm" variant="ghost" onClick={releasePending}>保護を解除して再実行可能にする</Button> : null}</div></div> : null}
          {currentRunError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">{currentRunError}</p> : null}
          {currentRun ? <Card className="border-border/70 bg-background/30"><CardHeader className="border-b border-border/60 py-3"><CardTitle className="text-sm">実行状態</CardTitle></CardHeader><CardContent className="space-y-3 p-3"><dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2"><div><dt className="text-muted-foreground">状態</dt><dd className="font-medium">{runtimeStatusLabel(currentRun.status)} <span className="font-mono text-[11px] text-muted-foreground">({currentRun.status})</span></dd></div><div><dt className="text-muted-foreground">mode</dt><dd>{currentRun.mode}</dd></div><div><dt className="text-muted-foreground">readiness</dt><dd>{currentRun.readiness_status}</dd></div><div><dt className="text-muted-foreground">process</dt><dd>{currentRun.process_status}</dd></div><div><dt className="text-muted-foreground">終了コード</dt><dd>{currentRun.exit_code === null || currentRun.exit_code === undefined ? "—" : currentRun.exit_code}</dd></div><div><dt className="text-muted-foreground">作成</dt><dd>{formatRuntimeTimestamp(currentRun.created_at)}</dd></div><div><dt className="text-muted-foreground">開始</dt><dd>{formatRuntimeTimestamp(currentRun.started_at)}</dd></div><div><dt className="text-muted-foreground">終了</dt><dd>{formatRuntimeTimestamp(currentRun.finished_at)}</dd></div></dl>{runtimeStatusExplanation(currentRun.status) ? <p className="rounded-md border border-warning/30 bg-warning/5 p-2 text-xs text-muted-foreground">{runtimeStatusExplanation(currentRun.status)}</p> : null}</CardContent></Card> : null}
          <AppRuntimeRenderer key={declaration.release.id} ui={declaration.ui} actions={actions} context={{ run: currentRun, logs: currentLogs, files: currentRunFiles }} canRun={canRun && availability?.available !== false} online={online} busyActionId={currentBusyActionId || pendingActionForScope?.action_id} busyActionIds={rendererBusyActionIds} onRunAction={runAction} onDownloadFile={downloadFile} />
        </CardContent>
      </Card>
      {latestRuns.length ? <Card className="border-border/80 bg-card/80"><CardHeader className="border-b border-border/70"><CardTitle className="flex items-center gap-1.5 text-sm"><History className="size-4" />最近の実行</CardTitle></CardHeader><CardContent className="space-y-2 p-4">{latestRuns.slice(0, 8).map((item) => <button key={item.id} type="button" className={`flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors ${currentRun?.id === item.id ? "border-primary/50 bg-primary/5" : "border-border/70 bg-background/30 hover:bg-muted/30"}`} onClick={() => selectRun(item.id)}><span className="flex min-w-0 items-center gap-2"><Clock3 className="size-3.5 shrink-0 text-muted-foreground" /><span className="min-w-0 truncate">{item.action_id || item.id}<span className="ml-2 font-mono text-[10px] text-muted-foreground">release:{item.release_id}</span></span></span><span className="shrink-0 text-right text-muted-foreground">{formatRuntimeStatus(item)} · {formatRuntimeTimestamp(item.finished_at || item.started_at || item.created_at)}</span></button>)}</CardContent></Card> : null}
    </div>
  );
}

