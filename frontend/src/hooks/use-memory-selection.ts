"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { chatApi, type ConversationMessage, type MemorySelectionResponse } from "@/lib/chat-api";
import type { GenerationLifecycle } from "@/lib/chat-generation-state";
import {
  memorySelectionStateForGeneration, observeMemorySelectionEvent,
  parseMemorySelectionObservation, type MemorySelectionState,
} from "@/lib/memory-selection-state";

/** Read-only projection; the existing chat dispatcher owns every model/tool call. */
export function useMemorySelection({ sessionId, lifecycle, messages, enabled, busy, toolRevision }: {
  sessionId: string | null; lifecycle: GenerationLifecycle;
  messages: ConversationMessage[]; enabled: boolean; busy: boolean; toolRevision: number;
}) {
  const lifecycleRef = useRef(lifecycle);
  useEffect(() => { lifecycleRef.current = lifecycle; }, [lifecycle]);
  const [events, setEvents] = useState<MemorySelectionState | null>(null);
  const state = useMemo(() => memorySelectionStateForGeneration(events, lifecycle), [events, lifecycle]);
  const onEvent = useCallback((message: Record<string, unknown>) => {
    setEvents(previous => observeMemorySelectionEvent(previous, lifecycleRef.current, message));
  }, []);
  // Never take an older assistant observation when a new user turn owns the view.
  const latest = messages.filter(message => message.is_active_branch !== false).at(-1);
  const currentRun = lifecycle.sessionId === sessionId && lifecycle.phase !== "idle"
    ? lifecycle.agentRunId : null;
  const messageId = !(busy && !currentRun) && latest?.role === "assistant" && !latest.metadata?.fork_source_message_id
    && (!currentRun || latest.metadata?.agent_run_id === currentRun) ? latest.id : null;
  const selectorKey = sessionId && (currentRun || messageId)
    ? `${sessionId}:${currentRun ?? ""}:${messageId ?? ""}` : null;
  const [read, setRead] = useState<{
    key: string; requestKey: string; status: "available" | "unavailable" | "error"; data: MemorySelectionResponse | null;
  } | null>(null);
  const requestKey = `${selectorKey ?? ""}:${state.lastSequence ?? ""}:${state.history.length}:${toolRevision}`;
  useEffect(() => {
    if (!enabled || !sessionId || !selectorKey) return;
    const controller = new AbortController();
    void chatApi.getMemorySelection(sessionId, {
      ...(currentRun ? { agentRunId: currentRun } : {}),
      ...(messageId ? { messageId } : {}),
    }, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      const binding = data.binding;
      const matches = binding.session_id === sessionId && binding.active_branch
        && (!currentRun || binding.agent_run_id === currentRun)
        && (!messageId || binding.message_id === messageId);
      setRead({ key: selectorKey, requestKey, status: matches ? data.status : "unavailable", data: matches ? data : null });
    }).catch(() => {
      if (!controller.signal.aborted) setRead({ key: selectorKey, requestKey, status: "error", data: null });
    });
    return () => controller.abort();
  }, [enabled, sessionId, selectorKey, currentRun, messageId, requestKey]);
  const matchingRead = read?.key === selectorKey ? read : null;
  const projection = matchingRead?.status === "available" ? matchingRead.data?.observation : null;
  const stages = useMemo(() => {
    const parsed: MemorySelectionState["stages"] = {};
    for (const row of Object.values(projection?.stages ?? {})) {
      const observation = parseMemorySelectionObservation(row);
      if (observation) parsed[observation.stage] = observation;
    }
    // The existing WS hook already checks the current connection. Old session
    // state cannot survive the active-session or immutable send epoch boundary.
    if (lifecycle.sessionId === sessionId) {
      for (const [stage, event] of Object.entries(state.stages)) {
        const key = stage as keyof typeof parsed;
        if (event && !(event.status === "running" && parsed[key] && parsed[key]?.status !== "running")) parsed[key] = event;
      }
    }
    return parsed;
  }, [projection, state.stages, lifecycle.sessionId, sessionId]);
  const history = useMemo(() => {
    const persisted = (projection?.history ?? []).flatMap(row => {
      const event = parseMemorySelectionObservation(row);
      return event ? [event] : [];
    });
    const live = lifecycle.sessionId === sessionId ? state.history : [];
    return persisted.length >= live.length ? persisted : live;
  }, [projection, state.history, lifecycle.sessionId, sessionId]);
  const recheckToolReference = useCallback(async (refKey: string, signal?: AbortSignal) => {
    if (!sessionId || !selectorKey) return null;
    const data = await chatApi.getMemorySelection(sessionId, {
      ...(currentRun ? { agentRunId: currentRun } : {}), ...(messageId ? { messageId } : {}),
    }, signal);
    if (data.binding.session_id !== sessionId || !data.binding.active_branch
        || (currentRun && data.binding.agent_run_id !== currentRun)
        || (messageId && data.binding.message_id !== messageId)) return null;
    return data.tool_recall?.items.find(item => item.ref_key === refKey) ?? null;
  }, [sessionId, selectorKey, currentRun, messageId]);
  const status: "loading" | "available" | "unavailable" | "error" = matchingRead?.status
    ?? (selectorKey && enabled ? "loading" : "unavailable");
  const liveObserved = lifecycle.sessionId === sessionId && state.history.length > 0
    && ["dispatching", "queued", "streaming", "tool"].includes(lifecycle.phase);
  // A prior partial GET can still contain no candidates after a newer WS stage.
  // Only a settled, scope-finished projection can establish an empty/ACL result.
  const itemsPending = Boolean(enabled && ((selectorKey && matchingRead?.requestKey !== requestKey)
    || (!selectorKey && busy) || projection?.stages.scope_selection?.status === "running"));
  return { onEvent, stages, history, liveObserved,
    itemsPending,
    items: projection?.items ?? [],
    toolRecall: matchingRead?.status === "available" ? matchingRead.data?.tool_recall ?? null : null,
    recheckToolReference,
    key: selectorKey ?? `${sessionId ?? "new"}:empty`,
    status };
}
