import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";
import { getConfiguredApiServerFingerprint } from "../../lib/api-client";
import { getChatIntentConfirmationEnabled, saveChatIntentConfirmationEnabled } from "../../lib/preferences";

/** Opt-in is isolated by account and server, never inferred from an active draft. */
export function useChatIntentPreference(userId: string | null) {
  const [state, setState] = useState({ userId, scope: null as string | null, enabled: false, loaded: false });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const version = useRef(0);
  const writing = useRef<number | null>(null);
  const currentUser = useRef(userId);
  useLayoutEffect(() => { currentUser.current = userId; }, [userId]);

  useFocusEffect(useCallback(() => {
    const current = ++version.current;
    setState({ userId, scope: null, enabled: false, loaded: false });
    setError(null);
    setSaving(false);
    const load = async () => {
      // An unbound composer can opt in for its lifetime but cannot write a
      // shared anonymous ON setting that a later account might inherit.
      const scope = userId ? JSON.stringify([await getConfiguredApiServerFingerprint(), userId]) : null;
      const enabled = scope ? await getChatIntentConfirmationEnabled(scope) : false;
      if (current === version.current) setState({ userId, scope, enabled, loaded: true });
    };
    void load().catch(() => {
      if (current !== version.current) return;
      setState({ userId, scope: null, enabled: false, loaded: true });
      setError("意図確認の設定を読み込めませんでした。現在はOFFです。");
    });
    return () => { version.current += 1; };
  }, [userId]));

  const loaded = state.userId === userId && state.loaded;
  const enabled = loaded && state.enabled;
  const update = useCallback(async (value: boolean) => {
    if (!loaded || writing.current === version.current || currentUser.current !== userId) return;
    const current = version.current;
    writing.current = current;
    setSaving(true);
    setError(null);
    try {
      if (state.scope) await saveChatIntentConfirmationEnabled(state.scope, value);
      if (current === version.current && currentUser.current === userId) {
        setState((previous) => ({ ...previous, enabled: value }));
      }
    } catch {
      if (current === version.current) setError("意図確認の設定を保存できませんでした。もう一度切り替えてください。");
    } finally {
      if (writing.current === current) writing.current = null;
      if (current === version.current) setSaving(false);
    }
  }, [loaded, state.scope, userId]);

  return { enabled, loaded, saving, error, update, scope: loaded ? state.scope : null };
}
