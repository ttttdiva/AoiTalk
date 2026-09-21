"use client";
import { useEffect, useId } from "react";
import { publishPetActivity } from "@/lib/pets/pet-activity";

/** Mirror the already-correlated chat lifecycle; no socket, polling or chat mutation. */
export function usePetChatActivity(phase: string, sessionId: string | null, activeSessionId: string | null) {
  const source = useId();
  useEffect(() => {
    publishPetActivity(source, sessionId && sessionId === activeSessionId ? phase : null);
  }, [source, phase, sessionId, activeSessionId]);
  useEffect(() => () => publishPetActivity(source, null), [source]);
}
