import { createContext, useContext } from "react";

export type ChatWaveTouchGuard = (event: unknown) => void;

export const ChatWaveTouchContext = createContext<ChatWaveTouchGuard>(() => undefined);

/**
 * Mark a child interaction as UI-owned without stopping the native event from
 * bubbling.  The frame observes this marker and skips the decorative ripple.
 */
export function useChatWaveTouchGuard(): ChatWaveTouchGuard {
  return useContext(ChatWaveTouchContext);
}
