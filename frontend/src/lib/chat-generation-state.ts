import type { ConversationGenerationStatus } from "@/lib/chat-api";

export type GenerationPhase =
  | "idle"
  | "hydrating"
  | "unknown"
  | "dispatching"
  | "queued"
  | "streaming"
  | "tool"
  | "stopping"
  | "cancellation_pending"
  | "completed"
  | "cancelled"
  | "failed"
  | "cancellation_failed";

/**
 * `generationEpoch` is allocated by the client at dispatch time and never
 * changes when the server-side run id becomes known.  Keeping the three
 * correlation values on every lifecycle shape prevents a run id arriving
 * later from accidentally creating a second logical generation.
 */
export type GenerationLifecycle = {
  phase: GenerationPhase;
  sessionId: string | null;
  generationEpoch: number | null;
  clientMessageId: string | null;
  agentRunId: string | null;
  startedAt: string | null;
  statusMessage: string | null;
  activeTool: string | null;
  assistantMessageId: string | null;
  awaitingPersistence: boolean;
};

export type GenerationTerminalNotification = {
  key: string;
  sessionId: string;
  generationEpoch: number;
  clientMessageId: string | null;
  agentRunId: string | null;
  phase: "completed" | "cancelled" | "failed";
};

export type ChatGenerationState = {
  lifecycle: GenerationLifecycle;
  nextGenerationEpoch: number;
  lastTerminal: GenerationTerminalNotification | null;
  terminalKeys: readonly string[];
  seenEventIds: readonly string[];
};

type GenerationEventBase = {
  sessionId: string;
  eventId?: string | null;
  generationEpoch?: number | null;
  clientMessageId?: string | null;
  agentRunId?: string | null;
};

export type ChatGenerationEvent =
  | { type: "session_changed"; sessionId: string | null }
  | { type: "reset"; sessionId: string | null }
  /** A newly-created empty session is known idle before status hydration. */
  | { type: "session_initialized"; sessionId: string }
  | { type: "hydration_failed"; sessionId: string }
  | (GenerationEventBase & {
      type: "dispatch_started";
      clientMessageId: string;
      startedAt?: string | null;
    })
  | (GenerationEventBase & {
      type: "dispatch_accepted";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "status_restored";
      status: ConversationGenerationStatus;
    })
  | (GenerationEventBase & {
      type: "stream_started";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "tool_started";
      tool: string;
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "tool_finished";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "status_updated";
      status: string;
      activeTool?: string | null;
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "stop_requested";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "cancellation_pending";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "completed";
      statusMessage?: string | null;
      assistantMessageId?: string | null;
      awaitingPersistence?: boolean;
    })
  | (GenerationEventBase & {
      type: "cancelled";
      statusMessage?: string | null;
      assistantMessageId?: string | null;
    })
  | (GenerationEventBase & {
      type: "failed";
      statusMessage?: string | null;
      assistantMessageId?: string | null;
    })
  | (GenerationEventBase & {
      type: "cancellation_failed";
      statusMessage?: string | null;
    })
  | (GenerationEventBase & {
      type: "assistant_persisted";
      assistantMessageId: string;
    });

function emptyLifecycle(
  phase: "idle" | "hydrating" | "unknown",
  sessionId: string | null,
): GenerationLifecycle {
  return {
    phase,
    sessionId,
    generationEpoch: null,
    clientMessageId: null,
    agentRunId: null,
    startedAt: null,
    statusMessage: null,
    activeTool: null,
    assistantMessageId: null,
    awaitingPersistence: false,
  };
}

export const initialChatGenerationState: ChatGenerationState = {
  lifecycle: emptyLifecycle("idle", null),
  nextGenerationEpoch: 0,
  lastTerminal: null,
  terminalKeys: [],
  seenEventIds: [],
};

const TERMINAL_PHASES = new Set<GenerationPhase>([
  "completed",
  "cancelled",
  "failed",
]);

const ACTIVE_PHASES = new Set<GenerationPhase>([
  "dispatching",
  "queued",
  "streaming",
  "tool",
  "stopping",
  "cancellation_pending",
  "cancellation_failed",
]);

function terminalKey(lifecycle: GenerationLifecycle): string | null {
  if (!lifecycle.sessionId || lifecycle.generationEpoch == null) return null;
  // run/client identity may be learned after the first terminal path. The
  // locally-issued epoch is immutable, so enrichment must not create a second
  // queue-advance notification for the same generation.
  return `${lifecycle.sessionId}|epoch:${lifecycle.generationEpoch}`;
}

function eventAgentRunId(event: ChatGenerationEvent): string | null {
  if (event.type === "status_restored") {
    return event.status.agent_run_id ?? event.agentRunId ?? null;
  }
  return "agentRunId" in event ? event.agentRunId ?? null : null;
}

function eventClientMessageId(event: ChatGenerationEvent): string | null {
  if (event.type === "status_restored") {
    return event.status.client_message_id ?? event.clientMessageId ?? null;
  }
  return "clientMessageId" in event ? event.clientMessageId ?? null : null;
}

function eventHasMatchingClientCorrelation(
  lifecycle: GenerationLifecycle,
  event: ChatGenerationEvent,
): boolean {
  const incomingClientMessageId = eventClientMessageId(event);
  return Boolean(
    incomingClientMessageId &&
      lifecycle.clientMessageId === incomingClientMessageId,
  );
}

function eventHasMatchingEpoch(
  lifecycle: GenerationLifecycle,
  event: ChatGenerationEvent,
): boolean {
  if (!("generationEpoch" in event) || event.generationEpoch == null) {
    return false;
  }
  return lifecycle.generationEpoch === event.generationEpoch;
}

function matchesCurrentGeneration(
  lifecycle: GenerationLifecycle,
  event: ChatGenerationEvent,
): boolean {
  if (!("sessionId" in event) || lifecycle.sessionId !== event.sessionId) {
    return false;
  }
  if (
    "generationEpoch" in event &&
    event.generationEpoch != null &&
    lifecycle.generationEpoch != null &&
    event.generationEpoch !== lifecycle.generationEpoch
  ) {
    return false;
  }
  const incomingClientMessageId = eventClientMessageId(event);
  if (
    incomingClientMessageId &&
    lifecycle.clientMessageId &&
    incomingClientMessageId !== lifecycle.clientMessageId
  ) {
    return false;
  }

  const incomingRunId = eventAgentRunId(event);
  if (
    incomingRunId &&
    lifecycle.agentRunId &&
    incomingRunId !== lifecycle.agentRunId
  ) {
    return false;
  }

  // While a newly-dispatched generation has not been bound to a run, an
  // arbitrary run id can be a delayed event from the previous generation.
  // Only the local dispatch response or an envelope carrying the immutable
  // client/epoch correlation is allowed to perform the initial bind.
  if (
    incomingRunId &&
    !lifecycle.agentRunId &&
    lifecycle.generationEpoch != null &&
    event.type !== "dispatch_accepted" &&
    !eventHasMatchingClientCorrelation(lifecycle, event) &&
    !eventHasMatchingEpoch(lifecycle, event)
  ) {
    return false;
  }

  if (
    TERMINAL_PHASES.has(event.type as GenerationPhase) &&
    !incomingRunId &&
    !eventHasMatchingClientCorrelation(lifecycle, event) &&
    !eventHasMatchingEpoch(lifecycle, event)
  ) {
    return false;
  }
  if (
    event.type === "assistant_persisted" &&
    !incomingRunId &&
    !eventHasMatchingClientCorrelation(lifecycle, event) &&
    !eventHasMatchingEpoch(lifecycle, event) &&
    lifecycle.assistantMessageId !== event.assistantMessageId
  ) {
    return false;
  }
  return true;
}

function markEventSeen(
  state: ChatGenerationState,
  eventId: string | null | undefined,
): ChatGenerationState | null {
  if (!eventId) return state;
  if (state.seenEventIds.includes(eventId)) return null;
  const seenEventIds = [...state.seenEventIds, eventId];
  return {
    ...state,
    seenEventIds:
      seenEventIds.length > 256 ? seenEventIds.slice(-256) : seenEventIds,
  };
}

function activeLifecycle(
  state: ChatGenerationState,
  event: GenerationEventBase,
  phase: GenerationPhase,
  statusMessage: string | null,
  extra: Partial<GenerationLifecycle> = {},
): GenerationLifecycle {
  const current = state.lifecycle;
  const incomingRunId = eventAgentRunId(event as ChatGenerationEvent);
  return {
    ...current,
    phase,
    sessionId: event.sessionId,
    generationEpoch:
      current.generationEpoch ?? event.generationEpoch ?? state.nextGenerationEpoch,
    clientMessageId:
      eventClientMessageId(event as ChatGenerationEvent) ??
      current.clientMessageId,
    agentRunId: incomingRunId ?? current.agentRunId,
    startedAt: current.startedAt ?? new Date().toISOString(),
    statusMessage,
    activeTool: null,
    assistantMessageId: current.assistantMessageId,
    awaitingPersistence: current.awaitingPersistence,
    ...extra,
  };
}

function transitionTerminal(
  state: ChatGenerationState,
  event: Extract<
    ChatGenerationEvent,
    { type: "completed" | "cancelled" | "failed" }
  >,
): ChatGenerationState {
  if (!matchesCurrentGeneration(state.lifecycle, event)) return state;
  if (state.lifecycle.generationEpoch == null) return state;

  const nextLifecycle = activeLifecycle(
    state,
    event,
    event.type,
    event.statusMessage ??
      (event.type === "completed"
        ? "応答生成が完了しました"
        : event.type === "cancelled"
          ? "応答生成を停止しました"
          : "応答生成に失敗しました"),
    {
      assistantMessageId: event.assistantMessageId ?? null,
      awaitingPersistence:
        event.type === "completed"
          ? (event.awaitingPersistence ?? !event.assistantMessageId)
          : false,
    },
  );
  const key = terminalKey(nextLifecycle);
  if (!key) return state;
  if (state.terminalKeys.includes(key)) {
    const boundAgentRunId = state.lifecycle.agentRunId ?? nextLifecycle.agentRunId;
    if (boundAgentRunId === state.lifecycle.agentRunId) return state;
    return {
      ...state,
      lifecycle: { ...state.lifecycle, agentRunId: boundAgentRunId },
      lastTerminal:
        state.lastTerminal?.key === key
          ? { ...state.lastTerminal, agentRunId: boundAgentRunId }
          : state.lastTerminal,
    };
  }

  const terminalKeys = [...state.terminalKeys, key];
  return {
    ...state,
    lifecycle: nextLifecycle,
    lastTerminal: {
      key,
      sessionId: event.sessionId,
      generationEpoch: nextLifecycle.generationEpoch!,
      clientMessageId: nextLifecycle.clientMessageId,
      agentRunId: nextLifecycle.agentRunId,
      phase: event.type,
    },
    terminalKeys:
      terminalKeys.length > 128 ? terminalKeys.slice(-128) : terminalKeys,
  };
}

function restoreStatus(
  state: ChatGenerationState,
  event: Extract<ChatGenerationEvent, { type: "status_restored" }>,
): ChatGenerationState {
  if (event.status.session_id && event.status.session_id !== event.sessionId) {
    return state;
  }
  const status = event.status;
  const phase = String(status.status || "").toLowerCase();

  if (!status.running && phase === "idle") {
    if (
      ACTIVE_PHASES.has(state.lifecycle.phase) ||
      (TERMINAL_PHASES.has(state.lifecycle.phase) &&
        state.lifecycle.awaitingPersistence)
    ) {
      return state;
    }
    return { ...state, lifecycle: emptyLifecycle("idle", event.sessionId) };
  }

  let workingState = state;
  if (state.lifecycle.generationEpoch == null) {
    const generationEpoch = state.nextGenerationEpoch + 1;
    workingState = {
      ...state,
      nextGenerationEpoch: generationEpoch,
      lifecycle: {
        ...emptyLifecycle("hydrating", event.sessionId),
        generationEpoch,
        clientMessageId:
          status.client_message_id ?? event.clientMessageId ?? null,
        agentRunId: status.agent_run_id ?? null,
        startedAt: status.started_at ?? new Date().toISOString(),
      },
    };
  }
  if (!matchesCurrentGeneration(workingState.lifecycle, event)) {
    return state;
  }

  if (!status.running && ["completed", "cancelled", "failed"].includes(phase)) {
    return transitionTerminal(workingState, {
      type: phase as "completed" | "cancelled" | "failed",
      sessionId: event.sessionId,
      agentRunId: status.agent_run_id ?? null,
      clientMessageId:
        status.client_message_id ?? workingState.lifecycle.clientMessageId,
      generationEpoch: workingState.lifecycle.generationEpoch,
      statusMessage: status.message ?? null,
      eventId: event.eventId,
      awaitingPersistence: false,
    });
  }

  if (phase === "cancellation_pending") {
    return {
      ...workingState,
      lifecycle: activeLifecycle(
        workingState,
        event,
        "cancellation_pending",
        status.message ?? "停止処理を継続しています",
      ),
    };
  }
  if (phase === "cancellation_failed") {
    return {
      ...workingState,
      lifecycle: activeLifecycle(
        workingState,
        event,
        "cancellation_failed",
        status.message ?? "応答生成を完全に停止できませんでした",
      ),
    };
  }
  if (phase === "queued") {
    return {
      ...workingState,
      lifecycle: activeLifecycle(
        workingState,
        event,
        "queued",
        status.message ?? "応答をキューに追加しました",
      ),
    };
  }
  if (phase === "tool" && status.active_tool) {
    return {
      ...workingState,
      lifecycle: activeLifecycle(
        workingState,
        event,
        "tool",
        status.message ?? null,
        { activeTool: status.active_tool },
      ),
    };
  }
  if (status.running) {
    return {
      ...workingState,
      lifecycle: activeLifecycle(
        workingState,
        event,
        "streaming",
        status.message ?? "応答を生成しています",
      ),
    };
  }
  return { ...state, lifecycle: emptyLifecycle("unknown", event.sessionId) };
}

export function chatGenerationReducer(
  state: ChatGenerationState,
  event: ChatGenerationEvent,
): ChatGenerationState {
  if (event.type === "session_changed") {
    if (
      state.lifecycle.sessionId === event.sessionId &&
      state.lifecycle.phase !== "idle"
    ) {
      return state;
    }
    return {
      ...state,
      lifecycle: emptyLifecycle(
        event.sessionId ? "hydrating" : "idle",
        event.sessionId,
      ),
      lastTerminal: null,
      seenEventIds: [],
    };
  }

  if (event.type === "reset") {
    // A route-created empty session is initialized idle before the persistence
    // hook issues its reset.  Keep that known-idle state so a missing status
    // endpoint cannot turn a sendable draft into an unknown/blocked composer.
    if (
      event.sessionId &&
      state.lifecycle.sessionId === event.sessionId &&
      state.lifecycle.phase === "idle"
    ) {
      return state;
    }
    return {
      ...state,
      lifecycle: emptyLifecycle(
        event.sessionId ? "hydrating" : "idle",
        event.sessionId,
      ),
      lastTerminal: null,
      seenEventIds: [],
    };
  }

  if (event.type === "session_initialized") {
    return {
      ...state,
      lifecycle: emptyLifecycle("idle", event.sessionId),
      lastTerminal: null,
      seenEventIds: [],
    };
  }

  if (event.type === "hydration_failed") {
    if (state.lifecycle.sessionId !== event.sessionId) return state;
    if (state.lifecycle.phase !== "hydrating") return state;
    return {
      ...state,
      lifecycle: emptyLifecycle("unknown", event.sessionId),
    };
  }

  if (event.type === "dispatch_started") {
    if (state.lifecycle.sessionId !== event.sessionId) return state;
    const generationEpoch = state.nextGenerationEpoch + 1;
    return {
      ...state,
      nextGenerationEpoch: generationEpoch,
      lifecycle: {
        phase: "dispatching",
        sessionId: event.sessionId,
        generationEpoch,
        clientMessageId: event.clientMessageId,
        agentRunId: null,
        startedAt: event.startedAt ?? new Date().toISOString(),
        statusMessage: "応答を送信しています",
        activeTool: null,
        assistantMessageId: null,
        awaitingPersistence: false,
      },
      seenEventIds: [],
    };
  }

  if (!matchesCurrentGeneration(state.lifecycle, event)) return state;
  const marked = markEventSeen(state, event.eventId);
  if (!marked) return state;
  state = marked;

  if (
    state.lifecycle.generationEpoch == null &&
    [
      "dispatch_accepted",
      "stream_started",
      "tool_started",
      "tool_finished",
      "status_updated",
      "stop_requested",
      "cancellation_pending",
      "cancellation_failed",
    ].includes(event.type)
  ) {
    const generationEpoch = state.nextGenerationEpoch + 1;
    state = {
      ...state,
      nextGenerationEpoch: generationEpoch,
      lifecycle: {
        ...state.lifecycle,
        generationEpoch,
        clientMessageId: eventClientMessageId(event),
        agentRunId: eventAgentRunId(event),
        startedAt: new Date().toISOString(),
      },
    };
  }

  const currentMessage = state.lifecycle.statusMessage;
  switch (event.type) {
    case "dispatch_accepted":
      if (
        [
          "streaming",
          "tool",
          "stopping",
          "cancellation_pending",
          "cancellation_failed",
        ].includes(state.lifecycle.phase)
      ) {
        return {
          ...state,
          lifecycle: activeLifecycle(
            state,
            event,
            state.lifecycle.phase,
            currentMessage,
            { activeTool: state.lifecycle.activeTool },
          ),
        };
      }
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "queued",
          event.statusMessage ?? "応答をキューに追加しました",
        ),
      };
    case "status_restored":
      return restoreStatus(state, event);
    case "stream_started":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "streaming",
          event.statusMessage ?? "応答を生成しています",
        ),
      };
    case "tool_started":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "tool",
          event.statusMessage ?? currentMessage,
          { activeTool: event.tool },
        ),
      };
    case "tool_finished":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "streaming",
          event.statusMessage ?? currentMessage,
        ),
      };
    case "status_updated":
      if (event.status === "tool" && event.activeTool) {
        return {
          ...state,
          lifecycle: activeLifecycle(
            state,
            event,
            "tool",
            event.statusMessage ?? currentMessage,
            { activeTool: event.activeTool },
          ),
        };
      }
      if (event.status === "queued") {
        return {
          ...state,
          lifecycle: activeLifecycle(
            state,
            event,
            "queued",
            event.statusMessage ?? currentMessage,
          ),
        };
      }
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "streaming",
          event.statusMessage ?? currentMessage,
        ),
      };
    case "stop_requested":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "stopping",
          event.statusMessage ?? "停止処理を開始しています",
        ),
      };
    case "cancellation_pending":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "cancellation_pending",
          event.statusMessage ?? "停止処理を継続しています",
        ),
      };
    case "completed":
    case "cancelled":
    case "failed":
      return transitionTerminal(state, event);
    case "cancellation_failed":
      return {
        ...state,
        lifecycle: activeLifecycle(
          state,
          event,
          "cancellation_failed",
          event.statusMessage ?? "応答生成を完全に停止できませんでした",
        ),
      };
    case "assistant_persisted":
      if (!TERMINAL_PHASES.has(state.lifecycle.phase)) return state;
      return {
        ...state,
        lifecycle: {
          ...state.lifecycle,
          assistantMessageId: event.assistantMessageId,
          awaitingPersistence: false,
        },
      };
    default:
      return state;
  }
}

export function selectGenerationIsBusy(
  state: ChatGenerationState,
  sessionId: string | null,
): boolean {
  return (
    state.lifecycle.sessionId === sessionId &&
    ACTIVE_PHASES.has(state.lifecycle.phase)
  );
}

export function selectGenerationIsStreaming(
  state: ChatGenerationState,
  sessionId: string | null,
): boolean {
  if (state.lifecycle.sessionId !== sessionId) return false;
  return state.lifecycle.phase === "streaming" || state.lifecycle.phase === "tool";
}

export function selectGenerationAgentRunId(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  return state.lifecycle.sessionId === sessionId
    ? state.lifecycle.agentRunId
    : null;
}

export function selectGenerationActiveTool(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  if (state.lifecycle.sessionId !== sessionId) return null;
  return state.lifecycle.phase === "tool" ? state.lifecycle.activeTool : null;
}

export function selectGenerationActivityMessage(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  if (state.lifecycle.sessionId !== sessionId) return null;
  return state.lifecycle.statusMessage;
}

export function selectGenerationStartedAt(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  return state.lifecycle.sessionId === sessionId
    ? state.lifecycle.startedAt
    : null;
}

export function selectGenerationEpochKey(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  if (
    state.lifecycle.sessionId !== sessionId ||
    !sessionId ||
    state.lifecycle.generationEpoch == null
  ) {
    return null;
  }
  return `${sessionId}:epoch:${state.lifecycle.generationEpoch}`;
}

export function selectGenerationTerminalKey(
  state: ChatGenerationState,
  sessionId: string | null,
): string | null {
  return state.lastTerminal?.sessionId === sessionId
    ? state.lastTerminal.key
    : null;
}

export function selectGenerationShowsActivity(
  state: ChatGenerationState,
  sessionId: string | null,
): boolean {
  if (state.lifecycle.sessionId !== sessionId) return false;
  return (
    ACTIVE_PHASES.has(state.lifecycle.phase) ||
    (TERMINAL_PHASES.has(state.lifecycle.phase) &&
      state.lifecycle.awaitingPersistence)
  );
}

export function isSameGenerationIdentity(
  left: GenerationLifecycle,
  right: GenerationLifecycle,
): boolean {
  return Boolean(
    left.sessionId &&
      left.sessionId === right.sessionId &&
      left.generationEpoch != null &&
      left.generationEpoch === right.generationEpoch &&
      (!left.clientMessageId ||
        !right.clientMessageId ||
        left.clientMessageId === right.clientMessageId) &&
      (!left.agentRunId ||
        !right.agentRunId ||
        left.agentRunId === right.agentRunId),
  );
}
