import { useCallback, useReducer, useRef } from "react";
import {
  createIdleGenerationState,
  generationReducer,
  isGenerationActive,
  type GenerationAction,
  type GenerationIdentity,
  type GenerationState,
} from "./generation-reducer";

export type ConversationGenerationController = {
  state: GenerationState;
  active: boolean;
  identity: () => GenerationIdentity | null;
  activateSession: (sessionId: string) => void;
  begin: (sessionId: string, requestId?: string) => GenerationIdentity | null;
  startStreaming: (startedAt?: number) => GenerationIdentity | null;
  requestCancel: () => GenerationIdentity | null;
  complete: (expected?: GenerationIdentity) => GenerationIdentity | null;
  fail: (error: string) => GenerationIdentity | null;
};

/** Reducer-backed ownership for the server generation lifecycle. */
export function useConversationGeneration(
  initialSessionId: string,
): ConversationGenerationController {
  const [state, dispatch] = useReducer(
    generationReducer,
    initialSessionId,
    createIdleGenerationState,
  );
  const lifecycleRef = useRef(0);
  const identityRef = useRef<GenerationIdentity | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const identity = useCallback(() => identityRef.current, []);

  const apply = useCallback((action: GenerationAction) => {
    const current = stateRef.current;
    const next = generationReducer(current, action);
    if (next === current) return false;
    // Keep imperative identity checks atomic even before React processes the
    // queued reducer action (multiple transport events can arrive in one tick).
    stateRef.current = next;
    dispatch(action);
    return true;
  }, []);

  const activateSession = useCallback((sessionId: string) => {
    if (!apply({ type: "activate-session", sessionId })) return;
    lifecycleRef.current += 1;
    identityRef.current = null;
  }, [apply]);

  const begin = useCallback((sessionId: string, requestId?: string) => {
    const activeIdentity = identityRef.current;
    if (activeIdentity && isGenerationActive(stateRef.current)) {
      return activeIdentity.sessionId === sessionId ? activeIdentity : null;
    }
    identityRef.current = null;
    const lifecycleId = ++lifecycleRef.current;
    const nextIdentity = {
      sessionId,
      lifecycleId,
      requestId: requestId ?? `generation-${lifecycleId}`,
    };
    if (!apply({ type: "begin", ...nextIdentity })) return null;
    identityRef.current = nextIdentity;
    return nextIdentity;
  }, [apply]);

  const startStreaming = useCallback((startedAt = Date.now()) => {
    const identity = identityRef.current;
    if (!identity) return null;
    const action = { type: "stream-started", ...identity, startedAt } as const;
    if (stateRef.current.phase !== "streaming" && !apply(action)) return null;
    return identity;
  }, [apply]);

  const requestCancel = useCallback(() => {
    const identity = identityRef.current;
    if (!identity) return null;
    if (stateRef.current.phase !== "cancelling") {
      if (!apply({ type: "cancel-requested", ...identity })) return null;
    }
    return identity;
  }, [apply]);

  const complete = useCallback((expected?: GenerationIdentity) => {
    const identity = identityRef.current;
    if (!identity) return null;
    if (
      expected &&
      (identity.sessionId !== expected.sessionId ||
        identity.lifecycleId !== expected.lifecycleId ||
        identity.requestId !== expected.requestId)
    ) {
      return null;
    }
    if (!apply({ type: "completed", ...identity })) return null;
    identityRef.current = null;
    return identity;
  }, [apply]);

  const fail = useCallback((error: string) => {
    const identity = identityRef.current;
    if (!identity) return null;
    if (!apply({ type: "failed", ...identity, error })) return null;
    return identity;
  }, [apply]);

  return {
    state,
    active: isGenerationActive(state),
    identity,
    activateSession,
    begin,
    startStreaming,
    requestCancel,
    complete,
    fail,
  };
}
