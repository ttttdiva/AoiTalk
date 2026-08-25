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
export function useConversationRuntimeFocus(): ConversationRuntimeFocus {
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

  return focus;
}

type ConversationFocusRecoveryArgs = ConversationRuntimeFocus & {
  enabled: boolean;
  onRecover: (focusEpoch: number) => void;
  onBlur: () => void;
};

/** Runs recovery once per focus epoch and always tears UI runtime down on blur. */
export function useConversationFocusRecovery({
  enabled,
  focusEpoch,
  isFocused,
  onRecover,
  onBlur,
}: ConversationFocusRecoveryArgs): void {
  const recoveredEpochRef = useRef(0);
  const onRecoverRef = useRef(onRecover);
  const onBlurRef = useRef(onBlur);
  onRecoverRef.current = onRecover;
  onBlurRef.current = onBlur;

  useEffect(() => {
    if (!isFocused) {
      onBlurRef.current();
      return;
    }
    if (!enabled || recoveredEpochRef.current === focusEpoch) return;
    recoveredEpochRef.current = focusEpoch;
    onRecoverRef.current(focusEpoch);
  }, [enabled, focusEpoch, isFocused]);
}
