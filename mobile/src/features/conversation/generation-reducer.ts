export type GenerationLifecycleId = string | number;

export interface GenerationIdentity {
  sessionId: string;
  lifecycleId: GenerationLifecycleId;
  requestId: string;
}

export type GenerationState =
  | { phase: "idle"; sessionId: string }
  | ({ phase: "waiting" } & GenerationIdentity)
  | ({ phase: "streaming"; startedAt: number } & GenerationIdentity)
  | ({ phase: "cancelling" } & GenerationIdentity)
  | ({ phase: "failed"; error: string } & GenerationIdentity);

export type GenerationAction =
  | { type: "activate-session"; sessionId: string }
  | ({ type: "begin" } & GenerationIdentity)
  | ({ type: "stream-started"; startedAt: number } & GenerationIdentity)
  | ({ type: "cancel-requested" } & GenerationIdentity)
  | ({ type: "completed" } & GenerationIdentity)
  | ({ type: "failed"; error: string } & GenerationIdentity)
  | ({ type: "dismiss-error" } & GenerationIdentity);

export function createIdleGenerationState(sessionId: string): GenerationState {
  return { phase: "idle", sessionId };
}

function stateMatchesIdentity(
  state: Exclude<GenerationState, { phase: "idle" }>,
  identity: GenerationIdentity,
): boolean {
  return (
    state.sessionId === identity.sessionId &&
    state.lifecycleId === identity.lifecycleId &&
    state.requestId === identity.requestId
  );
}

function actionIdentity(action: GenerationAction): GenerationIdentity | null {
  if (action.type === "activate-session") return null;
  return {
    sessionId: action.sessionId,
    lifecycleId: action.lifecycleId,
    requestId: action.requestId,
  };
}

/**
 * Invalid or stale transitions preserve the current object reference. This is
 * intentional: callers can dispatch late transport events without reopening a
 * completed generation or resetting the newly active session.
 */
export function generationReducer(
  state: GenerationState,
  action: GenerationAction,
): GenerationState {
  if (action.type === "activate-session") {
    if (action.sessionId === state.sessionId) return state;
    return createIdleGenerationState(action.sessionId);
  }

  const identity = actionIdentity(action);
  if (!identity || identity.sessionId !== state.sessionId) return state;

  if (action.type === "begin") {
    if (state.phase !== "idle" && state.phase !== "failed") return state;
    return { phase: "waiting", ...identity };
  }

  if (state.phase === "idle" || !stateMatchesIdentity(state, identity)) {
    return state;
  }

  switch (action.type) {
    case "stream-started":
      if (state.phase === "streaming") return state;
      if (state.phase !== "waiting") return state;
      return { phase: "streaming", ...identity, startedAt: action.startedAt };
    case "cancel-requested":
      if (state.phase !== "waiting" && state.phase !== "streaming") {
        return state;
      }
      return { phase: "cancelling", ...identity };
    case "completed":
      if (state.phase === "failed") return state;
      return createIdleGenerationState(state.sessionId);
    case "failed":
      return { phase: "failed", ...identity, error: action.error };
    case "dismiss-error":
      if (state.phase !== "failed") return state;
      return createIdleGenerationState(state.sessionId);
  }
}

export function isGenerationActive(state: GenerationState): boolean {
  return (
    state.phase === "waiting" ||
    state.phase === "streaming" ||
    state.phase === "cancelling"
  );
}

export function isGenerationStreaming(state: GenerationState): boolean {
  return state.phase === "streaming";
}
