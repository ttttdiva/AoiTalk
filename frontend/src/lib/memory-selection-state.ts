import type { GenerationLifecycle } from "@/lib/chat-generation-state";
import {
  getWebSocketMessageAgentRunId,
  getWebSocketMessageClientMessageId,
  getWebSocketMessageEventKey,
  getWebSocketMessageSessionId,
} from "@/lib/chat-websocket-events";

/** Automatic scoped-memory selection has its own canonical index and ACL. */
export const MEMORY_SELECTION_EVENT = "memory_selection_progress";

export type MemorySelectionStage = "scope_selection" | "relevance_decision" | "context_budget";
export type MemorySelectionStatus =
  | "running"
  | "completed"
  | "disabled"
  | "skipped"
  | "blocked"
  | "failed"
  | "cancelled";

export type MemorySelectionObservation = {
  stage: MemorySelectionStage;
  status: MemorySelectionStatus;
  engine: "host" | "jev" | "eg2";
  reason?: string;
  candidateCount?: number;
  selectedCount?: number;
  excludedCount?: number;
};

/** Display-only state. No query, body, candidate ID, or provider payload lives here. */
export type MemorySelectionState = {
  generationKey: string | null;
  stages: Partial<Record<MemorySelectionStage, MemorySelectionObservation>>;
  history: readonly MemorySelectionObservation[];
  lastSequence: number | null;
  seenEventKeys: readonly string[];
};

const STAGES = new Set<MemorySelectionStage>([
  "scope_selection", "relevance_decision", "context_budget",
]);
const STATUSES = new Set<MemorySelectionStatus>([
  "running", "completed", "disabled", "skipped", "blocked", "failed", "cancelled",
]);
const TERMINAL_PHASES = new Set<GenerationLifecycle["phase"]>([
  "completed", "cancelled", "failed",
]);

function generationKey(lifecycle: GenerationLifecycle): string | null {
  if (!lifecycle.sessionId || lifecycle.generationEpoch == null) return null;
  // Learning the run ID after REST dispatch must not start a second animation.
  return `${lifecycle.sessionId}:epoch:${lifecycle.generationEpoch}`;
}

export function memorySelectionStateForGeneration(
  previous: MemorySelectionState | null,
  lifecycle: GenerationLifecycle,
): MemorySelectionState {
  const key = generationKey(lifecycle);
  if (previous && previous.generationKey === key) return previous;
  return { generationKey: key, stages: {}, history: [], lastSequence: null, seenEventKeys: [] };
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value : undefined;
}

/** Project a strictly additive event; an absent field stays unknown, never zero. */
export function parseMemorySelectionObservation(message: Record<string, unknown>): MemorySelectionObservation | null {
  if (message.schema_version !== 1 || message.pipeline !== "automatic_context_memory") return null;
  const stage = message.stage as MemorySelectionStage;
  const status = message.status as MemorySelectionStatus;
  if (!STAGES.has(stage) || !STATUSES.has(status)) return null;
  const engine = message.engine;
  if (engine !== "host" && engine !== "jev" && engine !== "eg2") return null;
  // A host fallback is never a successful provider/semantic evaluation.
  if (stage === "relevance_decision") {
    if ((status === "running" || status === "completed") && engine !== "jev") return null;
  } else if (stage === "scope_selection" && engine !== "host" && engine !== "eg2") {
    return null;
  } else if (stage === "context_budget" && engine !== "host") {
    return null;
  }
  const reason = typeof message.reason === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(message.reason)
    ? message.reason : undefined;
  return {
    stage, status, engine, ...(reason ? { reason } : {}),
    ...(count(message.candidate_count) !== undefined ? { candidateCount: count(message.candidate_count) } : {}),
    ...(count(message.selected_count) !== undefined ? { selectedCount: count(message.selected_count) } : {}),
    ...(count(message.excluded_count) !== undefined ? { excludedCount: count(message.excluded_count) } : {}),
  };
}

/**
 * Consume an already session/connection-vetted WS envelope in the existing chat
 * hook. This additional turn check rejects old-run progress during resend, Back,
 * or hydration. It never changes the generation reducer or delays the response.
 */
export function observeMemorySelectionEvent(
  previous: MemorySelectionState | null,
  lifecycle: GenerationLifecycle,
  message: Record<string, unknown>,
): MemorySelectionState {
  const state = memorySelectionStateForGeneration(previous, lifecycle);
  if (!state.generationKey || TERMINAL_PHASES.has(lifecycle.phase)) return state;
  if (message.type !== MEMORY_SELECTION_EVENT) return state;
  if (getWebSocketMessageSessionId(message) !== lifecycle.sessionId) return state;
  const runId = getWebSocketMessageAgentRunId(message);
  const clientId = getWebSocketMessageClientMessageId(message);
  if (lifecycle.agentRunId && runId && runId !== lifecycle.agentRunId) return state;
  if (lifecycle.clientMessageId && clientId && clientId !== lifecycle.clientMessageId) return state;
  const sameRun = Boolean(lifecycle.agentRunId && runId === lifecycle.agentRunId);
  const sameClient = Boolean(lifecycle.clientMessageId && clientId === lifecycle.clientMessageId);
  if (!sameRun && !sameClient) return state;

  const nested = message.data;
  const record = nested && typeof nested === "object" && !Array.isArray(nested)
    ? { ...(nested as Record<string, unknown>), ...message } : message;
  const eventKey = getWebSocketMessageEventKey(message);
  const sequence = count(record.event_sequence ?? record.sequence);
  if (eventKey && state.seenEventKeys.includes(eventKey)) return state;
  if (sequence !== undefined && state.lastSequence !== null && sequence <= state.lastSequence) return state;
  const next = parseMemorySelectionObservation(record);
  if (!next) return state;
  return {
    ...state,
    stages: { ...state.stages, [next.stage]: next },
    history: [...state.history, next].slice(-8),
    lastSequence: sequence ?? state.lastSequence,
    seenEventKeys: eventKey ? [...state.seenEventKeys, eventKey].slice(-64) : state.seenEventKeys,
  };
}
