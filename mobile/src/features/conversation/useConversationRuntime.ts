import { useCallback, useEffect, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";

export type ConversationRuntimeFocus = {
  isFocused: boolean;
  focusEpoch: number;
};

/**
 * Navigation focus is the lifetime of UI-only conversation work. Screens may
 * stay mounted in the router stack, so mount state alone must not own sockets
 * and pollers.
 */
export function useConversationRuntimeFocus(
  explicitRuntimeFocus?: ConversationRuntimeFocus | null,
): ConversationRuntimeFocus {
  const [focus, setFocus] = useState<ConversationRuntimeFocus>({
    isFocused: false,
    focusEpoch: 0,
  });

  useFocusEffect(
    useCallback(() => {
      setFocus((current) => ({
        isFocused: true,
        focusEpoch: current.focusEpoch + 1,
      }));
      return () => {
        setFocus((current) =>
          current.isFocused ? { ...current, isFocused: false } : current,
        );
      };
    }, []),
  );

  // Root mounted overlays do not receive Expo Router focus events. They pass
  // an explicit epoch so their socket/poll lifecycle remains independent from
  // the route that happens to be underneath the modal. Ordinary route users
  // retain the useFocusEffect-derived behavior above.
  return explicitRuntimeFocus ?? focus;
}

type ConversationFocusRecoveryArgs = ConversationRuntimeFocus & {
  enabled: boolean;
  /** Identifies the conversation owner within one navigation focus epoch. */
  recoveryKey?: string | null;
  onRecover: (focusEpoch: number, reason: "focus" | "session") => void;
  onBlur: () => void;
};

/** Recover each focused owner once; disabled owners also lose their UI runtime. */
export function useConversationFocusRecovery({
  enabled,
  focusEpoch,
  isFocused,
  recoveryKey,
  onRecover,
  onBlur,
}: ConversationFocusRecoveryArgs): void {
  const recoveredOwnerRef = useRef<{
    focusEpoch: number;
    recoveryKey: string | null | undefined;
  } | null>(null);
  const onRecoverRef = useRef(onRecover);
  const onBlurRef = useRef(onBlur);
  onRecoverRef.current = onRecover;
  onBlurRef.current = onBlur;

  useEffect(() => {
    if (!isFocused) {
      onBlurRef.current();
      return;
    }
    if (!enabled) {
      recoveredOwnerRef.current = null;
      onBlurRef.current();
      return;
    }
    const previous = recoveredOwnerRef.current;
    const sessionChanged =
      previous !== null && previous.recoveryKey !== recoveryKey;
    if (
      previous?.focusEpoch === focusEpoch &&
      previous.recoveryKey === recoveryKey
    ) {
      return;
    }
    recoveredOwnerRef.current = { focusEpoch, recoveryKey };
    onRecoverRef.current(focusEpoch, sessionChanged ? "session" : "focus");
  }, [enabled, focusEpoch, isFocused, recoveryKey]);
}
