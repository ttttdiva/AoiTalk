"use client";

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { chatApi, type AgentRun } from "@/lib/chat-api";
import { isTerminalAgentRunStatus } from "@/lib/agent-run-timeline-state";

const AGENT_RUN_POLL_INTERVAL_MS = 2500;

export type AgentRunResourceState = {
  run: AgentRun | null;
  error: string | null;
  loading: boolean;
};

type AgentRunSubscriber = {
  listener: () => void;
  poll: boolean;
  pollUntil: number | null;
};

type AgentRunEntry = {
  snapshot: AgentRunResourceState;
  subscribers: Set<AgentRunSubscriber>;
  request: Promise<void> | null;
  intervalId: number | null;
};

const EMPTY_AGENT_RUN_STATE: AgentRunResourceState = {
  run: null,
  error: null,
  loading: false,
};

const entries = new Map<string, AgentRunEntry>();

function getEntry(runId: string): AgentRunEntry {
  const existing = entries.get(runId);
  if (existing) return existing;

  const entry: AgentRunEntry = {
    snapshot: EMPTY_AGENT_RUN_STATE,
    subscribers: new Set(),
    request: null,
    intervalId: null,
  };
  entries.set(runId, entry);
  return entry;
}

function notify(entry: AgentRunEntry) {
  entry.subscribers.forEach(({ listener }) => listener());
}

function setSnapshot(
  entry: AgentRunEntry,
  next: AgentRunResourceState,
) {
  entry.snapshot = next;
  notify(entry);
}

function hasActivePoller(entry: AgentRunEntry): boolean {
  const now = Date.now();
  for (const subscriber of entry.subscribers) {
    if (!subscriber.poll) continue;
    if (subscriber.pollUntil !== null && now >= subscriber.pollUntil) {
      subscriber.poll = false;
      continue;
    }
    return true;
  }
  return false;
}

function stopPolling(entry: AgentRunEntry) {
  if (entry.intervalId === null) return;
  window.clearInterval(entry.intervalId);
  entry.intervalId = null;
}

function updatePolling(runId: string, entry: AgentRunEntry) {
  const shouldPoll =
    hasActivePoller(entry) &&
    !isTerminalAgentRunStatus(entry.snapshot.run?.status);

  if (!shouldPoll) {
    stopPolling(entry);
    return;
  }
  if (entry.intervalId !== null) return;

  entry.intervalId = window.setInterval(() => {
    if (
      !hasActivePoller(entry) ||
      isTerminalAgentRunStatus(entry.snapshot.run?.status)
    ) {
      stopPolling(entry);
      return;
    }
    void refreshAgentRun(runId);
  }, AGENT_RUN_POLL_INTERVAL_MS);
}

export function refreshAgentRun(runId: string | null | undefined) {
  if (!runId) return Promise.resolve();
  const entry = getEntry(runId);
  if (entry.request) return entry.request;

  setSnapshot(entry, {
    ...entry.snapshot,
    loading: true,
  });

  const request = (async () => {
    try {
      const response = await chatApi.getAgentRun(runId);
      setSnapshot(entry, {
        run: response.agent_run,
        error: null,
        loading: false,
      });
    } catch (error) {
      setSnapshot(entry, {
        ...entry.snapshot,
        error:
          error instanceof Error
            ? error.message
            : "実行ログを取得できませんでした",
        loading: false,
      });
    } finally {
      entry.request = null;
      updatePolling(runId, entry);
      if (entry.subscribers.size === 0 && entries.get(runId) === entry) {
        stopPolling(entry);
        entries.delete(runId);
      }
    }
  })();

  entry.request = request;
  return request;
}

function subscribeAgentRun(
  runId: string,
  listener: () => void,
  options: { poll: boolean; pollTimeoutMs: number | null },
) {
  const entry = getEntry(runId);
  const subscriber: AgentRunSubscriber = {
    listener,
    poll: options.poll,
    pollUntil:
      options.poll && options.pollTimeoutMs !== null
        ? Date.now() + options.pollTimeoutMs
        : null,
  };
  entry.subscribers.add(subscriber);
  updatePolling(runId, entry);

  return () => {
    entry.subscribers.delete(subscriber);
    updatePolling(runId, entry);
    if (entry.subscribers.size === 0 && entry.request === null) {
      stopPolling(entry);
      entries.delete(runId);
    }
  };
}

function getSnapshot(runId: string | null | undefined) {
  return runId ? getEntry(runId).snapshot : EMPTY_AGENT_RUN_STATE;
}

export function useAgentRun(
  runId: string | null | undefined,
  options: {
    poll?: boolean;
    pollTimeoutMs?: number | null;
  } = {},
) {
  const poll = Boolean(options.poll);
  const pollTimeoutMs = options.pollTimeoutMs ?? null;
  const subscribe = useCallback(
    (listener: () => void) =>
      runId
        ? subscribeAgentRun(runId, listener, { poll, pollTimeoutMs })
        : () => undefined,
    [poll, pollTimeoutMs, runId],
  );
  const readSnapshot = useCallback(() => getSnapshot(runId), [runId]);
  const snapshot = useSyncExternalStore(
    subscribe,
    readSnapshot,
    () => EMPTY_AGENT_RUN_STATE,
  );
  const refresh = useCallback(
    () => refreshAgentRun(runId),
    [runId],
  );

  useEffect(() => {
    if (!runId) return;
    void refresh();
  }, [refresh, runId]);

  return { ...snapshot, refresh };
}
