import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatAppContext } from "../../lib/chat-api";
import type { ConversationSession } from "../../types/api";

type InitialAppContextBindingOptions = {
  session: ConversationSession | null;
  context?: ChatAppContext | null;
  enabled: boolean;
  bind: (context: ChatAppContext) => Promise<void>;
  onError: (error: unknown) => void;
};

/** One automatic attempt per route; a failed POST must not become a render loop. */
export function useInitialAppContextBinding({
  session,
  context,
  enabled,
  bind,
  onError,
}: InitialAppContextBindingOptions): () => boolean {
  const [retryEpoch, setRetryEpoch] = useState(0);
  const [settledEpoch, setSettledEpoch] = useState(0);
  const pendingSessionIdsRef = useRef(new Set<string>());
  const attemptRef = useRef<{ key: string; pending: boolean; failed: boolean } | null>(null);
  const activeKeyRef = useRef<string | null>(null);
  const mountedRef = useRef(false);
  const key = session && context?.appId
    ? JSON.stringify([
        session.id, context.appId, context.appTargetId ?? null,
        context.projectId ?? null, retryEpoch,
      ])
    : null;
  activeKeyRef.current = key;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!enabled || !key || !context || !session) return;
    if (
      session.app_id === context.appId &&
      (session.app_target_id ?? null) === (context.appTargetId ?? null)
    ) {
      // Once the initial route was satisfied, a later manual detach must not
      // silently reattach the route's App on the next render.
      attemptRef.current = { key, pending: false, failed: false };
      return;
    }
    if (attemptRef.current?.key === key) return;
    // A new App for the same local session must wait for its promotion to
    // settle, then use the durable remote target. Other sessions can proceed.
    if (pendingSessionIdsRef.current.has(session.id)) return;
    pendingSessionIdsRef.current.add(session.id);
    const attempt = { key, pending: true, failed: false };
    attemptRef.current = attempt;
    // Keep failed attempts latched. Context/callback identities often change
    // on the error render; clearing this guard would create another session
    // even when the first POST committed but its response never arrived.
    void Promise.resolve().then(() => bind(context)).catch((error: unknown) => {
      attempt.failed = true;
      if (mountedRef.current && activeKeyRef.current === key && attemptRef.current === attempt) onError(error);
    }).finally(() => {
      attempt.pending = false;
      pendingSessionIdsRef.current.delete(session.id);
      if (mountedRef.current) setSettledEpoch((epoch) => epoch + 1);
    });
  }, [bind, context, enabled, key, onError, session, settledEpoch]);

  return useCallback(() => {
    // Reload retries an actual initial failure, not a route already satisfied
    // and later changed/cleared by the user through the App picker.
    const attempt = attemptRef.current;
    if (!enabled || !attempt?.failed || attempt.pending || attempt.key !== activeKeyRef.current) return false;
    setRetryEpoch((epoch) => epoch + 1);
    return true;
  }, [enabled]);
}
