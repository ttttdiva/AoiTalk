import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChatIntentLookupError, fetchChatIntentCandidates, type ChatIntentCandidate, type ChatIntentResponse } from "@/lib/chat-intent-api";

type IntentRequest = {
  text: string;
  sessionId?: string | null;
  projectId?: string | null;
};
type IntentSnapshot = {
  key: string;
  request: IntentRequest;
  promotedKey?: string;
  sendStarted?: boolean;
};

/** Opt-in, draft-bound preview. Fetching never dispatches or changes a draft. */
export function useChatIntent({ text, scopeKey, draftKey, sessionId, projectId, enabled, onSend }: {
  text: string;
  scopeKey: string;
  draftKey?: string;
  sessionId?: string | null;
  projectId?: string | null;
  enabled: boolean;
  onSend: (candidate: ChatIntentCandidate, onSessionPromoted: (sessionId: string) => void) => Promise<boolean>;
}) {
  const key = JSON.stringify([scopeKey, draftKey, sessionId, projectId, text]);
  const [snapshot, setSnapshot] = useState<IntentSnapshot | null>(null);
  const [result, setResult] = useState<ChatIntentResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const openedKey = useRef<string | null>(null);
  const currentKey = useRef(key);
  const currentRequest = useRef<IntentRequest | null>(null);
  useLayoutEffect(() => {
    currentKey.current = key;
    currentRequest.current = snapshot?.request ?? null;
    if (!snapshot) openedKey.current = null;
  }, [key, snapshot]);
  const visible = Boolean(snapshot && snapshot.key === key && (enabled || snapshot?.sendStarted));

  const close = useCallback(() => {
    if (sendingRef.current) return;
    openedKey.current = null;
    setSnapshot(null);
    setResult(null);
    setSelectedId(null);
    setError(null);
    setLoading(false);
  }, []);
  const open = useCallback(() => {
    if (!enabled || !text.trim() || sendingRef.current || openedKey.current === key) return;
    // Lock synchronously so repeated Send/Enter in the same event turn cannot
    // restart a lookup or discard an already selected intention.
    openedKey.current = key;
    setResult(null);
    setSelectedId(null);
    setError(null);
    setLoading(true);
    setSnapshot({ key, request: { text, sessionId, projectId } });
  }, [enabled, key, text, sessionId, projectId]);

  // A promotion rebinds the same request; it must not refetch or discard the
  // candidate the user selected under the original new-conversation context.
  const request = snapshot?.request ?? null;
  useEffect(() => {
    if (!request) return;
    const controller = new AbortController();
    let current = true;
    const timeout = setTimeout(() => {
      controller.abort();
      if (current && currentRequest.current === request) {
        openedKey.current = null;
        setError("意図候補の取得がタイムアウトしました。下書きは保持されています。");
        setLoading(false);
      }
    }, 20_000);
    void fetchChatIntentCandidates(request.text, controller.signal, { session_id: request.sessionId, project_id: request.projectId }).then((response) => {
      if (current && !controller.signal.aborted && currentRequest.current === request) setResult(response);
    }).catch((error: unknown) => {
      if (current && !controller.signal.aborted && currentRequest.current === request) {
        openedKey.current = null;
        setError(error instanceof ChatIntentLookupError ? error.message
          : "意図候補を取得できませんでした。接続とサーバーのJEV設定を確認してください。");
      }
    }).finally(() => {
      clearTimeout(timeout);
      if (current) setLoading(false);
    });
    return () => { current = false; controller.abort(); clearTimeout(timeout); };
  }, [request]);

  // Only the send operation can authorize this exact new-chat -> created-chat
  // transition. All user navigation and any changed draft invalidate in render.
  if (snapshot && snapshot.key !== key && snapshot.promotedKey === key) {
    setSnapshot({ ...snapshot, key, promotedKey: undefined });
  } else if (snapshot && ((!enabled && !snapshot.sendStarted) || snapshot.key !== key)) {
    setSnapshot(null);
    setResult(null);
    setSelectedId(null);
  }

  const send = useCallback(async () => {
    const candidate = result?.candidates.find((item) => item.id === selectedId);
    if (!enabled || !visible || !snapshot || currentKey.current !== snapshot.key || !candidate || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setSnapshot({ ...snapshot, sendStarted: true });
    setError(null);
    const onSessionPromoted = (createdSessionId: string) => {
      if (sessionId || !createdSessionId || currentKey.current !== snapshot.key || currentRequest.current !== snapshot.request) return;
      const promotedKey = JSON.stringify([scopeKey, draftKey === undefined ? undefined : createdSessionId,
        createdSessionId, projectId, text]);
      setSnapshot((current) => current?.request === snapshot.request ? { ...current, promotedKey } : current);
    };
    try {
      const accepted = await onSend(candidate, onSessionPromoted);
      if (currentRequest.current !== snapshot.request) return;
      if (accepted) setSnapshot(null);
      else setError("送信の受付を確認できませんでした。下書きは保持されています。");
    } catch {
      if (currentRequest.current === snapshot.request) {
        setError("送信を開始できませんでした。下書きは保持されています。");
      }
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  }, [draftKey, enabled, onSend, projectId, result, scopeKey, selectedId, sessionId, snapshot, text, visible]);

  return { visible, open, close, result, selectedId, select: setSelectedId, loading, error, sending, sendDisabled: !enabled || sending, send };
}
