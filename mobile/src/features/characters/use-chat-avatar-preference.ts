import { useCallback, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";
import { getChatAvatarEnabled, saveChatAvatarEnabled } from "../../lib/preferences";

/** Device display preference. A selected/active model is not an opt-in. */
export function useChatAvatarPreference() {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const version = useRef(0);
  const writing = useRef(false);

  useFocusEffect(useCallback(() => {
    const current = ++version.current;
    setLoaded(false);
    setEnabled(false);
    setError(null);
    void getChatAvatarEnabled().then((value) => {
      if (current === version.current) setEnabled(value);
    }).catch(() => {
      if (current === version.current) setError("アバター表示設定を読み込めませんでした。");
    }).finally(() => {
      if (current === version.current) setLoaded(true);
    });
    return () => { version.current += 1; };
  }, []));

  const update = useCallback(async (value: boolean) => {
    if (!loaded || writing.current) return;
    writing.current = true;
    const current = ++version.current;
    setSaving(true);
    setError(null);
    try {
      await saveChatAvatarEnabled(value);
      if (current === version.current) setEnabled(value);
    } catch {
      if (current === version.current) setError("アバター表示設定を保存できませんでした。");
    } finally {
      writing.current = false;
      setSaving(false);
    }
  }, [loaded]);

  return { enabled, loaded, saving, error, update };
}
