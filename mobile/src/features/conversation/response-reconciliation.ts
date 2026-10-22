/**
 * Local/server models can take substantially longer than the initial REST
 * dispatch. Keep reconciling persisted history after the WebSocket drops so a
 * completed assistant answer is still surfaced without leaving the chat.
 */
export const SERVER_RESPONSE_REFRESH_DELAYS_MS = [
  1_500,
  5_000,
  15_000,
  30_000,
  45_000,
  60_000,
  90_000,
] as const;

export type ResponseReconciliationSchedulerArgs = {
  /**
   * The callback owns the session/auth/focus checks.  The scheduler only
   * starts work while the owner is still current; the callback must repeat
   * its check after every awaited operation before publishing state.
   */
  isCurrent: () => boolean;
  refresh: (delayMs: number) => Promise<void> | void;
  delays?: readonly number[];
  trackTimer?: () => () => void;
};

/**
 * Run the bounded response reconciliation cadence for one conversation owner.
 *
 * This deliberately schedules each delay from the same starting point.  A
 * slow generation therefore gets independent 30/45/60/90 second chances,
 * while callers can cancel every outstanding timer on blur, unmount, or a
 * route/scope change.  It never dispatches or retries a message.
 */
export function startResponseReconciliation(
  args: ResponseReconciliationSchedulerArgs,
): () => void {
  let active = true;
  const cancels = (args.delays ?? SERVER_RESPONSE_REFRESH_DELAYS_MS).map(
    (delayMs) => {
      const stopTracking = args.trackTimer?.();
      let pending = true;
      const finish = () => {
        if (!pending) return;
        pending = false;
        stopTracking?.();
      };
      const timer = setTimeout(() => {
        finish();
        if (!active || !args.isCurrent()) return;
        // Start in a microtask so synchronous failures are handled too, and
        // repeat ownership after a cancellation between the timer and work.
        void Promise.resolve().then(() => {
          if (active && args.isCurrent()) return args.refresh(delayMs);
        }).catch(() => undefined);
      }, delayMs);
      return () => { clearTimeout(timer); finish(); };
    },
  );

  return () => {
    if (!active) return;
    active = false;
    for (const cancel of cancels) cancel();
  };
}
