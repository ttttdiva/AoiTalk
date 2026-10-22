import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChatIntentLookupError, fetchChatIntentCandidates, type ChatIntentCandidate, type ChatIntentResponse } from "../../lib/chat-intent-api";

const MAX_LOOKUP_ATTEMPTS = 3;
const LOOKUP_LIMIT_MESSAGE = "意図候補の再試行上限に達しました。本文は保持されています。意図確認をOFFにすると通常送信できます。";

/** Opt-in, draft-bound preview. Fetching never dispatches or changes a draft. */
export function useChatIntent({ text, scopeKey, sessionId, projectId, enabled, onSend }: {
  text: string;
  scopeKey: string;
  sessionId?: string | null;
  projectId?: string | null;
  enabled: boolean;
  onSend: (candidate: ChatIntentCandidate) => Promise<boolean>;
}) {
  // scopeKey identifies the logical conversation across local → server promotion.
  const key = JSON.stringify([scopeKey, projectId, text]);
  const [snapshot, setSnapshot] = useState<{ key: string; text: string; sessionId?: string | null; projectId?: string | null } | null>(null);
  const [result, setResult] = useState<ChatIntentResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const openingRef = useRef(false);
  const attemptedSendRef = useRef(false);
  const lookupRef = useRef<{ key: string; attempts: number; result: ChatIntentResponse | null }>({ key, attempts: 0, result: null });
  // One draft in one owner/server/conversation scope owns its lookup budget.
  // Closing or toggling preserves the attempts already spent.
  if (lookupRef.current.key !== key) lookupRef.current = { key, attempts: 0, result: null };
  const currentKey = useRef(key);
  useLayoutEffect(() => { currentKey.current = key; }, [key]);
  const visible = Boolean(snapshot && snapshot.key === key && (enabled || sending || attemptedSendRef.current));

  const close = useCallback(() => {
    if (!sendingRef.current) setSnapshot(null);
  }, []);
  const open = useCallback(() => {
    if (!enabled || !text.trim() || sendingRef.current || openingRef.current || (visible && !error)) return;
    openingRef.current = true;
    queueMicrotask(() => { openingRef.current = false; });
    attemptedSendRef.current = false;
    setResult(lookupRef.current.result);
    setSelectedId(null);
    setError(null);
    setLoading(!lookupRef.current.result);
    setSnapshot({ key, text, sessionId, projectId });
  }, [enabled, error, key, projectId, sessionId, text, visible]);

  useEffect(() => {
    if (!visible || !snapshot || currentKey.current !== snapshot.key) return;
    const lookup = lookupRef.current;
    if (lookup.result) {
      setResult(lookup.result);
      setLoading(false);
      return;
    }
    if (lookup.attempts >= MAX_LOOKUP_ATTEMPTS) {
      setError(LOOKUP_LIMIT_MESSAGE);
      setLoading(false);
      return;
    }
    lookup.attempts += 1;
    const controller = new AbortController();
    let current = true;
    const timeout = setTimeout(() => {
      controller.abort();
      if (current) {
        setError(lookup.attempts >= MAX_LOOKUP_ATTEMPTS ? LOOKUP_LIMIT_MESSAGE
          : "意図候補の取得がタイムアウトしました。下書きは保持されています。");
        setLoading(false);
      }
    }, 20_000);
    void fetchChatIntentCandidates(snapshot.text, controller.signal, { session_id: snapshot.sessionId, project_id: snapshot.projectId }).then((response) => {
      if (current && !controller.signal.aborted && currentKey.current === snapshot.key) {
        lookup.result = response;
        setResult(response);
      }
    }).catch((error: unknown) => {
      if (current && !controller.signal.aborted) {
        setError(lookup.attempts >= MAX_LOOKUP_ATTEMPTS ? LOOKUP_LIMIT_MESSAGE
          : error instanceof ChatIntentLookupError ? error.message
          : "意図候補を取得できませんでした。接続とサーバーのJEV設定を確認してください。");
      }
    }).finally(() => {
      clearTimeout(timeout);
      if (current) setLoading(false);
    });
    return () => { current = false; controller.abort(); clearTimeout(timeout); };
  }, [snapshot, visible]);

  // Invalidate during render so no stale selection is ever visible/clickable.
  // Dispatch temporarily disables the composer. Keep its candidate snapshot
  // until acceptance settles, while still invalidating any changed draft.
  if (snapshot && ((!enabled && !sending && !attemptedSendRef.current) || snapshot.key !== key)) {
      setSnapshot(null);
      setResult(null);
      setSelectedId(null);
  }

  const send = useCallback(async () => {
    const candidate = result?.candidates.find((item) => item.id === selectedId);
    if (!enabled || !visible || !snapshot || currentKey.current !== snapshot.key || !candidate || sendingRef.current) return;
    attemptedSendRef.current = true;
    sendingRef.current = true;
    setSending(true);
    setError(null);
    try {
      if (await onSend(candidate)) setSnapshot(null);
      else setError("送信の受付を確認できませんでした。下書きは保持されています。");
    } catch {
      setError("送信を開始できませんでした。下書きは保持されています。");
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  }, [enabled, onSend, result, selectedId, snapshot, visible]);

  return { visible, canSend: enabled, canRetry: lookupRef.current.attempts < MAX_LOOKUP_ATTEMPTS, open, close, result, selectedId, select: setSelectedId, loading, error, sending, send };
}
