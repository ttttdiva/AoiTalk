/**
 * Lightweight bridge from mutation repositories to the sync engine.
 *
 * Keeping this bus independent avoids a repositories -> engine import cycle.
 * Notifications are durable only as outbox rows; this module only coalesces
 * an in-process wake request until the engine is ready to observe it.
 */

export type MutationWakeListener = (authScope: string) => void;
let listener: MutationWakeListener | null = null;
const pendingScopes = new Set<string>();
let drainScheduled = false;

function scheduleDrain(): void {
  if (drainScheduled || !listener || pendingScopes.size === 0) return;
  drainScheduled = true;
  queueMicrotask(() => {
    drainScheduled = false;
    if (!listener || pendingScopes.size === 0) return;
    const scopes = [...pendingScopes];
    pendingScopes.clear();
    for (const scope of scopes) listener(scope);
    // A listener may have queued another mutation while draining.
    scheduleDrain();
  });
}

/** Notify the sync engine after an authenticated outbox row is saved. */
export function notifyMutationQueued(
  authScope: string | null | undefined,
): void {
  if (!authScope) return;
  pendingScopes.add(authScope);
  scheduleDrain();
}

/**
 * Register the process-local sync wake listener.  Pending notifications are
 * drained after registration so a repository imported before the engine does
 * not lose its wake request.
 */
export function registerMutationWakeListener(
  next: MutationWakeListener,
): () => void {
  listener = next;
  scheduleDrain();
  return () => {
    if (listener === next) listener = null;
  };
}
