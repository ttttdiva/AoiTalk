export const DOUBLE_ESCAPE_WINDOW_MS = 750;
/**
 * Shell-owned transient panels consume their Escape at the window capture
 * boundary.  They dispatch this scoped bridge event so a Files editor never
 * keeps an armed first Escape after a higher-priority surface closes.
 */
export const DOUBLE_ESCAPE_RESET_EVENT = "files-double-escape-reset";

export type DoubleEscapeState = {
  armedAt: number | null;
};

export const EMPTY_DOUBLE_ESCAPE_STATE: DoubleEscapeState = {
  armedAt: null,
};

export function resetDoubleEscapeState(): DoubleEscapeState {
  return EMPTY_DOUBLE_ESCAPE_STATE;
}

export function registerEscape(
  state: DoubleEscapeState,
  now: number,
): { state: DoubleEscapeState; shouldClose: boolean } {
  const elapsed = state.armedAt === null ? null : now - state.armedAt;
  if (elapsed !== null && elapsed >= 0 && elapsed <= DOUBLE_ESCAPE_WINDOW_MS) {
    return { state: EMPTY_DOUBLE_ESCAPE_STATE, shouldClose: true };
  }

  return { state: { armedAt: now }, shouldClose: false };
}

export function transitionDoubleEscapeKey(
  state: DoubleEscapeState,
  event: {
    key: string;
    repeat: boolean;
    // Kept optional for callers that pass a DOM/React keyboard event.  The
    // capture boundary intentionally does not use this value: a child editor
    // may prevent the first Escape after the boundary has armed it.
    defaultPrevented?: boolean;
  },
  options: { blocked: boolean; now: number },
): {
  state: DoubleEscapeState;
  shouldClose: boolean;
  shouldConsume: boolean;
} {
  if (event.key !== "Escape") {
    return {
      state: resetDoubleEscapeState(),
      shouldClose: false,
      shouldConsume: false,
    };
  }
  if (event.repeat) {
    return {
      state: resetDoubleEscapeState(),
      shouldClose: false,
      shouldConsume: false,
    };
  }
  if (options.blocked) {
    return {
      state: resetDoubleEscapeState(),
      shouldClose: false,
      shouldConsume: false,
    };
  }
  const next = registerEscape(state, options.now);
  return { ...next, shouldConsume: next.shouldClose };
}
